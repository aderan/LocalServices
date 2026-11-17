"""V2.12 CLI 契约测试：新子命令的解析/分发/请求形状/退出码（HTTP 层打桩，不发真请求）。"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from localservices import cli


class FakeCall:
    """记录 (method, path, body) 并按脚本回放响应。"""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda method, path, body: (200, {"ok": True}))

    def __call__(self, method, path, body=None, timeout=10):
        self.calls.append({"method": method, "path": path, "body": body})
        return self.responder(method, path, body)


def run_cli(argv, responder=None):
    fake = FakeCall(responder)
    buf = io.StringIO()
    with patch.object(cli, "call", fake), contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, fake.calls, buf.getvalue()


class LocoveBypassTest(unittest.TestCase):
    def test_applies_bypass_through_daemon(self):
        rc, calls, out = run_cli(["locove-bypass", "--json"],
                                 lambda m, p, b: (200, {"ok": True, "message": "configured"}))
        self.assertEqual(calls, [{"method": "POST", "path": "/api/locove/bypass", "body": {}}])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_verification_failure_has_nonzero_exit_code(self):
        rc, _, out = run_cli(["locove-bypass"],
                             lambda m, p, b: (200, {"ok": False, "error": "not applied"}))
        self.assertEqual(rc, 1)
        self.assertIn("not applied", out)


class ProjectRemoveTest(unittest.TestCase):

    def test_default_disposition_keep_external(self):
        rc, calls, out = run_cli(["project-remove", "p1"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "DELETE")
        self.assertEqual(calls[0]["path"], "/api/projects/p1?disposition=keep-external")
        self.assertIn("已移除项目", out)

    def test_stop_and_dry_run_query(self):
        rc, calls, out = run_cli(["project-remove", "p1", "--stop", "--dry-run"])
        self.assertEqual(rc, 0)  # dry-run 成功同样 = 0
        self.assertEqual(calls[0]["path"], "/api/projects/p1?disposition=stop&dry_run=true")
        self.assertIn("项目移除预览", out)  # ok 响应 + dry_run → 走预览格式器

    def test_failure_exit_codes(self):
        rc, _, _ = run_cli(["project-remove", "p1"],
                           lambda m, p, b: (409, {"ok": False, "error": "conflict"}))
        self.assertEqual(rc, 5)
        rc, _, _ = run_cli(["project-remove", "p1"],
                           lambda m, p, b: (400, {"ok": False, "error": "bad"}))
        self.assertEqual(rc, 4)
        rc, _, _ = run_cli(["project-remove", "p1"],
                           lambda m, p, b: (500, {"ok": False, "error": "boom"}))
        self.assertEqual(rc, 1)

    def test_partial_failure_shows_stopped_and_remaining(self):
        res = {"ok": False, "stopped": ["svc-a"], "remaining": [{"id": "svc-b", "reason": "停止失败"}]}
        rc, _, out = run_cli(["project-remove", "p1"], lambda m, p, b: (409, res))
        self.assertEqual(rc, 5)
        self.assertIn("svc-a", out)
        self.assertIn("svc-b", out)
        self.assertIn("停止失败", out)

    def test_json_output(self):
        rc, calls, out = run_cli(["project-remove", "p1", "--json"],
                                 lambda m, p, b: (200, {"ok": True, "services": []}))
        self.assertEqual(rc, 0)
        parsed = json.loads(out)
        self.assertTrue(parsed["ok"])


class ServiceRemovePreviewTest(unittest.TestCase):

    def test_preview_calls_get_remove_preview(self):
        preview = {"ok": True, "service_id": "s1", "name": "S1", "state": "running",
                   "running_components": ["api"], "external_instances": [],
                   "keep": ["源码目录", "数据库"],
                   "dispositions": {"stop": {}, "keep-external": {"enabled": False, "reason": "launchd 保活"}},
                   "restore_hint": "lsctl restore s1"}
        rc, calls, out = run_cli(["service-remove", "s1", "--preview"],
                                 lambda m, p, b: (200, preview))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["path"], "/api/services/s1/remove-preview")
        self.assertIn("移除预览", out)
        self.assertIn("S1", out)
        self.assertIn("将停止的实例：api", out)
        self.assertIn("不可保留为外部实例", out)

    def test_plain_remove_default_keep_external(self):
        rc, calls, out = run_cli(["service-remove", "s1"],
                                 lambda m, p, b: (200, {"ok": True, "service_id": "s1",
                                                        "disposition": "keep-external",
                                                        "archive": "removed/s1-1.json",
                                                        "released": ["api"]}))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "DELETE")
        self.assertEqual(calls[0]["path"], "/api/services/s1?disposition=keep-external")
        self.assertIn("已移除登记", out)

    def test_stop_flag(self):
        rc, calls, _ = run_cli(["service-remove", "s1", "--stop"])
        self.assertEqual(calls[0]["path"], "/api/services/s1?disposition=stop")

    def test_preview_json(self):
        rc, calls, out = run_cli(["service-remove", "s1", "--preview", "--json"],
                                 lambda m, p, b: (200, {"ok": True, "service_id": "s1"}))
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])


class RemovedAndRestoreTest(unittest.TestCase):

    def test_removed_lists_services_and_projects(self):
        res = {"ok": True,
               "services": [{"sid": "s1", "project_id": "p1", "disposition": "stop",
                             "removed_at": "2026-11-01T10:00:00Z", "archive": "removed/s1-1.json"}],
               "projects": [{"pid": "p2", "removed_at": "2026-11-01T11:00:00Z",
                             "services": ["s2", "s3"]}]}
        rc, calls, out = run_cli(["removed"], lambda m, p, b: (200, res))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["path"], "/api/removed")
        self.assertIn("s1", out)
        self.assertIn("lsctl restore s1", out)
        self.assertIn("lsctl project-restore p2", out)

    def test_restore_body_carries_project_and_intent(self):
        rc, calls, _ = run_cli(["restore", "s1", "--project", "p9", "--intent", "running"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], "/api/removed/services/s1/restore")
        self.assertEqual(calls[0]["body"], {"project_id": "p9", "intent": "running"})

    def test_restore_defaults_empty_body(self):
        rc, calls, _ = run_cli(["restore", "s1"])
        self.assertEqual(calls[0]["body"], {})

    def test_restore_intent_choices_enforced(self):
        with self.assertRaises(SystemExit):  # argparse choices 拒绝非法 intent
            run_cli(["restore", "s1", "--intent", "sometimes"])

    def test_project_restore_body(self):
        rc, calls, out = run_cli(["project-restore", "p1", "--intent", "stopped"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], "/api/removed/projects/p1/restore")
        self.assertEqual(calls[0]["body"], {"intent": "stopped"})
        self.assertIn("已恢复项目", out)
        rc, calls, _ = run_cli(["project-restore", "p1"])
        self.assertEqual(calls[0]["body"], {})  # 缺省意图由服务端取 stopped


class ProjectAutostartTest(unittest.TestCase):

    def test_on_posts_true(self):
        rc, calls, out = run_cli(["project-autostart", "p1", "--on"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], "/api/projects/p1/autostart")
        self.assertEqual(calls[0]["body"], {"value": True})
        self.assertIn("已开启", out)

    def test_off_posts_false(self):
        rc, calls, _ = run_cli(["project-autostart", "p1", "--off"])
        self.assertEqual(calls[0]["body"], {"value": False})

    def test_flag_required(self):
        with self.assertRaises(SystemExit):  # --on/--off 互斥且必选
            run_cli(["project-autostart", "p1"])


class AnalyzeProjectFlagTest(unittest.TestCase):

    def test_project_flows_into_request_body(self):
        rc, calls, _ = run_cli(["analyze", "/tmp/x", "--agent", "codex", "--project", "p1"],
                               lambda m, p, b: (202, {"ok": True, "job_id": "job-1"}))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], "/api/analyze")
        self.assertEqual(calls[0]["body"],
                         {"directory": "/tmp/x", "agent": "codex", "project_id": "p1"})

    def test_without_project_omits_key(self):
        rc, calls, _ = run_cli(["analyze", "/tmp/x", "--agent", "claude"],
                               lambda m, p, b: (202, {"ok": True, "job_id": "job-1"}))
        self.assertNotIn("project_id", calls[0]["body"])

    def test_wait_polls_until_done(self):
        states = iter([(200, {"id": "job-1", "state": "running"}),
                       (200, {"id": "job-1", "state": "done",
                              "result": {"services": [{"id": "a", "name": "A",
                                                       "components": [{"id": "m"}]}],
                                         "unresolved": []}})])

        def responder(method, path, body):
            if path == "/api/analyze":
                return 202, {"ok": True, "job_id": "job-1"}
            return next(states)

        rc, calls, out = run_cli(["analyze", "/tmp/x", "--agent", "codex", "--project", "p1",
                                  "--wait", "2"], responder)  # 轮询下限 0.5s，取 2s 保证两轮
        self.assertEqual(rc, 0)
        self.assertIn("候选 a", out)


class ExitCode3Test(unittest.TestCase):
    """公共退出码：控制服务不可达 = 3（main 捕获 OSError/URLError）。"""

    def test_daemon_unreachable_is_3(self):
        def boom(method, path, body=None, timeout=10):
            raise urllib.error.URLError("connection refused")
        buf = io.StringIO()
        with patch.object(cli, "call", boom), contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            for argv in (["removed"], ["restore", "s1"], ["project-autostart", "p1", "--on"],
                         ["service-remove", "s1", "--preview"], ["project-remove", "p1"]):
                self.assertEqual(cli.main(argv), 3, argv)


if __name__ == "__main__":
    unittest.main()
