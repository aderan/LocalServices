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

import os
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


def name() -> str:
    """The pinned context name (env LOCALSERVICES_DOCKER_CONTEXT, default colima)."""
    return os.environ.get(ENV_VAR) or DEFAULT_CONTEXT


def scrub_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """os.environ minus docker redirect keys; `extra` wins (e.g. PATH, DOCKER_HOST pinning)."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    if extra:
        env.update(extra)
    return env


def argv(args: list[str]) -> list[str]:
    """Full docker command line for args, with the explicit --context flag."""
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
    env = scrub_env({"DOCKER_HOST": ep} if ep else None)
    proc = subprocess.run(["docker-compose", *args], capture_output=True, text=True,
                          timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker-compose {' '.join(args)} 失败（exit {proc.returncode}）："
                           f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return proc
