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


def _exit_code(code: int, res: dict) -> int:
    """V2 §5：0 成功；1 拒绝；4 schema/请求错误；5 冲突(409)；3 连接失败在 main 捕获。"""
    if res.get("ok"):
        return 0
    return 5 if code == 409 else (4 if code == 400 else 1)


def _fmt_service_preview(p: dict) -> str:
    """V2.1 remove-preview 的人类可读摘要。"""
    lines = [f"移除预览：{p.get('name') or p.get('service_id')}（状态 {p.get('state', '—')}）"]
    for label, key in (("将停止的实例", "running_components"), ("外部实例", "external_instances")):
        vals = p.get(key) or []
        if vals:
            lines.append(f"  {label}：{'、'.join(str(v) for v in vals)}")
    keep = p.get("keep") or []
    lines.append(f"  保留内容：{'、'.join(str(k) for k in keep) if keep else '（无）'}")
    disp = (p.get("dispositions") or {}).get("keep-external") or {}
    if disp.get("enabled") is False:
        lines.append(f"  不可保留为外部实例：{disp.get('reason') or '仍由本工具 launchd 保活'}")
    lines.append(f"  恢复方式：{p.get('restore_hint') or 'lsctl restore <service-id>，或 GUI「已移除登记」视图'}")
    return "\n".join(lines)


def _fmt_project_preview(p: dict) -> str:
    """V2.2 project remove-preview / dry_run 的人类可读摘要。"""
    proj = p.get("project") or {}
    lines = [f"项目移除预览：{proj.get('name') or proj.get('id')}"]
    for s in p.get("services") or []:
        ext = s.get("external_instances") or []
        lines.append(f"  服务 {s.get('id')}（{s.get('name', '')}，状态 {s.get('state', '—')}"
                     + (f"，外部实例 {'、'.join(str(e) for e in ext)}" if ext else "") + "）")
    for r in p.get("resources_referenced") or []:
        shared = "共享" if r.get("shared") else "独占"
        consumers = ",".join(r.get("consumers") or []) or "—"
        lines.append(f"  引用资源 {r.get('name')}（{r.get('state', '—')}，{shared}，使用方 {consumers}）")
    kept = p.get("kept") or []
    lines.append(f"  保留内容：{'、'.join(str(k) for k in kept) if kept else '（目录/数据库/容器/卷一律不动）'}")
    for w in p.get("warnings") or []:
        lines.append(f"  ⚠︎ {w}")
    lines.append("  恢复方式：lsctl project-restore <pid>，或 GUI「已移除登记」视图")
    return "\n".join(lines)


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
    sp.add_argument("--project", dest="project", help="target project id (V2.10 context)")
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
    sp.add_argument("--preview", action="store_true",
                    help="show the removal preview (zero side effects) and exit")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project-remove", help="remove a project and its member registrations (V2.2)")
    sp.add_argument("project")
    sp.add_argument("--stop", dest="disposition", action="store_const", const="stop")
    sp.add_argument("--keep-external", dest="disposition", action="store_const", const="keep-external",
                    help="deregister members only; running processes stay as external instances (default)")
    sp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="preview only (same shape as remove-preview, zero side effects)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("removed", help="list removed (tombstoned) services/projects (V2.2)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("restore", help="restore a removed service registration (V2.2)")
    sp.add_argument("service")
    sp.add_argument("--project", dest="project", help="target project id (default: original)")
    sp.add_argument("--intent", choices=["stopped", "running"], help="restored intent (default: stopped)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project-restore", help="restore a removed project and its members (V2.2)")
    sp.add_argument("project")
    sp.add_argument("--intent", choices=["stopped", "running"], help="restored intent (default: stopped)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("project-autostart", help="toggle project autostart (V2.9)")
    sp.add_argument("project")
    mg = sp.add_mutually_exclusive_group(required=True)
    mg.add_argument("--on", dest="value", action="store_true")
    mg.add_argument("--off", dest="value", action="store_false")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("resources", help="shared docker/compose resource inventory")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("endpoints", help="Locove 公开入口与域名模块健康（V3/D3，域名带 :5670 端口）")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("locove-domains", help="域名跳转设置（V3.5：默认本地 IP 直连）")
    mg = sp.add_mutually_exclusive_group()
    mg.add_argument("--on", dest="use", action="store_true")
    mg.add_argument("--off", dest="use", action="store_false")
    sp.add_argument("--form", choices=["suffix", "custom"])
    sp.add_argument("--port", type=int, help="业务域名端口（缺省 5670；修改后需重新启用）")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("locove-enable", help="按需启用域名模块（D3：预检→用户件→必要时管理员授权装 resolver→验证）")
    sp.add_argument("--port", type=int)
    sp.add_argument("--wait", type=int, default=90, help="等待启用完成秒数（含管理员授权等待）")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("locove-disable", help="关闭域名（回直连；停止 DNS/代理，安装件保留）")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("locove-uninstall", help="卸载域名件（用户级必删；resolver 默认保留）")
    mg2 = sp.add_mutually_exclusive_group()
    mg2.add_argument("--remove-resolver", dest="keep_resolver", action="store_false",
                     help="连 .locove 解析配置一并移除（需管理员授权）")
    mg2.add_argument("--keep-resolver", dest="keep_resolver", action="store_true")
    sp.set_defaults(keep_resolver=True)
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
            if a.project:
                body["project_id"] = a.project
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
            if a.preview:  # V2.1 预览：零副作用，只读
                code, res = call("GET", f"/api/services/{a.service}/remove-preview", timeout=30)
                if a.json:
                    print(json.dumps(res, ensure_ascii=False, indent=1))
                elif res.get("ok"):
                    print(_fmt_service_preview(res))
                else:
                    print(res.get("error") or res)
                return 0 if res.get("ok") else _exit_code(code, res)
            code, res = call("DELETE", f"/api/services/{a.service}?disposition={a.disposition or 'keep-external'}",
                             timeout=90)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            elif res.get("ok"):
                rel = ",".join(res.get("released") or []) or "—"
                print(f"已移除登记：{res.get('service_id', a.service)}"
                      f"（处置 {res.get('disposition')}，已归档 {res.get('archive') or '—'}，"
                      f"释放组件 {rel}；文件/数据未动）")
            else:
                print(res.get("error") or res)
            return _exit_code(code, res)
        if a.cmd == "project-remove":
            disposition = a.disposition or "keep-external"
            q = f"?disposition={disposition}" + ("&dry_run=true" if a.dry_run else "")
            code, res = call("DELETE", f"/api/projects/{a.project}{q}", timeout=120)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            elif res.get("ok") and a.dry_run:
                print(_fmt_project_preview(res))
            elif res.get("ok"):
                print(f"已移除项目 {a.project}（处置 {disposition}，成员与目录仅解除登记；源码/数据/共享容器未动）")
            else:
                stopped = ",".join(res.get("stopped") or []) or "—"
                remaining = "；".join(f"{m.get('id')}：{m.get('reason')}" for m in res.get("remaining") or []) or "—"
                print(f"移除中止：已停止 {stopped}；未处理 {remaining}（登记保留，可重试）")
            return _exit_code(code, res)  # dry_run 成功同样 = 0
        if a.cmd == "removed":
            code, res = call("GET", "/api/removed", timeout=20)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0 if res.get("ok") else 1
            for s in res.get("services", []):
                print(f"服务 {s.get('sid')}（原项目 {s.get('project_id') or '—'}，"
                      f"{s.get('disposition')}，移除于 {s.get('removed_at')}）"
                      f"  恢复：lsctl restore {s.get('sid')}")
            for p in res.get("projects", []):
                print(f"项目 {p.get('pid')}（成员 {','.join(p.get('services') or []) or '—'}）"
                      f"  恢复：lsctl project-restore {p.get('pid')}")
            return 0 if res.get("ok") else 1
        if a.cmd == "restore":
            body = {}
            if a.project:
                body["project_id"] = a.project
            if a.intent:
                body["intent"] = a.intent
            code, res = call("POST", f"/api/removed/services/{a.service}/restore", body, timeout=60)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            elif res.get("ok"):
                print(f"已恢复 {res.get('service_id', a.service)} 到项目 {res.get('project_id', a.project or '原项目')}"
                      f"，意图 {res.get('intent', a.intent or 'stopped')}（未自动启动）")
            else:
                print(res.get("error") or res)
            return _exit_code(code, res)
        if a.cmd == "project-restore":
            body = {"intent": a.intent} if a.intent else {}
            code, res = call("POST", f"/api/removed/projects/{a.project}/restore", body, timeout=120)
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            elif res.get("ok"):
                members = ",".join(res.get("services") or []) or "—"
                print(f"已恢复项目 {a.project}（成员 {members}，意图 {a.intent or 'stopped'}，未自动启动）")
            else:
                print(res.get("error") or res)
            return _exit_code(code, res)
        if a.cmd == "project-autostart":
            code, res = call("POST", f"/api/projects/{a.project}/autostart", {"value": bool(a.value)})
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            elif res.get("ok"):
                print(f"项目 {a.project} 自动启动已{'开启' if a.value else '关闭'}（只影响登录/重启后的意图恢复）")
            else:
                print(res.get("error") or res)
            return _exit_code(code, res)
        if a.cmd == "locove-domains":
            body = {}
            if a.use is not None:
                body["use_domains"] = a.use
            if a.form:
                body["domain_form"] = a.form
            if a.port:
                body["domain_port"] = a.port
            code, res = call("POST", "/api/locove/settings", body) if body else call("GET", "/api/locove/settings")
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
                return 0 if res.get("ok") else 1
            st = res.get("settings") or {}
            form_label = "后缀式 <id>.locove" if st.get("domain_form") == "suffix" else "自定义（仅显式域名）"
            print(f"域名跳转：{'开' if st.get('use_domains') else '关（默认，本地 IP 直连）'} · 形式：{form_label}"
                  + f" · 端口：{st.get('domain_port') or 5670}")
            return 0 if res.get("ok") else 1
        if a.cmd in ("locove-enable", "locove-disable", "locove-uninstall"):
            op = a.cmd.removeprefix("locove-")
            body = {}
            if op == "enable" and a.port:
                body["port"] = a.port
            if op == "uninstall":
                body["keep_resolver"] = bool(a.keep_resolver)
            code, res = call("POST", f"/api/locove/{op}", body, timeout=15)
            if code != 200 or not res.get("ok"):
                print(json.dumps(res, ensure_ascii=False, indent=1) if a.json
                      else f"操作未受理：{res.get('error') or res}")
                return 1
            if op == "enable" and not a.json:
                print("域名模块启动中（含管理员授权与验证）。进度：")
            deadline = time.time() + (a.wait if hasattr(a, "wait") and a.wait else 60)
            last = None
            while time.time() < deadline:
                _, st = call("GET", "/api/locove", timeout=10)
                p = st.get("optin") or {}
                if not p.get("active"):
                    err = p.get("error")
                    failed = p.get("ok") is False or bool(err)
                    if a.json:
                        print(json.dumps({"ok": not failed, "state": st.get("state"),
                                          "optin": p}, ensure_ascii=False, indent=1))
                        return 0 if not failed else 1
                    if failed:
                        print(f"未完成：{err or '（未知错误，见设置页域名诊断）'}")
                        return 1
                    print(f"完成：模块状态 {st.get('state')}（端口 {st.get('domain_port')}）"
                          + ("；旧 80 入口仍在，退役：sudo scripts/retire-locove-80.sh --apply"
                             if st.get("legacy_80") else ""))
                    return 0
                line = f"  {p.get('step') or ''}"
                if line != last:
                    print(line)
                    last = line
                time.sleep(1)
            if a.json:
                print(json.dumps({"ok": False, "error": "timeout", "state": None},
                                 ensure_ascii=False))
            else:
                print("仍在进行（如等待管理员授权）；稍后用 lsctl endpoints 查看状态")
            return 0 if a.json else 0
        if a.cmd == "endpoints":
            _, snap = call("GET", "/api/status", timeout=15)
            _, st = call("GET", "/api/locove", timeout=10)
            rows = [{"host": e.get("host"), "public_url": e.get("public_url"),
                     "service": p.get("id"), "state": p.get("state"),
                     "component": e.get("component")}
                    for p in snap.get("projects", [])
                    for e in (p.get("endpoints") or [])]
            if a.json:
                print(json.dumps({"ok": True, "locove": st, "endpoints": rows},
                                 ensure_ascii=False, indent=1))
                return 0
            for r in rows:
                print(f"{r['host']:<32} {str(r['service']):<20} {str(r['state']):<10} {r['public_url']}")
            if not rows:
                print("（尚无登记了公开入口的服务）")
            d, rv, px = st.get("dns", {}), st.get("resolver", {}), st.get("proxy", {})
            pb = st.get("proxy_bypass", {})
            state_labels = {"not_installed": "未安装（可选能力，默认直连）", "installed_off": "已安装 · 未开启",
                            "starting": "启动中", "available": "可用", "failed": "失败"}
            mark = lambda ok: "✅" if ok else "❌"  # noqa: E731
            settings = st.get("settings") or {}
            print(f"\n域名模块：{state_labels.get(st.get('state'), st.get('state'))}"
                  f"（端口 {st.get('domain_port')}）")
            if st.get("state") != "not_installed":
                print(f"分层：dns {mark(d.get('ok'))}（{d.get('detail', '')}）  "
                      f"代理 {mark(px.get('ok'))}（{px.get('detail', '')}）  "
                      f"解析配置 {mark(rv.get('ok'))}（{rv.get('detail', '')}）  "
                      f"代理绕行 {mark(pb.get('ok', True))}（{pb.get('detail', '')}）")
            if st.get("legacy_80"):
                print("旧 80 入口仍登记（V3.4 转发器）；退役：sudo scripts/retire-locove-80.sh --apply")
            print(f"域名跳转：{'开' if settings.get('use_domains') else '关（默认，本地 IP 直连）'} · "
                  f"形式：{'后缀式 <id>.locove' if settings.get('domain_form') == 'suffix' else '自定义（仅显式域名）'}"
                  + ("" if settings.get("use_domains") else "（启用：lsctl locove-enable）"))
            return 0
        if a.cmd == "resources":
            code, res = call("GET", "/api/resources", timeout=20)
            print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else
                  "\n".join(f"{r['id']:<40} {r.get('state',''):<10} {r.get('compose_project') or '-'}"
                            for r in res.get("resources", [])) or "（无）")
            return 0 if res.get("ok") else 1
        if a.cmd == "export":
            # 产出严格 register payload（schemas/register.schema.json）：目标机改绑 source/cwd
            # 后 `lsctl register --file` 即可导入。凭据：secret 类 env 值脱敏；env 文件只留引用。
            from . import manifest
            from .controller import SECRET_RE, redact
            projects = manifest.load_all()
            p = projects.get(a.service)
            if not p:
                print(f"未知服务 {a.service}", file=sys.stderr)
                return 1
            def clean_env(env: dict) -> dict:
                return {k: ("***REDACTED***" if SECRET_RE.search(f"{k}={v}") else v)
                        for k, v in env.items()}
            def clean_probe(pr) -> dict | None:
                if pr is None:
                    return None
                out = {}
                for k in ("http", "tcp", "json_ok", "timeout", "interval"):
                    v = getattr(pr, k, None)
                    if v is not None and v != "":
                        out[k] = v
                return out or None
            def clean_comp(c) -> dict:
                out = {"id": c.id, "kind": c.kind, "name": c.name or c.id, "cwd": c.cwd,
                       "cmd": c.cmd, "env": clean_env(c.env), "env_files": c.env_files}
                for k in ("port", "required", "start_timeout", "after", "hot", "watch",
                          "restart_on", "ignore", "build", "install", "install_when",
                          "install_marker", "auto_update", "external_cmd", "external_cwd",
                          "external_cwd_exact", "debounce", "shell_script"):
                    v = getattr(c, k, None)
                    if v not in (None, [], ""):
                        out[k] = v
                pr = clean_probe(c.ready)
                if pr:
                    out["ready"] = pr
                return {k: v for k, v in out.items() if v not in (None, [], "")}
            def clean_dep(d) -> dict:
                out = {"name": d.name}
                pr = clean_probe(d.probe)
                if pr:
                    out["probe"] = pr
                for k in ("affects", "required", "container", "volume", "note"):
                    v = getattr(d, k, None)
                    if v not in (None, [], ""):
                        out[k] = v
                return out
            payload = {
                "schemaVersion": 1,
                "project": {"id": p.project or p.id, "name": p.name},
                "service": {
                    "id": p.id, "name": p.name, "description": p.description,
                    "source": str(p.source), "open": p.open,
                    "autostart": bool(p.autostart), "adapter": p.adapter or None,
                    "observe_only": bool(p.observe_only),
                    "options": dict(p.options) if p.options else None,
                    "components": [clean_comp(c) for c in p.components],
                    "dependencies": [clean_dep(d) for d in p.dependencies],
                    "evidence": {"agent": None,
                                 "summary": "lsctl export 导出的可分享定义（secret 已脱敏）",
                                 "unresolved": []},
                },
            }
            text = redact(json.dumps(payload, ensure_ascii=False, indent=1))
            if a.file:
                Path(a.file).write_text(text, encoding="utf-8")
                print(f"已导出 {a.file}（凭据已脱敏；导入：改绑 source/cwd 后 lsctl register --file {a.file}）")
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
