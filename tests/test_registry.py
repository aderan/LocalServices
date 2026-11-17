"""Registry 单元测试：全部经 LOCALSERVICES_* 重定向到临时目录，不碰真实 home/launchd/网络。

覆盖：Project 增删改（含符号链接去重）、合法注册落盘与 TOML 回读、
unchanged/duplicate/revision conflict/各类无效提交、无效提交字节不变、草稿生命周期。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import manifest, register_api  # noqa: E402
from localservices import registry as reg  # noqa: E402

PY = sys.executable


class RegBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsreg-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        self.src = self.root / "src"
        for d in (self.home, self.services, self.user_services, self.src / "app", self.src / "scripts"):
            d.mkdir(parents=True)
        (self.src / "scripts" / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = reg.Registry()

    def payload(self, **over):
        base = {
            "schemaVersion": 1,
            "revision": 0,
            "project": {"id": "demo", "name": "Demo"},
            "service": {
                "id": "demo-web",
                "name": "Demo Web",
                "source": str(self.src),
                "open": "http://127.0.0.1:47901/",
                "components": [{
                    "id": "web",
                    "cmd": [PY, "-m", "http.server", "47901"],
                    "port": 47901,
                    "ready": {"http": "http://127.0.0.1:47901/"},
                }],
            },
        }
        base.update(over)
        return base

    def tree_hash(self):
        """registry.json + 两个 services 目录的全量字节指纹。"""
        parts = []
        rf = self.home / "registry.json"
        parts.append(("registry.json", rf.read_bytes() if rf.exists() else b"<absent>"))
        for d in (self.services, self.user_services):
            for f in sorted(d.rglob("*")):
                rel = str(f.relative_to(self.root))
                parts.append((rel + ("/" if f.is_dir() else ""), f.read_bytes() if f.is_file() else b""))
        blob = b"\0".join(k.encode() + b"\1" + v for k, v in sorted(parts))
        return hashlib.sha256(blob).hexdigest()


class ProjectTest(RegBase):
    def test_ensure_rename_and_symlink_dedup(self):
        real = self.root / "realproj"
        real.mkdir()
        link = self.root / "proj-link"
        os.symlink(real, link)
        proj, created = self.reg.ensure_project("p1", "P1", [{"path": str(link)}], None)
        self.assertTrue(created)
        # 注册时 resolve 符号链接：登记的是真实路径
        self.assertEqual([d["path"] for d in proj["directories"]], [str(real.resolve())])
        self.assertRegex(proj["directories"][0]["id"], r"^d-[0-9a-f]{6}$")

        # 同一真实目录经 symlink 与直连 path 只登记一次（幂等，不加 revision）
        before = self.reg.project("p1")["revision"]
        proj2 = self.reg.add_directory("p1", str(real))
        self.assertEqual(len(proj2["directories"]), 1)
        self.assertEqual(proj2["revision"], before)

        proj3 = self.reg.rename_project("p1", "New Name")
        self.assertEqual(proj3["name"], "New Name")
        self.assertEqual(proj3["revision"], before + 1)

        proj4 = self.reg.remove_directory("p1", proj["directories"][0]["id"])
        self.assertEqual(proj4["directories"], [])

        with self.assertRaises(reg.RegistryError):
            self.reg.rename_project("nope", "X")

    def test_unknown_project_and_dir_errors(self):
        with self.assertRaises(reg.RegistryError):
            self.reg.add_directory("ghost", str(self.src))
        self.reg.ensure_project("p1", "P1", None, None)
        with self.assertRaises(reg.RegistryError):
            self.reg.remove_directory("p1", "d-000000")
        with self.assertRaises(reg.RegistryError):
            self.reg.add_directory("p1", str(self.root / "missing"))

    def test_favorite(self):
        self.reg.ensure_project("p1", "P1", None, None)
        proj = self.reg.set_favorite("p1", True)
        self.assertTrue(proj["favorite"])
        self.assertTrue(self.reg.data()["projects"][0]["favorite"])


class RegisterTest(RegBase):
    def test_register_pins_docker_endpoint_and_preserves_binding_on_edit(self):
        payload = self.payload()
        payload["service"]["docker"] = {"context": "other-engine"}
        endpoint = "unix:///tmp/other-engine.sock"
        with patch("localservices.dockerctx.inspect_context", return_value=endpoint):
            result = self.reg.register_service(payload)
        self.assertTrue(result["ok"], result)
        changed = self.payload(revision=result["revision"])
        changed["service"]["name"] = "Updated"
        result = self.reg.register_service(changed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(manifest.load_all()["demo-web"].docker, {
            "context": "other-engine", "endpoint": endpoint, "autostart": False,
        })

    def test_register_creates_project_and_toml(self):
        res = self.reg.register_service(self.payload())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "created")
        self.assertEqual(res["service_id"], "demo-web")
        self.assertEqual(res["project_id"], "demo")
        self.assertEqual(res["revision"], 1)

        toml = self.user_services / "demo-web.toml"
        self.assertTrue(toml.exists())
        loaded = manifest.load_all()
        p = loaded["demo-web"]
        self.assertEqual(p.project, "demo")
        self.assertEqual(p.name, "Demo Web")
        self.assertEqual(str(p.source), str(self.src.resolve()))
        c = p.component("web")
        self.assertEqual(c.cmd, [PY, "-m", "http.server", "47901"])
        self.assertEqual(c.port, 47901)
        self.assertEqual(c.ready.http, "http://127.0.0.1:47901/")
        self.assertEqual(p.registered["agent"], "none")
        self.assertIn("at", p.registered)
        self.assertIn("evidence_summary", p.registered)

        data = self.reg.data()
        proj = data["projects"][0]
        self.assertEqual(proj["id"], "demo")
        self.assertEqual(proj["services"], ["demo-web"])
        self.assertEqual([d["path"] for d in proj["directories"]], [str(self.src.resolve())])
        self.assertEqual([d["role"] for d in proj["directories"]], ["source"])

    def test_register_without_project_derives_ids(self):
        p = self.payload()
        p.pop("project")
        p["service"]["id"] = "standalone"
        res = self.reg.register_service(p)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["project_id"], "standalone")
        self.assertEqual([x["id"] for x in self.reg.data()["projects"]], ["standalone"])

    def test_register_unchanged_keeps_bytes(self):
        self.reg.register_service(self.payload())
        before = (self.home / "registry.json").read_bytes()
        toml_before = (self.user_services / "demo-web.toml").read_bytes()
        res = self.reg.register_service(self.payload())
        self.assertEqual(res["action"], "unchanged")
        self.assertEqual(res["revision"], 1)
        self.assertEqual((self.home / "registry.json").read_bytes(), before)
        self.assertEqual((self.user_services / "demo-web.toml").read_bytes(), toml_before)

    def test_register_duplicate_identity(self):
        self.reg.register_service(self.payload())
        p2 = self.payload()
        p2["service"]["id"] = "demo-web-copy"
        res = self.reg.register_service(p2)
        self.assertEqual(res["action"], "duplicate")
        self.assertEqual(res["service_id"], "demo-web")  # 返回既有 id，不新建文件
        self.assertEqual(list(self.user_services.glob("*.toml")), [self.user_services / "demo-web.toml"])

    def test_revision_conflict_and_update(self):
        res1 = self.reg.register_service(self.payload())
        self.assertEqual(res1["revision"], 1)
        p2 = self.payload()
        p2["service"]["name"] = "Demo Web v2"
        conflict = self.reg.register_service(p2)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"], "revision conflict")
        self.assertEqual(conflict["current_revision"], 1)

        p3 = self.payload()
        p3["service"]["name"] = "Demo Web v2"
        p3["revision"] = 1
        res3 = self.reg.register_service(p3)
        self.assertEqual(res3["action"], "updated")
        self.assertEqual(res3["revision"], 2)
        self.assertEqual(manifest.load_all()["demo-web"].name, "Demo Web v2")

        # 更新后再以相同内容注册（带最新 revision）→ unchanged
        p4 = self.payload()
        p4["service"]["name"] = "Demo Web v2"
        p4["revision"] = 2
        self.assertEqual(self.reg.register_service(p4)["action"], "unchanged")

    def test_shell_script_maps_to_bin_sh(self):
        p = self.payload()
        comp = p["service"]["components"][0]
        comp.pop("cmd")
        comp.pop("port")
        comp.pop("ready")
        comp["shell_script"] = "scripts/run.sh"
        res = self.reg.register_service(p)
        self.assertTrue(res["ok"], res)
        c = manifest.load_all()["demo-web"].component("web")
        self.assertEqual(c.cmd, ["/bin/sh", "scripts/run.sh"])
        self.assertTrue(any("shell_script" in w for w in res["validation"]["warnings"]))


class ValidationTest(RegBase):
    def test_invalid_variants_keep_bytes(self):
        (self.root / "outside").mkdir()  # cwd 越界目标存在（存在性通过、范围不通过）
        (self.services / "occupied.toml").write_text(
            f'id = "occupied"\nname = "Occupied"\nsource = "{self.src}"\n'
            f'[[components]]\nid = "web"\ncmd = ["true"]\nport = 47901\n')

        def port_conflict(p):
            p["service"]["components"][0]["port"] = 47901
            p["service"]["components"][0]["ready"] = {"http": "http://127.0.0.1:47901/"}
            p["service"]["components"][0]["cmd"] = [PY, "-m", "http.server", "47901"]

        variants = [
            ("schemaVersion", lambda p: p.update(schemaVersion=2)),
            ("cwd 越界", lambda p: p["service"]["components"][0].update(cwd="../outside")),
            ("cmd 不可解析", lambda p: p["service"]["components"][0].update(cmd=["no-such-bin-xyz"])),
            ("端口冲突", port_conflict),
            ("坏 probe", lambda p: p["service"].update(dependencies=[{"name": "db", "probe": {}}])),
            ("ready 端口不一致", lambda p: p["service"]["components"][0].update(
                ready={"http": "http://127.0.0.1:47902/"})),
            ("shell_script 缺失", lambda p: p["service"]["components"][0].update(
                cmd=None, shell_script="missing.sh")),
            ("组件 id 非法", lambda p: p["service"]["components"][0].update(id="Bad Id")),
            ("端口越界", lambda p: p["service"]["components"][0].update(port=70000)),
            ("revision 类型", lambda p: p.update(revision="x")),
        ]
        for name, mutate in variants:
            with self.subTest(variant=name):
                before = self.tree_hash()
                p = self.payload()
                mutate(p)
                res = self.reg.register_service(p)
                self.assertFalse(res["ok"], f"{name}: {res}")
                self.assertIn("error", res)
                self.assertEqual(self.tree_hash(), before, f"{name} 改变了落盘字节")

    def test_invalid_details_name_component(self):
        p = self.payload()
        p["service"]["components"][0]["cmd"] = ["no-such-bin-xyz"]
        res = self.reg.register_service(p)
        joined = " ".join(res.get("details", []))
        self.assertIn("web", joined)

    def test_port_conflict_reports_both_sides(self):
        (self.services / "occupied.toml").write_text(
            f'id = "occupied"\nname = "Occupied"\nsource = "{self.src}"\n'
            f'[[components]]\nid = "web"\ncmd = ["true"]\nport = 47901\n')
        p = self.payload()
        p["service"]["components"][0]["cmd"] = [PY, "-m", "http.server", "47901"]
        res = self.reg.register_service(p)
        self.assertFalse(res["ok"])
        joined = " ".join(res.get("details", []))
        self.assertIn("occupied.web", joined)
        self.assertIn("demo-web.web", joined)

    def test_resource_refs(self):
        def with_port(p, port, sid=None):
            p = self.payload()
            if sid:
                p["service"]["id"] = sid
            p["service"]["components"][0]["port"] = port
            p["service"]["components"][0]["cmd"] = [PY, "-m", "http.server", str(port)]
            p["service"]["components"][0]["ready"] = {"http": f"http://127.0.0.1:{port}/"}
            p["service"]["open"] = f"http://127.0.0.1:{port}/"
            return p

        reachable = {"ok": True, "docker_context": "colima",
                     "resources": [{"id": "container:real-db", "name": "real-db"}]}
        with patch("localservices.resources.inventory", return_value=reachable):
            p = with_port(None, 47911)
            p["service"]["dependencies"] = [{
                "name": "Postgres", "probe": {"tcp": "127.0.0.1:5434"},
                "resource": "container:real-db", "required": True}]
            res = self.reg.register_service(p)
            self.assertTrue(res["ok"], res)
            dep = manifest.load_all()["demo-web"].dependencies[0]
            self.assertEqual(dep.container, "real-db")  # resource 引用映射到既有 container 字段
            self.assertEqual(res["validation"]["warnings"], [])

            p2 = with_port(None, 47912, sid="demo-web-2")
            p2["service"]["dependencies"] = [{
                "name": "Postgres", "probe": {"tcp": "127.0.0.1:5434"},
                "resource": "container:ghost"}]
            res2 = self.reg.register_service(p2)
            self.assertFalse(res2["ok"])
            self.assertIn("ghost", " ".join(res2.get("details", [])))

        with patch("localservices.resources.inventory",
                   return_value={"ok": False, "error": "docker down"}):
            p3 = with_port(None, 47913, sid="demo-web-3")
            p3["revision"] = 1  # 项目已存在：新增服务需携带当前 revision
            p3["service"]["dependencies"] = [{
                "name": "Postgres", "probe": {"tcp": "127.0.0.1:5434"},
                "resource": "container:real-db"}]
            res3 = self.reg.register_service(p3)
            self.assertTrue(res3["ok"], res3)
            self.assertTrue(any("未验证" in w for w in res3["validation"]["warnings"]))


class DraftTest(RegBase):
    def make_draft(self):
        p = self.payload()
        p["service"]["draft"] = True
        p["service"]["components"][0].pop("cmd")
        return self.reg.register_service(p)

    def test_draft_saved_outside_services(self):
        res = self.make_draft()
        self.assertEqual(res["action"], "draft")
        self.assertTrue(res["draft_id"])
        drafts = self.reg.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["id"], res["draft_id"])
        self.assertEqual(list(self.user_services.glob("*.toml")), [])  # 草稿绝不进 services 目录
        self.assertEqual(self.reg.draft(res["draft_id"])["payload"]["service"]["id"], "demo-web")
        self.assertIsNone(self.reg.draft("d-nonexistent"))

    def test_draft_register_completes_and_deletes(self):
        res = self.make_draft()
        draft_id = res["draft_id"]
        completion = {
            "schemaVersion": 1,
            "revision": 0,
            "service": {
                "draft": False,
                "components": [{
                    "id": "web", "cmd": [PY, "-m", "http.server", "47901"],
                    "port": 47901, "ready": {"http": "http://127.0.0.1:47901/"}}],
            },
        }
        status, body = register_api.handle_draft_register(draft_id, completion, None)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["action"], "created")
        self.assertIsNone(self.reg.draft(draft_id))  # 转正后删除草稿
        self.assertTrue((self.user_services / "demo-web.toml").exists())

    def test_draft_register_still_incomplete_keeps_draft(self):
        res = self.make_draft()
        status, body = register_api.handle_draft_register(res["draft_id"], {"service": {"draft": False}}, None)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIsNotNone(self.reg.draft(res["draft_id"]))
        self.assertEqual(list(self.user_services.glob("*.toml")), [])

    def test_draft_delete(self):
        res = self.make_draft()
        self.assertEqual(self.reg.delete_draft(res["draft_id"])["ok"], True)
        with self.assertRaises(reg.RegistryError):
            self.reg.delete_draft(res["draft_id"])


class FakeCtl:
    def __init__(self, state="stopped", label="已停止"):
        self.snapshot_data = {"projects": [{"id": "demo-web", "state": state, "label": label}]}
        self.ops: dict = {}
        self.stopped = None
        # 移除前的停止确认（registry._stop_via_controller）会查 projects/label
        self.projects = {}

    def snapshot(self):
        return self.snapshot_data

    def reload(self):
        pass

    def label(self, p, c):
        return f"label.{p.id}.{c.id}"

    def stop(self, sid):
        self.stopped = sid
        return {"ok": True, "accepted": False, "message": "未运行"}


class ApiTest(RegBase):
    def test_handle_register_ok_and_bad(self):
        status, body = register_api.handle_register(self.payload(), None)
        self.assertEqual(status, 200)
        self.assertEqual(body["action"], "created")
        p = self.payload()
        p["schemaVersion"] = 9
        status, body = register_api.handle_register(p, None)
        self.assertEqual(status, 400)

    def test_handle_register_conflict_maps_409(self):
        self.reg.register_service(self.payload())
        p = self.payload()
        p["service"]["name"] = "v2"
        status, body = register_api.handle_register(p, None)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "revision conflict")

    def test_registry_get_joins_snapshot(self):
        self.reg.register_service(self.payload())
        status, body = register_api.handle_registry_get(FakeCtl(state="stopped"))
        self.assertEqual(status, 200)
        svc = body["services"][0]
        self.assertEqual(svc, {"id": "demo-web", "name": "Demo Web", "project": "demo",
                               "state": "stopped", "label": "已停止",
                               "source": str(self.src.resolve())})
        self.assertEqual(body["registry"]["projects"][0]["id"], "demo")

    def test_registry_get_unknown_state_without_ctl(self):
        self.reg.register_service(self.payload())
        self.assertEqual(register_api.handle_registry_get(None), (503, {"ok": False, "error": "controller unavailable"}))
        status, body = register_api.handle_registry_get(FakeCtl())  # 快照里没有别的服务
        self.assertEqual(body["services"][0]["state"], "stopped")
        ctl = FakeCtl()
        ctl.snapshot_data = {"projects": []}
        status, body = register_api.handle_registry_get(ctl)
        self.assertEqual(body["services"][0]["state"], "unknown")

    def test_service_remove_keep_external(self):
        self.reg.register_service(self.payload())
        status, body = register_api.handle_service_remove("demo-web", "keep-external", None)
        self.assertEqual(status, 200, body)
        self.assertEqual(list(self.user_services.glob("*.toml")), [])
        self.assertEqual(self.reg.project("demo")["services"], [])  # 登记移除，目录保留

    def test_service_remove_stop_without_ctl(self):
        self.reg.register_service(self.payload())
        status, body = register_api.handle_service_remove("demo-web", "stop", None)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "controller unavailable")

    def test_service_remove_stop_with_ctl(self):
        self.reg.register_service(self.payload())
        ctl = FakeCtl()
        status, body = register_api.handle_service_remove("demo-web", "stop", ctl)
        self.assertEqual(status, 200, body)
        self.assertEqual(ctl.stopped, "demo-web")

    def test_service_remove_unknown(self):
        status, body = register_api.handle_service_remove("ghost", "keep-external", None)
        self.assertEqual(status, 404)

    def test_project_endpoints(self):
        status, body = register_api.handle_project_upsert({"name": "New", "directories": [{"path": str(self.src)}]})
        self.assertEqual(status, 200)
        self.assertTrue(body["created"])
        pid = body["project"]["id"]
        self.assertRegex(pid, r"^[a-z0-9][a-z0-9-]*$")

        status, _ = register_api.handle_project_upsert({"name": ""})
        self.assertEqual(status, 400)

        status, body = register_api.handle_directory_add(pid, {"path": str(self.root), "role": "docs"})
        self.assertEqual(status, 200)
        dir_id = body["project"]["directories"][-1]["id"]
        status, body = register_api.handle_directory_remove(pid, dir_id)
        self.assertEqual(status, 200)
        self.assertEqual(status, 200)
        status, _ = register_api.handle_directory_remove(pid, "d-xxxxxx")
        self.assertEqual(status, 404)

        status, body = register_api.handle_favorite(pid, {"value": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["project"]["favorite"])
        status, _ = register_api.handle_favorite(pid, {"value": "yes"})
        self.assertEqual(status, 400)

    def test_drafts_endpoint(self):
        p = self.payload()
        p["service"]["draft"] = True
        register_api.handle_register(p, None)
        status, body = register_api.handle_drafts()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["drafts"]), 1)
        status, body = register_api.handle_draft_delete(body["drafts"][0]["id"])
        self.assertEqual(status, 200)
        status, _ = register_api.handle_draft_delete("d-zzz")
        self.assertEqual(status, 404)


class TomlEmitterTest(RegBase):
    def test_emitter_roundtrip(self):
        import tomllib
        from localservices.registry import dumps_toml
        data = {
            "id": "x", "name": "引号\"与\\反斜杠\n换行", "count": 3, "ratio": 2.5, "flag": True,
            "open": None,
            "registered": {"at": "2026-01-01", "by": "cli", "agent": "codex", "evidence_summary": "a\"b"},
            "components": [
                {"id": "web", "cmd": ["node", "server.js"], "port": 8080,
                 "env": {"A": "1", "B": "x\"y"}, "watch": ["src"], "ready": {"http": "http://127.0.0.1:8080/", "timeout": 5.0}},
                {"id": "gen", "kind": "generator", "cmd": ["make", "gen"], "debounce": 1.5},
            ],
            "dependencies": [{"name": "db", "probe": {"tcp": "127.0.0.1:5432"}, "affects": ["api"]}],
        }
        text = dumps_toml(data)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["name"], data["name"])
        self.assertNotIn("\n", text.split("name = ", 1)[1].split("\n", 1)[0])  # 转义后单行
        self.assertEqual(parsed["components"][0]["env"]["B"], "x\"y")
        self.assertEqual(parsed["components"][0]["ready"]["timeout"], 5.0)
        self.assertEqual(parsed["components"][1]["debounce"], 1.5)
        self.assertEqual(parsed["dependencies"][0]["probe"]["tcp"], "127.0.0.1:5432")
        self.assertNotIn("open", parsed)
        self.assertEqual(parsed["registered"]["evidence_summary"], "a\"b")


if __name__ == "__main__":
    unittest.main()
