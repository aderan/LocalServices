"""review R5：派生域名声明的编辑往返保真。

复现（review 原文）：未声明 endpoints 的服务在 custom 模式没有路由；读取 definition、
仅改 description 并保存后，derived 变 False，custom 模式开始生成 sample.locove——
普通文案编辑意外改变网络行为。

修复语义：
- GET /api/services/<sid>/definition 的 endpoints 行带 derived 标记。
- 注册端：既有服务无显式入口时，payload 原样带回的入口与「稳定派生」一致 → 不固化
  为显式 [[endpoints]]（无关字段编辑保持派生态）。
- 用户真正编辑入口（host/组件/落点变化）→ 固化为显式声明。
- 全新注册显式提供的 endpoints 照常写显式（不因碰巧等于派生而丢弃）。"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import locove, register_api  # noqa: E402
from localservices.registry import Registry  # noqa: E402


class RoundtripEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-r5-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "home"),
            "LOCALSERVICES_SERVICES": str(self.root / "services"),
            "LOCALSERVICES_USER_SERVICES": str(self.root / "user-services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-r5",
        })
        env.start()
        self.addCleanup(env.stop)
        for d in ("home", "services", "user-services", "src"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.reg = Registry()

    def register(self, service: dict, revision: int = 0):
        return self.reg.register_service({"schemaVersion": 1, "revision": revision,
                                          "service": service})

    @staticmethod
    def svc(endpoints=None, description="样例服务"):
        s = {"id": "sample", "name": "Sample", "source": None,
             "components": [{"id": "web", "cmd": ["/bin/sleep", "10"], "cwd": ".",
                             "port": 30850, "ready": {"tcp": "127.0.0.1:30850"}}],
             "description": description}
        if endpoints is not None:
            s["endpoints"] = endpoints
        return s

    def register_fresh(self, **kw):
        src = self.root / "src"
        payload = self.svc(**kw)
        payload["source"] = str(src)
        res = self.register(payload, revision=1)
        assert res.get("ok"), res
        return res

    def definition(self, sid="sample"):
        code, res = register_api.handle_service_definition(sid)
        assert code == 200, res
        return res["definition"]

    def user_toml(self, sid="sample") -> dict:
        return tomllib.loads((self.root / "user-services" / f"{sid}.toml").read_text())


class DefinitionDerivedFlagTest(RoundtripEnv):
    def test_definition_marks_derived_endpoints(self):
        self.register_fresh()  # 未提供 endpoints → 派生
        d = self.definition()
        self.assertEqual(len(d["endpoints"]), 1)
        self.assertTrue(d["endpoints"][0]["derived"])
        self.assertEqual(d["endpoints"][0]["host"], "sample.locove")

    def test_definition_marks_explicit_endpoints(self):
        self.register_fresh(endpoints=[{"host": "mysite.locove", "component": "web",
                                        "open_path": "/x"}])
        d = self.definition()
        self.assertFalse(d["endpoints"][0]["derived"])
        self.assertEqual(d["endpoints"][0]["host"], "mysite.locove")


class EditRoundtripTest(RoundtripEnv):
    def test_description_edit_keeps_derived_declaration(self):
        """R5 主复现：definition 往返只改 description → TOML 仍无 [[endpoints]]，
        custom 模式仍无路由（网络行为不因文案编辑改变）。"""
        self.register_fresh()
        rev = self.reg.project("sample")["revision"]
        payload = copy.deepcopy(self.definition())
        payload["description"] = "只改了描述"
        res = self.register(payload, revision=rev)
        self.assertTrue(res.get("ok"), res)
        self.assertNotIn("endpoints", self.user_toml(), "派生入口被固化为显式声明")
        # custom 模式：无显式入口 → 无路由
        loaded = __import__("localservices.manifest", fromlist=["load_all"]).load_all()
        routes = locove.desired_routes(dict(loaded), None, {"domain_form": "custom"})
        self.assertNotIn("sample.locove", routes)

    def test_real_entry_edit_persists_explicit(self):
        """用户真正编辑入口（host 变化）→ 固化为显式；custom 模式获得路由。"""
        self.register_fresh()
        rev = self.reg.project("sample")["revision"]
        payload = copy.deepcopy(self.definition())
        payload["endpoints"][0]["host"] = "renamed.locove"
        res = self.register(payload, revision=rev)
        self.assertTrue(res.get("ok"), res)
        t = self.user_toml()
        self.assertIn("endpoints", t)
        self.assertEqual(t["endpoints"][0]["host"], "renamed.locove")
        loaded = __import__("localservices.manifest", fromlist=["load_all"]).load_all()
        self.assertFalse(loaded["sample"].endpoints[0].derived)
        routes = locove.desired_routes(dict(loaded), None, {"domain_form": "custom"})
        self.assertIn("renamed.locove", routes)

    def test_fresh_explicit_registration_kept_even_if_equals_derivation(self):
        """全新注册显式提供入口（即使碰巧等于派生形式）→ 尊重用户声明，写显式。"""
        self.register_fresh(endpoints=[{"host": "sample.locove", "component": "web",
                                        "open_path": "/"}])
        self.assertIn("endpoints", self.user_toml())
        d = self.definition()
        self.assertFalse(d["endpoints"][0]["derived"])

    def test_existing_explicit_not_downgraded_by_roundtrip(self):
        """已有显式入口的服务：编辑往返保持显式（不因等价检查被降级回派生）。"""
        self.register_fresh(endpoints=[{"host": "sample.locove", "component": "web",
                                        "open_path": "/x"}])
        rev = self.reg.project("sample")["revision"]
        payload = copy.deepcopy(self.definition())
        payload["description"] = "再次编辑"
        res = self.register(payload, revision=rev)
        self.assertTrue(res.get("ok"), res)
        t = self.user_toml()
        self.assertEqual(t["endpoints"][0]["open_path"], "/x")

    def test_dry_run_diff_shows_no_phantom_endpoint_change(self):
        """dry_run 预览与实际一致：无关编辑的 diff 不得出现「新增公开入口」。"""
        self.register_fresh()
        rev = self.reg.project("sample")["revision"]
        payload = copy.deepcopy(self.definition())
        payload["description"] = "预览检查"
        res = self.reg.register_service({"schemaVersion": 1, "revision": rev,
                                         "service": payload}, dry_run=True)
        self.assertTrue(res.get("ok"), res)
        fields = [d.get("field") for d in res.get("diff", [])]
        self.assertNotIn("endpoints[sample.locove]", str(fields))
        self.assertIn("description", fields)


if __name__ == "__main__":
    unittest.main()
