"""lsctl: thin client of the daemon API (same status source as menubar and dashboard)."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths


def call(method: str, path: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict]:
    token = paths.token_file().read_text().strip()
    url = f"http://127.0.0.1:{paths.daemon_port()}{path}"
    data = json.dumps(body or {}).encode() if method in ("POST", "DELETE") else None
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


def _emit(res: dict, json_out: bool, human: str | None = None) -> int:
    if json_out:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    elif human is not None:
        print(human)
    else:
        print(res.get("message") or res.get("error") or res)
    return 0 if res.get("ok") else 1


def _read_payload(args) -> dict:
    if getattr(args, "stdin", False):
        return json.loads(sys.stdin.read())
    with open(args.file) as f:
        return json.load(f)


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

    # ---- workspace / registry (machine-readable; same backend as GUI and Skill) ----
    sp = sub.add_parser("register", help="register a service definition (JSON payload, CONTRACTS §2)")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--file")
    grp.add_argument("--stdin", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("projects", help="list workspace projects")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project-new")
    sp.add_argument("--name", required=True)
    sp.add_argument("--dir", action="append", dest="dirs", help="associated directory (repeatable)")
    sp.add_argument("--id")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project-rename")
    sp.add_argument("project")
    sp.add_argument("--name", required=True)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("dir-add")
    sp.add_argument("project")
    sp.add_argument("--path", required=True)
    sp.add_argument("--role", default="source")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("dir-remove")
    sp.add_argument("project")
    sp.add_argument("dir_id")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("agents", help="agent connections (codex / claude)")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("agent-scan")
    sp.add_argument("--path", action="append", dest="paths", help="extra lookup path (repeatable)")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("agent-verify")
    sp.add_argument("kind", choices=["codex", "claude"])
    sp.add_argument("--timeout", type=float, default=60)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("analyze", help="run agent recognition over a directory")
    sp.add_argument("directory")
    sp.add_argument("--agent", required=True, choices=["codex", "claude"])
    sp.add_argument("--focus", help="e.g. 只识别 Admin 管理端")
    sp.add_argument("--wait", type=float, default=0, help="seconds to wait for the job")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("jobs")
    sp.add_argument("job_id")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("service-remove")
    sp.add_argument("service")
    sp.add_argument("--stop", dest="disposition", action="store_const", const="stop")
    sp.add_argument("--keep-external", dest="disposition", action="store_const", const="keep-external",
                    help="deregister only; the running process becomes an external instance (default)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("resources", help="shared docker/compose resource inventory")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("export", help="export a shareable service definition (no credentials, no local state)")
    sp.add_argument("service")
    sp.add_argument("--file", help="write to file instead of stdout")
    sp.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            code, snap = call("POST", "/api/refresh")
            print(json.dumps(snap, ensure_ascii=False, indent=1) if a.json else fmt_status(snap))
            return 0
        if a.cmd == "register":
            try:
                payload = _read_payload(a)
            except (OSError, ValueError) as e:
                print(json.dumps({"ok": False, "error": f"payload 不可读：{e}"}, ensure_ascii=False)
                      if a.json else f"payload 不可读：{e}")
                return 4
            code, res = call("POST", "/api/register", payload, timeout=30)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            else:
                act = res.get("action")
                print(f"{res.get('project_id','?')}/{res.get('service_id','?')}：{act}"
                      + (f"（{res['validation']['warnings']}）" if res.get("validation", {}).get("warnings") else "")
                      if res.get("ok") else res.get("error", res))
            if res.get("ok"):
                return 0
            return 5 if code == 409 else (4 if code == 400 else 1)
        if a.cmd == "projects":
            code, res = call("GET", "/api/registry", timeout=15)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0 if res.get("ok") else 1
            for p in res.get("registry", {}).get("projects", []):
                dirs = ", ".join(d["path"] for d in p.get("directories", []))
                print(f"{p['id']:<20} {p.get('name',''):<24} services={','.join(p.get('services', []))}"
                      + (f"  dirs=[{dirs}]" if dirs else "") + ("  ★" if p.get("favorite") else ""))
            return 0
        if a.cmd == "project-new":
            body = {"name": a.name, "directories": [{"path": d} for d in (a.dirs or [])]}
            if a.id:
                body["id"] = a.id
            code, res = call("POST", "/api/projects", body)
            return _emit(res, a.json, f"Project {res.get('id', a.name)} 就绪")
        if a.cmd == "project-rename":
            code, res = call("POST", "/api/projects", {"id": a.project, "name": a.name})
            return _emit(res, a.json, f"已重命名为 {a.name}")
        if a.cmd == "dir-add":
            code, res = call("POST", f"/api/projects/{a.project}/directories", {"path": a.path, "role": a.role})
            return _emit(res, a.json, f"已关联 {a.path}")
        if a.cmd == "dir-remove":
            code, res = call("DELETE", f"/api/projects/{a.project}/directories/{a.dir_id}")
            return _emit(res, a.json, "已解除关联（文件未动）")
        if a.cmd == "agents":
            code, res = call("GET", "/api/agents", timeout=30)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0 if res.get("ok") else 1
            conns = res.get("connections") or {}
            conns = conns.values() if isinstance(conns, dict) else conns
            for c in conns:
                mark = "★默认" if c.get("is_default") else ""
                print(f"{c['kind']:<8} {c.get('status',''):<14} {c.get('version',''):<10} {c.get('path','')} {mark}"
                      + (f"  — {c.get('detail','')}" if c.get("detail") else ""))
            return 0
        if a.cmd == "agent-scan":
            body = {"paths": a.paths} if a.paths else {}
            code, res = call("POST", "/api/agents/scan", body, timeout=30)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0 if res.get("ok") else 1
            conns = res.get("connections") or {}
            conns = conns.values() if isinstance(conns, dict) else conns
            for c in conns:
                print(f"{c['kind']:<8} {c.get('status',''):<14} {c.get('path','')}")
            return 0
        if a.cmd == "agent-verify":
            code, res = call("POST", "/api/agents/verify",
                             {"kind": a.kind, "timeout": a.timeout}, timeout=a.timeout + 15)
            return _emit(res, a.json)
        if a.cmd == "analyze":
            body = {"directory": a.directory, "agent": a.agent}
            if a.focus:
                body["focus"] = a.focus
            code, res = call("POST", "/api/analyze", body, timeout=30)
            if not res.get("ok"):
                print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else res.get("error", res))
                return 1
            job_id = res["job_id"]
            if not a.wait and not a.json:
                print(job_id)
                return 0
            if a.json and not a.wait:
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0
            end = time.time() + a.wait
            while time.time() < end:
                time.sleep(min(3, max(0.5, a.wait / 10)))
                _, job = call("GET", f"/api/jobs/{job_id}", timeout=15)
                if job.get("state") in ("done", "failed"):
                    if a.json:
                        print(json.dumps(job, ensure_ascii=False, indent=1))
                    else:
                        r = job.get("result") or {}
                        for s in r.get("services", []):
                            print(f"候选 {s['id']}: {s['name']}  组件 {','.join(c['id'] for c in s.get('components', []))}")
                        for u in r.get("unresolved", []):
                            print(f"未解决: {u}")
                    return 0 if job.get("state") == "done" else 2
            print("等待超时（任务仍在跑）：" + job_id)
            return 2
        if a.cmd == "jobs":
            code, res = call("GET", f"/api/jobs/{a.job_id}", timeout=15)
            print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else res)
            return 0 if res.get("ok") and res.get("state") == "done" else 1
        if a.cmd == "service-remove":
            code, res = call("DELETE", f"/api/services/{a.service}?disposition={a.disposition or 'keep-external'}",
                             timeout=90)
            return _emit(res, a.json)
        if a.cmd == "resources":
            code, res = call("GET", "/api/resources", timeout=20)
            print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else
                  "\n".join(f"{r['id']:<40} {r.get('state',''):<10} {r.get('compose_project') or '-'}"
                            for r in res.get("resources", [])) or "（无）")
            return 0 if res.get("ok") else 1
        if a.cmd == "export":
            from . import manifest
            from .controller import redact, SECRET_RE
            projects = manifest.load_all()
            p = projects.get(a.service)
            if not p:
                print(f"未知服务 {a.service}", file=sys.stderr)
                return 1
            def clean_env(env: dict) -> dict:
                out = {}
                for k, v in env.items():
                    if SECRET_RE.search(f"{k}={v}"):
                        out[k] = "***REDACTED***"
                    else:
                        out[k] = v
                return out
            svc = {
                "schemaVersion": 1,
                "kind": "localservices-shareable-service",
                "service": {
                    "id": p.id, "name": p.name, "description": p.description,
                    "source": str(p.source), "open": p.open,
                    "components": [{
                        "id": c.id, "kind": c.kind, "name": c.name, "cwd": c.cwd,
                        "cmd": c.cmd, "env": clean_env(c.env), "env_files": c.env_files,
                        "env_files_note": "引用本机 .env 文件；凭据不入导出" if c.env_files else None,
                        "port": c.port, "ready": c.ready and {
                            "http": c.ready.http, "tcp": c.ready.tcp, "json_ok": c.ready.json_ok,
                            "timeout": c.ready.timeout, "interval": c.ready.interval},
                        "required": c.required, "start_timeout": c.start_timeout,
                        "after": c.after, "hot": c.hot, "watch": c.watch,
                        "restart_on": c.restart_on, "install": c.install,
                        "install_when": c.install_when, "install_marker": c.install_marker,
                        "auto_update": c.auto_update,
                        "external_cmd": c.external_cmd, "external_cwd": c.external_cwd,
                        "external_cwd_exact": c.external_cwd_exact,
                    } for c in p.components],
                    "dependencies": [{
                        "name": d.name, "probe": {"http": d.probe.http, "tcp": d.probe.tcp,
                                                  "json_ok": d.probe.json_ok, "timeout": d.probe.timeout},
                        "affects": d.affects, "required": d.required,
                        "container": d.container, "volume": d.volume, "note": d.note,
                    } for d in p.dependencies],
                },
                "note": "可分享定义：不含凭据（secret 类 env 值已脱敏、env 文件只留引用）。"
                        "导入 = 在目标机器把 source/cwd 改绑本机路径后执行 lsctl register --file；"
                        "register 会校验路径、命令与端口并显示修改结果，显式导入才授予命令执行能力。",
            }
            svc["service"]["components"] = [{k: v for k, v in c.items() if v is not None}
                                            for c in svc["service"]["components"]]
            text = json.dumps(svc, ensure_ascii=False, indent=1)
            # 双保险：即便结构外混入令牌样式字符串也再脱敏一遍
            text = redact(text)
            if a.file:
                Path(a.file).write_text(text, encoding="utf-8")
                print(f"已导出 {a.file}（凭据已脱敏）" if not a.json else
                      json.dumps({"ok": True, "file": a.file, "redacted": True}, ensure_ascii=False))
            else:
                print(text)
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
