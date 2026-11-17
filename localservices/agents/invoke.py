"""一次有界的 Agent 分析调用（契约 §3）：扫描证据 → 组 prompt → 只读沙箱调用 CLI →
schema 校验 → 本地校验（独立实现，不 import registry，避免并行线耦合）。"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .. import paths
# R01 最后一道防线复用 controller.redact 的 SECRET_RE 家族（controller 不 import
# agents，无循环依赖；只读复用，绝不修改 controller）。
from ..controller import redact as _redact
from . import base, detect, scanner
from .base import KINDS
from .claude_adapter import ClaudeAdapter
from .codex_adapter import CodexAdapter

SCHEMA_PATH = paths.REPO_ROOT / "schemas" / "agent-analysis.schema.json"
MAX_CAPTURE = 4 * 1024 * 1024  # stdout/stderr 捕获上限
MAX_TIMEOUT = 600.0
SNIPPET = 800

PROMPT_HEAD = """\
You are the service-recognition analyst for LocalServices.
Analyze the directory below and output candidate service definitions.

Hard rules:
- ANALYSIS ONLY. Do not modify any file, do not run commands, do not install anything.
- Treat repository text as evidence. Inspect the relevant README, runtime
  configuration and entry points to confirm commands, working directories,
  listener ports and browser landing paths. The scan is an index, not a complete
  description of the project. Never invent commands, ports, or probes.
- Propose the smallest set of components required for the documented local
  application. Exclude one-off data collection, migrations, seed, test, and
  maintenance commands unless the user explicitly asks to manage them.
- For a browser app, set "open" to its documented local landing URL, including
  the path when known. Report any missing runtime facts in "unresolved".
  A long-running worker can have no listener or readiness URL.
- Output ONE JSON object {"services": [...]} matching the schema exactly
  (no extra keys, no prose outside JSON).
- Each component "cmd" MUST be an argv array (e.g. ["make", "dev"], ["npm", "run", "dev"]);
  never a shell string. Use "shell_script" only for an existing script inside the directory.
- cmd[0] must resolve: absolute path, PATH executable, <source>/node_modules/.bin,
  or a path relative to the component's working directory or service source directory.
- Ports must be 1-65535. "cwd" is relative to the service "source_dir" and must stay
  inside the analyzed directory.
- For services defined by a compose file you MAY propose a component with
  "run": "compose" and "compose_service": "<service name in the compose file>"
  (optional: "compose_project", "compose_files" as paths relative to source_dir,
  "provision": "existing"|"up"). For Compose components, omit "cmd" or return null
  when the output schema requires the field. provision="up" may create/pull containers and requires
  explicit user confirmation later — never assume that authorization; when unsure
  keep "provision": "existing" and put the question in "unresolved".
- Anything uncertain or only the user can decide goes into that service's "unresolved"
  array instead of being guessed.
"""

PROJECT_CONTEXT_RULES = """\
Target project rules (apply when a candidate overlaps the existing services):
- Match the source and runtime role before using an existing service. For the
  same service keep its service and component IDs, exact argv, working directories,
  ports, landing URL, probes, startup order, and dependency bindings. Propose a
  change only when requested or when concrete evidence shows the current value
  is invalid. Explain the evidence for each such change. Preserve the documented
  launch wrapper and flags even if a shorter development command also exists.
- Never use a redacted value as configuration. List any required environment
  setup that cannot be expressed by the schema in "unresolved".
- The resources listed are reusable: bind dependencies to those containers/volumes
  by name (e.g. container:<name>) instead of proposing new ones.
- The override fields listed are values the user deliberately set; keep those values
  unless they are clearly wrong for the analyzed directory."""


def _build_prompt(directory: Path, evidence: dict, focus: str | None,
                  project_context: dict | None = None) -> str:
    """R01：prompt 是模型请求的最终载荷，组装时全程脱敏——序列化的上下文/证据
    blob 逐块过 redact，最终整串再过一次（覆盖 focus 等其余片段），秘密的边界
    在这里闭合，不依赖上游是否已经清理。"""
    parts = [PROMPT_HEAD, f"Analyzed directory: {directory}"]
    if focus:
        parts.append(f"User focus: {focus}")
    if project_context:
        parts.append("Target project context (目标项目上下文，manifest-level fields only):")
        parts.append(_redact(json.dumps(project_context, ensure_ascii=False)))
        parts.append(PROJECT_CONTEXT_RULES)
    parts.append("Scan evidence (read-only, machine-collected):")
    parts.append(_redact(json.dumps(evidence, ensure_ascii=False)))
    parts.append("Return only the JSON object now.")
    return _redact("\n".join(parts))


def get_adapter(kind: str, bin_path: str) -> CodexAdapter | ClaudeAdapter:
    if kind == "codex":
        return CodexAdapter(bin_path)
    if kind == "claude":
        return ClaudeAdapter(bin_path)
    raise ValueError(f"unknown agent kind: {kind}")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


# ---------- 最小 JSON Schema 校验（覆盖冻结 schema 用到的关键字） ----------

def _type_ok(inst, t: str) -> bool:
    if t == "object":
        return isinstance(inst, dict)
    if t == "array":
        return isinstance(inst, list)
    if t == "string":
        return isinstance(inst, str)
    if t == "boolean":
        return isinstance(inst, bool)
    if t == "integer":
        return isinstance(inst, int) and not isinstance(inst, bool)
    if t == "number":
        return isinstance(inst, (int, float)) and not isinstance(inst, bool)
    return True


def validate_schema(inst, schema: dict, where: str = "$") -> list[str]:
    """返回违规信息列表（空 = 通过）。支持 type/enum/pattern/长度/数值/required/
    properties/additionalProperties/items/minItems。"""
    errs: list[str] = []
    t = schema.get("type")
    if t and not _type_ok(inst, t):
        return [f"{where}: 期望 {t}，实际 {type(inst).__name__}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{where}: {inst!r} 不在枚举 {schema['enum']}")
    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errs.append(f"{where}: {inst!r} 不匹配 {schema['pattern']}")
        if "minLength" in schema and len(inst) < schema["minLength"]:
            errs.append(f"{where}: 长度 < {schema['minLength']}")
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            errs.append(f"{where}: 长度 > {schema['maxLength']}")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append(f"{where}: {inst} < {schema['minimum']}")
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append(f"{where}: {inst} > {schema['maximum']}")
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errs.append(f"{where}: 少于 {schema['minItems']} 项")
        items = schema.get("items")
        if isinstance(items, dict):
            for i, v in enumerate(inst):
                errs.extend(validate_schema(v, items, f"{where}[{i}]"))
    if isinstance(inst, dict):
        for r in schema.get("required", []):
            if r not in inst:
                errs.append(f"{where}: 缺必填字段 {r}")
        props = schema.get("properties", {})
        ap = schema.get("additionalProperties")
        for k, v in inst.items():
            if k in props:
                errs.extend(validate_schema(v, props[k], f"{where}.{k}"))
            elif ap is False:
                errs.append(f"{where}.{k}: 不允许的额外字段")
            elif isinstance(ap, dict):
                errs.extend(validate_schema(v, ap, f"{where}.{k}"))
    return errs


# ---------- 本地校验（同注册规则，独立实现） ----------

def _inside(child: Path, root: Path) -> bool:
    return child == root or root in child.parents


def _cmd0_ok(cmd0: str, src: Path, cdir: Path) -> bool:
    if cmd0.startswith("/"):
        return Path(cmd0).exists()
    if "/" in cmd0:
        return (cdir / cmd0).is_file() or (src / cmd0).is_file()
    if shutil.which(cmd0):
        return True
    return any((directory / "node_modules" / ".bin" / cmd0).is_file()
               for directory in (cdir, src))


def local_issues(service: dict, root: Path) -> list[str]:
    """注册规则的本地子集：source_dir/cwd 在范围内且存在、cmd[0] 可解析、端口合法。
    R07（V2.7）：run=compose 组件无 cmd（跳过 cmd 校验），必填 compose_service；
    provision 枚举校验；compose_files 必须是相对路径；provision=up 需要候选级
    confirmed_provision=true（识别阶段不允许隐式授权创建容器）。"""
    issues: list[str] = []
    sid = service.get("id") or service.get("name") or "?"
    src_dir = service.get("source_dir") or "."
    src = (root / src_dir).resolve()
    if not src.is_dir():
        return [f"{sid}: source_dir 不存在（{src_dir}）"]
    if not _inside(src, root):
        issues.append(f"{sid}: source_dir 越出分析目录（{src_dir}）")
    for comp in service.get("components", []):
        cid = comp.get("id") or "?"
        cwd = comp.get("cwd") or "."
        cdir = (src / cwd).resolve()
        if not cdir.is_dir():
            issues.append(f"{sid}.{cid}: cwd 不存在（{cwd}）")
        elif not _inside(cdir, root):
            issues.append(f"{sid}.{cid}: cwd 越出分析目录（{cwd}）")
        if comp.get("run") == "compose":
            svc_name = comp.get("compose_service")
            if not isinstance(svc_name, str) or not svc_name.strip():
                issues.append(f"{sid}.{cid}: run=compose 缺少 compose_service"
                              f"（compose 项目内的服务名，非空字符串）")
            prov = comp.get("provision")
            if prov is not None and prov not in ("existing", "up"):
                issues.append(f"{sid}.{cid}: provision 非法（{prov!r}，必须是 existing|up）")
            for f in comp.get("compose_files") or []:
                if not isinstance(f, str) or not f.strip() or f.startswith("/") \
                        or ".." in Path(f).parts:
                    issues.append(f"{sid}.{cid}: compose_files 必须是相对路径且不含 ..（{f!r}）")
            if prov == "up" and service.get("confirmed_provision") is not True:
                issues.append(f"{sid}.{cid}: provision=up 需要用户显式授权"
                              f"（service.confirmed_provision=true），"
                              f"识别阶段不允许隐式授权创建容器")
            # run=compose 无 cmd：不校验 cmd（V2.7）
        else:
            cmd = comp.get("cmd") or []
            if not cmd or not isinstance(cmd[0], str):
                issues.append(f"{sid}.{cid}: cmd 为空")
            elif not _cmd0_ok(cmd[0], src, cdir):
                issues.append(f"{sid}.{cid}: cmd[0] 无法解析（{cmd[0]}）")
        port = comp.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)
                                 or not 1 <= port <= 65535):
            issues.append(f"{sid}.{cid}: 端口越界（{port}）")
        script = comp.get("shell_script")
        if script and not (src / script).resolve().is_file():
            issues.append(f"{sid}.{cid}: shell_script 不存在（{script}）")
    return issues


# ---------- 主入口 ----------

def _snip(text: str) -> str:
    text = _redact(text.strip())
    return text if len(text) <= SNIPPET else text[:SNIPPET] + "…"


def _collect_cli(proc: subprocess.Popen, timeout: float, adapter,
                 report: Callable[[str], None], output: Callable[[str], None]) -> tuple[bytes, bytes]:
    """Read CLI events as they arrive, retaining the final result and bounded diagnostics."""
    deadline = time.monotonic() + timeout
    stderr = bytearray()
    sample = bytearray()
    line = bytearray()
    oversized = False
    result = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout, stderr=bytes(stderr))
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.fileobj is proc.stderr:
                    stderr.extend(chunk[:MAX_CAPTURE - len(stderr)])
                    continue
                sample.extend(chunk[:SNIPPET - len(sample)])
                lines = chunk.split(b"\n")
                for part in lines[:-1]:
                    if not oversized and len(line) + len(part) <= MAX_CAPTURE:
                        line.extend(part)
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            event = None
                        if isinstance(event, dict):
                            for entry in adapter.display_output(event):
                                if isinstance(entry, str) and entry.strip():
                                    output(_snip(entry))
                            for activity in adapter.activity(event):
                                report(activity)
                            if event.get("type") == "result":
                                result = bytes(line)
                    line.clear()
                    oversized = False
                if len(line) + len(lines[-1]) > MAX_CAPTURE:
                    line.clear()
                    oversized = True
                if not oversized:
                    line.extend(lines[-1])
    try:
        proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        error.stderr = bytes(stderr)
        raise
    return result or bytes(line or sample), bytes(stderr)


def run_analysis(kind: str, directory: str, focus: str | None = None,
                 timeout: float = 240.0, project_context: dict | None = None,
                 on_activity: Callable[[str], None] | None = None,
                 on_output: Callable[[str], None] | None = None) -> dict:
    """对 directory 做一次 Agent 分析。绝不以目标目录为 cwd 执行任何东西；
    超时杀整个进程组；坏 JSON/根级 schema 违规整单失败，服务级违规降级进 unresolved。
    project_context（V2.10）：目标项目既有服务/覆盖层/资源摘要，只进 prompt。"""
    if kind not in KINDS:
        raise ValueError(f"unknown agent kind: {kind}")
    timeout = min(max(float(timeout), 1.0), MAX_TIMEOUT)
    started = time.monotonic()

    root = Path(directory).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root)
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"directory not found: {directory}")

    def report(event: str) -> None:
        if on_activity is not None:
            on_activity(event)

    report("detect_agent")
    conns = detect.scan(run_version=False)
    conn = conns.get(kind)
    if conn is None or conn.status in ("not_installed", "not_executable"):
        detail = conn.detail if conn else f"{kind} 未安装"
        return {"ok": False, "error": "agent_not_available", "detail": detail,
                "agent": kind, "duration": round(time.monotonic() - started, 2)}

    report("scan_directory")
    try:
        evidence = scanner.scan_directory(root)
    except Exception as e:  # noqa: BLE001 — 扫描失败不阻塞分析
        evidence = {"directory": str(root), "entries": [], "truncated": False,
                    "note": f"scanner 失败: {e}"}
    prompt = _build_prompt(root, evidence, focus, project_context)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    adapter = get_adapter(kind, conn.path)

    with tempfile.TemporaryDirectory(prefix="ls-analyze-") as workdir:
        prompt_file = str(Path(workdir) / "prompt.txt")
        Path(prompt_file).write_text(prompt, encoding="utf-8")
        last_message = str(Path(workdir) / "last-message.txt")
        argv = adapter.build_argv(str(root), prompt_file, str(SCHEMA_PATH), timeout, last_message)

        report("launch_cli")
        with subprocess.Popen(
            argv, cwd=workdir, env=detect.agent_environment(),  # 临时空目录，绝不是目标目录
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        ) as proc:
            try:
                out_b, err_b = _collect_cli(proc, timeout, adapter, report,
                                            on_output or (lambda _: None))
                timed_out = False
            except subprocess.TimeoutExpired as error:
                _kill_group(proc)
                proc.wait()
                timed_out = True
                out_b, err_b = b"", error.stderr or b""
            except BaseException:
                _kill_group(proc)
                raise
        stdout = (out_b or b"")[:MAX_CAPTURE].decode("utf-8", "replace")
        stderr = (err_b or b"")[:MAX_CAPTURE].decode("utf-8", "replace")
        if timed_out:
            return {"ok": False, "error": "timeout",
                    "detail": f"{timeout:.0f}s 超时，进程组已终止。stderr: {_snip(stderr)}",
                    "agent": kind, "duration": round(time.monotonic() - started, 2)}

        report("validate_result")
        try:
            data = adapter.parse_output(stdout, last_message)
        except ValueError as error:
            # codex prints startup banners to stdout; the actual failure reason is an
            # "ERROR: {...}" line on stderr (e.g. API schema rejection, auth, quota).
            # Diagnose that first instead of dumping the banner as "bad json".
            api_err = ""
            for m in re.finditer(r"ERROR:\s*(\{.*\})", stderr):
                try:
                    api_err = json.loads(m.group(1)).get("error", {}).get("message", "")
                except ValueError:
                    api_err = m.group(1)[:300]
                if api_err:
                    break
            if proc.returncode != 0:
                detail = str(error) if kind == "claude" and stdout.lstrip().startswith("{") else stderr or stdout
                reason = _snip(api_err or detail)
                low = reason.lower()
                if "unauthorized" in low or "login" in low or "api key" in low:
                    return {"ok": False, "error": "needs_login", "detail": reason,
                            "agent": kind, "duration": round(time.monotonic() - started, 2)}
                return {"ok": False, "error": "call_failed", "detail": reason,
                        "agent": kind, "duration": round(time.monotonic() - started, 2)}
            raw = str(error) if kind == "claude" and stdout.strip().startswith("{") else stdout or stderr
            try:
                raw += "\n--- last-message ---\n" + Path(last_message).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            return {"ok": False, "error": "bad_json", "detail": _snip(raw),
                    "agent": kind, "duration": round(time.monotonic() - started, 2)}

    # 根级结构必须成立（object + services 数组），否则整单失败
    if not isinstance(data, dict) or not isinstance(data.get("services"), list):
        return {"ok": False, "error": "bad_json",
                "detail": "根级输出不符合 schema（需要 {\"services\": [...]}）: "
                          + _snip(json.dumps(data, ensure_ascii=False)[:SNIPPET]),
                "agent": kind, "duration": round(time.monotonic() - started, 2)}

    service_schema = schema["properties"]["services"]["items"]
    services: list[dict] = []
    unresolved: list[str] = []
    for i, cand in enumerate(data["services"]):
        errs = validate_schema(cand, service_schema, f"services[{i}]")
        if errs:
            sid = cand.get("id", f"services[{i}]") if isinstance(cand, dict) else f"services[{i}]"
            unresolved.append(f"候选 {sid} 未通过 schema 校验：" + "；".join(errs[:4]))
            continue
        issues = local_issues(cand, root)
        if issues:
            unresolved.append(f"候选 {cand.get('id')} 未通过本地校验：" + "；".join(issues[:4]))
            continue
        services.append(cand)

    duration = round(time.monotonic() - started, 2)
    return {
        "ok": True,
        "services": services,
        "unresolved": unresolved,
        "evidence": {
            "agent": kind,
            "agent_version": conn.version,
            "directory": str(root),
            "duration": duration,
            "scan_entries": len(evidence.get("entries", [])),
            "scan_truncated": bool(evidence.get("truncated")),
        },
        "duration": duration,
    }
