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
from pathlib import Path

from . import paths
from .agents import base, detect, invoke
from .agents.base import KINDS

MAX_VERIFY_TIMEOUT = 70.0
MAX_ANALYZE_TIMEOUT = 600.0
JOB_TTL = 24 * 3600.0  # >24h 的已结束任务在新任务提交时顺带删除

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ls-analyze")
_LOCK = threading.Lock()
_RUNNING: dict[str, str] = {}  # f"{agent}\0{directory}" -> job_id（分析进行中）

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
    """对 {kind} 做一次真实最小调用（≤70s）并更新缓存。"""
    try:
        if not isinstance(body, dict) or body.get("kind") not in KINDS:
            return 400, {"ok": False, "error": f"kind 必须是 {list(KINDS)}"}
        timeout = 60.0
        if body.get("timeout") is not None:
            if not isinstance(body["timeout"], (int, float)) or isinstance(body["timeout"], bool):
                return 400, {"ok": False, "error": "timeout 必须是数字"}
            timeout = min(max(float(body["timeout"]), 1.0), MAX_VERIFY_TIMEOUT)
        conn = detect.verify(body["kind"], timeout=timeout)
        return 200, {"ok": True, "connection": conn.to_dict()}
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def handle_analyze(body: dict) -> tuple[int, dict]:
    """启动后台分析任务 → (202, {"ok": true, "job_id": ...})。"""
    try:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body 必须是 JSON 对象"}
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

        key = f"{agent}\0{d}"
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
                   "agent": agent, "directory": str(d),
                   "created_at": time.time(), "started_at": None,
                   "finished_at": None, "result": None, "error": None}
            _write_job(job)
            _RUNNING[key] = job_id
        _POOL.submit(_run_job, job_id, agent, str(d), focus, timeout, key)
        return 202, {"ok": True, "job_id": job_id}
    except Exception as e:  # noqa: BLE001
        return 500, {"ok": False, "error": str(e)}


def _run_job(job_id: str, agent: str, directory: str, focus: str | None,
             timeout: float, key: str) -> None:
    """线程体：queued → running → done/failed，每次状态变化原子落盘。"""
    try:
        job = _read_job(job_id) or {"id": job_id, "kind": "analyze"}
        job.update({"state": "running", "started_at": time.time()})
        _write_job(job)
        result = invoke.run_analysis(agent, directory, focus=focus, timeout=timeout)
        if result.get("ok"):
            job.update({"state": "done", "result": result, "error": None,
                        "finished_at": time.time()})
        else:
            job.update({"state": "failed", "error": f"{result.get('error')}: {result.get('detail', '')}".strip(": "),
                        "finished_at": time.time()})
    except Exception as e:  # noqa: BLE001
        job = _read_job(job_id) or {"id": job_id, "kind": "analyze"}
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
