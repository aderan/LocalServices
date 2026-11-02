"""V2.2 恢复测试：服务/项目恢复、intent 显式设定、目标项目重建、内置来源入用户层。

覆盖：
- 默认 stopped intent（绝不从归档带回 running、不自动启动）；显式 running 只设状态
- 目标项目缺省原 id，不存在则以归档名重建（目录随归档 project 对象恢复）
- 同 id 已登记 → 409；端口冲突 → 409
- builtin 来源恢复写 user_services_dir 且遮蔽条目保留（V2.0 修订）
- 项目恢复：条目 + 全部成员；tombstone 清理；removed 列表联动
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

from localservices import manifest, register_api, tombstones  # noqa: E402
from localservices.registry import Registry, RegistryError  # noqa: E402

PY = sys.executable


class _SimpleCtl:
    ops: dict = {}
    projects: dict = {}

    def reload(self):
        pass

    def label(self, p, c):
        return f"label.{p.id}.{c.id}"

    def stop(self, sid):
        return {"ok": True, "accepted": False, "message": "未运行"}

    def release_service(self, sid):
        return {"ok": True, "released": [f"{sid}.web"], "errors": []}


class RestoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsrs2-")
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
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsrs2",
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = Registry()

    def payload(self, sid="demo", revision=None, port=48601, project="p1", deps=None):
        svc = {"id": sid, "name": sid.title(), "source": str(self.src),
               "components": [{"id": "web", "kind": "server",
                               "cmd": [PY, "-c", f"print('{sid}')"], "cwd": ".",
                               "port": port}]}
        if deps:
            svc["dependencies"] = deps
        body = {"schemaVersion": 1, "project": {"id": project, "name": project.upper()},
                "service": svc}
        if revision is not None:
            body["revision"] = revision
        return body

    def register(self, sid="demo", port=48601, project="p1", revision=None, deps=None):
        if revision is None:  # 缺省取当前项目 revision（0=仅当不存在时创建）
            proj = self.reg.project(project)
            revision = proj["revision"] if proj else 0
        res = self.reg.register_service(self.payload(sid=sid, port=port, project=project,
                                                     revision=revision, deps=deps))
        self.assertTrue(res["ok"], res)
        return res

    def remove(self, sid="demo", disposition="keep-external"):
        res = self.reg.remove_service(sid, disposition, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        return res

    def state_json(self) -> dict:
        return json.loads((self.home / "state.json").read_text(encoding="utf-8"))


class RestoreServiceTest(RestoreBase):
    def test_restore_default_stopped_intent(self):
        """缺省 intent=stopped：不从归档自动带回 running，恢复本身不启动。"""
        self.register()
        # 归档里带 running 状态快照（恢复绝不能采用它作为 intent）
        (self.home / "state.json").write_text(
            json.dumps({"projects": {"demo": {"intent": "running"}}}), encoding="utf-8")
        self.remove()
        res = self.reg.restore_service("demo", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["intent"], "stopped")
        self.assertTrue((self.user_services / "demo.toml").exists())
        self.assertIn("demo", manifest.load_all())
        self.assertEqual(self.reg.project("p1")["services"], ["demo"])
        state = self.state_json()["projects"]["demo"]
        self.assertEqual(state["intent"], "stopped", "intent 显式设为 stopped")
        self.assertEqual(state["intent_by"], "restore")
        self.assertNotIn("demo", tombstones.services(), "纯用户层 id 恢复后解除 tombstone")

    def test_restore_explicit_running_sets_intent_only(self):
        self.register()
        self.remove()
        ctl = _SimpleCtl()
        started = []
        ctl.start = lambda sid: started.append(sid)  # 恢复绝不调用 start
        res = self.reg.restore_service("demo", {"intent": "running"}, ctl)
        self.assertEqual(res["intent"], "running")
        self.assertEqual(self.state_json()["projects"]["demo"]["intent"], "running")
        self.assertEqual(started, [], "restore 不自动启动（reconcile 稍后按 intent 拉起）")

    def test_restore_recreates_project_with_archived_name_and_dirs(self):
        self.reg.ensure_project("p1", "P1", None, None)
        (self.root / "projdir").mkdir()
        self.reg.add_directory("p1", str(self.root / "projdir"), role="docs")
        self.register()
        self.reg.remove_project("p1", "stop", _SimpleCtl())  # 连项目一起移除
        self.assertIsNone(self.reg.project("p1"))
        res = self.reg.restore_service("demo", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["project_recreated"])
        proj = self.reg.project("p1")
        self.assertEqual(proj["name"], "P1", "以归档名重建")
        self.assertEqual({d["role"] for d in proj["directories"]}, {"docs", "source"},
                         "目录从归档 project 对象恢复")
        self.assertEqual(proj["services"], ["demo"])

    def test_restore_already_registered_409(self):
        self.register()
        res = self.remove()
        # 同 id 已重新在登记层存在（直接落用户层文件，模拟外部导入/手工恢复）
        archive = json.loads((self.home / "removed" / res["archive"]).read_text(encoding="utf-8"))
        (self.user_services / "demo.toml").write_text(archive["toml"], encoding="utf-8")
        self.assertIn("demo", manifest.load_all())
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_service("demo", {}, _SimpleCtl())
        self.assertEqual(cm.exception.status, 409)
        status, body = register_api.handle_restore_service("demo", {}, _SimpleCtl())
        self.assertEqual(status, 409)

    def test_restore_unknown_404(self):
        status, body = register_api.handle_restore_service("ghost", {}, None)
        self.assertEqual(status, 404)

    def test_restore_port_conflict_409(self):
        self.register(sid="svc-a", port=48601)
        self.remove("svc-a")
        # 期间别的服务占用了同一端口
        self.register(sid="svc-b", port=48601, revision=2)
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_service("svc-a", {}, _SimpleCtl())
        self.assertEqual(cm.exception.status, 409)
        self.assertFalse((self.user_services / "svc-a.toml").exists(), "冲突时零写入")
        self.assertIn("svc-a", tombstones.services())

    def test_restore_brings_back_overrides(self):
        p = self.payload()
        p["service"]["name"] = "我的名字"
        p["service"]["edited"] = ["name"]
        self.assertTrue(self.reg.register_service(p)["ok"])
        self.remove()
        res = self.reg.restore_service("demo", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        from localservices import overrides
        self.assertEqual(overrides.fields("demo"), {"name": "我的名字"}, "覆盖层随恢复带回")


class BuiltinRestoreTest(RestoreBase):
    def setUp(self):
        super().setUp()
        self.builtin_toml = (f'id = "shadow"\nname = "Shadow"\nsource = "{self.src}"\n'
                             f'project = "pb"\n\n[[components]]\nid = "web"\nkind = "server"\n'
                             f'cmd = ["true"]\ncwd = "."\nport = 48651\n')
        (self.services / "shadow.toml").write_text(self.builtin_toml, encoding="utf-8")

    def test_builtin_origin_restores_into_user_layer(self):
        res = self.reg.remove_service("shadow", "keep-external", None)
        self.assertTrue(res["builtin"])
        r = self.reg.restore_service("shadow", {}, _SimpleCtl())
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["builtin_shadow"], "遮蔽条目保留（内置模板继续被遮蔽）")
        self.assertTrue((self.user_services / "shadow.toml").exists(), "恢复写入用户层")
        self.assertEqual((self.services / "shadow.toml").read_text(encoding="utf-8"),
                         self.builtin_toml, "内置模板字节不动")
        loaded = manifest.load_all()
        self.assertEqual(len([p for p in loaded.values() if p.id == "shadow"]), 1)
        self.assertEqual(str(loaded["shadow"].file), str(self.user_services / "shadow.toml"))
        self.assertIn("shadow", tombstones.services())


class RestoreProjectTest(RestoreBase):
    def test_project_restore_full(self):
        self.register(sid="svc-a", port=48601)
        self.register(sid="svc-b", port=48602, revision=1)
        removed = self.reg.remove_project("p1", "stop", _SimpleCtl())
        self.assertTrue(removed["ok"])
        self.assertIsNone(self.reg.project("p1"))
        res = self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["services"], ["svc-a", "svc-b"])
        proj = self.reg.project("p1")
        self.assertEqual(proj["name"], "P1", "归档原 project 对象重建")
        self.assertEqual(sorted(proj["services"]), ["svc-a", "svc-b"])
        loaded = manifest.load_all()
        self.assertIn("svc-a", loaded)
        self.assertIn("svc-b", loaded)
        for sid in ("svc-a", "svc-b"):
            self.assertTrue((self.user_services / f"{sid}.toml").exists())
            self.assertEqual(self.state_json()["projects"][sid]["intent"], "stopped")
        self.assertEqual(tombstones.services(), set(), "成员 tombstone 全部解除")
        self.assertEqual(tombstones.projects(), set(), "项目 tombstone 解除")
        self.assertEqual(self.reg.removed_list()["services"], [])

    def test_project_restore_conflict_member_409(self):
        self.register(sid="svc-a", port=48601)
        self.reg.remove_project("p1", "stop", _SimpleCtl())
        self.register(sid="svc-a", port=48601, project="p2")  # 成员已被单独重新登记
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("svc-a", str(cm.exception))

    def test_project_restore_unknown_404(self):
        status, body = register_api.handle_restore_project("ghost", {}, None)
        self.assertEqual(status, 404)

    def test_project_restore_explicit_running_intent(self):
        self.register(sid="svc-a", port=48601)
        self.reg.remove_project("p1", "stop", _SimpleCtl())
        res = self.reg.restore_project("p1", {"intent": "running"}, _SimpleCtl())
        self.assertEqual(res["intent"], "running")
        self.assertEqual(self.state_json()["projects"]["svc-a"]["intent"], "running")


if __name__ == "__main__":
    unittest.main()
