"""Control daemon (a launchd agent): HTTP API + dashboard on 127.0.0.1, reconcile loops.

Security: loopback bind only; Host header must be a loopback name (DNS-rebinding guard);
every /api call except /api/ping needs the token from the 0600 token file; mutating calls
additionally reject foreign Origins. Only fixed, registered operations exist.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, paths
from . import agent_api, register_api, resources
from .controller import Controller

WEB = Path(__file__).resolve().parent / "web"
STARTED = time.time()


def load_token() -> str:
    p = paths.token_file()
    try:
        t = p.read_text().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalServices/" + __version__
    ctl: Controller
    token: str
    port: int

    def log_message(self, fmt, *args):  # quiet; errors still go to stderr via _fail
        pass

    def _allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser client (menubar / CLI) with token
        return origin in {f"http://{h}" for h in self._allowed_hosts()}

    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, need_token: bool = True, mutating: bool = False) -> bool:
        if self.headers.get("Host") not in self._allowed_hosts():
            self._send(403, {"ok": False, "error": "host not allowed"})
            return False
        if mutating and not self._origin_ok():
            self._send(403, {"ok": False, "error": "origin not allowed"})
            return False
        if need_token:
            got = self.headers.get("X-LocalServices-Token", "")
            if not hmac.compare_digest(got, self.token):
                self._send(401, {"ok": False, "error": "unauthorized"})
                return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/ping":
                if self._guard(need_token=False):
                    self._send(200, {"ok": True, "version": __version__, "pid": os.getpid(), "started": STARTED})
                return
            if u.path in ("/", "/index.html"):
                if not self._guard(need_token=False):
                    return
                if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                    self._send(403, {"ok": False, "error": "cross-site navigation refused"})
                    return
                html = (WEB / "dashboard.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
                self._send(200, html.encode(), "text/html; charset=utf-8")
                return
            if not self._guard():
                return
            if u.path == "/api/status":
                self._send(200, self.ctl.snapshot())
                return
            if u.path == "/api/registry":
                self._send(*register_api.handle_registry_get(self.ctl))
                return
            if u.path == "/api/agents":
                self._send(*agent_api.handle_agents_get())
                return
            if u.path == "/api/agents/candidates":
                self._send(*agent_api.handle_agents_candidates())
                return
            if u.path == "/api/resources":
                self._send(200, resources.inventory())
                return
            if u.path == "/api/drafts":
                self._send(*register_api.handle_drafts())
                return
            m = re.fullmatch(r"/api/removed", u.path)
            if m:
                self._send(*register_api.handle_removed_list())
                return
            m = re.fullmatch(r"/api/services/([\w-]+)/remove-preview", u.path)
            if m:
                self._send(*register_api.handle_service_remove_preview(m.group(1), self.ctl))
                return
            m = re.fullmatch(r"/api/services/([\w-]+)/overrides", u.path)
            if m:
                self._send(*register_api.handle_overrides_get(m.group(1)))
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/remove-preview", u.path)
            if m:
                self._send(*register_api.handle_project_remove_preview(m.group(1), self.ctl))
                return
            m = re.fullmatch(r"/api/jobs/([\w-]+)", u.path)
            if m:
                self._send(*agent_api.handle_job_get(m.group(1)))
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/logs", u.path)
            if m:
                q = parse_qs(u.query)
                comp = q.get("component", [None])[0]
                lines = min(int(q.get("lines", ["200"])[0]), 2000)
                self._send(200, self.ctl.logs(m.group(1), comp, lines))
                return
            self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})

    def _registry_changed(self, res: dict) -> None:
        """A register/remove landed: make the new manifests visible immediately."""
        if res.get("ok") and res.get("action") != "draft":
            self.ctl.reload()
            threading.Thread(target=self.ctl.refresh_snapshot, daemon=True).start()

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if not self._guard(mutating=True):
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if u.path == "/api/refresh":
                self._send(200, self.ctl.refresh_snapshot(wait=6))
                return
            if u.path == "/api/register":
                res = register_api.handle_register(body, self.ctl)
                self._registry_changed(res[1])
                self._send(*res)
                return
            if u.path == "/api/projects":
                self._send(*register_api.handle_project_upsert(body))
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/directories", u.path)
            if m:
                self._send(*register_api.handle_directory_add(m.group(1), body))
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/favorite", u.path)
            if m:
                self._send(*register_api.handle_favorite(m.group(1), body))
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/autostart", u.path)
            if m:
                self._send(*register_api.handle_autostart(m.group(1), body))
                return
            m = re.fullmatch(r"/api/removed/services/([\w-]+)/restore", u.path)
            if m:
                res = register_api.handle_restore_service(m.group(1), body, self.ctl)
                self._registry_changed(res[1])
                self._send(*res)
                return
            m = re.fullmatch(r"/api/removed/projects/([\w-]+)/restore", u.path)
            if m:
                res = register_api.handle_restore_project(m.group(1), body, self.ctl)
                self._registry_changed(res[1])
                self._send(*res)
                return
            m = re.fullmatch(r"/api/drafts/([\w-]+)/register", u.path)
            if m:
                res = register_api.handle_draft_register(m.group(1), body, self.ctl)
                self._registry_changed(res[1])
                self._send(*res)
                return
            if u.path == "/api/agents/scan":
                self._send(*agent_api.handle_agents_scan(body))
                return
            if u.path == "/api/agents/verify":
                self._send(*agent_api.handle_agents_verify(body))
                return
            if u.path == "/api/agents/default":
                self._send(*agent_api.handle_agents_default(body))
                return
            if u.path == "/api/analyze":
                self._send(*agent_api.handle_analyze(body))
                return
            workspace = re.fullmatch(r"/api/groups/([\w-]+)/(start|stop|restart)", u.path)
            if workspace:
                res = self.ctl.group_action(*workspace.groups())
                threading.Thread(target=self.ctl.refresh_snapshot, daemon=True).start()
                self._send(200 if res.get("ok") else 409, res)
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/(start|stop|restart|open|open-source|action|takeover)", u.path)
            if not m:
                self._send(404, {"ok": False, "error": "not found"})
                return
            pid, act = m.groups()
            if act == "start":
                res = self.ctl.start(pid, open_after=bool(body.get("open")))
            elif act == "stop":
                res = self.ctl.stop(pid)
            elif act == "restart":
                res = self.ctl.restart(pid, components=body.get("components"))
            elif act == "open":
                res = self.ctl.open(pid)
            elif act == "open-source":
                res = self.ctl.open_source(pid)
            elif act == "takeover":
                res = self.ctl.takeover(pid)
            else:
                res = self.ctl.project_action(pid, str(body.get("name", "")))
            threading.Thread(target=self.ctl.refresh_snapshot, daemon=True).start()
            self._send(200 if res.get("ok") else 409, res)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})

    def do_DELETE(self):
        u = urlparse(self.path)
        try:
            if not self._guard(mutating=True):
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/directories/([\w-]+)", u.path)
            if m:
                self._send(*register_api.handle_directory_remove(*m.groups()))
                return
            m = re.fullmatch(r"/api/drafts/([\w-]+)", u.path)
            if m:
                self._send(*register_api.handle_draft_delete(m.group(1)))
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)/directories/([\w-]+)", u.path)
            if m:
                self._send(*register_api.handle_directory_remove(*m.groups()))
                return
            m = re.fullmatch(r"/api/services/([\w-]+)", u.path)
            if m:
                q = parse_qs(u.query)
                res = register_api.handle_service_remove(m.group(1), q.get("disposition", ["keep-external"])[0], self.ctl)
                self._registry_changed(res[1])
                self._send(*res)
                return
            m = re.fullmatch(r"/api/projects/([\w-]+)", u.path)
            if m:
                q = parse_qs(u.query)
                disposition = q.get("disposition", ["keep-external"])[0]
                dry_run = q.get("dry_run", ["false"])[0].lower() in ("1", "true", "yes")
                res = register_api.handle_project_remove(m.group(1), disposition, dry_run, self.ctl)
                self._registry_changed(res[1])
                self._send(*res)
                return
            self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"ok": False, "error": str(e)})


def loop(fn, interval: float, name: str):
    def run():
        while True:
            t0 = time.time()
            try:
                fn()
            except Exception:  # noqa: BLE001
                print(f"[{name}] error", file=sys.stderr)
                traceback.print_exc()
            time.sleep(max(0.2, interval - (time.time() - t0)))
    threading.Thread(target=run, name=name, daemon=True).start()


def main() -> int:
    paths.ensure()
    agent_api.mark_stale_jobs_failed()
    ctl = Controller()
    port = paths.daemon_port()
    Handler.ctl, Handler.token, Handler.port = ctl, load_token(), port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"[localservices] {time.strftime('%F %T')} daemon {__version__} on http://127.0.0.1:{port} pid {os.getpid()}", flush=True)
    ctl.refresh_snapshot()
    loop(ctl.reconcile, float(os.environ.get("LOCALSERVICES_RECONCILE", "5")), "reconcile")
    loop(ctl.refresh_snapshot, float(os.environ.get("LOCALSERVICES_STATUS", "3")), "status")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
