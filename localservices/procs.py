"""Process / port inspection used for readiness and ownership checks."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

# Docker 宿主侧端口转发进程（V2.8）：它们持有端口是替容器发布，不是普通进程冲突。
DOCKER_PROXY_RE = re.compile(r"gvproxy|com\.docker\.backend|docker-proxy|vpnkit")


@dataclass
class Proc:
    pid: int
    ppid: int
    start: float
    cmd: str


def snapshot() -> dict[int, Proc]:
    r = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,command="], capture_output=True, text=True, timeout=10)
    out: dict[int, Proc] = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            start = datetime.strptime(" ".join(parts[2:7]), "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            continue
        out[pid] = Proc(pid, ppid, start, parts[7])
    return out


def descendants(root: int, procs: dict[int, Proc]) -> set[int]:
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    seen, stack = {root}, [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def looks_like_docker_proxy(cmd: str) -> bool:
    """True when the command line is a docker host-side port forwarder
    (gvproxy / com.docker.backend / docker-proxy / vpnkit): the port it holds is
    published on behalf of a container, so it must be attributed to that container,
    not treated as a process conflict."""
    return bool(cmd) and bool(DOCKER_PROXY_RE.search(cmd))


def all_cwds() -> dict[int, str]:
    r = subprocess.run(["lsof", "-nP", "-w", "-d", "cwd", "-Fpn"], capture_output=True, text=True, timeout=20)
    out: dict[int, str] = {}
    pid = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def listeners() -> dict[int, int]:
    """port -> pid for TCP listeners."""
    r = subprocess.run(["lsof", "-nP", "-w", "-iTCP", "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True, timeout=20)
    out: dict[int, int] = {}
    pid = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            try:
                port = int(line.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
            out.setdefault(port, pid)
    return out


def tcp_ok(hostport: str, timeout: float = 1.0) -> bool:
    host, port = hostport.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def http_get(url: str, timeout: float = 2.0, headers: dict | None = None,
             *, max_bytes: int = 256 * 1024) -> tuple[int, bytes, str]:
    """Returns (status, body, error). status 0 = unreachable. Never follows redirects."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            body = r.read(max_bytes + 1)
            if len(body) > max_bytes:
                return r.status, b"", f"HTTP response exceeds {max_bytes} bytes"
            return r.status, body, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(64 * 1024)
        except Exception:
            body = b""
        return e.code, body, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return 0, b"", str(getattr(e, "reason", e))


def probe(p, timeout: float | None = None) -> tuple[bool, str]:
    """Run a manifest Probe. Returns (ok, detail)."""
    t = timeout or p.timeout
    if p.tcp:
        ok = tcp_ok(p.tcp, t)
        return ok, "" if ok else f"无法连接 {p.tcp}"
    if p.http:
        code, body, err = http_get(p.http, t)
        if code == 0:
            return False, f"无法连接：{err}"
        if code >= 400:
            return False, f"HTTP {code}"
        if p.json_ok:
            try:
                val = json.loads(body)
                for k in p.json_ok.split("."):
                    val = val[k]
            except Exception:  # noqa: BLE001
                return False, f"响应缺少 {p.json_ok}"
            if not val:
                return False, f"{p.json_ok} = {val!r}"
        return True, ""
    return True, ""


def wait_until(fn, timeout: float, interval: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return fn()
