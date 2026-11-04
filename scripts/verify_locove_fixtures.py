#!/usr/bin/env python3
"""Locove M3 真实夹具验收：Next + Vite 两种样例走完
登记 → 启动就绪 → 无端口域名代理访问（页面/资源/HMR WebSocket/流式响应）→
停止=明确不可用 → 移除=路由撤销 → DNS 事实同步。

真实组件：隔离 daemon（launchd 真启动 npm dev server）+ 真实 Caddy 进程 + 本地 DNS 模块。
不动生产 home/端口（全部临时目录与 478xx 隔离端口；标签前缀独立，结束卸载）。
输出：stdout 摘要 + artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/fixtures-evidence.txt
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DAEMON_PORT = 47812
PROXY_PORT = 47897
ADMIN_PORT = 47898
LABEL_PREFIX = "com.mybrain.ls-locove-fix"
VITE_HOST = "vite-demo.locove"
NEXT_HOST = "next-demo.locove"

CADDY = os.environ.get("LOCOVE_CADDY_BIN", "/opt/homebrew/bin/caddy")


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw)


# 本机常驻网关代理会拒绝回环目标（urllib 默认读环境变量代理）——全部请求显式直连
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, url: str, token: str | None = None, body: dict | None = None,
         timeout: float = 8.0, host_header: str | None = None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("X-LocalServices-Token", token)
    if host_header:
        req.add_header("Host", host_header)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        return 0, str(e).encode()


def wait_port(port: float, deadline: float = 20.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def wait_until(cond, deadline: float, interval: float = 0.3, what=""):
    end = time.time() + deadline
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError(f"timeout waiting: {what}")


def ws_upgrade(host_header: str, path: str, subproto: str | None = None) -> int:
    """curl 发 WebSocket 升级请求，返回 HTTP 状态码（101 = 升级成功）。"""
    cmd = ["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}",
           "-m", "4", "-i", "-N",
           "-H", f"Host: {host_header}", "-H", "Connection: Upgrade",
           "-H", "Upgrade: websocket", "-H", "Sec-WebSocket-Version: 13",
           "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="]
    if subproto:
        cmd += ["-H", f"Sec-WebSocket-Protocol: {subproto}"]
    cmd.append(f"http://127.0.0.1:{PROXY_PORT}{path}")
    r = sh(cmd, timeout=8)
    return int(r.stdout.strip() or 0)


class Report:
    def __init__(self, path: Path | None):
        self.path = path
        self.lines: list[str] = []
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else "")
        print(line, flush=True)
        self.lines.append(line)
        if not ok:
            self.failures += 1
        return ok

    def info(self, msg: str):
        print(f"      {msg}", flush=True)
        self.lines.append(f"      {msg}")

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def cleanup_labels(rep: Report):
    out = sh(["launchctl", "list"]).stdout or ""
    for label in [l.split()[-1] for l in out.splitlines() if LABEL_PREFIX in l]:
        sh(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
        rep.info(f"bootout {label}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default=str(
        REPO / "artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/fixtures-evidence.txt"))
    args = ap.parse_args()
    rep = Report(Path(args.artifact))
    tmp = Path(tempfile.mkdtemp(prefix="ls-locove-fixtures-"))
    env = {
        **os.environ,
        "LOCALSERVICES_HOME": str(tmp / "home"),
        "LOCALSERVICES_SERVICES": str(tmp / "services"),
        "LOCALSERVICES_USER_SERVICES": str(tmp / "user-services"),
        "LOCALSERVICES_PORT": str(DAEMON_PORT),
        "LOCALSERVICES_LABEL_PREFIX": LABEL_PREFIX,
        "LOCALSERVICES_RECONCILE": "3",
        "LOCALSERVICES_STATUS": "3",
        "LOCOVE_PROXY_PORT": str(PROXY_PORT),
        "LOCOVE_ADMIN_PORT": str(ADMIN_PORT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for d in ("home", "services", "user-services"):
        (tmp / d).mkdir(parents=True, exist_ok=True)

    daemon = caddy = None
    try:
        # ---- 1. 隔离 daemon ----
        daemon = subprocess.Popen(
            [sys.executable, "-m", "localservices.daemon"], cwd=str(REPO), env=env,
            stdout=open(tmp / "daemon.log", "w"), stderr=subprocess.STDOUT)
        wait_until(lambda: http("GET", f"http://127.0.0.1:{DAEMON_PORT}/api/ping")[0] == 200,
                   30, 0.4, "daemon ping")
        token = (tmp / "home/token").read_text().strip()
        base = f"http://127.0.0.1:{DAEMON_PORT}"
        rep.check("隔离 daemon 启动", True)

        def register(sid: str, source: Path, port: int, host: str, ready: str):
            payload = {
                "schemaVersion": 1,
                "project": {"id": sid, "name": sid},
                "service": {
                    "id": sid, "name": sid, "source": str(source),
                    "endpoints": [{"host": host, "component": "web", "open_path": "/"}],
                    "components": [{
                        "id": "web", "kind": "server", "cmd": ["npm", "run", "dev"],
                        "port": port, "start_timeout": 90,
                        "ready": {"http": ready, "timeout": 3, "interval": 10},
                        "hot": True, "watch": ["src"],
                        "install": ["npm", "install"], "install_when": ["package-lock.json"],
                        "install_marker": "node_modules",
                    }],
                },
            }
            code, body = http("POST", f"{base}/api/register", token, payload, timeout=60)
            return code, json.loads(body or b"{}")

        code, res = register("vite-demo", REPO / "tests/fixtures/vite-demo", 18210,
                             VITE_HOST, "http://127.0.0.1:18210/")
        rep.check("注册 vite-demo（含 endpoints）", res.get("ok") is True, json.dumps(res, ensure_ascii=False)[:200])
        code, res = register("next-demo", REPO / "tests/fixtures/next-demo", 18218,
                             NEXT_HOST, "http://127.0.0.1:18218/api/health")
        rep.check("注册 next-demo（含 endpoints）", res.get("ok") is True, json.dumps(res, ensure_ascii=False)[:200])

        routes_file = tmp / "home/locove/routes.json"
        wait_until(lambda: routes_file.exists() and
                   VITE_HOST in json.loads(routes_file.read_text()).get("routes", {}),
                   20, 0.5, "routes.json contains vite host")
        routes = json.loads(routes_file.read_text())["routes"]
        rep.check("daemon 收敛 routes.json（2 域名，均 unavailable）",
                  set(routes) == {VITE_HOST, NEXT_HOST}
                  and all(r["state"] == "unavailable" for r in routes.values()),
                  json.dumps({k: v["state"] for k, v in routes.items()}))

        # ---- 2. 真实 Caddy 进程（读到 pending 落盘的配置）----
        caddy = subprocess.Popen(
            [CADDY, "run", "--config", str(tmp / "home/locove/Caddyfile")],
            stdout=open(tmp / "caddy.log", "w"), stderr=subprocess.STDOUT, env=env)
        rep.check("Caddy 运行（HTTP 入口 :%d）" % PROXY_PORT, wait_port(PROXY_PORT, 10))
        code, _ = http("GET", f"http://127.0.0.1:{PROXY_PORT}/", host_header=NEXT_HOST)
        rep.check("启动前：域名入口明确不可用（503）", code == 503, f"got {code}")

        # ---- 3. 启动两个样例并等就绪 ----
        for sid in ("vite-demo", "next-demo"):
            code, body = http("POST", f"{base}/api/projects/{sid}/start", token, {}, timeout=15)
            res = json.loads(body or b"{}")
            rep.check(f"启动 {sid} accepted", res.get("ok") is True, str(res)[:120])
        for sid in ("vite-demo", "next-demo"):
            wait_until(
                lambda s=sid: any(p.get("id") == s and p.get("state") in ("available", "partial")
                                  for p in json.loads(http("GET", f"{base}/api/status", token)[1]
                                                      or b'{"projects":[]}').get("projects", [])),
                120, 1.0, f"{sid} available")
            rep.check(f"{sid} 就绪", True)
        # daemon sync（3s 间隔）把路由切到 proxy 后再生效（走 caddy reload）
        wait_until(lambda: json.loads(routes_file.read_text())["routes"][NEXT_HOST]["state"] == "proxy",
                   15, 0.5, "route → proxy")

        # ---- 4. 经域名的真实访问 ----
        code, body = http("GET", f"http://127.0.0.1:{PROXY_PORT}/", host_header=VITE_HOST)
        rep.check("Vite 页面经域名 200", code == 200 and b"<title>" in body, f"{code} {len(body)}B")
        code, body = http("GET", f"http://127.0.0.1:{PROXY_PORT}/@vite/client", host_header=VITE_HOST)
        rep.check("Vite 资源（/@vite/client）200", code == 200 and b"createHotContext" in body, f"{code}")
        rep.check("Vite HMR WebSocket 升级 101", ws_upgrade(VITE_HOST, "/", subproto="vite-hmr") == 101)

        code, body = http("GET", f"http://127.0.0.1:{PROXY_PORT}/api/health", host_header=NEXT_HOST)
        rep.check("Next /api/health 经域名 ok", code == 200 and b'"ok"' in body, f"{code} {body[:60]}")
        code, body = http("GET", f"http://127.0.0.1:{PROXY_PORT}/", host_header=NEXT_HOST)
        rep.check("Next 页面经域名 200", code == 200 and b"<html" in body.lower(), f"{code} {len(body)}B")
        rep.check("Next HMR WebSocket 升级 101", ws_upgrade(NEXT_HOST, "/_next/webpack-hmr") == 101)
        t0 = time.time()
        code, body = http("GET", f"http://127.0.0.1:{PROXY_PORT}/api/stream", host_header=NEXT_HOST,
                          timeout=15)
        dt = time.time() - t0
        rep.check("Next 流式响应不被代理缓冲（3 段、≥0.8s）",
                  code == 200 and body == b"chunk-1\nchunk-2\nchunk-3\n" and dt >= 0.8,
                  f"{code} {dt:.2f}s {body[:40]!r}")

        # ---- 5. 停止 → reload → 503；移除 → 路由/DNS 事实撤销 ----
        http("POST", f"{base}/api/projects/next-demo/stop", token, {}, timeout=15)
        wait_until(lambda: json.loads(routes_file.read_text())["routes"][NEXT_HOST]["state"] == "unavailable",
                   15, 0.5, "route → unavailable")
        code, _ = http("GET", f"http://127.0.0.1:{PROXY_PORT}/", host_header=NEXT_HOST)
        rep.check("停止后：明确不可用页（503）", code == 503, f"got {code}")

        code, body = http("DELETE", f"{base}/api/services/vite-demo?disposition=stop", token, timeout=30)
        res = json.loads(body or b"{}")
        rep.check("移除 vite-demo（stop）", res.get("ok") is True, str(res)[:160])
        wait_until(lambda: VITE_HOST not in json.loads(routes_file.read_text()).get("routes", {}),
                   15, 0.5, "route revoked")
        code, _ = http("GET", f"http://127.0.0.1:{PROXY_PORT}/", host_header=VITE_HOST)
        rep.check("移除后：未知 Host 404（不再转发）", code == 404, f"got {code}")

        from localservices import locove_dns
        zr = tmp / "dns-probe-routes.json"
        zr.write_text(routes_file.read_text())
        zone = locove_dns.Zone(zr)
        zone.refresh()
        rep.check("DNS 事实：移除的域名不再回答回环", not zone.contains(VITE_HOST))

    except Exception as e:  # noqa: BLE001
        import traceback
        rep.check("脚本执行完整", False, f"{e}\n{traceback.format_exc(limit=4)}")
    finally:
        for p in (caddy, daemon):
            if p and p.poll() is None:
                p.send_signal(signal.SIGTERM)
        cleanup_labels(rep)
        # 隔离 launchd 组件已 bootout；临时目录留证据日志，由 --keep 控制
        rep.info(f"workspace: {tmp} (daemon.log / caddy.log)")
        rep.save()
    print(f"\n{'ALL PASS' if rep.failures == 0 else f'{rep.failures} FAILURES'}")
    return 0 if rep.failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
