"""V2 注册核心行为测试（CONTRACTS V2.4/V2.7/V2.9 + N00 复现项 1/2a/2b/3/6a/7 的等价单测）。

覆盖：
- 读取硬化：非 dict JSON 走 corrupt 备份；不可读 fail-closed（绝不静默空库）
- 事务：持续 I/O 故障下更新 = 两文件字节不变；新建 = 无半成品；rename 失败原地写回
- dry_run：incomplete 不写草稿；duplicate 与实际一致；diff 零写入
- 更新语义：空 description 不清空人工描述；identity_changed 提示
- V2.7 compose 组件（无 cmd、confirmed_provision 门禁、TOML 往返）
- V2.9 set_autostart / autostart_map
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

from localservices import manifest, register_api  # noqa: E402
from localservices import registry as reg  # noqa: E402
from localservices.registry import Registry, RegistryError  # noqa: E402

PY = sys.executable


class V2Base(unittest.TestCase):
    """每测试独立隔离环境（LOCALSERVICES_* 重定向，绝不碰真实 home/launchd）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsreg2-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        self.src = self.root / "src"
        for d in (self.home, self.services, self.user_services, self.src / "app"):
            d.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsreg2",
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = Registry()

    def payload(self, sid="demo", revision=None, port=48301, source=None, svc_over=None, **over):
        svc = {"id": sid, "name": over.pop("name", sid.title()),
               "source": str(source or self.src),
               "components": [{"id": "web", "kind": "server",
                               "cmd": [PY, "-m", "http.server", str(port)],
                               "cwd": ".", "port": port}]}
        if "description" in over:
            if over["description"] is not None:
                svc["description"] = over["description"]
            del over["description"]
        svc.update(svc_over or {})
        body = {"schemaVersion": 1, "project": {"id": over.pop("project_id", "p1"),
                                                "name": over.pop("project_name", "P1")},
                "service": svc}
        if revision is not None:
            body["revision"] = revision
        body.update(over)
        return body

    def tree_bytes(self):
        parts = []
        for d in (self.services, self.user_services, self.home):
            for f in sorted(d.rglob("*")):
                if f.is_file() and "logs" not in f.parts:
                    parts.append((str(f.relative_to(self.root)), f.read_bytes()))
        return sorted(parts)


class LoadHardeningTest(V2Base):
    def test_array_json_backed_up_then_proceeds(self):
        """合法 JSON 但非对象：corrupt 备份后从空继续，绝不无备份覆盖（复现项 2a）。"""
        rf = self.home / "registry.json"
        rf.write_text("[1, 2, 3]", encoding="utf-8")
        res = self.reg.register_service(self.payload())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "created")
        backups = list(self.home.glob("registry.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "[1, 2, 3]")
        self.assertIsInstance(json.loads(rf.read_text()), dict)

    def test_unreadable_registry_fail_closed(self):
        """无读权限：读路径返回失败标记（不伪装空库），写路径 RegistryError（复现项 2b）。"""
        rf = self.home / "registry.json"
        original = '{"schemaVersion": 1, "revision": 3, "projects": [{"id": "keep", "name": "K"}]}'
        rf.write_text(original, encoding="utf-8")
        os.chmod(rf, 0)
        try:
            data = self.reg.data()
            self.assertIs(data.get("ok"), False, "读失败必须显式失败，不得静默当空库")
            self.assertNotIn("projects", data)
            self.assertRaises(RegistryError, self.reg.ensure_project, "px", "PX", None, None)
            self.assertRaises(RegistryError, self.reg.projects)
        finally:
            os.chmod(rf, 0o644)
        self.assertEqual(rf.read_text(), original, "原文件字节不动")


class TransactionTest(V2Base):
    def _register_demo(self):
        self.assertTrue(self.reg.register_service(self.payload())["ok"])
        return self.user_services / "demo.toml"

    def test_persistent_io_failure_update_consistent(self):
        """持续 I/O 故障：更新抛错后 TOML 与 registry.json 都保持原字节（复现项 1）。"""
        toml = self._register_demo()
        toml_before = toml.read_bytes()
        reg_before = (self.home / "registry.json").read_bytes()
        real = reg._write_bytes_atomic
        calls = {"n": 0}

        def failing(path, data):
            calls["n"] += 1
            if calls["n"] >= 2:  # 第 1 次=TOML 新内容；之后（registry 保存）全部失败
                raise OSError("simulated persistent disk-full")
            return real(path, data)

        with patch.object(reg, "_write_bytes_atomic", side_effect=failing):
            with self.assertRaises(OSError):
                self.reg.register_service(self.payload(revision=1, name="Demo v2"))
        self.assertEqual(toml.read_bytes(), toml_before, "TOML 不得残留新内容")
        self.assertEqual((self.home / "registry.json").read_bytes(), reg_before,
                         "registry.json 不得残留新 revision")

    def test_persistent_io_failure_create_no_half_file(self):
        self._register_demo()
        reg_before = (self.home / "registry.json").read_bytes()
        real = reg._write_bytes_atomic
        calls = {"n": 0}

        def failing(path, data):
            calls["n"] += 1
            if calls["n"] >= 1:
                raise OSError("disk full")
            return real(path, data)

        with patch.object(reg, "_write_bytes_atomic", side_effect=failing):
            with self.assertRaises(OSError):
                self.reg.register_service(self.payload(sid="fresh", revision=1, port=48302))
        self.assertFalse((self.user_services / "fresh.toml").exists(), "新建失败不得留半成品")
        self.assertEqual((self.home / "registry.json").read_bytes(), reg_before)

    def test_replace_failure_restores_toml_in_place(self):
        """rename 阶段失败（TOML 已换、registry 换失败）→ 原地写回恢复 TOML 原字节。"""
        toml = self._register_demo()
        toml_before = toml.read_bytes()
        reg_before = (self.home / "registry.json").read_bytes()
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst, *a, **kw):
            calls["n"] += 1
            # 1=阶段1临时写内部 replace；2=TOML 落位；3=registry.json 落位 → 在这里失败
            # （TOML 已换新内容、registry 仍旧 → 必须原地写回 TOML）
            if calls["n"] == 3:
                raise OSError("replace failed")
            return real_replace(src, dst, *a, **kw)

        with patch.object(reg.os, "replace", side_effect=flaky_replace):
            with self.assertRaises(OSError):
                self.reg.register_service(self.payload(revision=1, name="Demo v2"))
        self.assertEqual(toml.read_bytes(), toml_before, "已替换文件必须原地写回原字节")
        self.assertEqual((self.home / "registry.json").read_bytes(), reg_before)
        self.assertEqual(sorted(p.name for p in self.user_services.glob(".*")), [],
                         "不得残留 .tmp 文件")


class DryRunTest(V2Base):
    def test_incomplete_dryrun_writes_nothing(self):
        """dry_run + 不完整 payload → incomplete + missing，零写入（复现项 3）。"""
        p = self.payload()
        p["service"]["components"][0].pop("cmd")
        before = self.tree_bytes()
        res = self.reg.register_service(p, dry_run=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "incomplete")
        self.assertTrue(res["missing"])
        self.assertIn("components[web].cmd", res["missing"])
        self.assertEqual(self.tree_bytes(), before, "dry_run 不得写草稿/任何文件")
        # 非 dry_run 才产生草稿
        res2 = self.reg.register_service(p)
        self.assertEqual(res2["action"], "draft")
        self.assertTrue(res2["draft_id"])

    def test_duplicate_dryrun_matches_real(self):
        """同 identity 不同 id：dry_run 返回 duplicate+diff，与实际一致（复现项 7）。"""
        self.assertTrue(self.reg.register_service(self.payload(sid="svc-a"))["ok"])
        before = self.tree_bytes()
        d = self.reg.register_service(self.payload(sid="svc-b"), dry_run=True)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["action"], "duplicate")
        self.assertEqual(d["service_id"], "svc-a")
        self.assertIsInstance(d["diff"], list)
        self.assertEqual(self.tree_bytes(), before, "dry_run duplicate 分支零写入")
        real = self.reg.register_service(self.payload(sid="svc-b"))
        self.assertEqual(real["action"], "duplicate")
        self.assertEqual(real["service_id"], "svc-a")

    def test_diff_dryrun_zero_writes(self):
        self.assertTrue(self.reg.register_service(self.payload())["ok"])
        before = self.tree_bytes()
        res = self.reg.register_service(self.payload(revision=1, name="Demo 改名"), dry_run=True)
        self.assertEqual(res["action"], "diff")
        fields = {d["field"] for d in res["diff"]}
        self.assertIn("name", fields)
        self.assertEqual(self.tree_bytes(), before)


class UpdateSemanticsTest(V2Base):
    def test_empty_description_preserves_manual_text(self):
        """GUI 恒携带 description；重新识别的空串不得清空用户手写描述（复现项 4 的登记层防线）。"""
        self.assertTrue(self.reg.register_service(
            self.payload(name="我的名字", description="手工描述"))["ok"])
        res = self.reg.register_service(self.payload(revision=1, name="识别名", description=""))
        self.assertTrue(res["ok"], res)
        text = (self.user_services / "demo.toml").read_text(encoding="utf-8")
        self.assertNotIn('description = ""', text)
        self.assertIn("手工描述", text)
        loaded = manifest.load_all()["demo"]
        self.assertEqual(loaded.description, "手工描述")
        self.assertEqual(loaded.name, "识别名")

    def test_identity_changed_surfaced(self):
        """同 id 换 source/cwd 集合的更新：响应与 dry_run 都带 identity_changed 提示。"""
        self.assertTrue(self.reg.register_service(self.payload())["ok"])
        other = self.root / "other-src"
        other.mkdir()
        d = self.reg.register_service(self.payload(revision=1, source=other), dry_run=True)
        self.assertEqual(d["action"], "diff")
        self.assertTrue(d["identity_changed"])
        self.assertTrue(d["identity_note"])
        res = self.reg.register_service(self.payload(revision=1, source=other))
        self.assertEqual(res["action"], "updated")
        self.assertTrue(res["identity_changed"])
        self.assertTrue(res["identity_note"])
        # 普通（身份不变）更新不带提示
        res2 = self.reg.register_service(
            self.payload(revision=2, source=other, name="Same Source New Name"))
        self.assertEqual(res2["action"], "updated")
        self.assertFalse(res2["identity_changed"])

    def test_unchanged_detects_merged_equivalence(self):
        """payload 省略的字段经 merge 保留：合并后等价 → unchanged，不重写不 bump。"""
        self.assertTrue(self.reg.register_service(
            self.payload(description="保留我"))["ok"])
        before = (self.user_services / "demo.toml").read_bytes()
        res = self.reg.register_service(self.payload(revision=1, description=""))
        self.assertEqual(res["action"], "unchanged")
        self.assertEqual((self.user_services / "demo.toml").read_bytes(), before)


class ComposeComponentTest(V2Base):
    def compose_payload(self, **over):
        p = self.payload(**over)
        p["service"]["components"] = [{
            "id": "db", "kind": "server", "run": "compose",
            "compose_service": "postgres", "compose_project": "demo",
            "compose_files": ["docker-compose.yml"], "cwd": "."}]
        return p

    def test_compose_component_roundtrip(self):
        res = self.reg.register_service(self.compose_payload())
        self.assertTrue(res["ok"], res)
        c = manifest.load_all()["demo"].component("db")
        self.assertEqual(c.run, "compose")
        self.assertEqual(c.compose_service, "postgres")
        self.assertEqual(c.compose_project, "demo")
        self.assertEqual(c.compose_files, ["docker-compose.yml"])
        self.assertEqual(c.cmd, [])
        text = (self.user_services / "demo.toml").read_text(encoding="utf-8")
        self.assertIn('run = "compose"', text)
        self.assertIn('compose_service = "postgres"', text)

    def test_provision_up_requires_confirmation(self):
        p = self.compose_payload()
        p["service"]["components"][0]["provision"] = "up"
        res = self.reg.register_service(p)
        self.assertFalse(res["ok"])
        self.assertIn("confirmed_provision", " ".join(res.get("details", [])))
        p["service"]["confirmed_provision"] = True
        res2 = self.reg.register_service(p)
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(manifest.load_all()["demo"].component("db").provision, "up")

    def test_compose_needs_compose_service(self):
        p = self.compose_payload()
        p["service"]["components"][0].pop("compose_service")
        res = self.reg.register_service(p)
        self.assertFalse(res["ok"])
        self.assertIn("compose_service", " ".join(res.get("details", [])))


class AutostartTest(V2Base):
    def test_set_autostart_and_map(self):
        self.reg.ensure_project("p1", "P1", None, None)
        proj = self.reg.set_autostart("p1", True)
        self.assertTrue(proj["autostart"])
        self.assertEqual(self.reg.autostart_map(), {"p1": True})
        self.assertEqual(self.reg.data()["projects"][0]["autostart"], True)
        self.assertFalse(self.reg.set_autostart("p1", False)["autostart"])
        self.assertEqual(self.reg.autostart_map(), {"p1": False})
        with self.assertRaises(RegistryError) as cm:
            self.reg.set_autostart("ghost", True)
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(RegistryError):
            self.reg.set_autostart("p1", "yes")

    def test_ensure_project_update_without_name(self):
        """GUI 自动开关联动只发 {id, autostart}：更新无需 name（复现项 6a）。"""
        self.reg.ensure_project("p1", "P1", None, None)
        proj, created = self.reg.ensure_project("p1", None, None, None, autostart=True)
        self.assertFalse(created)
        self.assertTrue(proj["autostart"])
        # 创建仍要求 name
        with self.assertRaises(RegistryError) as cm:
            self.reg.ensure_project("p2", None, None, None)
        self.assertIn("name", str(cm.exception))

    def test_autostart_endpoint(self):
        self.reg.ensure_project("p1", "P1", None, None)
        status, body = register_api.handle_autostart("p1", {"value": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["project"]["autostart"])
        status, body = register_api.handle_autostart("p1", {"value": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["project"]["autostart"])
        self.assertEqual(register_api.handle_autostart("p1", {"value": 1})[0], 400)
        self.assertEqual(register_api.handle_autostart("ghost", {"value": True})[0], 404)


if __name__ == "__main__":
    unittest.main()
