"""Locove 路由事实与代理激活行为。

- desired_routes：登记 × 运行快照 → host→upstream（含外部实例实际端口）；停机/缺快照 → unavailable。
- render_caddyfile：精确 Host 站点；proxy 反代、unavailable 503 页、未知 Host catch-all 404。
- apply_routes：validate → 写盘 → reload；reload 失败回滚到上次有效配置。
- 真实 Caddy（本机存在时）：adapt 校验生成的配置 + 起真实 caddy 进程走通
  「Host 头 → 反代上游」与 503 页（无端口入口的系统级 wiring 见 install 脚本与验收记录）。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import locove  # noqa: E402
from localservices.locove import Route  # noqa: E402
from localservices.manifest import Component, Endpoint, Project  # noqa: E402

CADDY = locove.caddy_bin()
HAS_CADDY = Path(CADDY).exists()


def make_project(pid: str, host: str, port: int, component: str = "web") -> Project:
    return Project(id=pid, name=pid, source=Path("/tmp"),
                   components=[Component(id=component, cmd=["sleep", "1"], port=port)],
                   endpoints=[Endpoint(id="default", host=host, component=component, open_path="/zh")])


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-routes-")
        self.addCleanup(self.tmp.cleanup)
        # admin 端口取空闲临时端口：绝不指向生产 Caddy 的 admin（否则单测会重载真实例）
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_admin = probe.getsockname()[1]
        probe.close()
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(Path(self.tmp.name) / "home"),
            "LOCOVE_PROXY_PORT": "47890",
            "LOCOVE_ADMIN_PORT": str(free_admin),
        })
        self.env.start()
        self.addCleanup(self.env.stop)


class DesiredRoutesTest(Env):
    def test_running_service_routes_to_component_port(self):
        p = make_project("buildbase", "buildbase.locove", 3000)
        snap = {"projects": [{"id": "buildbase", "state": "available"}]}
        routes = locove.desired_routes({"buildbase": p}, snap)
        r = routes["buildbase.locove"]
        self.assertEqual((r.state, r.upstream_port, r.service), ("proxy", 3000, "buildbase"))

    def test_stopped_service_is_unavailable(self):
        p = make_project("app", "app.locove", 3001)
        snap = {"projects": [{"id": "app", "state": "stopped"}]}
        routes = locove.desired_routes({"app": p}, snap)
        self.assertEqual(routes["app.locove"].state, "unavailable")
        # 事实保留配置端口（诊断用）；渲染层对 unavailable 一律 503，不转发
        self.assertEqual(routes["app.locove"].upstream_port, 3001)

    def test_missing_snapshot_converges_unavailable(self):
        p = make_project("app", "app.locove", 3002)
        self.assertEqual(locove.desired_routes({"app": p}, None)["app.locove"].state, "unavailable")

    def test_external_instance_uses_actual_port(self):
        p = make_project("app", "app.locove", 3003)
        snap = {"projects": [{"id": "app", "state": "external", "external_ports": {"web": 13003}}]}
        routes = locove.desired_routes({"app": p}, snap)
        self.assertEqual((routes["app.locove"].state, routes["app.locove"].upstream_port),
                         ("proxy", 13003))

    def test_partial_counts_as_served(self):
        p = make_project("app", "app.locove", 3004)
        snap = {"projects": [{"id": "app", "state": "partial"}]}
        self.assertEqual(locove.desired_routes({"app": p}, snap)["app.locove"].state, "proxy")

    def test_removed_service_has_no_route(self):
        p = make_project("app", "app.locove", 3005)
        self.assertEqual(locove.desired_routes({}, {}), {})


class RenderTest(Env):
    def test_proxy_and_unavailable_and_catchall(self):
        cfg = locove.render_caddyfile({
            "a.locove": Route("a.locove", "a", "web", 3000, "proxy"),
            "b.locove": Route("b.locove", "b", "web", None, "unavailable"),
        })
        self.assertIn("http://a.locove {", cfg)
        self.assertIn("reverse_proxy 127.0.0.1:3000", cfg)
        self.assertIn("flush_interval -1", cfg)
        self.assertIn("http://b.locove {", cfg)
        self.assertIn("503", cfg)
        self.assertIn("未运行", cfg)
        self.assertIn("http:// {", cfg)  # 未知 Host catch-all
        self.assertIn("404", cfg)
        self.assertIn("auto_https off", cfg)

    def test_sync_no_change_is_noop(self):
        p = make_project("a", "a.locove", 3010)
        snap = {"projects": [{"id": "a", "state": "available"}]}
        fake = Path(self.tmp.name) / "fake-caddy"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        os.environ["LOCOVE_CADDY_BIN"] = str(fake)
        self.addCleanup(os.environ.pop, "LOCOVE_CADDY_BIN", None)
        self.assertEqual(locove.sync({"a": p}, snap)["changed"], True)
        self.assertEqual(locove.sync({"a": p}, snap)["changed"], False)


class ApplyRollbackTest(Env):
    def _fake_caddy(self, fail_reload: bool) -> Path:
        script = "#!/bin/sh\n" + (
            "if [ \"$1\" = reload ]; then echo boom >&2; exit 1; fi\nexit 0\n"
            if fail_reload else "exit 0\n")
        f = Path(self.tmp.name) / "fake-caddy"
        f.write_text(script)
        f.chmod(0o755)
        os.environ["LOCOVE_CADDY_BIN"] = str(f)
        self.addCleanup(os.environ.pop, "LOCOVE_CADDY_BIN", None)
        return f

    def test_apply_writes_files_and_routes_json(self):
        self._fake_caddy(False)
        res = locove.apply_routes({"a.locove": Route("a.locove", "a", "web", 3000, "proxy")})
        self.assertTrue(res["ok"])
        data = json.loads(locove.routes_file().read_text())
        self.assertEqual(data["routes"]["a.locove"]["upstream_port"], 3000)
        self.assertIn("reverse_proxy 127.0.0.1:3000", locove.caddyfile().read_text())

    def test_reload_failure_keeps_last_good_config(self):
        self._fake_caddy(False)
        locove.apply_routes({"a.locove": Route("a.locove", "a", "web", 3000, "proxy")})
        old_cfg = locove.caddyfile().read_text()
        old_routes = locove.routes_file().read_text()
        # 模拟运行中的 Caddy：admin 端口有人听，但 reload 被拒绝
        import socket as _s
        admin = _s.socket()
        admin.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        admin.bind(("127.0.0.1", locove.admin_port()))
        admin.listen(1)
        self.addCleanup(admin.close)
        self._fake_caddy(True)
        res = locove.apply_routes({"b.locove": Route("b.locove", "b", "web", 3001, "proxy")})
        self.assertFalse(res["ok"])
        self.assertIn("reload", res["error"])
        self.assertEqual(locove.caddyfile().read_text(), old_cfg)
        self.assertEqual(locove.routes_file().read_text(), old_routes)

    def test_apply_without_running_caddy_is_pending_not_failed(self):
        self._fake_caddy(False)
        res = locove.apply_routes({"a.locove": Route("a.locove", "a", "web", 3000, "proxy")})
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("pending"))
        self.assertTrue(locove.caddyfile().exists())

    def test_validate_failure_rejects_before_any_write(self):
        # 一个把任何配置都判为非法的 caddy 桩
        f = Path(self.tmp.name) / "fake-caddy"
        f.write_text("#!/bin/sh\necho bad-config >&2\nexit 1\n")
        f.chmod(0o755)
        os.environ["LOCOVE_CADDY_BIN"] = str(f)
        self.addCleanup(os.environ.pop, "LOCOVE_CADDY_BIN", None)
        res = locove.apply_routes({"a.locove": Route("a.locove", "a", "web", 3000, "proxy")})
        self.assertFalse(res["ok"])
        self.assertFalse(locove.routes_file().exists())
        self.assertFalse(locove.caddyfile().exists())


class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"upstream:{self.headers.get('Host')}:{self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@unittest.skipUnless(HAS_CADDY, "caddy not installed")
class RealCaddyTest(Env):
    """真实 Caddy：adapt 校验生成配置；起真进程验证 Host 路由与 503 页。"""

    PROXY_PORT = 47895
    ADMIN_PORT = 47896

    def setUp(self):
        super().setUp()
        os.environ["LOCOVE_PROXY_PORT"] = str(self.PROXY_PORT)
        os.environ["LOCOVE_ADMIN_PORT"] = str(self.ADMIN_PORT)
        os.environ["LOCOVE_CADDY_BIN"] = CADDY
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        self.up_port = self.up.server_address[1]
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.addCleanup(self.up.shutdown)

    def _start_caddy(self):
        res = locove.apply_routes({
            "live.locove": Route("live.locove", "live", "web", self.up_port, "proxy"),
            "down.locove": Route("down.locove", "down", "web", None, "unavailable"),
        })
        self.assertTrue(res["ok"], res)
        proc = subprocess.Popen(
            [CADDY, "run", "--config", str(locove.caddyfile())],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(proc.terminate)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.PROXY_PORT), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("caddy did not start: " + proc.stderr.read(400).decode(errors="replace"))

    def test_generated_config_adapts(self):
        cfg = locove.render_caddyfile({
            "x.locove": Route("x.locove", "x", "web", 3000, "proxy"),
            "y.locove": Route("y.locove", "y", "web", None, "unavailable"),
        })
        f = Path(self.tmp.name) / "adapt.caddyfile"
        f.write_text(cfg)
        r = subprocess.run([CADDY, "adapt", "--config", str(f), "--adapter", "caddyfile"],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("x.locove", r.stdout)

    def test_host_routing_and_unavailable_page(self):
        self._start_caddy()
        # 命中已登记 Host → 反代到上游（上游看到 Host 头原样传递）
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-H", "Host: live.locove",
             f"http://127.0.0.1:{self.PROXY_PORT}/zh"], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout, f"upstream:live.locove:/zh")
        # 停止的服务 → 503 明确不可用页
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}",
             "-H", "Host: down.locove", f"http://127.0.0.1:{self.PROXY_PORT}/"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout, "503")
        # 未知 Host → 404，绝不转发
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}",
             "-H", "Host: stranger.locove", f"http://127.0.0.1:{self.PROXY_PORT}/"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout, "404")

    def test_reload_switches_route_atomically(self):
        self._start_caddy()
        locove.apply_routes({
            "live.locove": Route("live.locove", "live", "web", None, "unavailable"),
        })
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-o", "/dev/null", "-w", "%{http_code}",
             "-H", "Host: live.locove", f"http://127.0.0.1:{self.PROXY_PORT}/zh"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout, "503")


if __name__ == "__main__":
    unittest.main()


class RebuildAfterRestartTest(Env):
    def test_daemon_proxy_dns_restart_rebuilds_identical_config(self):
        """daemon/代理/DNS 重启收敛：落盘事实被清后，同一输入重建出完全一致的
        routes.json/Caddyfile（DNS responder 每次 mtime 重读，无需重建）。"""
        fake = Path(self.tmp.name) / "fake-caddy"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        os.environ["LOCOVE_CADDY_BIN"] = str(fake)
        self.addCleanup(os.environ.pop, "LOCOVE_CADDY_BIN", None)
        p = make_project("app", "app.locove", 3010)
        snap = {"projects": [{"id": "app", "state": "available"}]}
        self.assertEqual(locove.sync({"app": p}, snap)["changed"], True)
        routes_before = locove.routes_file().read_text()
        cfg_before = locove.caddyfile().read_text()
        locove.routes_file().unlink()
        locove.caddyfile().unlink()
        res = locove.sync({"app": p}, snap)
        self.assertTrue(res["changed"])
        import json as _json

        def facts(text):
            d = _json.loads(text)
            d.pop("generated_at", None)  # 时间戳不属于配置事实
            return d

        self.assertEqual(facts(locove.routes_file().read_text()), facts(routes_before))
        self.assertEqual(locove.caddyfile().read_text(), cfg_before)
