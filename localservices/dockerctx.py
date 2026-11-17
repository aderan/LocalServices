"""Docker context pinning (CONTRACTS V2.6).

Every docker invocation from this tool goes through here: `docker --context <name>`
with DOCKER_HOST / DOCKER_CONTEXT / DOCKER_CONFIG scrubbed from the child environment,
so behavior never depends on — and never mutates — the user's global context selection
(`docker context use` is never called).

Locality rule: a context whose endpoint is a local unix socket may be operated
(ensure/stop/attach-style actions); anything else (tcp://, ssh://, remote…) is
read-only territory — inventory may look, actions must refuse with 远程 engine 不自动操作.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import subprocess
import time

DEFAULT_CONTEXT = "colima"
ENV_VAR = "LOCALSERVICES_DOCKER_CONTEXT"
REMOTE_REFUSAL = "远程 engine 不自动操作"
TIMEOUT = 10.0

# Child env keys that would silently redirect docker to another engine.
_SCRUB = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")

# endpoint() resolution is a docker call; cache it briefly (tests can reset).
_ENDPOINT_TTL = 30.0
_endpoint_cache: tuple[float, str | None] = (0.0, None)
_binding: ContextVar[dict | None] = ContextVar("docker_binding", default=None)
CONTEXT_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


class BindingError(RuntimeError):
    pass


def canonical_endpoint(value: str) -> str:
    if value.startswith("unix:///"):
        return f"unix://{os.path.realpath(value.removeprefix('unix://'))}"
    return value


def inspect_context(context: str) -> str:
    if not CONTEXT_NAME.fullmatch(context):
        return ""
    try:
        result = subprocess.run(
            ["docker", "--context", context, "context", "inspect",
             "--format", "{{.Endpoints.docker.Host}}", context],
            capture_output=True, text=True, timeout=TIMEOUT, env=scrub_env())
    except (OSError, subprocess.SubprocessError):
        return ""
    return canonical_endpoint(result.stdout.strip()) if result.returncode == 0 else ""


@contextmanager
def bound(binding: dict | None, *, allow_stopped_colima=False):
    """Validate an explicit binding once and pin its socket for the operation."""
    if not binding or _binding.get() == binding:
        yield
        return
    context = binding["context"]
    target = inspect_context(context)
    if not target and allow_stopped_colima:
        target = stopped_colima_endpoint(context)
    if not target or target != binding["endpoint"]:
        raise BindingError(f"Docker 连接 {context} 已变化或无法解析，请核对服务的运行环境绑定")
    if not target.startswith("unix:///"):
        raise BindingError(REMOTE_REFUSAL)
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)


def colima_root() -> Path:
    return Path(os.environ.get("COLIMA_HOME") or (Path.home() / ".colima")).expanduser().resolve()


def colima_context(profile: str) -> str:
    return "colima" if profile == "default" else f"colima-{profile}"


def colima_endpoint(profile: str) -> str:
    return f"unix://{colima_root() / profile / 'docker.sock'}"


def is_orbstack(endpoint_value: str) -> bool:
    return endpoint_value == canonical_endpoint(f"unix://{Path.home() / '.orbstack/run/docker.sock'}")


def colima_profile(endpoint_value: str, profiles: list[dict]) -> str | None:
    for record in profiles:
        profile = record.get("name")
        if (not isinstance(profile, str) or not CONTEXT_NAME.fullmatch(profile)
                or record.get("runtime") != "docker"):
            continue
        if endpoint_value == colima_endpoint(profile):
            return profile
    return None


def colima_profiles(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        if line.strip():
            record = json.loads(line)
            rows.extend(record if isinstance(record, list) else [record])
    profiles = [row for row in rows if isinstance(row, dict)]
    for record in profiles:
        profile = record.get("name")
        if record.get("runtime") or not isinstance(profile, str) or not CONTEXT_NAME.fullmatch(profile):
            continue
        # Stopped profiles omit runtime from the CLI response. Check the saved setting.
        try:
            config = (colima_root() / profile / "colima.yaml").read_text()
        except OSError:
            continue
        match = re.search(r"""(?m)^runtime:\s*['"]?([a-z]+)['"]?\s*(?:#.*)?$""", config)
        if match:
            record["runtime"] = match.group(1)
    return profiles


def list_colima_profiles() -> list[dict]:
    try:
        result = subprocess.run(["colima", "list", "--json"], capture_output=True, text=True,
                                timeout=4, env=scrub_env())
        return colima_profiles(result.stdout) if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def stopped_colima_endpoint(context: str) -> str:
    if context != "colima" and not context.startswith("colima-"):
        return ""
    profile = "default" if context == "colima" else context.removeprefix("colima-")
    if not CONTEXT_NAME.fullmatch(profile) or context != colima_context(profile):
        return ""
    endpoint_value = colima_endpoint(profile)
    return endpoint_value if colima_profile(endpoint_value, list_colima_profiles()) == profile else ""


def active_binding() -> dict | None:
    return _binding.get()


def same_environment(left: dict | None, right: dict | None) -> bool:
    if not left and not right:
        return True
    if not left or not right:
        legacy = inspect_context(default_name()) or stopped_colima_endpoint(default_name())
        return (left or right)["endpoint"] == legacy
    return left["endpoint"] == right["endpoint"]


def default_name() -> str:
    return os.environ.get(ENV_VAR) or DEFAULT_CONTEXT


def name() -> str:
    """The pinned context name (env LOCALSERVICES_DOCKER_CONTEXT, default colima)."""
    binding = _binding.get()
    return binding["context"] if binding else default_name()


def scrub_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ minus docker redirect keys; `extra` wins (e.g. PATH, DOCKER_HOST pinning)."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    if extra:
        env.update(extra)
    return env


def argv(args: list[str]) -> list[str]:
    """Full docker command line for args, with the explicit --context flag."""
    binding = _binding.get()
    if binding:
        return ["docker", "--host", binding["endpoint"], *args]
    return ["docker", "--context", name(), *args]


def run(args: list[str], timeout: float = TIMEOUT, check: bool = False,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run `docker --context <name> <args...>` with a scrubbed environment."""
    proc = subprocess.run(argv(args), capture_output=True, text=True, timeout=timeout,
                          env=env if env is not None else scrub_env())
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} 失败（exit {proc.returncode}）："
                           f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return proc


def reset_cache() -> None:
    """Forget the cached endpoint (tests / context switches)."""
    global _endpoint_cache
    _endpoint_cache = (0.0, None)


def endpoint(refresh: bool = False) -> str | None:
    """The pinned context's docker endpoint (e.g. unix:///…/docker.sock); None = unknown.

    Cached for _ENDPOINT_TTL; docker being unreachable is a normal None, not an error.
    """
    binding = _binding.get()
    if binding:
        return binding["endpoint"]
    global _endpoint_cache
    ts, cached = _endpoint_cache
    now = time.monotonic()
    if not refresh and cached is not None and now - ts < _ENDPOINT_TTL:
        return cached
    try:
        r = run(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}", name()],
                timeout=min(TIMEOUT, 8.0))
    except (OSError, subprocess.SubprocessError):
        _endpoint_cache = (now, None)
        return None
    value = r.stdout.strip() if r.returncode == 0 else ""
    _endpoint_cache = (now, value or None)
    return _endpoint_cache[1]


def is_local(endpoint_value: str | None = None) -> bool:
    """True iff the (given or resolved) endpoint is a local unix socket."""
    if endpoint_value is None:
        endpoint_value = endpoint()
    return bool(endpoint_value) and endpoint_value.startswith("unix://")


def compose_run(args: list[str], timeout: float = 120.0,
                check: bool = False) -> subprocess.CompletedProcess:
    """Run the standalone docker-compose binary against the pinned context.

    docker-compose has no --context flag: pin the engine by resolving the context
    endpoint and exporting DOCKER_HOST in the scrubbed child env. When the endpoint
    cannot be resolved, DOCKER_HOST is left unset (the call itself will fail loudly);
    callers gate actions on is_local() first.
    """
    ep = endpoint()
    if not ep or not is_local(ep):
        raise BindingError(f"Docker 连接 {name()} 不可用于本机 Compose 操作")
    env = scrub_env({"DOCKER_HOST": ep})
    try:
        proc = subprocess.run(["docker-compose", *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except FileNotFoundError:
        proc = subprocess.run(["docker", "--host", ep, "compose", *args], capture_output=True,
                              text=True, timeout=timeout, env=scrub_env())
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker-compose {' '.join(args)} 失败（exit {proc.returncode}）："
                           f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return proc
