"""更新策略测试（AC-17）：热更/构建后/手动更新语义保留；忙碌延后；未知不当作空闲自动重启。"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lsupd-"))
os.environ.update({
    "LOCALSERVICES_HOME": str(TMP / "home"),
    "LOCALSERVICES_SERVICES": str(TMP / "services"),
    "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsupd",
})
sys.path.insert(0, str(REPO))

from localservices import adapters, launchd  # noqa: E402
from localservices.controller import Controller  # noqa: E402


class FakeAdapter(adapters.Adapter):
    def __init__(self) -> None:
        self.busy_reason: str | None = None

    def busy(self) -> str | None:
        return self.busy_reason

    def open_url(self) -> str | None:
        return None


class UpdatePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = TMP / "src"
        src.mkdir(parents=True)
        (src / "code.py").write_text("v1")
        (TMP / "services").mkdir(exist_ok=True)
        (TMP / "services" / "fxupd.toml").write_text(textwrap.dedent(f"""
            id = "fxupd"
            name = "Fx Upd"
            source = "{src}"
            [[components]]
            id = "api"
            cmd = ["/bin/sleep", "100"]
            port = 48123
            ready = {{ tcp = "127.0.0.1:48123" }}
            start_timeout = 5
            watch = ["."]
            auto_update = true
        """))
        cls.ctl = Controller()
        cls.adapter = FakeAdapter()
        cls.ctl.adapters["fxupd"] = cls.adapter

    @classmethod
    def tearDownClass(cls):
        p = cls.ctl.projects.get("fxupd")
        if p:
            for c in p.managed_components():
                launchd.bootout(cls.ctl.label(p, c))

    def _comp(self):
        p = self.ctl.projects["fxupd"]
        st = self.ctl.project_status(p)
        return next(c for c in st["components"] if c["id"] == "api")

    def test_1_busy_defers_update(self):
        self.adapter.busy_reason = "录制进行中"
        p = self.ctl.projects["fxupd"]
        comp = {"api": {"running": True, "code": {"state": "pending", "changed_at": time.time() - 10}}}
        self.ctl._tick_updates(p, comp)
        self.assertEqual(self.ctl.update_state["fxupd.api"]["deferred"], "录制进行中")
        self.assertNotIn("fxupd", self.ctl.ops, "忙碌时不得自动重启")
        self.adapter.busy_reason = None

    def test_2_unknown_busy_also_defers(self):
        # 适配器返回未知形态（如 None 之外的意外值按未知处理）：仍不自动中断
        self.adapter.busy_reason = "unknown-state"
        p = self.ctl.projects["fxupd"]
        comp = {"api": {"running": True, "code": {"state": "pending", "changed_at": time.time() - 10}}}
        self.ctl._tick_updates(p, comp)
        self.assertNotIn("fxupd", self.ctl.ops, "未知忙碌状态不得当作空闲自动重启")
        self.adapter.busy_reason = None

    def test_3_idle_change_schedules_restart(self):
        p = self.ctl.projects["fxupd"]
        comp = {"api": {"running": True, "code": {"state": "pending", "changed_at": time.time() - 10}}}
        self.ctl._tick_updates(p, comp)
        self.assertIn("fxupd", self.ctl.ops, "空闲且待更新应排队重启")
        # 等待操作结束，避免影响后续
        deadline = time.time() + 30
        while "fxupd" in self.ctl.ops and time.time() < deadline:
            time.sleep(0.3)
        self.ctl.update_state.pop("fxupd.api", None)

    def test_4_manual_update_policy_respected(self):
        deadline = time.time() + 45
        while "fxupd" in self.ctl.ops and time.time() < deadline:
            time.sleep(0.3)
        # auto_update=false 的组件（如 explore API）即使空闲也不自动重启
        p = self.ctl.projects["fxupd"]
        c = p.component("api")
        c.auto_update = False
        comp = {"api": {"running": True, "code": {"state": "pending", "changed_at": time.time() - 10}}}
        self.ctl._tick_updates(p, comp)
        self.assertNotIn("fxupd", self.ctl.ops, "auto_update=false 不得自动重启")
        c.auto_update = True


if __name__ == "__main__":
    unittest.main()
