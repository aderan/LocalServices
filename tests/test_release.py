"""release_service（V2.1/V2.7）：bootout + 删 plist/run 记录/state 条目；幂等、
不越界（其他 sid 的文件不动）、不停任何容器。launchd 子进程全部 fake，目录全在 tmp。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import launchd, paths  # noqa: E402
from localservices.controller import Controller  # noqa: E402

PREFIX = "com.mybrain.ls-release-test"


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-release-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "home"),
            "LOCALSERVICES_SERVICES": str(self.root / "services"),
            "LOCALSERVICES_LABEL_PREFIX": PREFIX,
        })
        env.start()
        self.addCleanup(env.stop)
        (self.root / "services").mkdir(parents=True)
        self.ctl = Controller()
        self.addCleanup(self.ctl._pool.shutdown, wait=True)

        # 现场布置：fx 的两个组件 + 另一服务 other 的文件
        agents = paths.agents_dir()
        run = paths.run_dir()
        self.fx_plists = [agents / f"{PREFIX}.fx.api.plist", agents / f"{PREFIX}.fx.worker.plist"]
        self.other_plist = agents / f"{PREFIX}.other.api.plist"
        self.fx_records = [run / "fx.api.json", run / "fx.web.json"]
        self.other_record = run / "other.api.json"
        for p in [*self.fx_plists, self.other_plist]:
            p.write_text("placeholder")
        for p in [*self.fx_records, self.other_record]:
            p.write_text(json.dumps({"failures": 0}))
        self.ctl.store.proj("fx").update({"intent": "running", "intent_by": "user"})
        self.ctl.store.proj("other").update({"intent": "running"})
        self.ctl.store.save()

        self.bootouts: list[str] = []
        self.loaded = {f"{PREFIX}.fx.api", f"{PREFIX}.fx.worker", f"{PREFIX}.other.api"}
        p1 = patch.object(launchd, "loaded_labels", side_effect=lambda: set(self.loaded))
        p2 = patch.object(launchd, "bootout", side_effect=self._bootout)
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)

    def _bootout(self, label):
        self.bootouts.append(label)
        self.loaded.discard(label)
        if label.endswith("fx.worker"):
            return False, "launchctl error: not loaded"  # 模拟一个失败
        return True, ""

    def test_release_boots_out_deletes_and_drops_state(self):
        result = self.ctl.release_service("fx")
        self.assertFalse(result["ok"])  # worker bootout 失败 → overall False
        self.assertIn(f"{PREFIX}.fx.api", result["released"])
        self.assertEqual(len([e for e in result["errors"] if "fx.worker" in e]), 1)
        # plist 删除仍执行（bootout 失败不阻断文件清理）
        for p in self.fx_plists:
            self.assertFalse(p.exists(), p)
        self.assertTrue(self.other_plist.exists(), "其他服务的 plist 不得删除")
        for p in self.fx_records:
            self.assertFalse(p.exists(), p)
        self.assertTrue(self.other_record.exists(), "其他服务的 run 记录不得删除")
        self.assertIn(f"state:fx", result["released"])
        self.assertNotIn("fx", self.ctl.store.data["projects"])
        self.assertNotIn("fx", json.loads(paths.state_file().read_text())["projects"])
        self.assertIn("other", self.ctl.store.data["projects"])
        self.assertEqual(self.bootouts, [f"{PREFIX}.fx.api", f"{PREFIX}.fx.worker"])

    def test_release_is_idempotent(self):
        self.ctl.release_service("fx")
        first_len = len(self.bootouts)
        result2 = self.ctl.release_service("fx")
        self.assertTrue(result2["ok"], result2)
        self.assertEqual(result2["released"], [])
        self.assertEqual(len(self.bootouts), first_len)

    def test_release_tolerates_missing_everything(self):
        result = self.ctl.release_service("ghost-sid")
        self.assertEqual(result, {"ok": True, "released": [], "errors": []})
        self.assertEqual(self.bootouts, [])  # 不 bootout 其他服务的标签

    def test_release_never_stops_containers(self):
        # release_service 只做簿记清理；容器/Compose 一律不动（无任何 compose 调用路径）
        with patch("localservices.compose.stop_component",
                   side_effect=AssertionError("release must not stop containers")) as stop:
            self.ctl.release_service("fx")
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
