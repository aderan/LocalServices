"""V3.5 域名跳转设置：默认本地 IP 直连；开启「使用域名」+ 配置「域名形式」才经域名。

覆盖：
- 设置持久化与缺省（use_domains=false / domain_form=suffix）；非法值 400。
- 打开行为：默认直连（入口健康也直连）/ 开启+入口健康=域名 / 开启+入口故障=直连+缺口说明 /
  custom 形式下无显式域名的服务直连且不算故障。
- 域名形式=custom：路由只用显式登记入口（仅派生域名的服务无路由）；suffix 全参与。
- derived 标记：显式 [[endpoints]] 存在就不派生（契约 V3.1），派生入口 derived=True。
- API 端点（GET/POST /api/locove/settings）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import locove, manifest, register_api  # noqa: E402
from localservices.controller import Controller  # noqa: E402
from localservices.manifest import Component, Endpoint, Project  # noqa: E402
from localservices.registry import Registry, RegistryError  # noqa: E402


class SettingsEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-locove-set-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "home"),
            "LOCALSERVICES_SERVICES": str(self.root / "services"),
            "LOCALSERVICES_USER_SERVICES": str(self.root / "user-services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-locove-set",
        })
        env.start()
        self.addCleanup(env.stop)
        for d in ("home", "services", "user-services", "src"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.reg = Registry()


class RegistrySettingsTest(SettingsEnv):
    def test_defaults_are_direct_ip(self):
        self.assertEqual(self.reg.locove_settings(),
                         {"use_domains": False, "domain_form": "suffix",
                          "domain_port": 5670, "domains_enabled_at": None})

    def test_persist_and_roundtrip(self):
        st = self.reg.locove_settings_update({"use_domains": True, "domain_form": "custom"})
        self.assertTrue(st["use_domains"])
        self.assertEqual(st["domain_form"], "custom")
        self.assertEqual(Registry().locove_settings(), st)  # 新实例读同一文件

    def test_invalid_form_rejected(self):
        with self.assertRaises(RegistryError):
            self.reg.locove_settings_update({"domain_form": "wildcard"})

    def test_api_handlers(self):
        code, res = register_api.handle_locove_settings_post({"use_domains": True})
        self.assertEqual(code, 200)
        self.assertTrue(res["settings"]["use_domains"])
        code, res = register_api.handle_locove_settings_post({"domain_form": "nope"})
        self.assertEqual(code, 400)
        code, res = register_api.handle_locove_settings_get()
        self.assertEqual(code, 200)


def _proj(explicit=None):
    """真实形态：显式与派生互斥（契约 V3.1——有显式 [[endpoints]] 就不派生）。"""
    comps = [Component(id="web", cmd=["sleep", "1"], port=8801)]
    if explicit:
        eps = [Endpoint(id="custom", host=explicit, component="web", open_path="/x", derived=False)]
    else:
        eps = [Endpoint(id="default", host="snap.locove", component="web", open_path="/", derived=True)]
    return Project(id="snap", name="Snap", source=Path("/tmp"), components=comps, endpoints=eps)


class FormRoutingTest(SettingsEnv):
    SNAP_OK = {"projects": [{"id": "snap", "state": "available"}]}

    def test_suffix_form_includes_derived(self):
        routes = locove.desired_routes({"snap": _proj()}, self.SNAP_OK,
                                        {"use_domains": True, "domain_form": "suffix"})
        self.assertIn("snap.locove", routes)

    def test_custom_form_routes_only_explicit(self):
        routes = locove.desired_routes({"snap": _proj(explicit="panel.snap.locove")},
                                       self.SNAP_OK,
                                       {"use_domains": True, "domain_form": "custom"})
        self.assertIn("panel.snap.locove", routes)
        # 仅派生域名的服务在 custom 形式下没有路由
        routes2 = locove.desired_routes({"snap": _proj()}, self.SNAP_OK,
                                        {"use_domains": True, "domain_form": "custom"})
        self.assertEqual(routes2, {})

    def test_manifest_marks_derived_endpoints(self):
        head = 'id = "d"\nname = "D"\nsource = "/tmp"\n' \
               "[[components]]\nid = \"web\"\ncmd = [\"sleep\", \"1\"]\nport = 8811\n"
        f = self.root / "services" / "d.toml"
        f.write_text(head + "[[endpoints]]\nhost = \"x.d.locove\"\ncomponent = \"web\"\n",
                     encoding="utf-8")
        self.assertEqual({e.host: e.derived for e in manifest.load_file(f).endpoints},
                         {"x.d.locove": False})
        g = self.root / "services" / "e.toml"
        g.write_text(head.replace('"d"', '"e"'), encoding="utf-8")
        self.assertEqual({e.host: e.derived for e in manifest.load_file(g).endpoints},
                         {"e.locove": True})


class OpenGatingTest(SettingsEnv):
    def _ctl(self):
        toml = (
            'id = "snap"\nname = "Snap"\nsource = "%s"\nopen = "http://localhost:8821/"\n'
            "[[components]]\nid = \"web\"\ncmd = [\"sleep\", \"1\"]\nport = 8821\n"
            % (self.root / "src"))
        (self.root / "services" / "snap.toml").write_text(toml, encoding="utf-8")
        ctl = Controller()
        ctl._snapshot = {"projects": [{"id": "snap", "state": "available"}]}
        return ctl

    def _open(self, ctl, settings, entry_ok=True):
        ji = SimpleNamespace(running=False, loaded=False, pid=None, last_exit=None)
        with patch("localservices.controller.launchd.info", return_value=ji), \
             patch("localservices.controller.procs.snapshot", return_value={}), \
             patch("localservices.controller.procs.listeners", return_value={}), \
             patch("localservices.controller.procs.probe", return_value=(False, "")), \
             patch("localservices.controller.procs.all_cwds", return_value={}), \
             patch.object(ctl, "domain_settings", return_value=settings), \
             patch("localservices.locove.module_status",
                   return_value={"state": "available" if entry_ok else "failed"}), \
             patch("localservices.controller.subprocess") as sub:
            res = ctl.open("snap")
            return res, sub.Popen.call_args[0][0][1]

    def test_default_direct_even_when_entry_healthy(self):
        res, url = self._open(self._ctl(), {"use_domains": False, "domain_form": "suffix"})
        self.assertTrue(res["ok"])
        self.assertNotIn("note", res)
        self.assertEqual(url, "http://localhost:8821/")

    def test_enabled_suffix_uses_domain(self):
        res, url = self._open(self._ctl(), {"use_domains": True, "domain_form": "suffix"})
        self.assertEqual(url, "http://snap.locove:5670/")
        self.assertNotIn("note", res)

    def test_enabled_custom_without_explicit_falls_back_direct(self):
        res, url = self._open(self._ctl(), {"use_domains": True, "domain_form": "custom"})
        self.assertEqual(url, "http://localhost:8821/")
        self.assertNotIn("note", res)  # 该服务本无显式域名：直连是正确行为，不是故障

    def test_enabled_custom_port_uses_configured(self):
        res, url = self._open(self._ctl(), {"use_domains": True, "domain_form": "suffix",
                                            "domain_port": 6789})
        self.assertEqual(url, "http://snap.locove:6789/")

    def test_enabled_entry_down_notes_gap(self):
        res, url = self._open(self._ctl(), {"use_domains": True, "domain_form": "suffix"},
                              entry_ok=False)
        self.assertEqual(url, "http://localhost:8821/")
        self.assertIn("note", res)


if __name__ == "__main__":
    unittest.main()
