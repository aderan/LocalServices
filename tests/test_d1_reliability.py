"""D1 核心可靠性（review 2026-11-05）：S1 停止竞态 / S2 resolver 读失败删除 / S3 观测写意图。

先于修复编写：当前实现会让这三组用例失败。
- S1：旧快照 intent=running + 当前 Store 已 stopped（停止完成、ops 清空）时，
  调度层不得再排队重启；排队到执行之间用户停止同样优先。
- S2：resolver 内容不可读时 uninstall 必须拒绝删除（读失败 ≠ 已确认归属）。
- S3：纯观测（refresh_snapshot/project_status）不得创建或写入 state.json；
  缺省意图初始化只属于显式初始化/登记/调度（reconcile/set_intent）流程。
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lsd1-"))
sys.path.insert(0, str(REPO))

from localservices import adapters  # noqa: E402
from localservices.controller import Controller  # noqa: E402
from localservices.locove_resolver import uninstall as resolver_uninstall  # noqa: E402
from localservices import paths  # noqa: E402


class FakeAdapter(adapters.Adapter):
    def __init__(self) -> None:
        self.busy_reason: str | None = None

    def busy(self) -> str | None:
        return self.busy_reason

    def open_url(self) -> str | None:
        return None


class D1Env(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(TMP / "home"),
            "LOCALSERVICES_SERVICES": str(TMP / "services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-d1",
        })
        cls.env.start()
        src = TMP / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "code.py").write_text("v1")
        (TMP / "services").mkdir(exist_ok=True)
        (TMP / "services" / "fx1.toml").write_text(textwrap.dedent(f"""
            id = "fx1"
            name = "Fx One"
            source = "{src}"
            [[components]]
            id = "api"
            cmd = ["/bin/sleep", "100"]
            port = 48331
            ready = {{ tcp = "127.0.0.1:48331" }}
            start_timeout = 5
            watch = ["."]
            auto_update = true
        """))
        cls.ctl = Controller()
        cls.adapter = FakeAdapter()
        cls.ctl.adapters["fx1"] = cls.adapter

    @classmethod
    def tearDownClass(cls):
        try:
            p = cls.ctl.projects.get("fx1")
            if p:
                from localservices import launchd
                for c in p.managed_components():
                    launchd.bootout(cls.ctl.label(p, c))
        finally:
            cls.env.stop()

    def _stale_running_snapshot(self):
        """旧快照：intent=running、组件运行中、代码待更新（S1 复现的前提事实）。"""
        return {"projects": [{
            "id": "fx1", "state": "available", "intent": "running", "label": "可用",
            "components": [{"id": "api", "kind": "server", "running": True, "port": 48331,
                            "state": "ready",
                            "code": {"state": "pending", "label": "待更新",
                                     "changed_at": time.time() - 10, "detail": ""}}],
        }], "generated_at": time.time()}


class S1StopBeatsStaleSnapshotTest(D1Env):
    def _as_loaded(self):
        from localservices.launchd import JobInfo
        return patch("localservices.controller.launchd.info",
                     return_value=JobInfo(loaded=True, running=True))

    def test_stale_running_snapshot_does_not_restart_stopped_service(self):
        """用户已停止（Store=stopped、ops 空）后，旧快照不得触发 _schedule_updates 重启。"""
        self.ctl.store.set_intent("fx1", "stopped", by="user")
        self.ctl._snapshot = self._stale_running_snapshot()
        self.ctl.ops.pop("fx1", None)  # 停止操作已完成并清空
        restarts = []
        with self._as_loaded(), \
                patch.object(self.ctl, "_do_restart",
                             side_effect=lambda p, c: restarts.append((p.id, list(c)))):
            self.ctl._schedule_updates()
            deadline = time.time() + 2
            while "fx1" in self.ctl.ops and time.time() < deadline:
                time.sleep(0.02)
        self.assertEqual(restarts, [], "停止后的服务不得被旧快照重新调度启动")
        self.assertNotIn("fx1", self.ctl.ops)

    def test_queued_update_aborts_when_user_stops_before_execution(self):
        """排队（_begin 成功）到线程执行之间用户停止：执行前复核最新意图并放弃。"""
        self.ctl.store.set_intent("fx1", "running", by="test")
        self.ctl._snapshot = self._stale_running_snapshot()
        self.ctl.ops.pop("fx1", None)
        captured: dict = {}

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                captured["target"], captured["args"] = target, args

            def start(self):
                captured["started"] = True

        restarts = []
        with self._as_loaded(), \
                patch("localservices.controller.threading.Thread", FakeThread), \
                patch.object(self.ctl, "_do_restart",
                             side_effect=lambda p, c, *a, **k: restarts.append((p.id, list(c)))):
            self.ctl._schedule_updates()
            self.assertIn("fx1", self.ctl.ops, "前置条件：调度已排队启动")
            # 停止发生在排队之后、线程体执行之前（review 复现的交错时序）
            self.ctl.store.set_intent("fx1", "stopped", by="user")
            self.ctl._end("fx1")  # 停止操作完成，ops 清空
            captured["target"](*captured["args"])
        self.assertEqual(restarts, [], "执行前复核必须放弃重启")
        self.assertNotIn("fx1", self.ctl.ops, "放弃路径必须释放操作槽")

    def test_running_intent_still_schedules_update(self):
        """回归保护：服务确实在运行且待更新时，调度行为不变（排队重启）。"""
        self.ctl.store.set_intent("fx1", "running", by="test")
        self.ctl._snapshot = self._stale_running_snapshot()
        self.ctl.ops.pop("fx1", None)
        restarts = []
        with self._as_loaded(), \
                patch.object(self.ctl, "_do_restart",
                             side_effect=lambda p, c: restarts.append((p.id, list(c)))):
            self.ctl._schedule_updates()
            deadline = time.time() + 5
            while not restarts and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(restarts, [("fx1", ["api"])])
        self.ctl.ops.pop("fx1", None)


class S3ObservationDoesNotWriteIntentTest(unittest.TestCase):
    """每个用例用全新 Controller：S1 用例对共享 Store 的内存写入不掩盖本组断言。"""

    def setUp(self):
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(TMP / "home"),
            "LOCALSERVICES_SERVICES": str(TMP / "services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-d1",
        })
        self.env.start()
        paths.state_file().unlink(missing_ok=True)
        self.ctl = Controller()

    def tearDown(self):
        self.env.stop()

    def test_fresh_controller_refresh_creates_no_state_file(self):
        """fresh 状态根上，单次观测（refresh_snapshot）不得创建 state.json。"""
        self.assertFalse(paths.state_file().exists())
        snap = self.ctl.refresh_snapshot()
        self.assertFalse(paths.state_file().exists(), "纯观测不得写运行意图文件")
        intents = [p.get("intent") for p in snap["projects"]]
        self.assertTrue(all(i in ("running", "stopped") for i in intents),
                        "观测仍须返回有效意图值")

    def test_project_status_read_only_intent(self):
        """project_status 是观测路径：读取意图但不落盘初始化。"""
        p = self.ctl.projects["fx1"]
        st = self.ctl.project_status(p)
        self.assertIn(st["intent"], ("running", "stopped"))
        self.assertFalse(paths.state_file().exists(), "观测不得因缺省意图初始化而写 state.json")

    def test_reconcile_still_initializes_intent(self):
        """调度流程（reconcile）仍是合法的意图初始化入口（V2.9 缺省语义不变）。"""
        from localservices.launchd import JobInfo
        with patch("localservices.controller.launchd.info", return_value=JobInfo()):
            self.ctl.reconcile()
        state = paths.state_file()
        self.assertTrue(state.exists(), "reconcile 应完成缺省意图初始化")
        data = __import__("json").loads(state.read_text())
        self.assertIn("fx1", data["projects"])


class S2UnreadableResolverTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="lsresolver-d1-"))
        self.f = self.dir / "locove"
        self.f.write_text("# managed-by: locove (com.mybrain.ls)\nnameserver 127.0.0.1\nport 5354\n")
        self.saved_mode = self.f.stat().st_mode

    def tearDown(self):
        try:
            self.f.chmod(self.saved_mode)
            self.f.unlink(missing_ok=True)
            self.dir.rmdir()
        except OSError:
            pass

    @unittest.skipIf(os.geteuid() == 0, "root 不受权限位约束，无法在 root 下复现读失败")
    def test_uninstall_refuses_when_unreadable(self):
        """内容读取失败 ≠ 已确认归属：拒绝删除并说明原因，文件保持原样。"""
        os.chmod(self.f, 0)
        try:
            res = resolver_uninstall(self.dir, "locove")
        finally:
            pass
        self.assertFalse(res.get("ok"), f"读失败必须拒绝删除，got {res}")
        self.assertEqual(res.get("reason"), "unreadable")
        self.assertTrue(self.f.exists(), "拒绝删除时文件必须保持")


if __name__ == "__main__":
    unittest.main()
