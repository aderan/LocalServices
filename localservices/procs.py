"""Process / port inspection used for readiness and ownership checks."""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

# Docker 宿主侧端口转发进程（V2.8）：它们持有端口是替容器发布，不是普通进程冲突。
DOCKER_PROXY_RE = re.compile(r"gvproxy|com\.docker\.backend|docker-proxy|vpnkit|^ssh: .*/_lima/[^/]+/ssh\.sock \[mux\]")


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
    r = subprocess.run([shutil.which("lsof") or "/usr/sbin/lsof", "-nP", "-w", "-d", "cwd", "-Fpn"], capture_output=True, text=True, timeout=20)
    out: dict[int, str] = {}
    pid = None
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


@dataclass(frozen=True)
class TCPListener:
    pid: int
    address: str
    family: str = "IPv4"


def tcp_listeners() -> dict[int, list[TCPListener]]:
    """Snapshot TCP listeners by port, retaining every address, family and owner."""
    result = subprocess.run([shutil.which("lsof") or "/usr/sbin/lsof", "-nP", "-w", "-iTCP", "-sTCP:LISTEN", "-Fpftn"],
                            capture_output=True, text=True, timeout=3)
    if result.returncode > 1 or (result.returncode == 1 and result.stderr):
        raise OSError("无法读取本机 TCP 监听")
    out: dict[int, list[TCPListener]] = {}
    pid, family = None, ""
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("f"):
            family = ""
        elif line.startswith("t"):
            family = line[1:]
        elif line.startswith("n") and pid is not None:
            address, _, port = line[1:].rpartition(":")
            if not port.isdigit():
                continue
            listener = TCPListener(pid, address.strip("[]"), family)
            rows = out.setdefault(int(port), [])
            if listener not in rows:
                rows.append(listener)
    return out


def listeners() -> dict[int, int]:
    """Compatibility view for callers that only need one listener per port."""
    return {port: rows[0].pid for port, rows in tcp_listeners().items()}


def matching_listeners(listen: dict[int, list[TCPListener]], host: str, port: int) -> list[TCPListener]:
    """Find listeners that can serve the target address, including dual-stack wildcards."""
    rows = listen.get(port, [])
    if host == "localhost":
        return list(dict.fromkeys(matching_listeners(listen, "127.0.0.1", port)
                                  + matching_listeners(listen, "::1", port)))
    try:
        target = ipaddress.ip_address(host)
    except ValueError:
        return list(rows)  # A hostname outside localhost gives no reliable bind address.
    matches, ipv6_wildcards = [], []
    for row in rows:
        if row.address == "*":
            if target.version == 4 and row.family == "IPv6":
                ipv6_wildcards.append(row)
            elif row.family == f"IPv{target.version}":
                matches.append(row)
            continue
        try:
            address = ipaddress.ip_address(row.address)
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address == target or (address.is_unspecified and address.version == target.version):
            matches.append(row)
        elif isinstance(address, ipaddress.IPv6Address) and address.is_unspecified and target.version == 4:
            ipv6_wildcards.append(row)
    if not matches and ipv6_wildcards and target.is_loopback and tcp_ok(f"{target}:{port}", 0.1):
        return ipv6_wildcards
    return matches


def tcp_ok(hostport: str, timeout: float = 1.0) -> bool:
    host, port = hostport.rsplit(":", 1)
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=timeout):
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
    deadline = time.monotonic() + (timeout if timeout is not None else p.timeout)
    if p.tcp and not tcp_ok(p.tcp, max(0, deadline - time.monotonic())):
        return False, f"无法连接 {p.tcp}"
    if p.http:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, "HTTP 探针超时"
        code, body, err = http_get(p.http, remaining)
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
