"""Agent 检测与状态机（契约 §4）。

scan() 只做廉价的本地探测（找可执行、跑 --version、看配置根与登录信号文件），
产出 not_installed / not_executable / config_only；needs_login / call_failed /
available 只能来自 verify() 的一次真实最小调用。scan 不写任何文件，verify 会保存。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import base
from .base import AgentConnection, KINDS, VERIFIED_STATUSES

VERSION_TIMEOUT = 10.0
NPM_TIMEOUT = 5.0

# 需求里的固定发现顺序（PATH 之前可叠加 custom_paths）
FALLBACK_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
USER_SHIM_DIRS = ("~/.volta/bin", "~/.asdf/shims", "~/.local/share/mise/shims")

# 登录/认证类错误信号（小写匹配 stdout+stderr）
_LOGIN_HINTS = (
    "unauthorized", "not logged in", "login required", "please log in", "please login",
    "run /login", "api key", "api_key", "invalid api", "authentication", "401",
    "credential", "sign in",
)

VERIFY_PROMPT = "Reply with exactly: OK"


def _expanded(p: str) -> Path:
    return Path(os.path.expanduser(p))


def _candidate_dirs() -> list[Path]:
    """PATH 之外的固定发现目录（存在才有意义，去了重复）。"""
    dirs: list[Path] = []
    for raw in FALLBACK_DIRS + USER_SHIM_DIRS:
        d = _expanded(raw)
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    return dirs


def _npm_global_prefix() -> Path | None:
    """npm prefix -g；失败（无 npm / 超时）返回 None。"""
    try:
        r = subprocess.run(
            ["npm", "prefix", "-g"],
            capture_output=True, text=True, timeout=NPM_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out:
        return None
    d = Path(out[0].strip())
    return d if d.is_dir() else None


def _find_binary(kind: str, custom_paths: list[str] | None) -> tuple[str, str]:
    """按发现顺序找 <kind> 可执行。返回 (状态, path)：
    ("found", p) 可执行；（"not_executable", p) 找到文件但不可执行；（"not_installed", "")。"""
    candidates: list[Path] = []
    for raw in custom_paths or []:
        p = Path(os.path.expanduser(raw))
        if p.is_file():
            candidates.append(p)
        elif p.is_dir() and (p / kind).is_file():
            candidates.append(p / kind)
    # PATH（shutil.which 只返回可执行的，不可执行的命中要靠目录扫描补报）
    which = shutil.which(kind)
    if which:
        candidates.append(Path(which))
    else:
        env_path = os.environ.get("PATH", "")
        for d in env_path.split(os.pathsep):
            if d:
                p = Path(d) / kind
                if p.is_file():
                    candidates.append(p)
    for d in _candidate_dirs():
        p = d / kind
        if p.is_file():
            candidates.append(p)
    npm = _npm_global_prefix()
    if npm is not None:
        p = npm / "bin" / kind
        if p.is_file():
            candidates.append(p)
    seen: set[Path] = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if os.access(p, os.X_OK):
            return "found", str(p)
    if seen:
        return "not_executable", str(sorted(seen)[0])
    return "not_installed", ""


def _run_version(path: str) -> tuple[str, str]:
    """跑 <bin> --version；返回 (version_first_line, detail)。"""
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "", "--version 超时"
    except OSError as e:
        return "", f"--version 启动失败: {e}"
    if r.returncode != 0:
        return "", f"--version 退出码 {r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}"
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line:
            return line, ""
    return "", "--version 无输出"


def _config_root(kind: str) -> Path:
    if kind == "codex":
        return _expanded(os.environ.get("CODEX_HOME") or "~/.codex")
    return _expanded(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def _login_signal(kind: str, config_root: Path) -> bool | None:
    """廉价本地登录信号。True=有信号；None=未知（凭据可能在 Keychain，绝不当 needs_login）。"""
    if kind == "codex":
        auth = config_root / "auth.json"
        try:
            data = json.loads(auth.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and ("tokens" in data or "OPENAI_API_KEY" in data):
            return True
        return None
    # claude：凭据可能在 macOS Keychain，无文件不算未登录
    return True if (config_root / ".credentials.json").is_file() else None


def _merge_verified(prev: AgentConnection | None, cur: AgentConnection) -> AgentConnection:
    """path 未变且版本未变（或本次没测版本）时保留 verify 得到的状态；否则降级 config_only。"""
    if prev is None or prev.kind != cur.kind:
        return cur
    if prev.path != cur.path or prev.status not in VERIFIED_STATUSES:
        return cur
    same_version = (prev.version == cur.version) or not cur.version
    if same_version:
        cur.status = prev.status
        cur.verified_at = prev.verified_at
        if prev.detail and prev.detail not in cur.detail:
            cur.detail = f"{cur.detail}；保留上次验证结果（{prev.status}）".lstrip("；")
        return cur
    cur.detail = f"版本变化（{prev.version} → {cur.version or '未知'}），需重新验证。{cur.detail}"
    return cur


def scan(custom_paths: list[str] | None = None, run_version: bool = True) -> dict[str, AgentConnection]:
    """发现 codex/claude 并按契约 §4 归类。不发起任何模型调用，不写文件。"""
    conns: dict[str, AgentConnection] = {}
    for kind in KINDS:
        config_root = _config_root(kind)
        state, path = _find_binary(kind, custom_paths)
        version, detail = "", ""
        if state == "found":
            if run_version:
                version, vdetail = _run_version(path)
                detail = vdetail
        elif state == "not_executable":
            detail = "找到文件但无执行权限"
        else:
            detail = "所有发现位置均未找到可执行文件"
        signal_flag = _login_signal(kind, config_root) if state == "found" else None
        status = {"found": "config_only", "not_executable": "not_executable"}.get(state, "not_installed")
        if state == "found":
            notes = []
            if not config_root.is_dir():
                notes.append(f"配置根 {config_root} 不存在")
            if signal_flag is True:
                notes.append("检测到本地登录信号，未经真实调用验证，可运行 verify 确认")
            else:
                notes.append("无本地登录信号；凭据可能在 Keychain，运行 verify 确认")
            detail = "；".join(x for x in [detail, *notes] if x)
        conns[kind] = AgentConnection(
            kind=kind, path=path, version=version,
            config_root=str(config_root), status=status, detail=detail,
        )
    # 与缓存合并：保留未过期的 verify 结果
    prev, default = base.load_connections()
    for kind in KINDS:
        conns[kind] = _merge_verified(prev.get(kind), conns[kind])
        conns[kind].is_default = kind == default
    return conns


def _kill_group(proc: subprocess.Popen) -> None:
    """杀整个进程组（start_new_session=True 使子进程自成一组的补偿）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _system_proxies() -> dict[str, str]:
    from urllib import request

    get_system = getattr(request, "getproxies_macosx_sysconf", None)
    return get_system() if get_system else {}


def agent_environment() -> dict[str, str]:
    """Use macOS proxy settings for Agent children when their environment has no override."""
    env = dict(os.environ)
    keys = {key.lower() for key in env}
    override = env.pop("LOCALSERVICES_AGENT_PROXY", "")
    proxies = {"http": override, "https": override} if override else _system_proxies()
    for scheme in ("http", "https"):
        key = f"{scheme}_proxy"
        if key not in keys and "all_proxy" not in keys and proxies.get(scheme):
            env[key] = proxies[scheme]
    if "no_proxy" not in keys and proxies:
        env["no_proxy"] = "localhost,127.0.0.1,::1"
    return env


def _run_bounded(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str, bool]:
    """有界运行：返回 (returncode, stdout, stderr, timed_out)；超时杀整组绝无残留。"""
    proc = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, env=agent_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return proc.returncode or -9, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"), True


def _looks_like_login_failure(combined: str) -> bool:
    low = combined.lower()
    return any(h in low for h in _LOGIN_HINTS)


def _verify_argv(kind: str, path: str, tmpdir: str, last_message: str) -> list[str]:
    """最小真实调用 argv（只读沙箱）。"""
    if kind == "codex":
        return [
            path, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "-C", tmpdir, "-o", last_message, VERIFY_PROMPT,
        ]
    # claude：--tools 白名单只保留读类工具（无 Bash/Edit/Write），--permission-prompts
    # none 让其余审批自动拒绝 → 等效只读。不用 --restricted：它会丢弃用户 settings，
    # 本机经 settings 配置的网关认证会一并失效（实测 "Not logged in"）。
    return [
        path, "-p", VERIFY_PROMPT, "--output-format", "json",
        "--tools", "Read,Glob,Grep", "--permission-prompts", "none",
    ]


def verify(kind: str, timeout: float = 60.0) -> AgentConnection:
    """对 <kind> 做一次真实最小调用并更新状态；结果落盘 agent-connections.json。"""
    if kind not in KINDS:
        raise ValueError(f"unknown agent kind: {kind}")
    timeout = min(max(timeout, 1.0), 70.0)
    conns = scan(custom_paths=None, run_version=True)
    conn = conns[kind]
    if conn.status in ("not_installed", "not_executable"):
        conn.detail = f"无法验证：{conn.detail}"
        base.save_connections(conns)
        return conn

    with tempfile.TemporaryDirectory(prefix="ls-verify-") as tmp:
        last_message = str(Path(tmp) / "last-message.txt")
        argv = _verify_argv(kind, conn.path, tmp, last_message)
        rc, out, err, timed_out = _run_bounded(argv, tmp, timeout)
        tail = ""
        try:
            tail = Path(last_message).read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            pass
        combined = f"{out}\n{err}".strip()
        if timed_out:
            conn = conn.replace(status="call_failed", detail=f"verify 超时（{timeout:.0f}s）后已杀进程组", verified_at=time.time())
        elif rc == 0 and "OK" in (f"{out}\n{tail}"):
            conn = conn.replace(status="available", detail="最小调用返回 OK", verified_at=time.time())
        elif _looks_like_login_failure(combined):
            conn = conn.replace(
                status="needs_login", verified_at=time.time(),
                detail=f"真实调用返回登录/认证错误: {combined[:300]}",
            )
        else:
            conn = conn.replace(
                status="call_failed", verified_at=time.time(),
                detail=f"真实调用失败（退出码 {rc}）: {combined[:300]}",
            )
    conns[kind] = conn
    base.save_connections(conns)
    return conn
