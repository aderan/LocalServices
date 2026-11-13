"""daemon 侧 Agent API 的纯函数处理器（契约 §3/§4）。

这些函数不碰 HTTP、永不 raise；daemon.py 只负责路由与鉴权后调用它们。
- GET  /api/agents        → handle_agents_get
- POST /api/agents/scan   → handle_agents_scan(body)
- POST /api/agents/verify → handle_agents_verify(body)（同步 ≤70s）
- POST /api/analyze       → handle_analyze(body)（后台线程，202 + job_id）
- GET  /api/jobs/<id>     → handle_job_get(job_id)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from . import paths
from .agents import base, detect, invoke
from .agents.base import KINDS
# 复用 controller.redact 的 SECRET_RE 家族（键值对/bearer/URL 凭据/DSN/长十六进制）。
# controller 不 import agents/agent_api，无循环依赖；controller.py 为 Integrator 所有，
# 此处只读复用，绝不修改。
from .controller import redact as _redact_text

MAX_VERIFY_TIMEOUT = 180.0
MAX_ANALYZE_TIMEOUT = 600.0
JOB_TTL = 24 * 3600.0  # >24h 的已结束任务在新任务提交时顺带删除

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ls-analyze")
_LOCK = threading.Lock()
_RUNNING: dict[tuple, str] = {}  # 同一识别请求 -> job_id

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _job_path(job_id: str) -> Path:
    return paths.jobs_dir() / f"{job_id}.json"


def _write_job(job: dict) -> None:
    p = _job_path(job["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _read_job(job_id: str) -> dict | None:
    try:
        return json.loads(_job_path(job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cleanup_old_jobs() -> None:
    now = time.time()
    try:
        files = list(paths.jobs_dir().glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("state") in ("done", "failed"):
            finished = data.get("finished_at") or 0
            if finished and now - finished > JOB_TTL:
                try:
                    f.unlink()
                except OSError:
                    pass


def mark_stale_jobs_failed() -> None:
    """Daemon startup: jobs left queued/running by a previous process cannot finish."""
    try:
        files = list(paths.jobs_dir().glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("state") in ("queued", "running"):
            data["state"] = "failed"
            data["error"] = "控制服务重启，任务中断；请重新发起"
            data["finished_at"] = time.time()
            try:
                _write_job(data)
            except OSError:
                pass


def _agents_payload(conns: dict, default: str) -> dict:
    return {"ok": True, "default": default,
            "connections": {k: c.to_dict() for k, c in conns.items()}}


def handle_agents_get() -> tuple[int, dict]:
    """缓存读取；无缓存则自动 scan 一次（不做真实调用）。"""
    try:
        conns, default = base.load_connections()
        if not conns:
            conns = detect.scan()
            default = base.save_connections(conns, default)
        return 200, _agents_payload(conns, default)
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def handle_agents_scan(body: dict) -> tuple[int, dict]:
    """重扫（廉价探测，不真实调用）。body 可含 paths: [自定义 bin 或目录]。"""
    try:
        custom = None
        if body:
            paths_raw = body.get("paths")
            if paths_raw is not None:
                if not isinstance(paths_raw, list) or not all(isinstance(x, str) and x for x in paths_raw):
                    return 400, {"ok": False, "error": "paths 必须是非空字符串数组"}
                custom = paths_raw
        conns = detect.scan(custom_paths=custom)
        _, default = base.load_connections()
        default = base.save_connections(conns, default)
        return 200, _agents_payload(conns, default)
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def handle_agents_verify(body: dict) -> tuple[int, dict]:
    """对 {kind} 做一次真实最小调用并更新缓存。默认 120s：实测 codex 最小调用在
    提供方繁忙时也要 60s+，60s 会把可用连接误判为 call_failed。"""
    try:
        if not isinstance(body, dict) or body.get("kind") not in KINDS:
            return 400, {"ok": False, "error": f"kind 必须是 {list(KINDS)}"}
        timeout = 120.0
        if body.get("timeout") is not None:
            if not isinstance(body["timeout"], (int, float)) or isinstance(body["timeout"], bool):
                return 400, {"ok": False, "error": "timeout 必须是数字"}
            timeout = min(max(float(body["timeout"]), 1.0), MAX_VERIFY_TIMEOUT)
        conn = detect.verify(body["kind"], timeout=timeout)
        return 200, {"ok": True, "connection": conn.to_dict()}
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def handle_agents_default(body: dict) -> tuple[int, dict]:
    """POST /api/agents/default {"kind": "codex"|"claude"}：切换默认连接。"""
    try:
        kind = (body or {}).get("kind")
        if kind not in base.KINDS:
            return 400, {"ok": False, "error": "kind 必须是 codex 或 claude"}
        conns, _ = base.load_connections()
        if not conns:
            conns = detect.scan()
        if kind not in conns:
            return 404, {"ok": False, "error": f"未发现 {kind}，请先扫描或指定路径"}
        default = base.save_connections(conns, kind)
        return 200, _agents_payload(conns, default)
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def handle_agents_candidates() -> tuple[int, dict]:
    """GET /api/agents/candidates：已知工具能以受支持方式提供的项目目录线索。

    只读本机配置的结构化字段（目录键名），不解析会话/聊天原文；无受支持机制的工具
    明确返回 supported=false 与原因，不假设必有统一项目列表。"""
    out: dict = {"ok": True, "candidates": {}}
    # Claude Code：~/.claude.json 顶层 projects 键即已知项目目录（本机配置，受支持字段）
    try:
        home = os.path.expanduser("~")
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", home)
        path = os.path.join(cfg, ".claude.json") if cfg != home else os.path.join(home, ".claude.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        projects = data.get("projects") if isinstance(data, dict) else None
        if isinstance(projects, dict):
            paths = sorted({str(k) for k in projects.keys() if isinstance(k, str) and k.startswith("/")})
            out["candidates"]["claude"] = {"supported": True, "candidates": paths}
        else:
            out["candidates"]["claude"] = {"supported": False, "reason": "本机配置中无 projects 记录"}
    except (OSError, ValueError) as e:
        out["candidates"]["claude"] = {"supported": False, "reason": f"读取配置失败：{e}"}
    # Codex：没有受支持的统一项目列表机制；解析 sessions 原文违反边界，明确声明不支持
    out["candidates"]["codex"] = {
        "supported": False,
        "reason": "Codex 无受支持的统一项目列表；不解析会话原文。如需添加，请用「添加项目」选择目录。",
    }
    return 200, out


def _cmd_summary(cmd: list[str]) -> str:
    """组件命令摘要（仅清单层信息；绝不包含 env 值）。
    argv 元素逐个过字符串脱敏：--password=<val>、URL token= 等绝不进上下文。"""
    argv = [_redact_text(str(x)) for x in (cmd or [])]
    head = " ".join(argv[:4])
    return head + (" …" if len(argv) > 4 else "")


def _component_context(component) -> dict:
    fields = asdict(component)
    # Keep environment variable names, never values, in the model context.
    fields["env_keys"] = sorted(fields.pop("env"))
    # Redact before serializing so split flags such as --password VALUE are
    # handled alongside the existing string and nested-field redaction.
    for key in ("cmd", "build", "install"):
        argv = fields[key]
        hide_next = False
        for i, arg in enumerate(argv):
            argv[i] = "***" if hide_next else _redact_text(arg)
            hide_next = arg.startswith("-") and "=" not in arg and bool(_SECRET_KEY_RE.search(arg))
    fields["cmd_summary"] = _cmd_summary(fields["cmd"])
    return {key: value for key, value in fields.items() if value is not None}


_SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|credential|api[_-]?key|private[_-]?key)", re.I)


# env 形态 dict 的判定：父键名是 env（含 dotted 覆盖路径 "components[x].env"），
# 或全部键形如经典环境变量名（UPPER_SNAKE）。
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _env_like(d: dict, key) -> bool:
    if isinstance(key, str) and (key == "env" or key.endswith(".env")):
        return True
    keys = list(d.keys())
    return (bool(keys) and all(isinstance(k, str) and _ENV_NAME_RE.match(k) for k in keys))


def _env_value(value):
    """env 变量值投影：字符串过 redact（保留 127.0.0.1 等非敏感值）；非标量只留引用指示。"""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "***"


def _sanitize(value, key: str | None = None):
    """递归脱敏进入项目上下文的一切数据（R01：边界=模型请求构造前的上下文层）。

    - 敏感键（password/token/...）：任意类型值一律 "***"（宁严勿漏，dict 值也不例外）；
    - env 形态 dict：投影为变量名 + 非敏感值（{"DB_PASSWORD": "***", "DB_HOST": "127.0.0.1"}）；
    - 字符串：过 controller.redact（掩掉 token=/--password=/DSN/bearer 等模式子串）；
    - list/dict 递归（列表元素继承父键提示，深层嵌套同样命中）。"""
    if key is not None and _SECRET_KEY_RE.search(str(key)):
        return "***"
    if isinstance(value, dict):
        if _env_like(value, key):
            return {str(k): _env_value(v) if not _SECRET_KEY_RE.search(str(k))
                    else "***" for k, v in value.items()}
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, key) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_override(key: str, value):
    """覆盖层值只承载清单层字段；键名带敏感信号的值一律脱敏（宁严勿漏）。
    R01：值本身可能是 dict/list（如 components[api].env），必须递归脱敏。"""
    return _sanitize(value, str(key))


def _overrides_raw() -> dict:
    """读覆盖层原始数据：优先 Core 线 overrides 模块，退化到直接读 overrides.json。"""
    try:
        from . import overrides as _ov  # Core 线（V2.3）；可能尚未落地
        for name in ("read", "load", "load_all", "snapshot"):
            fn = getattr(_ov, name, None)
            if callable(fn):
                data = fn()
                if isinstance(data, dict):
                    return data
    except Exception:  # noqa: BLE001 — 模块缺失/接口不符都退到文件直读
        pass
    try:
        return json.loads(paths.overrides_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _project_overrides(sids: set[str]) -> dict:
    """{sid: {dotted path: value}}（清单层字段，敏感键脱敏）。"""
    data = _overrides_raw() or {}
    svc = data.get("services") if isinstance(data, dict) else None
    if not isinstance(svc, dict):
        return {}
    out: dict[str, dict] = {}
    for sid in sids:
        entry = svc.get(sid)
        fields = entry.get("fields") if isinstance(entry, dict) else entry
        if isinstance(fields, dict) and fields:
            out[sid] = {str(k): _redact_override(k, v) for k, v in fields.items()}
    return out


def _resources_summary() -> dict:
    """资源盘点摘要（V2.6 容许旧 shape / 失败 → {}）。"""
    try:
        from . import resources
        inv = resources.inventory()
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(inv, dict) or not inv.get("ok"):
        return {}
    items = inv.get("resources")
    if not isinstance(items, list):  # 旧 shape / 空
        return {}
    return {
        "docker_context": inv.get("docker_context"),
        "items": [{"name": r.get("name"), "state": r.get("state") or "",
                   "ports": r.get("ports") or {}, "volumes": r.get("volumes") or []}
                  for r in items if isinstance(r, dict)],
    }


def build_project_context(pid: str) -> dict | None:
    """V2.10 目标项目上下文：项目/既有服务/覆盖层/资源摘要。

    防御式：registry/manifest/overrides/resources 任何一层失败都只降级（缺省/空），
    项目不存在返回 None（调用方据此 400）。上下文只含清单层字段——绝不含 env 值；
    R01：返回前对整个上下文递归脱敏（敏感键→"***"、env 投影为名字+非敏感值、
    字符串过 redact），使嵌套覆盖值/argv 摘要中的秘密无法到达模型请求。"""
    try:
        from .registry import Registry
        proj = Registry().project(pid)
    except Exception:  # noqa: BLE001 — registry 读失败按未知项目处理
        proj = None
    if not isinstance(proj, dict) or not proj:
        return None
    ctx = {"project": {"id": proj.get("id"), "name": proj.get("name"),
                       "directories": [{"id": d.get("id"), "path": d.get("path")}
                                       for d in (proj.get("directories") or [])
                                       if isinstance(d, dict)]}}
    try:
        from . import manifest
        loaded = manifest.load_all()
    except Exception:  # noqa: BLE001
        loaded = {}
    sids: list[str] = list(proj.get("services") or [])
    sids += [sid for sid, p in loaded.items() if getattr(p, "project", None) == pid]
    seen: set[str] = set()
    services: list[dict] = []
    for sid in sids:
        if not isinstance(sid, str) or sid in seen:
            continue
        seen.add(sid)
        p = loaded.get(sid)
        if p is None:
            services.append({"id": sid, "name": sid, "source": None,
                             "ports": [], "components": []})
            continue
        services.append({
            "id": p.id, "name": p.name, "source": str(p.source), "open": p.open,
            "description": p.description,
            "ports": [{"component": c.id, "port": c.port}
                      for c in p.components if getattr(c, "port", None)],
            "components": [_component_context(c) for c in p.components],
            "dependencies": [asdict(d) for d in p.dependencies],
        })
    ctx["services"] = services
    ctx["overrides"] = _project_overrides(seen)
    ctx["resources"] = _resources_summary()
    # R01：整个上下文（含覆盖层任意嵌套值、cmd 摘要、资源字段）递归脱敏后才可进入 prompt。
    return _sanitize(ctx)


def handle_analyze(body: dict) -> tuple[int, dict]:
    """启动后台分析任务 → (202, {"ok": true, "job_id": ...})。V2.10：可带 project_id。"""
    try:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body 必须是 JSON 对象"}
        project_id = body.get("project_id")
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            return 400, {"ok": False, "error": "project_id 必须是非空字符串"}
        project_ctx = None
        if project_id is not None:
            project_ctx = build_project_context(project_id)
            if project_ctx is None:
                return 400, {"ok": False, "error": "未知项目"}
        directory = body.get("directory")
        if not isinstance(directory, str) or not directory:
            return 400, {"ok": False, "error": "directory 必填"}
        agent = body.get("agent")
        if agent not in KINDS:
            return 400, {"ok": False, "error": f"agent 必须是 {list(KINDS)}"}
        d = Path(directory).expanduser()
        if not d.is_absolute():
            return 400, {"ok": False, "error": "directory 必须是绝对路径"}
        d = d.resolve()
        if not d.is_dir():
            return 400, {"ok": False, "error": f"directory 不存在: {d}"}
        focus = body.get("focus")
        if focus is not None and not isinstance(focus, str):
            return 400, {"ok": False, "error": "focus 必须是字符串"}
        timeout = 240.0
        if body.get("timeout") is not None:
            if not isinstance(body["timeout"], (int, float)) or isinstance(body["timeout"], bool):
                return 400, {"ok": False, "error": "timeout 必须是数字"}
            timeout = min(max(float(body["timeout"]), 1.0), MAX_ANALYZE_TIMEOUT)

        key = (agent, str(d), project_id, focus)
        with _LOCK:
            existing = _RUNNING.get(key)
            if existing:
                job = _read_job(existing)
                if job and job.get("state") in ("queued", "running"):
                    return 202, {"ok": True, "job_id": existing, "existing": True}
                _RUNNING.pop(key, None)
            _cleanup_old_jobs()
            job_id = "job-" + secrets.token_hex(8)
            job = {"id": job_id, "kind": "analyze", "state": "queued",
                   "agent": agent, "directory": str(d), "project_id": project_id,
                   "timeout": timeout, "activity": [],
                   "created_at": time.time(), "started_at": None,
                   "finished_at": None, "result": None, "error": None}
            _write_job(job)
            _RUNNING[key] = job_id
        _POOL.submit(_run_job, job_id, agent, str(d), focus, timeout, key, project_ctx)
        return 202, {"ok": True, "job_id": job_id}
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def _run_job(job_id: str, agent: str, directory: str, focus: str | None,
             timeout: float, key: tuple, project_ctx: dict | None = None) -> None:
    """线程体：queued → running → done/failed，每次状态变化原子落盘。"""
    job = _read_job(job_id) or {"id": job_id, "kind": "analyze"}

    def record_activity(event: str) -> None:
        activity = job.setdefault("activity", [])
        now = time.time()
        if activity and activity[-1]["event"] == event and now - activity[-1]["at"] < 1:
            return
        activity.append({"event": event, "at": now})
        del activity[:-16]
        _write_job(job)

    def record_output(text: str) -> None:
        output = job.setdefault("output", [])
        output.append({"text": text, "at": time.time()})
        del output[:-100]
        _write_job(job)

    try:
        job.update({"state": "running", "started_at": time.time()})
        _write_job(job)
        result = invoke.run_analysis(agent, directory, focus=focus, timeout=timeout,
                                     project_context=project_ctx, on_activity=record_activity,
                                     on_output=record_output)
        if result.get("ok"):
            job.update({"state": "done", "result": result, "error": None,
                        "finished_at": time.time()})
        else:
            job.update({"state": "failed", "error": f"{result.get('error')}: {result.get('detail', '')}".strip(": "),
                        "finished_at": time.time()})
    except Exception as e:  # noqa: BLE001
        job.update({"state": "failed", "error": str(e)[:500], "finished_at": time.time()})
    finally:
        _write_job(job)
        with _LOCK:
            if _RUNNING.get(key) == job_id:
                _RUNNING.pop(key, None)


def handle_job_get(job_id: str) -> tuple[int, dict]:
    try:
        if not isinstance(job_id, str) or not _JOB_ID_RE.match(job_id):
            return 400, {"ok": False, "error": "invalid job id"}
        job = _read_job(job_id)
        if job is None:
            return 404, {"ok": False, "error": "not found"}
        return 200, {"ok": True, **job}
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}
