"""V2.0/V2.1/V2.2 移除测试：tombstone + 归档 + 内置遮蔽 + 项目级移除。

覆盖：
- 预览零副作用；取消后真实移除流程
- 用户层服务：归档（TOML 字节相等）→ 删除 → tombstone → registry 去成员 → release
- keep-external 在 launchd 仍保活时 409 拒绝（登记不变）；stop 路径既有语义保持
- release_service 失败 → 409 登记保留；不在任何项目也照常移除
- 同 id 重注册解除 tombstone；daemon 重启（重新 load_all）不复活
- 内置模板：字节不变、遮蔽、重注册接管（V2.0 修订）、再移除再遮蔽
- 项目移除：空项目直删；全成员停止流；成员失败全体保留；dry_run 零写入；资源引用 shared
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
from localservices import registry as reg  # noqa: E402
from localservices.registry import Registry, RegistryError  # noqa: E402

PY = sys.executable


class FakeJI:
    def __init__(self, running=False, loaded=False):
        self.running, self.loaded, self.pid, self.last_exit = running, loaded, None, None


class FakeComp:
    def __init__(self, cid):
        self.id = cid


class FakeProj:
    def __init__(self, sid, comps=("api", "web")):
        self.id = sid
        self.components = [FakeComp(c) for c in comps]

    def managed_components(self):
        return self.components


class FakeCtl:
    """controller 假体：stop/release 可注入结果；label/launchd 查询走 patch。"""

    def __init__(self, stop_ok=True, release_ok=True, fail_stop_for=()):
        self.ops: dict = {}
        self.stopped: list[str] = []
        self.released: list[str] = []
        self.stop_ok = stop_ok
        self.release_ok = release_ok
        self.fail_stop_for = set(fail_stop_for)
        self.projects: dict[str, FakeProj] = {}
        self.snapshot_data = {"projects": []}

    def reload(self):
        pass

    def snapshot(self):
        return self.snapshot_data

    def label(self, p, c):
        return f"com.mybrain.lsrm.{p.id}.{c.id}"

    def stop(self, sid):
        self.stopped.append(sid)
        if sid in self.fail_stop_for:
            return {"ok": False, "error": f"停止失败：{sid}"}
        return {"ok": True, "accepted": False, "message": "已停止"}

    def release_service(self, sid):
        self.released.append(sid)
        if not self.release_ok:
            return {"ok": False, "released": [], "errors": ["bootout 失败"]}
        return {"ok": True, "released": [f"{sid}.api", f"{sid}.web"], "errors": []}

    def external_instances(self, p):
        return [{"pid": 4312, "cmd": f"/usr/local/bin/{p.id}-server", "cwd": str(p.source)}]


class RemovalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsrm2-")
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
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsrm2",
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = Registry()

    def payload(self, sid="demo", revision=None, port=48501, project="p1", name=None):
        # cmd 随 sid 变化：identity = source+cwd 集合+cmd 集合，同源多服务必须可区分
        svc = {"id": sid, "name": name or sid.title(), "source": str(self.src),
               "components": [{"id": "web", "kind": "server",
                               "cmd": [PY, "-c", f"print('{sid}')"], "cwd": ".", "port": port}]}
        body = {"schemaVersion": 1, "project": {"id": project, "name": project.upper()},
                "service": svc}
        if revision is not None:
            body["revision"] = revision
        return body

    def register(self, sid="demo", port=48501, project="p1", name=None, revision=None):
        res = self.reg.register_service(self.payload(sid=sid, port=port, project=project,
                                                     name=name, revision=revision))
        self.assertTrue(res["ok"], res)
        return res

    def removed_json(self) -> dict:
        return json.loads((self.home / "removed.json").read_text(encoding="utf-8"))

    def core_bytes(self):
        """registry + 两个清单目录的字节指纹（预览/中止断言用）。"""
        parts = []
        for d in (self.services, self.user_services):
            for f in sorted(d.glob("*.toml")):
                parts.append((str(f), f.read_bytes()))
        rf = self.home / "registry.json"
        parts.append(("reg", rf.read_bytes() if rf.exists() else b""))
        rm = self.home / "removed.json"
        parts.append(("removed", rm.read_bytes() if rm.exists() else b""))
        return sorted(parts)


class ServiceRemoveTest(RemovalBase):
    def test_preview_zero_side_effects(self):
        self.register()
        before = self.core_bytes()
        ctl = FakeCtl()
        ctl.projects["demo"] = FakeProj("demo", comps=("web",))
        res = self.reg.service_remove_preview("demo", ctl)
        self.assertTrue(res["ok"])
        self.assertEqual(res["service_id"], "demo")
        for key in ("name", "state", "running_components", "external_instances", "keep",
                    "dispositions", "restore_hint"):
            self.assertIn(key, res)
        self.assertTrue(res["dispositions"]["keep-external"]["enabled"])
        self.assertIn(str(self.src.resolve()), res["keep"])
        self.assertEqual(self.core_bytes(), before, "预览零副作用")
        status, body = register_api.handle_service_remove_preview("demo", ctl)
        self.assertEqual(status, 200)

    def test_preview_unknown_404(self):
        status, body = register_api.handle_service_remove_preview("ghost", None)
        self.assertEqual(status, 404)

    def test_remove_user_service_full_flow(self):
        """归档（TOML 原文）→ 删用户层 → tombstone → registry 去成员 → release（V2.1）。"""
        self.register()
        toml_path = self.user_services / "demo.toml"
        original = toml_path.read_bytes()
        ctl = FakeCtl()
        res = self.reg.remove_service("demo", "keep-external", ctl)
        self.assertTrue(res.get("ok") if isinstance(res, dict) else True, res)
        self.assertEqual(res["service_id"], "demo")
        self.assertEqual(res["project_id"], "p1")
        self.assertTrue(res["archived"])
        self.assertFalse(res["builtin"])
        self.assertEqual(res["released"], ["demo.api", "demo.web"])
        self.assertEqual(ctl.released, ["demo"])
        # 归档：完整 TOML 文本 + 快照
        archive_path = self.home / "removed" / res["archive"]
        self.assertTrue(archive_path.exists())
        doc = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["sid"], "demo")
        self.assertEqual(doc["project_id"], "p1")
        self.assertEqual(doc["disposition"], "keep-external")
        self.assertEqual(doc["toml"].encode("utf-8"), original, "归档 TOML 字节相等")
        self.assertEqual(doc["state"], {})
        self.assertEqual(doc["overrides"], {})
        # 用户层删除 + tombstone + registry 去成员 + 不再加载
        self.assertFalse(toml_path.exists())
        self.assertIn("demo", self.removed_json()["services"])
        self.assertEqual(self.reg.project("p1")["services"], [])
        self.assertNotIn("demo", manifest.load_all(), "daemon 重启（重新 load_all）不复活")
        self.assertEqual(list((self.home / "removed").glob("demo-*.json")), [archive_path])

    def test_remove_state_snapshot_and_run_records(self):
        """state.json 该 sid 条目快照进归档；release 由 ctl 完成（断言调用）。"""
        self.register()
        state = {"projects": {"demo": {"intent": "running", "intent_at": 1.0,
                                       "components": {"web": {"pid": 9}}}}}
        (self.home / "state.json").write_text(json.dumps(state), encoding="utf-8")
        ctl = FakeCtl()
        res = self.reg.remove_service("demo", "keep-external", ctl)
        doc = json.loads((self.home / "removed" / res["archive"]).read_text(encoding="utf-8"))
        self.assertEqual(doc["state"]["intent"], "running")
        self.assertEqual(ctl.released, ["demo"], "runtime 对象（state 条目/run 记录）经 ctl 释放")

    def test_release_failure_keeps_registration(self):
        self.register()
        toml_path = self.user_services / "demo.toml"
        before = self.core_bytes()
        ctl = FakeCtl(release_ok=False)
        with self.assertRaises(RegistryError) as cm:
            self.reg.remove_service("demo", "keep-external", ctl)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.core_bytes(), before, "释放失败：登记原样保留（无归档/tombstone）")
        self.assertTrue(toml_path.exists())

    def test_keep_external_refused_while_launchd_alive(self):
        """组件仍由本工具 launchd 保活 → 409「无法保留为外部实例」，登记不变。"""
        self.register()
        before = self.core_bytes()
        ctl = FakeCtl()
        ctl.projects["demo"] = FakeProj("demo", comps=("web",))
        from localservices import launchd
        with patch.object(launchd, "info", return_value=FakeJI(running=True, loaded=True)):
            with self.assertRaises(RegistryError) as cm:
                self.reg.remove_service("demo", "keep-external", ctl)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("无法保留为外部实例", str(cm.exception))
        self.assertIn("launchd", str(cm.exception))
        self.assertEqual(self.core_bytes(), before, "拒绝时登记不变")
        # 外部运行/已停止时正常移除（launchd 无记录）
        with patch.object(launchd, "info", return_value=FakeJI()):
            res = self.reg.remove_service("demo", "keep-external", ctl)
        self.assertTrue(res["ok"])

    def test_stop_verified_refusal_kept(self):
        """stop 处置：停止未完成 → 409 登记保留（既有语义）。"""
        self.register()
        ctl = FakeCtl()
        ctl.projects["demo"] = FakeProj("demo", comps=("web",))
        from localservices import launchd
        with patch.object(launchd, "info", return_value=FakeJI(running=True, loaded=True)):
            with self.assertRaises(RegistryError) as cm:
                self.reg.remove_service("demo", "stop", ctl)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("仍在运行", str(cm.exception))
        self.assertTrue((self.user_services / "demo.toml").exists())

    def test_cancel_then_real_flow(self):
        """GUI 流：预览（取消）→ 再次预览 → 真实移除；行为一致且幂等。"""
        self.register()
        ctl = FakeCtl()
        preview1 = self.reg.service_remove_preview("demo", ctl)
        preview2 = self.reg.service_remove_preview("demo", ctl)
        self.assertEqual(preview1["service_id"], preview2["service_id"])
        res = self.reg.remove_service("demo", "keep-external", ctl)
        self.assertTrue(res["ok"])
        with self.assertRaises(RegistryError) as cm:  # 幂等重试：未知服务 404
            self.reg.remove_service("demo", "keep-external", ctl)
        self.assertEqual(cm.exception.status, 404)

    def test_reregister_clears_tombstone(self):
        self.register()
        self.reg.remove_service("demo", "keep-external", FakeCtl())
        self.assertIn("demo", tombstones.services())
        res = self.register(revision=2)  # 同 id 重新注册（显式重新添加，带当前 revision）
        self.assertEqual(res["action"], "created")
        self.assertNotIn("demo", tombstones.services(), "纯用户层 id 重注册解除 tombstone")
        self.assertIn("demo", manifest.load_all())

    def test_remove_service_without_project_membership(self):
        """不在任何项目（owner=None）也照常：tombstone + 归档 + 删用户 TOML。"""
        self.register()
        data = self.reg.data()
        data["projects"][0]["services"] = []
        self.reg._save(data)  # 直接去掉成员关系，模拟孤儿登记
        res = self.reg.remove_service("demo", "keep-external", FakeCtl())
        self.assertTrue(res["ok"])
        self.assertEqual(res["project_id"], "p1", "归档 project_id 取 TOML 声明值")
        self.assertFalse((self.user_services / "demo.toml").exists())
        self.assertIn("demo", self.removed_json()["services"])


class BuiltinShadowTest(RemovalBase):
    """V2.0 修订：tombstone 只遮蔽内置目录模板；用户层同 id 文件始终加载。"""

    def setUp(self):
        super().setUp()
        self.builtin_toml = (f'id = "shadow"\nname = "Shadow"\nsource = "{self.src}"\n'
                             f'project = "pb"\n\n[[components]]\nid = "web"\nkind = "server"\n'
                             f'cmd = ["true"]\ncwd = "."\nport = 48551\n')
        (self.services / "shadow.toml").write_text(self.builtin_toml, encoding="utf-8")

    def test_remove_builtin_keeps_file_and_shadows(self):
        res = self.reg.remove_service("shadow", "keep-external", None)
        self.assertTrue(res["ok"])
        self.assertTrue(res["builtin"])
        self.assertEqual((self.services / "shadow.toml").read_text(encoding="utf-8"),
                         self.builtin_toml, "随仓模板字节不变")
        self.assertNotIn("shadow", manifest.load_all(), "登记层失效（遮蔽）")
        self.assertIn("shadow", tombstones.services())
        # daemon 重启（全新 load_all）仍不复活
        self.assertNotIn("shadow", manifest.load_all())

    def test_reregister_shadow_takes_over_builtin(self):
        self.reg.remove_service("shadow", "keep-external", None)
        res = self.reg.register_service(self.payload(sid="shadow", port=48552, project="pb"))
        self.assertEqual(res["action"], "created")
        self.assertTrue((self.user_services / "shadow.toml").exists())
        self.assertTrue((self.services / "shadow.toml").exists(), "内置模板仍在磁盘")
        loaded = manifest.load_all()
        self.assertIn("shadow", loaded, "用户层接管，无 duplicate id 错误")
        self.assertEqual(len([1 for p in loaded.values() if p.id == "shadow"]), 1)
        self.assertEqual(str(loaded["shadow"].file), str(self.user_services / "shadow.toml"))
        # 遮蔽条目保留（GET /api/removed 过滤掉已登记的）
        self.assertIn("shadow", tombstones.services())
        self.assertEqual([s["sid"] for s in self.reg.removed_list()["services"]], [],
                         "已登记（load_all 含该 id）的条目不再出现在 removed 列表")

    def test_remove_again_shadowed_again(self):
        self.reg.remove_service("shadow", "keep-external", None)
        self.reg.register_service(self.payload(sid="shadow", port=48552, project="pb"))
        res = self.reg.remove_service("shadow", "keep-external", None)
        self.assertTrue(res["ok"])
        self.assertFalse(res["builtin"], "第二次移除的是用户层文件")
        self.assertFalse((self.user_services / "shadow.toml").exists())
        self.assertEqual((self.services / "shadow.toml").read_text(encoding="utf-8"),
                         self.builtin_toml)
        self.assertNotIn("shadow", manifest.load_all(), "内置模板继续被遮蔽")
        self.assertEqual([s["sid"] for s in self.reg.removed_list()["services"]], ["shadow"])


class ProjectRemoveTest(RemovalBase):
    def test_empty_project_removed_directly(self):
        self.reg.ensure_project("p1", "P1", None, None)
        res = self.reg.remove_project("p1", "stop", None)
        self.assertTrue(res["ok"])
        self.assertIsNone(self.reg.project("p1"))
        entry = self.removed_json()["projects"]["p1"]
        self.assertEqual(entry["services"], [])
        self.assertEqual(entry["project"]["name"], "P1")

    def test_full_project_stop_flow(self):
        self.register(sid="svc-a", port=48501)
        self.register(sid="svc-b", port=48502, revision=1)
        proj_before = self.reg.project("p1")
        ctl = FakeCtl()
        ctl.projects["svc-a"] = FakeProj("svc-a", comps=("web",))
        ctl.projects["svc-b"] = FakeProj("svc-b", comps=("web",))
        res = self.reg.remove_project("p1", "stop", ctl)
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["stopped"]), ["svc-a", "svc-b"])
        self.assertEqual(sorted(ctl.stopped), ["svc-a", "svc-b"], "逐成员停止")
        self.assertEqual(sorted(ctl.released), ["svc-a", "svc-b"])
        # 归档 + 用户层删除 + registry 项目消失
        for sid in ("svc-a", "svc-b"):
            self.assertFalse((self.user_services / f"{sid}.toml").exists())
            self.assertEqual(len(list((self.home / "removed").glob(f"{sid}-*.json"))), 1)
        self.assertIn("svc-a", self.removed_json()["services"])
        self.assertIsNone(self.reg.project("p1"))
        entry = self.removed_json()["projects"]["p1"]
        self.assertEqual(sorted(entry["services"]), ["svc-a", "svc-b"])
        self.assertEqual(entry["project"]["name"], proj_before["name"], "原 project 对象入档")
        self.assertEqual(entry["project"]["directories"], proj_before["directories"])
        loaded = manifest.load_all()
        self.assertNotIn("svc-a", loaded)
        self.assertNotIn("svc-b", loaded)

    def test_member_stop_failure_aborts_all(self):
        self.register(sid="svc-a", port=48501)
        self.register(sid="svc-b", port=48502, revision=1)
        before = self.core_bytes()
        ctl = FakeCtl(fail_stop_for=("svc-b",))
        ctl.projects["svc-a"] = FakeProj("svc-a", comps=("web",))
        ctl.projects["svc-b"] = FakeProj("svc-b", comps=("web",))
        res = self.reg.remove_project("p1", "stop", ctl)
        self.assertFalse(res["ok"])
        self.assertEqual(res["stopped"], ["svc-a"], "已停止成员如实上报（进程不回拉）")
        self.assertEqual([r["id"] for r in res["remaining"]], ["svc-b"])
        self.assertTrue(res["remaining"][0]["reason"])
        self.assertEqual(self.core_bytes(), before, "全体成员与项目登记保留、零写入")
        self.assertEqual(self.reg.project("p1")["services"], ["svc-a", "svc-b"])
        status, body = register_api.handle_project_remove("p1", "stop", False, ctl)
        self.assertEqual(status, 409)

    def test_keep_external_member_alive_aborts_all(self):
        self.register(sid="svc-a", port=48501)
        self.register(sid="svc-b", port=48502, revision=1)
        before = self.core_bytes()
        ctl = FakeCtl()
        ctl.projects["svc-a"] = FakeProj("svc-a", comps=("web",))
        ctl.projects["svc-b"] = FakeProj("svc-b", comps=("web",))
        from localservices import launchd
        with patch.object(launchd, "info",
                          side_effect=lambda label: FakeJI(running=True, loaded=True)
                          if "svc-a" in label else FakeJI()):
            res = self.reg.remove_project("p1", "keep-external", ctl)
        self.assertFalse(res["ok"])
        self.assertEqual([r["id"] for r in res["remaining"]], ["svc-a"])
        self.assertEqual(self.core_bytes(), before)

    def test_dry_run_preview_zero_writes(self):
        self.register(sid="svc-a", port=48501)
        before = self.core_bytes()
        ctl = FakeCtl()
        ctl.projects["svc-a"] = FakeProj("svc-a", comps=("web",))
        res = self.reg.remove_project("p1", "stop", ctl, dry_run=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["project"], {"id": "p1", "name": "P1"})
        self.assertEqual([s["id"] for s in res["services"]], ["svc-a"])
        self.assertIn("directories", res)
        self.assertIn("resources_referenced", res)
        self.assertIn("kept", res)
        self.assertIn("warnings", res)
        self.assertEqual(self.core_bytes(), before, "dry_run 零写入")
        # DELETE?dry_run=true 同形
        status, body = register_api.handle_project_remove("p1", "stop", True, ctl)
        self.assertEqual(status, 200)
        self.assertEqual(body["project"]["id"], "p1")

    def test_resources_referenced_shared_flag(self):
        self.register(sid="svc-a", port=48501)
        # 另一个项目的服务引用同一容器/卷
        (self.services / "neighbor.toml").write_text(
            f'id = "neighbor"\nname = "Neighbor"\nsource = "{self.src}"\n'
            f'project = "p9"\n\n[[components]]\nid = "web"\ncmd = ["true"]\nport = 48503\n\n'
            f'[[dependencies]]\nname = "Postgres"\nprobe = {{tcp = "127.0.0.1:5432"}}\n'
            f'container = "pg"\nvolume = "pgdata"\n', encoding="utf-8")
        # 本项目成员 svc-b 声明同一容器依赖
        p = self.payload(sid="svc-b", port=48504, revision=1)
        p["service"]["dependencies"] = [
            {"name": "Postgres", "probe": {"tcp": "127.0.0.1:5432"}, "container": "pg",
             "volume": "pgdata"}]
        self.assertTrue(self.reg.register_service(p)["ok"])
        inv = {"ok": True, "docker_context": "colima",
               "resources": [{"id": "container:pg", "name": "pg", "state": "running"}]}
        with patch("localservices.resources.inventory", return_value=inv):
            res = self.reg.remove_project_preview("p1", FakeCtl())
        by_name = {r["name"]: r for r in res["resources_referenced"]}
        self.assertIn("container:pg", by_name)
        pg = by_name["container:pg"]
        self.assertTrue(pg["shared"], "其他项目的服务也在引用 → shared")
        self.assertEqual(pg["state"], "running")
        self.assertEqual(sorted(pg["consumers"]), ["neighbor", "svc-b"])
        self.assertIn("volume:pgdata", by_name)

    def test_removed_list_shapes(self):
        self.register()
        self.reg.remove_service("demo", "keep-external", FakeCtl())
        out = self.reg.removed_list()
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["services"]), 1)
        entry = out["services"][0]
        for key in ("sid", "project_id", "removed_at", "disposition", "archive"):
            self.assertIn(key, entry)
        self.assertEqual(out["projects"], [])
        status, body = register_api.handle_removed_list()
        self.assertEqual(status, 200)
        self.assertEqual([s["sid"] for s in body["services"]], ["demo"])


if __name__ == "__main__":
    unittest.main()
