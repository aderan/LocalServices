"""V2 集成补充：清单卸载后的状态收敛（reload force + state 条目修剪）。

场景 A 真实验证发现的两处缺陷的行为门禁：
1. tombstone（removed.json）移除不改 TOML mtime 签名 → 普通 reload() 不感知，
   登记面变更必须 force=True 才卸载；
2. 并发 refresh 持旧清单经 Store.intent 懒播种，可能在 release_service 之后把
   state 条目写回 → reload 必须收敛式修剪已卸载项目的 state 条目。
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
sys.path.insert(0, str(REPO))

from localservices import paths  # noqa: E402
from localservices.controller import Controller  # noqa: E402

TOML = """id = "{sid}"
name = "{sid}"
source = "{src}"

[[components]]
id = "web"
kind = "server"
cmd = ["/usr/bin/true"]
cwd = "."
"""


class ReloadConvergenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-reload-conv-"))
        (self.tmp / "builtin").mkdir()
        (self.tmp / "user").mkdir()
        (self.tmp / "home").mkdir()
        (self.tmp / "src").mkdir()
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.tmp / "home"),
            "LOCALSERVICES_SERVICES": str(self.tmp / "builtin"),
            "LOCALSERVICES_USER_SERVICES": str(self.tmp / "user"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsconv",
        })
        self.env.start()
        # 内置目录模板：tombstone 只遮蔽内置层（V2.0 修订语义）
        (self.tmp / "builtin" / "svc-a.toml").write_text(
            TOML.format(sid="svc-a", src=self.tmp / "src"), encoding="utf-8")

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state_entries(self) -> dict:
        f = paths.state_file()
        if not f.exists():
            return {}
        return json.loads(f.read_text()).get("projects", {})

    def test_tombstone_removal_needs_force_and_prunes_state(self):
        ctl = Controller()
        self.assertIn("svc-a", ctl.projects)
        ctl.store.set_intent("svc-a", "running", by="user")
        self.assertIn("svc-a", self._state_entries())

        # tombstone：不动任何 TOML，仅 removed.json
        paths.removed_file().write_text(json.dumps(
            {"schemaVersion": 1, "services": {"svc-a": {"removed_at": "now"}},
             "projects": {}}), encoding="utf-8")
        ctl.reload()  # 签名未变：不感知（旧行为保留给纯 mtime 路径）
        self.assertIn("svc-a", ctl.projects)

        ctl.reload(force=True)  # 登记面变更路径：强制卸载 + 修剪
        self.assertNotIn("svc-a", ctl.projects)
        self.assertNotIn("svc-a", self._state_entries())

    def test_racy_reseed_is_pruned_on_next_reload(self):
        ctl = Controller()
        (self.tmp / "builtin" / "svc-a.toml").unlink()
        ctl.reload(force=True)
        self.assertNotIn("svc-a", ctl.projects)
        self.assertNotIn("svc-a", self._state_entries())

        # 模拟并发 refresh 带旧清单懒播种写回
        ctl.store.data["projects"]["svc-a"] = {"intent": "stopped", "intent_by": "default"}
        ctl.store.save()
        self.assertIn("svc-a", self._state_entries())

        ctl.reload(force=True)  # 收敛：再次修剪
        self.assertNotIn("svc-a", self._state_entries())

    def test_registered_service_state_not_pruned(self):
        ctl = Controller()
        ctl.store.set_intent("svc-a", "running", by="user")
        ctl.reload(force=True)  # 服务仍在清单：条目必须保留
        self.assertIn("svc-a", ctl.projects)
        self.assertEqual(self._state_entries()["svc-a"]["intent"], "running")


if __name__ == "__main__":
    unittest.main()
