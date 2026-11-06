"""D3：可选 5670 域名模块按需启用（2026-11-06 Goal）。

- enable 全链路（预检→用户件→resolver 授权→验证→设置）成功才写 use_domains=true。
- 授权取消：回滚本次新装用户件，零安装副作用，继续直连。
- 端口被他人占用 / caddy 缺失 / resolver 他人所有 / 验证失败：报告具体原因，不启用。
- disable 停止 agent 且回直连；设置校验 domain_port。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lsd3-"))
sys.path.insert(0, str(REPO))

from localservices import locove, locove_optin  # noqa: E402
from localservices.registry import Registry  # noqa: E402


def _completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


class OptInEnv(unittest.TestCase):
    def setUp(self):
        self.la = TMP / "la-optin"
        self.resolver_dir = TMP / "resolver-optin"
        self.home = TMP / "home-optin"
        for d in (self.la, self.resolver_dir, self.home):
            d.mkdir(parents=True, exist_ok=True)
        for job in ("dns", "proxy"):
            (self.la / f"com.mybrain.ls.locove.{job}.plist").unlink(missing_ok=True)
        (self.resolver_dir / "locove").unlink(missing_ok=True)
        (self.home / "registry.json").unlink(missing_ok=True)
        locove_optin._progress.update({"active": False, "op": None, "step": None,
                                       "steps": [], "error": None, "finished_at": None})
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCOVE_LAUNCHAGENTS": str(self.la),
            "LOCOVE_RESOLVER_DIR": str(self.resolver_dir),
            "LOCALSERVICES_DOMAIN_SUFFIX": "locove",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _resolver_ours(self):
        from localservices.locove_resolver import MARKER
        (self.resolver_dir / "locove").write_text(f"{MARKER}\nnameserver 127.0.0.1\nport 5354\n")

    def _agents_installed(self):
        return (self.la / "com.mybrain.ls.locove.dns.plist").exists()

    def _enable_with(self, *, port_open=False, wait_port=True, resolves=True,
                     admin=None, script_rc=0, installed_before=False):
        if installed_before:
            for job in ("dns", "proxy"):
                (self.la / f"com.mybrain.ls.locove.{job}.plist").write_text("<plist/>")
        calls = {"rollback": 0, "admin": 0}

        def fake_run(cmd, timeout=60.0):
            joined = " ".join(map(str, cmd))
            if "uninstall-locove-system.sh" in joined:  # 先判 uninstall：它是 install 的超串
                calls["rollback"] += 1
                for job in ("dns", "proxy"):
                    (self.la / f"com.mybrain.ls.locove.{job}.plist").unlink(missing_ok=True)
                return _completed()
            if "install-locove-system.sh" in joined:
                if script_rc == 0:
                    for job in ("dns", "proxy"):
                        (self.la / f"com.mybrain.ls.locove.{job}.plist").write_text("<plist/>")
                return _completed(rc=script_rc, err="" if script_rc == 0 else "boom")
            return _completed()

        def fake_admin(args, prompt, timeout=600.0):
            calls["admin"] += 1
            if admin == "ok":
                self._resolver_ours()
                return {"ok": True}
            if admin == "cancel":
                return {"ok": False, "reason": "cancelled", "error": "User canceled"}
            return {"ok": False, "reason": "auth-failed", "error": "no privilege"}

        with patch.object(locove_optin, "_run", side_effect=fake_run), \
                patch.object(locove_optin, "admin_run", side_effect=fake_admin), \
                patch.object(locove_optin, "_wait_port", return_value=wait_port), \
                patch.object(locove_optin, "_resolves", return_value=resolves), \
                patch.object(locove_optin, "_listener_name", return_value=None), \
                patch.object(locove, "_port_open", return_value=port_open), \
                patch.object(locove, "module_status", return_value={"caddy_available": True}), \
                patch.object(locove_optin, "_probe_host", return_value="snap.locove"):
            res = locove_optin.enable()
        res["_calls"] = calls
        return res


class PreflightTest(OptInEnv):
    def test_port_conflict_with_foreign_process(self):
        with patch.object(locove, "_port_open", return_value=True), \
                patch.object(locove_optin, "_listener_name", return_value="nginx"), \
                patch.object(locove, "module_status", return_value={"caddy_available": True}):
            pre = locove_optin.preflight(5670)
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["reason"], "port-conflict")
        self.assertIn("nginx", pre["error"])

    def test_port_conflict_tolerates_our_caddy(self):
        """我们自己的 caddy 占着目标端口（重复启用）不算冲突。"""
        with patch.object(locove, "_port_open", return_value=True), \
                patch.object(locove_optin, "_listener_name", return_value="caddy"), \
                patch.object(locove, "module_status", return_value={"caddy_available": True}):
            pre = locove_optin.preflight(5670)
        self.assertTrue(pre["ok"])

    def test_invalid_port_rejected(self):
        for bad in (80, 443, 1023, 65536, 47800, 47891, 5354):
            pre = locove_optin.preflight(bad)
            self.assertFalse(pre["ok"], f"port {bad} 应被拒绝")
            self.assertEqual(pre["reason"], "invalid-port")

    def test_missing_caddy_rejected(self):
        with patch.object(locove, "_port_open", return_value=False), \
                patch.object(locove, "module_status", return_value={"caddy_available": False}):
            pre = locove_optin.preflight(5670)
        self.assertEqual(pre["reason"], "caddy-missing")

    def test_foreign_resolver_rejected_before_anything(self):
        (self.resolver_dir / "locove").write_text("# someone else's\nnameserver 9.9.9.9\n")
        with patch.object(locove, "_port_open", return_value=False), \
                patch.object(locove, "module_status", return_value={"caddy_available": True}):
            pre = locove_optin.preflight(5670)
        self.assertEqual(pre["reason"], "resolver-foreign")
        self.assertIn("拒绝", pre["error"])


class EnableFlowTest(OptInEnv):
    def test_success_requires_all_steps_and_writes_settings(self):
        self._resolver_ours()  # resolver 已装（归属本工具）→ 全程无需授权
        res = self._enable_with(wait_port=True, resolves=True)
        self.assertTrue(res["ok"], res)
        st = Registry().locove_settings()
        self.assertTrue(st["use_domains"])
        self.assertEqual(st["domain_port"], 5670)
        self.assertTrue(st["domains_enabled_at"], "启用时间戳用于 starting/failed 宽限判定")
        self.assertEqual(res["_calls"]["admin"], 0, "resolver 已就位时不得再要授权")

    def test_cancel_authorization_rolls_back_fresh_install(self):
        # resolver 未安装 → 触发授权；用户取消 → 回滚新装用户件，设置不变（直连）
        res = self._enable_with(admin="cancel", wait_port=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "cancelled")
        self.assertIn("继续直连", res["error"])
        self.assertEqual(res["_calls"]["rollback"], 1, "取消必须回滚本次新装的用户级件")
        self.assertFalse(self._agents_installed(), "回滚后不留半套安装")
        self.assertFalse(Registry().locove_settings()["use_domains"])

    def test_cancel_with_existing_install_keeps_it(self):
        # 既有安装（agents 在、resolver 未装）→ 请求授权被取消：既有件不拆，仅不启用
        res = self._enable_with(admin="cancel", wait_port=True, installed_before=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "cancelled")
        self.assertEqual(res["_calls"]["rollback"], 0, "既有安装不因取消被拆")
        self.assertTrue(self._agents_installed())
        self.assertFalse(Registry().locove_settings()["use_domains"])

    def test_port_conflict_aborts_before_installing(self):
        res = self._enable_with(port_open=True)  # _listener_name 默认 None 但 port_open=True→conflict? 
        # port_open=True + listener None → port_free False → conflict
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "port-conflict")
        self.assertFalse(self._agents_installed(), "冲突中止不得安装任何件")
        self.assertFalse(Registry().locove_settings()["use_domains"])

    def test_local_verify_failure_rolls_back_fresh_install(self):
        self._resolver_ours()
        res = self._enable_with(wait_port=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "local-verify-failed")
        self.assertEqual(res["_calls"]["rollback"], 1)

    def test_resolution_failure_reports_and_does_not_enable(self):
        self._resolver_ours()
        res = self._enable_with(resolves=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "resolution-failed")
        self.assertFalse(Registry().locove_settings()["use_domains"],
                         "验证失败绝不保存启用成功")

    def test_busy_guard(self):
        locove_optin._progress.update({"active": True})
        try:
            res = locove_optin.enable()
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "busy")
        finally:
            locove_optin._progress.update({"active": False})

    def test_empty_routes_probe_returns_none_and_enable_succeeds(self):
        """真实机器踩过的坑：routes.json 为空时 _probe_host 曾抛 StopIteration，
        异常 str 为空被误判为成功。空路由=无已登记入口 → 跳过解析探针，正常启用。"""
        self._resolver_ours()
        with patch.object(locove_optin, "_run", return_value=_completed()), \
                patch.object(locove_optin, "_wait_port", return_value=True), \
                patch.object(locove_optin, "_resolves", return_value=False), \
                patch.object(locove_optin, "_listener_name", return_value=None), \
                patch.object(locove, "_port_open", return_value=False), \
                patch.object(locove, "module_status", return_value={"caddy_available": True}):
            # 真实空路由路径（不走 mock）：临时清空 routes.json
            import json as _json
            loc_dir = Path(os.environ["LOCALSERVICES_HOME"]) / "locove"
            loc_dir.mkdir(parents=True, exist_ok=True)
            (loc_dir / "routes.json").write_text(_json.dumps(
                {"schemaVersion": 1, "routes": {}}))
            self.assertEqual(locove_optin._probe_host(), None)
            res = locove_optin.enable()
        self.assertTrue(res["ok"], res)
        self.assertTrue(Registry().locove_settings()["use_domains"])

    def test_unexpected_exception_reported_nonempty(self):
        """异常路径的错误信息永不空白（空 str 异常不得伪装成功）。"""
        with patch.object(locove_optin, "preflight", side_effect=StopIteration()):
            res = locove_optin.enable()
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"], "错误信息必须非空")
        self.assertIn("StopIteration", res["error"])


class DisableTest(OptInEnv):
    def test_disable_stops_agents_and_keeps_files(self):
        Registry().locove_settings_update({"use_domains": True})
        self._resolver_ours()
        for job in ("dns", "proxy"):
            (self.la / f"com.mybrain.ls.locove.{job}.plist").write_text("<plist/>")
        boots = []

        def fake_sub(cmd, **kw):
            if "bootout" in cmd:
                boots.append(cmd)
            return _completed()

        with patch.object(locove_optin.subprocess, "run", side_effect=fake_sub):
            res = locove_optin.disable()
        self.assertTrue(res["ok"])
        self.assertFalse(Registry().locove_settings()["use_domains"])
        self.assertEqual(len(boots), 2, "dns 与 proxy agent 都要 bootout")
        self.assertTrue((self.la / "com.mybrain.ls.locove.dns.plist").exists(),
                        "关闭保留安装件（状态=已安装未开启），卸载是独立动作")
        self.assertTrue((self.resolver_dir / "locove").exists(), "resolver 保留")


class SettingsPortTest(OptInEnv):
    def test_domain_port_validation(self):
        self.assertTrue(Registry.locove_port_valid(5670))
        self.assertTrue(Registry.locove_port_valid(6789))
        for bad in (True, "5670", 80, 1023, 65536, 47800, None):
            self.assertFalse(Registry.locove_port_valid(bad), f"{bad!r} 不应通过")
        with self.assertRaises(Exception):
            Registry().locove_settings_update({"domain_port": 80})

    def test_settings_roundtrip_port(self):
        st = Registry().locove_settings_update({"domain_port": 6789})
        self.assertEqual(st["domain_port"], 6789)
        self.assertEqual(Registry().locove_settings()["domain_port"], 6789)


class AdminRunQuoteTest(unittest.TestCase):
    def test_prompt_and_command_escaped(self):
        """osascript 组装：命令 shlex 转义、提示语转义——中文提示不破坏 AppleScript。"""
        with patch.object(locove_optin, "_run", return_value=_completed()) as r:
            res = locove_optin.admin_run(["bash", "/path/with space/x.sh", "--a b"],
                                         prompt='安装 "仅 .locove" 的解析')
        self.assertTrue(res["ok"])
        apple = r.call_args[0][0][2]
        self.assertIn("bash '/path/with space/x.sh' '--a b'", apple)  # shlex 转义
        self.assertIn("with administrator privileges", apple)
        self.assertIn('prompt "安装 \\"仅 .locove\\" 的解析"', apple)  # 提示语双引号转义


if __name__ == "__main__":
    unittest.main()
