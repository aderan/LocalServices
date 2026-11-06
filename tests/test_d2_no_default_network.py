"""D2：网络件退出默认安装与运行（2026-11-06 Goal）。

- install.sh 不再安装/引导任何 locove 网络件（普通安装 = 控制面 + 菜单栏 + CLI + Skill）。
- daemon 路由收敛只在用户开启域名时运行（gated_sync）；默认不写任何 locove 文件。
- /api/locove 健康面区分 未安装/已装未开/启动中/可用/失败；未安装不显示为故障。"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lsd2-"))
sys.path.insert(0, str(REPO))

from localservices import locove  # noqa: E402

INSTALL_SH = (REPO / "scripts" / "install.sh").read_text(encoding="utf-8")


class InstallNoDefaultNetworkTest(unittest.TestCase):
    def test_install_sh_does_not_install_network_parts(self):
        """普通安装脚本不得调用 locove 网络件安装器（D2 验收：普通升级不偷偷重装网络件）。
        注释里的启用指引是说明文字，允许；任何非注释行的调用都不允许。"""
        active = [ln for ln in INSTALL_SH.splitlines()
                  if "install-locove-system" in ln and ln.lstrip().split("#", 1)[0].strip()]
        self.assertEqual(active, [], f"install.sh 仍调用网络件安装器：{active}")
        self.assertIn("可选", INSTALL_SH, "应提示域名是可选能力")

    def test_install_sh_still_installs_core_parts(self):
        for marker in ("com.mybrain.localservices.daemon", "install-skill.sh", "lsctl"):
            self.assertIn(marker, INSTALL_SH)


class GatedSyncTest(unittest.TestCase):
    def test_gated_sync_noop_when_domains_off(self):
        """use_domains=false（默认）：不调用 sync、不写任何 locove 文件。"""
        home = TMP / "gated-home"
        env = patch.dict(os.environ, {"LOCALSERVICES_HOME": str(home),
                                      "LOCOVE_LAUNCHAGENTS": str(TMP / "la"),
                                      "LOCOVE_RESOLVER_DIR": str(TMP / "resolver")})
        with env, patch.object(locove, "sync") as sync:
            res = locove.gated_sync({"use_domains": False, "domain_form": "suffix"}, {}, None)
            sync.assert_not_called()
            self.assertIsNone(res)
            self.assertFalse((home / "locove").exists(), "未开启时不得创建 locove 目录/文件")

    def test_gated_sync_runs_when_domains_on(self):
        with patch.object(locove, "sync", return_value={"ok": True}) as sync:
            res = locove.gated_sync({"use_domains": True, "domain_form": "suffix"}, {}, None)
            sync.assert_called_once()
            self.assertEqual(res, {"ok": True})


class ModuleStatusTest(unittest.TestCase):
    """状态机（D3 状态面）：未安装/已装未开/启动中/可用/失败，互不伪装。"""

    def setUp(self):
        self.la = TMP / "la-status"
        self.resolver_dir = TMP / "resolver-status"
        self.la.mkdir(parents=True, exist_ok=True)
        self.resolver_dir.mkdir(parents=True, exist_ok=True)
        (self.resolver_dir / "locove").unlink(missing_ok=True)
        for job in ("dns", "proxy"):
            (self.la / f"com.mybrain.ls.locove.{job}.plist").unlink(missing_ok=True)
        self.env = patch.dict(os.environ, {"LOCALSERVICES_HOME": str(TMP / "home-status"),
                                           "LOCOVE_LAUNCHAGENTS": str(self.la),
                                           "LOCOVE_RESOLVER_DIR": str(self.resolver_dir),
                                           "LOCALSERVICES_DOMAIN_SUFFIX": "locove"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _install_fake(self, agents=True, resolver=False):
        if agents:
            for job in ("dns", "proxy"):
                (self.la / f"com.mybrain.ls.locove.{job}.plist").write_text("<plist/>")
        if resolver:
            from localservices.locove_resolver import MARKER
            (self.resolver_dir / "locove").write_text(
                f"{MARKER}\nnameserver 127.0.0.1\nport 5354\n")

    def test_not_installed_is_not_a_fault(self):
        st = locove.module_status({"use_domains": False})
        self.assertEqual(st["state"], "not_installed")
        self.assertIsNone(st["dns"]["ok"])  # 未知，不是失败
        self.assertFalse(st["installed"]["dns_agent"])
        self.assertEqual(st["domain_port"], 5670)

    def test_installed_off_when_agents_present_but_disabled(self):
        self._install_fake(agents=True)
        st = locove.module_status({"use_domains": False})
        self.assertEqual(st["state"], "installed_off")

    def test_available_when_enabled_and_healthy(self):
        self._install_fake(agents=True, resolver=True)
        with patch.object(locove, "_port_open", return_value=True):
            st = locove.module_status({"use_domains": True})
        self.assertEqual(st["state"], "available")
        self.assertTrue(st["dns"]["ok"] and st["proxy"]["ok"] and st["resolver"]["ok"])

    def test_starting_within_grace_then_failed(self):
        import time as _t
        self._install_fake(agents=True, resolver=True)
        with patch.object(locove, "_port_open", return_value=False):
            recent = locove.module_status({"use_domains": True, "domains_enabled_at": _t.time() - 5})
            self.assertEqual(recent["state"], "starting")
            stale = locove.module_status({"use_domains": True, "domains_enabled_at": _t.time() - 10 ** 6})
            self.assertEqual(stale["state"], "failed")

    def test_failed_reports_each_layer(self):
        self._install_fake(agents=True, resolver=False)
        with patch.object(locove, "_port_open", return_value=True), \
                patch.object(locove, "legacy_80_present", return_value=True):
            st = locove.module_status({"use_domains": True, "domains_enabled_at": 1})
        self.assertEqual(st["state"], "failed")
        self.assertFalse(st["resolver"]["ok"])
        self.assertIn("未安装", st["resolver"]["detail"])
        self.assertTrue(st["legacy_80"], "旧 80 件存在必须如实报告，不参与新健康判定")

    def test_caddyfile_uses_effective_port(self):
        cfg = locove.render_caddyfile({}, settings={"domain_port": 6789})
        self.assertIn("http_port 6789", cfg)
        cfg2 = locove.render_caddyfile({}, port=7000)
        self.assertIn("http_port 7000", cfg2)
        self.assertIn("default_bind 127.0.0.1 ::1", cfg2, "回环绑定不随端口变化放松")


class EndpointUrlPortTest(unittest.TestCase):
    """公开地址统一含端口（D3）：project_status 是 UI/CLI/复制/导出的单一来源。"""

    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(TMP / "home-url"),
            "LOCALSERVICES_SERVICES": str(TMP / "services-url"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-d2url",
        })
        cls.env.start()
        src = TMP / "src-url"
        src.mkdir(parents=True, exist_ok=True)
        (TMP / "services-url").mkdir(exist_ok=True)
        (TMP / "services-url" / "fx2.toml").write_text(textwrap.dedent(f"""
            id = "fx2"
            name = "Fx Two"
            source = "{src}"
            [[components]]
            id = "web"
            cmd = ["/bin/sleep", "100"]
            port = 48341
            ready = {{ tcp = "127.0.0.1:48341" }}
        """))
        from localservices.controller import Controller
        cls.ctl = Controller()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_public_url_carries_default_port(self):
        p = self.ctl.projects["fx2"]
        with patch.object(self.ctl, "domain_settings",
                          return_value={"use_domains": False, "domain_form": "suffix", "domain_port": 5670}):
            st = self.ctl.project_status(p)
        self.assertEqual(st["endpoints"][0]["public_url"], "http://fx2.locove:5670/")

    def test_public_url_carries_configured_port(self):
        p = self.ctl.projects["fx2"]
        with patch.object(self.ctl, "domain_settings",
                          return_value={"use_domains": True, "domain_form": "suffix", "domain_port": 6789}):
            st = self.ctl.project_status(p)
        self.assertEqual(st["endpoints"][0]["public_url"], "http://fx2.locove:6789/")


if __name__ == "__main__":
    unittest.main()
