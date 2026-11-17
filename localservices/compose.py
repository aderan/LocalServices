"""Compose run adapter (CONTRACTS V2.7): components with run="compose" are managed as
docker compose services instead of launchd jobs.

Hard rules baked into this module:
- Only the declared compose service of the declared compose project is ever touched;
- Stop is `docker-compose … stop <svc>` — NEVER down/rm/kill, never volume removal
  (a token guard refuses those verbs outright);
- Actions (ensure/stop) run only against a local engine (dockerctx.is_local),
  remote endpoints raise with 远程 engine 不自动操作;
- All failures raise ComposeError with an actionable, user-facing message.

Subprocess seams: `_run` (docker CLI) and `_compose_run` (docker-compose binary) are
module-level so tests can patch them; both default to dockerctx (explicit context,
scrubbed env; docker-compose gets DOCKER_HOST=<context endpoint>).
"""

from __future__ import annotations

import json
from functools import wraps
from pathlib import Path
from subprocess import SubprocessError

from . import dockerctx, manifest
from .manifest import Component, Project


class ComposeError(RuntimeError):
    """Actionable compose failure (message is user-facing)."""


TIMEOUT = 30.0
COMPOSE_TIMEOUT = 120.0
UP_TIMEOUT = 300.0  # up may pull/build; the caller's deadline still applies at controller level
_FORBIDDEN_VERBS = {"down", "rm", "kill"}  # compose 适配器绝不执行（不删容器/网络/卷）
_BASE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


def _bound(fn):
    @wraps(fn)
    def call(p, *args, **kwargs):
        try:
            with dockerctx.bound(getattr(p, "docker", None)):
                return fn(p, *args, **kwargs)
        except dockerctx.BindingError as exc:
            raise ComposeError(str(exc)) from exc
        except (OSError, SubprocessError) as exc:
            raise ComposeError(f"Docker {dockerctx.name()} 命令失败：{exc}") from exc
    return call


def _run(args: list[str], timeout: float = TIMEOUT):
    """docker CLI seam（测试 monkeypatch 这里）。"""
    return dockerctx.run(args, timeout=timeout)


def _compose_run(args: list[str], timeout: float = COMPOSE_TIMEOUT):
    """docker-compose CLI seam（测试 monkeypatch 这里）。"""
    bad = _FORBIDDEN_VERBS.intersection(args)
    if bad:
        raise ComposeError(f"compose 适配器绝不执行 {'/'.join(sorted(bad))}（不删容器/网络/卷）")
    return dockerctx.compose_run(args, timeout=timeout)


def _require_local() -> None:
    if not dockerctx.is_local():
        endpoint = dockerctx.endpoint()
        raise ComposeError(f"{dockerctx.REMOTE_REFUSAL}：compose 上下文 {dockerctx.name()} 指向 "
                           f"{endpoint or '不可达'} engine")


def project_name(p: Project, c: Component) -> str:
    """compose 项目名：显式声明或缺省取组件 cwd 目录名。"""
    return c.compose_project or p.comp_cwd(c).name


def compose_files(p: Project, c: Component) -> list[Path]:
    """compose 文件：显式声明（相对组件 cwd）或自动探测 cwd 下 docker-compose*.y*ml / compose*.y*ml。"""
    cwd = p.comp_cwd(c)
    if c.compose_files:
        files = [Path(f) if Path(f).is_absolute() else cwd / f for f in c.compose_files]
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            raise ComposeError(f"compose 文件不存在：{', '.join(missing)}")
        return files
    found: list[Path] = []
    for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml"):
        found.extend(sorted(cwd.glob(pattern)))
    unique: list[Path] = []
    for f in found:
        if f not in unique:
            unique.append(f)
    if not unique:
        raise ComposeError(f"找不到 compose 文件（{cwd} 下无 docker-compose*.y*ml / compose*.y*ml）")
    # 基础文件在前，override/其他文件在后（compose 语义：后给的 -f 覆盖先给的）
    unique.sort(key=lambda f: (0 if f.name in _BASE_FILES else 1, f.name))
    return unique


def _ps_labels(row: dict) -> dict:
    """docker ps --format {{json .}} 的 Labels 是逗号拼接字符串（真实引擎行为，
    inspect 才返回字典）；统一规范化，避免 .get 崩溃（N04 F-B1）。"""
    labels = row.get("Labels")
    if isinstance(labels, str):
        out: dict[str, str] = {}
        for kv in labels.split(","):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k] = v
        return out
    return labels if isinstance(labels, dict) else {}


@_bound
def container_name(p: Project, c: Component) -> str | None:
    """声明服务实际对应的容器名（docker ps -a，**只按 compose project+service 标签**解析）。

    容器名巧合不能证明归属（任何人都能创建同名无标签容器）——名字回退已移除（R04）：
    无标签匹配 → None（调用方绝不据此 start/stop 未知容器）；docker 本身不可用 → ComposeError。
    """
    proj = project_name(p, c)
    r = _run(["ps", "-a", "--filter", f"label=com.docker.compose.project={proj}", "--format", "{{json .}}"])
    if r.returncode != 0:
        raise ComposeError(f"无法列出 compose 项目 {proj} 的容器：{(r.stderr or r.stdout).strip()[:200]}")
    for line in r.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        labels = _ps_labels(row)
        if labels.get("com.docker.compose.service") == c.compose_service:
            name = str(row.get("Names", "")).split(",")[0].strip()
            if name:
                return name
    return None


def _inspect_state(container: str) -> dict | None:
    r = _run(["container", "inspect", "--format", "{{json .State}}", container])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise ComposeError(f"无法解析容器 {container} 的状态：{e}") from e


@_bound
def ensure_started(p: Project, c: Component) -> dict:
    """把声明的 compose 服务带起来（幂等）。

    - 容器已存在且在运行 → no-op；
    - 容器已存在但停止 → `docker start <container>`（provision=existing 授权范围）；
    - 容器缺失 → 仅 provision="up" 时 `docker-compose -p … -f … up -d --no-deps <svc>`；
      否则 ComposeError（绝不擅自创建/拉取）。
    """
    _require_local()
    proj = project_name(p, c)
    name = container_name(p, c)
    if name:
        state = _inspect_state(name) or {}
        if state.get("Running"):
            return {"ok": True, "action": "already-running", "container": name}
        r = _run(["start", name], timeout=60)
        if r.returncode != 0:
            raise ComposeError(f"容器 {name} 启动失败：{(r.stderr or r.stdout).strip()[:300]}")
        return {"ok": True, "action": "started", "container": name}
    if c.provision != "up":
        raise ComposeError(f"容器不存在且未授权创建（provision=existing）："
                           f"compose 项目 {proj} 的服务 {c.compose_service}，请先手工创建或改用 provision=up")
    files = compose_files(p, c)
    args = ["-p", proj]
    for f in files:
        args += ["-f", str(f)]
    args += ["up", "-d", "--no-deps", c.compose_service]
    r = _compose_run(args, timeout=UP_TIMEOUT)
    if r.returncode != 0:
        raise ComposeError(f"compose up 失败（{proj}/{c.compose_service}）：{(r.stderr or r.stdout).strip()[:300]}")
    return {"ok": True, "action": "up", "container": container_name(p, c) or f"{proj}-{c.compose_service}-1"}


@_bound
def stop_component(p: Project, c: Component) -> dict:
    """停止声明的 compose 服务（只 stop，绝不 down/rm/删卷）；容器缺失 → no-op。"""
    _require_local()
    proj = project_name(p, c)
    name = container_name(p, c)
    if not name:
        return {"ok": True, "action": "missing", "container": None}
    state = _inspect_state(name) or {}
    if not state.get("Running"):
        return {"ok": True, "action": "already-stopped", "container": name}
    try:
        registered = manifest.load_all()
    except (OSError, ValueError) as exc:
        raise ComposeError(f"无法核对容器 {name} 的共享引用：{exc}") from exc
    for other in registered.values():
        if other.id == p.id:
            continue
        uses_container = any(d.container == name for d in other.dependencies)
        same_workload = any(oc.run == "compose" and oc.compose_service == c.compose_service
                            and project_name(other, oc) == proj for oc in other.components)
        if (uses_container or same_workload) and dockerctx.same_environment(p.docker, other.docker):
            raise ComposeError(f"容器 {name} 也被 {other.name} 引用，请保留共享容器")
    files = compose_files(p, c)
    args = ["-p", proj]
    for f in files:
        args += ["-f", str(f)]
    args += ["stop", c.compose_service]
    r = _compose_run(args, timeout=90)
    if r.returncode != 0:
        raise ComposeError(f"compose stop 失败（{proj}/{c.compose_service}）：{(r.stderr or r.stdout).strip()[:300]}")
    return {"ok": True, "action": "stopped", "container": name}


@_bound
def component_state(p: Project, c: Component) -> dict:
    """inspect → {"state": running|<Status>|missing|unknown, "health": healthy/…|None, "container": …}。"""
    name = container_name(p, c)
    if not name:
        return {"state": "missing", "health": None, "container": None}
    state = _inspect_state(name)
    if state is None:
        return {"state": "missing", "health": None, "container": None}
    health = (state.get("Health") or {}).get("Status")  # None = 容器没有 healthcheck
    if state.get("Running"):
        return {"state": "running", "health": health, "container": name}
    return {"state": state.get("Status") or "exited", "health": health, "container": name}


@_bound
def logs(p: Project, c: Component, lines: int = 200) -> str:
    """`docker logs --tail N <container>`（只读）。"""
    name = container_name(p, c)
    if not name:
        raise ComposeError("容器不存在，无日志可读")
    r = _run(["logs", "--tail", str(lines), name])
    if r.returncode != 0:
        raise ComposeError(f"读取容器 {name} 日志失败：{(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout
