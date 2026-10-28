"""lsctl: thin client of the daemon API (same status source as menubar and dashboard)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from . import paths


def call(method: str, path: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict]:
    token = paths.token_file().read_text().strip()
    url = f"http://127.0.0.1:{paths.daemon_port()}{path}"
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-LocalServices-Token": token, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def fmt_status(snap: dict) -> str:
    lines = []
    for p in snap["projects"]:
        code = p.get("code", {}).get("label", "")
        lines.append(f"{p['id']:<16} {p.get('label',''):<8} {code:<14} {p.get('reason','')[:140]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lsctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    for name in ("start", "stop", "restart", "open", "logs", "takeover"):
        sp = sub.add_parser(name)
        sp.add_argument("project")
        if name in ("start", "takeover"):
            sp.add_argument("--open", action="store_true")
            sp.add_argument("--wait", type=float, default=0, help="seconds to wait for 可用")
        if name == "logs":
            sp.add_argument("--component")
            sp.add_argument("-n", type=int, default=60)
        if name == "restart":
            sp.add_argument("--component", action="append", help="restart only this registered component")
    for name in ("start-group", "stop-group", "restart-group"):
        sub.add_parser(name).add_argument("group")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            code, snap = call("POST", "/api/refresh")
            print(json.dumps(snap, ensure_ascii=False, indent=1) if a.json else fmt_status(snap))
            return 0
        if a.cmd == "logs":
            q = f"?lines={a.n}" + (f"&component={a.component}" if a.component else "")
            code, res = call("GET", f"/api/projects/{a.project}/logs{q}")
            for comp, text in res.get("logs", {}).items():
                print(f"===== {comp}\n{text}")
            return 0 if code == 200 else 1
        if a.cmd.endswith("-group"):
            action = a.cmd.removesuffix("-group")
            code, res = call("POST", f"/api/groups/{a.group}/{action}")
            print(res.get("message") or res.get("error") or res)
            return 0 if res.get("ok") else 1
        body = {"open": True} if getattr(a, "open", False) else {}
        if a.cmd == "restart" and a.component:
            body["components"] = a.component
        code, res = call("POST", f"/api/projects/{a.project}/{a.cmd}", body)
        print(res.get("message") or res.get("error") or res)
        if a.cmd in ("start", "takeover") and a.wait and res.get("ok"):
            end = time.time() + a.wait
            while time.time() < end:
                time.sleep(2)
                _, snap = call("POST", "/api/refresh")
                st = next(p for p in snap["projects"] if p["id"] == a.project)
                if st["state"] in ("available", "partial", "failed", "conflict", "external") and not st.get("op"):
                    print(f"{st['label']} {st.get('reason','')}")
                    return 0 if st["state"] in ("available", "partial") else 2
            print("等待超时")
            return 2
        return 0 if res.get("ok") else 1
    except (OSError, urllib.error.URLError) as e:
        print(f"无法连接控制服务：{e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
