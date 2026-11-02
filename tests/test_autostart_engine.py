"""V2.9 自动启动引擎：Store intent 初始缺省 = 清单 autostart OR registry autostart_map。

Registry.autostart_map 由 Core 线提供；这里用 fake（patch create=True）+ 失败回退验证。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import manifest, paths  # noqa: E402
from localservices.controller import Controller, Store  # noqa: E402
from localservices.manifest import Project  # noqa: E402


def make_project(pid, autostart=False):
    return Project(id=pid, name=pid, source=Path("/tmp"), autostart=autostart)


class IsolatedEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-autostart-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "home"),
            "LOCALSERVICES_SERVICES": str(self.root / "services"),
            "LOCALSERVICES_USER_SERVICES": str(self.root / "user-services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-autostart-test",
        })
        env.start()
        self.addCleanup(env.stop)
        paths.ensure()
        (self.root / "services").mkdir(parents=True)


class StoreDefaultIntentTest(IsolatedEnv):
    def test_registry_map_true_defaults_running(self):
        store = Store(autostart=lambda: {"eng": True})
        self.assertEqual(store.intent(make_project("eng")), "running")
        # 落盘后不再询问 provider
        saved = store.data["projects"]["eng"]
        self.assertEqual(saved["intent"], "running")
        self.assertEqual(saved["intent_by"], "default")

    def test_registry_map_false_or_missing_defaults_stopped(self):
        store = Store(autostart=lambda: {"eng": False})
        self.assertEqual(store.intent(make_project("eng")), "stopped")
        store2 = Store(autostart=lambda: {"other": True})
        self.assertEqual(store2.intent(make_project("eng")), "stopped")

    def test_manifest_autostart_still_honored_without_registry(self):
        store = Store(autostart=lambda: {})
        self.assertEqual(store.intent(make_project("eng", autostart=True)), "running")
        store2 = Store()  # 无 provider（V1 行为）
        self.assertEqual(store2.intent(make_project("eng", autostart=True)), "running")
        self.assertEqual(store2.intent(make_project("plain")), "stopped")

    def test_provider_failure_falls_back_to_manifest_only(self):
        def boom():
            raise RuntimeError("registry unreadable")
        store = Store(autostart=boom)
        self.assertEqual(store.intent(make_project("eng")), "stopped")
        self.assertEqual(store.intent(make_project("auto-proj", autostart=True)), "running")

    def test_existing_state_intent_not_overridden(self):
        store = Store(autostart=lambda: {"eng": True})
        store.set_intent("eng", "stopped", by="user")
        self.assertEqual(store.intent(make_project("eng")), "stopped")


class ControllerWiringTest(IsolatedEnv):
    def write_service(self, sid, autostart):
        (self.root / "services" / f"{sid}.toml").write_text(
            f'id="{sid}"\nname="{sid}"\nsource="{self.root}"\nautostart={str(autostart).lower()}\n'
            '[[components]]\nid="web"\ncmd=["true"]\n'
        )

    def test_controller_uses_registry_autostart_map(self):
        self.write_service("reg-proj", False)
        with patch("localservices.registry.Registry.autostart_map",
                   return_value={"reg-proj": True}, create=True):
            ctl = Controller()
            self.addCleanup(ctl._pool.shutdown, wait=True)
            self.assertEqual(ctl.store.intent(ctl.projects["reg-proj"]), "running")

    def test_controller_without_map_helper_stays_stopped(self):
        # Registry.autostart_map 尚未落地（Core 线并行）→ getattr 缺省 → 不影响缺省
        self.write_service("reg-proj", False)
        ctl = Controller()
        self.addCleanup(ctl._pool.shutdown, wait=True)
        self.assertEqual(ctl.store.intent(ctl.projects["reg-proj"]), "stopped")

    def test_controller_registry_error_falls_back(self):
        self.write_service("reg-proj", False)
        with patch("localservices.registry.Registry.autostart_map",
                   side_effect=RuntimeError("boom"), create=True):
            ctl = Controller()
            self.addCleanup(ctl._pool.shutdown, wait=True)
            self.assertEqual(ctl.store.intent(ctl.projects["reg-proj"]), "stopped")

    def test_manifest_autostart_true_even_when_map_empty(self):
        self.write_service("selfstart", True)
        with patch("localservices.registry.Registry.autostart_map",
                   return_value={}, create=True):
            ctl = Controller()
            self.addCleanup(ctl._pool.shutdown, wait=True)
            self.assertEqual(ctl.store.intent(ctl.projects["selfstart"]), "running")


if __name__ == "__main__":
    unittest.main()
