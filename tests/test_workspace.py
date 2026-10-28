"""Scoped operations use temporary state and never signal real project processes."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from localservices.controller import Controller
from localservices.daemon import Handler


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        defs = root / "services"
        defs.mkdir()
        for name, components in [("explore", ["api", "web"]), ("atlas", ["web"]), ("unrelated", ["web"])]:
            (defs / f"{name}.toml").write_text(
                f'id="{name}"\nname="{name}"\nsource="{root}"\n' +
                ''.join(f'[[components]]\nid="{c}"\ncmd=["true"]\n' for c in components)
            )
        env = patch.dict(os.environ, {"LOCALSERVICES_HOME": str(root / "runtime"),
                                    "LOCALSERVICES_SERVICES": str(defs),
                                    "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.workspace-test"})
        env.start()
        self.addCleanup(env.stop)
        self.ctl = Controller()
        self.addCleanup(self.ctl._pool.shutdown, wait=True)
        for name, order in [("explore", 0), ("atlas", 1)]:
            p = self.ctl.projects[name]
            p.group = "suite"
            p.group_name = "Workspace"
            p.group_order = order
        ext = patch.object(self.ctl, "external_instances", return_value=[])
        ext.start()
        self.addCleanup(ext.stop)

    def finish(self):
        end = time.monotonic() + 2
        while self.ctl.ops and time.monotonic() < end:
            time.sleep(.005)
        self.assertFalse(self.ctl.ops)

    def test_api_only_restart_stops_and_starts_only_api(self):
        ctl = self.ctl
        def started(p, open_after, **kwargs):
            self.assertEqual(kwargs["only"], ["api"])
            ctl._end(p.id)
        with patch.object(ctl, "_bootout_wait", return_value=(True, "")) as stop, \
                patch.object(ctl, "_do_start", side_effect=started) as start:
            self.assertTrue(ctl.restart("explore", ["api"])["accepted"])
            self.finish()
            self.assertEqual([c.args[1].id for c in stop.call_args_list], ["api"])
            self.assertEqual(start.call_args.args[0].id, "explore")

    def test_frontend_only_restart_preserves_api(self):
        def started(p, open_after, **kwargs):
            self.assertEqual(kwargs["only"], ["web"])
            self.ctl._end(p.id)
        with patch.object(self.ctl, "_bootout_wait", return_value=(True, "")) as stop, \
                patch.object(self.ctl, "_do_start", side_effect=started):
            self.ctl.restart("explore", ["web"])
            self.finish()
        self.assertEqual([c.args[1].id for c in stop.call_args_list], ["web"])

    def test_failed_component_stop_is_retained_and_does_not_stop_more_components(self):
        p = self.ctl.projects["explore"]
        self.ctl._begin(p.id, "stopping")
        with patch.object(self.ctl, "_bootout_wait", return_value=(False, "still running")) as stop:
            self.ctl._do_stop(p)
        self.assertIn("still running", self.ctl.errors[p.id]["message"])
        self.assertEqual(stop.call_count, 1)

    def test_invalid_component_selection_has_no_side_effect(self):
        for components in ([], ["missing"], "api", ["api", "api"]):
            with self.subTest(components=components), patch.object(self.ctl, "_do_restart") as work:
                result = self.ctl.restart("explore", components)
                self.assertFalse(result["ok"])
                work.assert_not_called()
                self.assertFalse(self.ctl.ops)

    def test_workspace_restart_runs_explore_before_atlas_and_no_unrelated_project(self):
        order = []
        def work(p, components, by_user=False):
            order.append((p.id, components))
            self.ctl._end(p.id)
        with patch.object(self.ctl, "_do_restart", side_effect=work):
            self.assertTrue(self.ctl.group_action("suite", "restart")["accepted"])
            self.finish()
        self.assertEqual(order, [("explore", None), ("atlas", None)])

    def test_workspace_stop_reverses_dependency_order(self):
        order = []
        def work(p):
            order.append(p.id)
            self.ctl._end(p.id)
        with patch.object(self.ctl, "_do_stop", side_effect=work):
            self.assertTrue(self.ctl.group_action("suite", "stop")["accepted"])
            self.finish()
        self.assertEqual(order, ["atlas", "explore"])

    def test_workspace_busy_member_rejects_before_starting_any_member(self):
        self.ctl.adapters["explore"].busy = lambda: "capture running"
        with patch.object(self.ctl, "_do_restart") as work:
            self.assertFalse(self.ctl.group_action("suite", "restart")["ok"])
            work.assert_not_called()
        self.assertFalse(self.ctl.ops)

    def test_workspace_failure_does_not_restart_later_members(self):
        order = []
        def work(p, components, by_user=False):
            order.append(p.id)
            self.ctl._set_error(p.id, "api", "failed")
            self.ctl._end(p.id)
        with patch.object(self.ctl, "_do_restart", side_effect=work):
            self.ctl.group_action("suite", "restart")
            self.finish()
        self.assertEqual(order, ["explore"])


class OperationHttpTest(unittest.TestCase):
    def post(self, route, body):
        ctl = Mock()
        ctl.restart.return_value = {"ok": True}
        ctl.group_action.return_value = {"ok": True}
        cls = type("TestHandler", (Handler,), {"ctl": ctl, "token": "fixture"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
        cls.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = Request(f"http://127.0.0.1:{cls.port}{route}", data=json.dumps(body).encode(),
                          headers={"X-LocalServices-Token": "fixture", "Content-Type": "application/json"})
            try:
                with urlopen(req) as response:
                    status = response.status
            except HTTPError as response:
                status = response.code
            return ctl, status
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_component_scope_reaches_controller(self):
        ctl, status = self.post("/api/projects/explore/restart", {"components": ["api"]})
        self.assertEqual(status, 200)
        ctl.restart.assert_called_once_with("explore", components=["api"])

    def test_workspace_route_reaches_controller(self):
        ctl, status = self.post("/api/groups/suite/restart", {})
        self.assertEqual(status, 200)
        ctl.group_action.assert_called_once_with("suite", "restart")


class ExploreBusyTest(unittest.TestCase):
    def adapter(self):
        from localservices.adapters.explore import Adapter
        from localservices.manifest import load_all
        project = load_all(Path(__file__).resolve().parents[1] / "services")["explore"]
        return Adapter(project)

    def test_running_capture_blocks_restart(self):
        with patch("localservices.procs.http_get", return_value=(200, b'{"tasks":[{"status":"running"}]}', "")):
            self.assertIsNotNone(self.adapter().busy())

    def test_paused_and_completed_history_does_not_block_restart(self):
        with patch("localservices.procs.http_get", return_value=(200, b'{"tasks":[{"status":"paused"},{"status":"completed"}]}', "")):
            self.assertIsNone(self.adapter().busy())

    def test_invalid_task_response_is_not_treated_as_idle(self):
        with patch("localservices.procs.http_get", return_value=(200, b'{"tasks":null}', "")):
            self.assertIsNotNone(self.adapter().busy())


class LargeHistoryTest(unittest.TestCase):
    def test_completed_history_above_default_probe_limit_does_not_block_restart(self):
        from http.server import BaseHTTPRequestHandler
        from localservices.adapters.explore import Adapter
        from localservices.manifest import load_all
        payload = json.dumps({"tasks": [{"status": "completed", "context": "x" * 300_000}]}).encode()
        class History(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), History)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            project = load_all(Path(__file__).resolve().parents[1] / "services")["explore"]
            project.options["api"] = f"http://127.0.0.1:{server.server_port}"
            self.assertIsNone(Adapter(project).busy())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
