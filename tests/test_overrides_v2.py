"""V2.3 覆盖层测试：人工覆盖优先于重新识别值，dry_run 预览 = 实际。

覆盖：
- edited 路径随注册落覆盖层；覆盖在新注册（created/updated）时优先生效
- dry_run diff 在应用覆盖后计算（预览 = 实际）
- accept_new 指定路径 / "*" 显式采用新值
- 组件字段覆盖（components[api].port）
- get / clear / snapshot / restore 与 GET /api/services/<sid>/overrides 端点
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

from localservices import manifest, overrides, register_api  # noqa: E402
from localservices.registry import Registry  # noqa: E402

PY = sys.executable


class OverrideBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsov2-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        self.src = self.root / "src"
        for d in (self.home, self.services, self.user_services, self.src):
            d.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsov2",
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = Registry()

    def payload(self, sid="demo", revision=None, port=48401, name=None, description=None,
                extra_svc=None, comps_over=None):
        # cmd 固定（不含端口参数）：identity 不随 port 变化，便于覆盖层断言
        comp = {"id": "api", "kind": "server",
                "cmd": [PY, "-m", "http.server"], "cwd": ".", "port": port}
        comp.update(comps_over or {})
        svc = {"id": sid, "name": name or sid.title(), "source": str(self.src),
               "components": [comp]}
        if description is not None:
            svc["description"] = description
        svc.update(extra_svc or {})
        body = {"schemaVersion": 1, "project": {"id": "p1", "name": "P1"}, "service": svc}
        if revision is not None:
            body["revision"] = revision
        return body


class RecordAndApplyTest(OverrideBase):
    def test_edited_fields_recorded_on_created(self):
        res = self.reg.register_service(self.payload(
            name="我的名字", description="手工描述", extra_svc={"edited": ["name", "description"]}))
        self.assertEqual(res["action"], "created")
        flds = overrides.fields("demo")
        self.assertEqual(flds.get("name"), "我的名字")
        self.assertEqual(flds.get("description"), "手工描述")
        self.assertTrue((self.home / "overrides.json").exists())

    def test_override_wins_on_reregister(self):
        """用户覆盖优先生效：重新识别的新 name/description 不落盘（action=unchanged）。"""
        self.reg.register_service(self.payload(
            name="我的名字", description="手工描述", extra_svc={"edited": ["name", "description"]}))
        res = self.reg.register_service(self.payload(revision=1, name="识别名", description="识别描述"))
        self.assertEqual(res["action"], "unchanged", res)
        p = manifest.load_all()["demo"]
        self.assertEqual(p.name, "我的名字")
        self.assertEqual(p.description, "手工描述")

    def test_dryrun_diff_reflects_overrides(self):
        """dry_run 在应用覆盖后计算：diff 不显示被覆盖字段，实际注册也不改（预览=实际）。"""
        self.reg.register_service(self.payload(
            name="我的名字", extra_svc={"edited": ["name"]}))
        d = self.reg.register_service(self.payload(revision=1, name="识别名"), dry_run=True)
        self.assertEqual(d["action"], "diff")
        self.assertEqual([x["field"] for x in d["diff"]], [], "覆盖字段不得出现在 diff")
        real = self.reg.register_service(self.payload(revision=1, name="识别名"))
        self.assertEqual(real["action"], "unchanged")

    def test_override_only_when_recorded(self):
        """未标记 edited 的普通更新仍按 payload 落盘（重命名是合法流）。"""
        self.reg.register_service(self.payload(name="旧名"))
        res = self.reg.register_service(self.payload(revision=1, name="新名"))
        self.assertEqual(res["action"], "updated")
        self.assertEqual(manifest.load_all()["demo"].name, "新名")
        self.assertEqual(overrides.fields("demo"), {})


class AcceptNewTest(OverrideBase):
    def _register_with_overrides(self):
        self.reg.register_service(self.payload(
            name="我的名字", description="手工描述", extra_svc={"edited": ["name", "description"]}))

    def test_accept_new_specific_path(self):
        self._register_with_overrides()
        res = self.reg.register_service(self.payload(
            revision=1, name="新识别名", description="新描述",
            extra_svc={"accept_new": ["name"]}))
        self.assertTrue(res["ok"], res)
        p = manifest.load_all()["demo"]
        self.assertEqual(p.name, "新识别名", "accept_new 的字段采用新值")
        self.assertEqual(p.description, "手工描述", "未 accept 的覆盖继续生效")
        flds = overrides.fields("demo")
        self.assertNotIn("name", flds)
        self.assertEqual(flds.get("description"), "手工描述")

    def test_accept_new_star(self):
        self._register_with_overrides()
        res = self.reg.register_service(self.payload(
            revision=1, name="新名", description="新描述", extra_svc={"accept_new": ["*"]}))
        self.assertTrue(res["ok"], res)
        p = manifest.load_all()["demo"]
        self.assertEqual((p.name, p.description), ("新名", "新描述"))
        self.assertEqual(overrides.fields("demo"), {})


class ComponentOverrideTest(OverrideBase):
    def test_component_port_override_roundtrip(self):
        """components[api].port 覆盖：编辑值持久化，重新识别的端口不回写。"""
        res = self.reg.register_service(self.payload(
            port=48402, comps_over={"port": 48402},
            extra_svc={"edited": ["components[api].port"]}))
        self.assertEqual(res["action"], "created")
        self.assertEqual(overrides.fields("demo").get("components[api].port"), 48402)
        res2 = self.reg.register_service(self.payload(revision=1, port=48403,
                                                      comps_over={"port": 48403}))
        self.assertEqual(res2["action"], "unchanged", res2)
        self.assertEqual(manifest.load_all()["demo"].component("api").port, 48402)

    def test_unknown_override_paths_ignored(self):
        overrides.record("demo", ["no.such.path"],
                         {"name": "X", "components": [{"id": "api"}]})
        self.assertEqual(overrides.fields("demo"), {})


class _SimpleCtl:
    """最小 fake：无 launchd 记录、release 成功。"""

    ops: dict = {}
    projects: dict = {}

    def reload(self):
        pass

    def label(self, p, c):
        return f"label.{p.id}.{c.id}"

    def stop(self, sid):
        return {"ok": True, "accepted": False, "message": "未运行"}

    def release_service(self, sid):
        return {"ok": True, "released": [f"{sid}.api"], "errors": []}


class LifecycleApiTest(OverrideBase):
    def test_get_clear_snapshot_restore(self):
        self.reg.register_service(self.payload(
            name="N1", extra_svc={"edited": ["name"]}))
        snap = overrides.snapshot("demo")
        self.assertEqual(snap["fields"], {"name": "N1"})
        overrides.clear("demo", ["name"])
        self.assertEqual(overrides.fields("demo"), {})
        overrides.restore("demo", snap)
        self.assertEqual(overrides.fields("demo"), {"name": "N1"})
        overrides.clear("demo")  # 缺省 = 全部
        self.assertEqual(overrides.fields("demo"), {})

    def test_overrides_endpoint(self):
        self.reg.register_service(self.payload(name="N1", extra_svc={"edited": ["name"]}))
        status, body = register_api.handle_overrides_get("demo")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["sid"], "demo")
        self.assertEqual(body["fields"], {"name": "N1"})
        status, body = register_api.handle_overrides_get("ghost")
        self.assertEqual((status, body["fields"]), (200, {}))

    def test_removal_clears_live_overrides(self):
        """移除时覆盖层随归档、活跃层清空（restore 测试验证带回）。"""
        self.reg.register_service(self.payload(name="N1", extra_svc={"edited": ["name"]}))
        self.reg.remove_service("demo", "keep-external", _SimpleCtl())
        self.assertEqual(overrides.fields("demo"), {})
        archive = json_load(next((self.home / "removed").glob("demo-*.json")))
        self.assertEqual(archive["overrides"]["fields"], {"name": "N1"})


def json_load(p: Path) -> dict:
    import json
    return json.loads(p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
