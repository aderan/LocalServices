"""review R7 / Goal C4：观测与调度职责分离。

- _refresh 是纯观测（采集+投影）：不得触发更新调度（_tick_updates）、不得写运行意图。
- 更新调度统一进 reconcile 周期，依据最新快照驱动：忙碌延后、空闲应用、
  auto_update=false 不动、观测线程在跑也不重复调度。
- 先于重构编写：refresh 触发 tick 的旧行为会让 test_refresh_pure 失败。"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lsobssched-"))
sys.path.insert(0, str(REPO))

from localservices import adapters  # noqa: E402
from localservices.controller import Controller  # noqa: E402


class FakeAdapter(adapters.Adapter):
    def __init__(self) -> None:
        self.busy_reason: str | None = None

    def busy(self) -> str | None:
        return self.busy_reason

    def open_url(self) -> str | None:
        return None


class SplitEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(TMP / "home"),
            "LOCALSERVICES_SERVICES": str(TMP / "services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-obssched",
        })
        cls.env.start()
        src = TMP / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "code.py").write_text("v1")
        (TMP / "services").mkdir(exist_ok=True)
        (TMP / "services" / "fxobs.toml").write_text(textwrap.dedent(f"""
            id = "fxobs"
            name = "Fx Obs"
            source = "{src}"
            [[components]]
            id = "api"
            cmd = ["/bin/sleep", "100"]
            port = 48321
            ready = {{ tcp = "127.0.0.1:48321" }}
            start_timeout = 5
            watch = ["."]
            auto_update = true
        """))
        cls.ctl = Controller()
        cls.adapter = FakeAdapter()
        cls.ctl.adapters["fxobs"] = cls.adapter

    @classmethod
    def tearDownClass(cls):
        try:
            p = cls.ctl.projects.get("fxobs")
            if p:
                from localservices import launchd
                for c in p.managed_components():
                    launchd.bootout(cls.ctl.label(p, c))
        finally:
            cls.env.stop()

    def _synth_snapshot(self, running=True, pending=True):
        """构造「组件运行中 + 代码待更新」的快照事实（与 project_status 输出同形）。"""
        code_state = "pending" if pending else "synced"
        return {"projects": [{
            "id": "fxobs", "state": "available", "intent": "running", "label": "可用",
            "components": [{"id": "api", "kind": "server", "running": running, "port": 48321,
                            "state": "ready" if running else "stopped",
                            "code": {"state": code_state, "label": "待更新",
                                     "changed_at": time.time() - 10, "detail": ""}}],
        }], "generated_at": time.time()}


class RefreshIsPureObservationTest(SplitEnv):
    def test_refresh_does_not_invoke_update_scheduling(self):
        """_refresh 只采集与投影：绝不调用 _tick_updates（无论运行意图如何）。"""
        with patch.object(self.ctl, "_tick_updates") as tick:
            # 运行意图下的刷新（旧行为在此路径内联调用 tick）
            self.ctl.store.set_intent("fxobs", "running", by="test")
            self.ctl._snapshot = self._synth_snapshot()
            self.ctl.refresh_snapshot()
            tick.assert_not_called()

    def test_refresh_does_not_set_update_state(self):
        """观测不产生调度副作用：不写 update_state（延后标签属调度层输出）。"""
        self.ctl.store.set_intent("fxobs", "running", by="test")
        self.ctl.refresh_snapshot()
        self.assertNotIn("fxobs.api", self.ctl.update_state)


class ReconcileSchedulesFromSnapshotTest(SplitEnv):
    def _run_as_loaded(self):
        """launchd 视角与合成快照一致（job 已加载运行中）：reconcile 的启动分支不插队，
        让测试聚焦「调度层按快照驱动更新」这一行为本身。"""
        from localservices.launchd import JobInfo
        info = JobInfo(loaded=True, running=True)
        return patch("localservices.controller.launchd.info", return_value=info)

    def test_reconcile_defers_update_when_busy(self):
        self.ctl.store.set_intent("fxobs", "running", by="test")
        self.ctl._snapshot = self._synth_snapshot()
        self.adapter.busy_reason = "录制进行中"
        self.ctl.update_state.clear()
        with self._run_as_loaded():
            self.ctl.reconcile()
        self.assertEqual(self.ctl.update_state.get("fxobs.api", {}).get("deferred"), "录制进行中")
        self.assertNotIn("fxobs", self.ctl.ops, "忙碌时不得排队重启")

    def test_reconcile_applies_update_when_idle(self):
        self.ctl.store.set_intent("fxobs", "running", by="test")
        self.ctl._snapshot = self._synth_snapshot()
        self.adapter.busy_reason = None
        restarts = []
        with self._run_as_loaded(), \
                patch.object(self.ctl, "_do_restart", side_effect=lambda p, c: restarts.append((p.id, list(c)))):
            self.ctl.reconcile()
        deadline = time.time() + 5
        while "fxobs" not in self.ctl.ops and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(restarts, [("fxobs", ["api"])], "空闲且待更新应由 reconcile 排队重启")
        self.ctl.ops.pop("fxobs", None)  # 桩不清 ops，手工复位避免污染同类后续用例

    def test_reconcile_skips_stopped_intent(self):
        """停止意图的项目不调度更新（快照里 intent 不是 running）。"""
        self.ctl.store.set_intent("fxobs", "stopped", by="test")
        snap = self._synth_snapshot()
        snap["projects"][0]["intent"] = "stopped"
        self.ctl._snapshot = snap
        self.ctl.update_state.clear()
        from localservices.launchd import JobInfo
        with patch("localservices.controller.launchd.info", return_value=JobInfo()), \
                patch.object(self.ctl, "_tick_updates") as tick:
            self.ctl.reconcile()
            tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
