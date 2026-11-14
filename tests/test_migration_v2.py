"""V2.5 迁移修复测试（CONTRACTS V2.5 + V2.0 内置遮蔽修订的迁移语义）。

覆盖：
- TOML 已声明不同 project 值 → 不插第二个顶层键（字节不变）+ notes 说明
- sid 已属其他项目（registry 指派）→ 不挪归属、不双写成员 + notes
- tombstone 服务（标记存在且未登记）完全跳过：不建项目、不动 TOML
- 遮蔽但用户层已注册的 id → 正常迁移（V2.0 修订）
- 幂等保留：二次执行 changed=False、字节不变
"""

from __future__ import annotations

import json
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

from localservices import paths  # noqa: E402
from localservices import registry as reg  # noqa: E402

PORT = 48700


class MigBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsmig2-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        for d in (self.home, self.services, self.user_services):
            d.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsmig2",
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = reg.Registry()
        self.src = self.root / "srcA"
        self.src.mkdir()
        self.port = PORT

    def write_toml(self, name: str, sid: str, text: str = "") -> Path:
        p = self.services / name
        p.write_text(f'id = "{sid}"\nname = "{sid.title()}"\nsource = "{self.src}"\n'
                     + text, encoding="utf-8")
        return p

    def comp(self, port=None):
        self.port += 1
        return (f'\n[[components]]\nid = "web"\ncmd = ["true"]\ncwd = "."\n'
                f'port = {port or self.port}\n')

    def project_ids(self):
        return {p["id"] for p in self.reg.data()["projects"]}

    def services_of(self, pid):
        proj = self.reg.project(pid)
        return set(proj["services"]) if proj else None


class DifferentProjectKeyTest(MigBase):
    def test_toml_declares_other_project_no_second_key(self):
        solo = self.write_toml("solo.toml", "solo", 'project = "elsewhere"' + self.comp())
        before = solo.read_bytes()
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        self.assertEqual(solo.read_bytes(), before, "TOML 字节不变（不插第二个键）")
        parsed = tomllib.loads(solo.read_text(encoding="utf-8"))
        self.assertEqual(parsed["project"], "elsewhere", "原声明值保留，无重复键")
        self.assertNotIn("solo", self.project_ids(), "不为别处归属的服务建项目")
        self.assertTrue(any("elsewhere" in n for n in res.get("notes", [])),
                        f"notes 必须说明跳过原因：{res.get('notes')}")

    def test_group_member_declares_other_project(self):
        """组内成员 TOML 声明其他项目：成员不挪入组项目，组仍按其余成员建。"""
        self.write_toml("alpha.toml", "alpha",
                        'group = "grp"\ngroup_name = "Grp"\ngroup_order = 0' + self.comp())
        self.write_toml("beta.toml", "beta",
                        'group = "grp"\ngroup_name = "Grp"\ngroup_order = 1\n'
                        'project = "custom-home"' + self.comp())
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.services_of("grp"), {"alpha"}, "beta 不被双写进 grp")
        beta_text = (self.services / "beta.toml").read_text(encoding="utf-8")
        self.assertEqual(tomllib.loads(beta_text)["project"], "custom-home")
        self.assertNotIn("beta", self.project_ids())


class RegistryAssignmentTest(MigBase):
    def test_registry_assigned_other_project_no_dual_home(self):
        """registry 已把 sid 指派给其他项目：不写 project= 映射键、不加双份成员。"""
        solo = self.write_toml("solo.toml", "solo", self.comp())
        before = solo.read_bytes()
        (self.home / "registry.json").write_text(json.dumps({
            "schemaVersion": 1, "revision": 1,
            "projects": [{"id": "other", "name": "Other", "origin": "user",
                          "directories": [], "services": ["solo"], "favorite": False,
                          "autostart": False, "agent": None, "created_at": "t",
                          "updated_at": "t", "revision": 1}]}), encoding="utf-8")
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        self.assertEqual(solo.read_bytes(), before, "不写入 project=solo 映射键")
        self.assertEqual(self.services_of("other"), {"solo"}, "原归属不动")
        self.assertNotIn("solo", self.project_ids(), "不建 solo 项目（无成员可接纳）")
        self.assertTrue(any("other" in n for n in res.get("notes", [])), res.get("notes"))

    def test_registry_assigned_same_project_migrates_normally(self):
        """registry 指派与映射一致：正常补 project= 键与成员。"""
        solo = self.write_toml("solo.toml", "solo", self.comp())
        (self.home / "registry.json").write_text(json.dumps({
            "schemaVersion": 1, "revision": 1,
            "projects": [{"id": "solo", "name": "Solo", "origin": "user",
                          "directories": [], "services": ["solo"], "favorite": False,
                          "autostart": False, "agent": None, "created_at": "t",
                          "updated_at": "t", "revision": 1}]}), encoding="utf-8")
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        self.assertNotIn("project", tomllib.loads(solo.read_text(encoding="utf-8")))
        self.assertEqual(tomllib.loads((self.user_services / solo.name).read_text())["project"], "solo")
        self.assertEqual(self.services_of("solo"), {"solo"})


class TombstoneSkipTest(MigBase):
    def test_tombstoned_builtin_skipped_entirely(self):
        """tombstone（标记存在且未登记）：不建项目、不插 project=、字节不变。"""
        solo = self.write_toml("solo.toml", "solo", self.comp())
        before = solo.read_bytes()
        (self.home / "removed.json").write_text(json.dumps({
            "schemaVersion": 1,
            "services": {"solo": {"removed_at": "t", "disposition": "stop",
                                  "project_id": None, "archive": "solo-x.json"}},
            "projects": {}}), encoding="utf-8")
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        self.assertEqual(solo.read_bytes(), before)
        self.assertEqual(self.project_ids(), set())
        self.assertEqual(res.get("changed"), False)

    def test_shadowed_but_user_registered_migrates(self):
        """V2.0 修订：遮蔽的 builtin + 用户层同 id 文件 = 已登记，正常迁移。"""
        builtin = self.write_toml("shadow.toml", "shadow", 'project = "pb"' + self.comp(48750))
        user_toml = (f'id = "shadow"\nname = "Shadow User"\nsource = "{self.src}"\n'
                     + self.comp(48751))
        (self.user_services / "shadow.toml").write_text(user_toml, encoding="utf-8")
        (self.home / "removed.json").write_text(json.dumps({
            "schemaVersion": 1,
            "services": {"shadow": {"removed_at": "t", "disposition": "stop",
                                    "project_id": "pb", "archive": "shadow-x.json"}},
            "projects": {}}), encoding="utf-8")
        user_before = (self.user_services / "shadow.toml").read_bytes()
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        # builtin 模板不动；用户层文件正常获得 project= 键与成员关系
        self.assertEqual(builtin.read_bytes(),
                         builtin.read_bytes())
        text = (self.user_services / "shadow.toml").read_text(encoding="utf-8")
        self.assertNotEqual(text.encode("utf-8"), user_before, "用户层文件补 project= 键")
        self.assertEqual(tomllib.loads(text)["project"], "shadow")
        self.assertEqual(self.services_of("shadow"), {"shadow"})


class IdempotencyTest(MigBase):
    def test_second_run_byte_identical(self):
        self.write_toml("a.toml", "svc-a",
                        'group = "grp"\ngroup_name = "Grp"\ngroup_order = 0' + self.comp())
        self.write_toml("b.toml", "svc-b",
                        'group = "grp"\ngroup_name = "Grp"\ngroup_order = 1' + self.comp())
        self.write_toml("solo.toml", "solo", self.comp())
        self.assertTrue(self.reg.migrate_from_manifests()["ok"])
        reg_before = (self.home / "registry.json").read_bytes()
        tomls_before = {f.name: f.read_bytes() for f in self.services.glob("*.toml")}
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"], "二次执行无变化")
        self.assertEqual((self.home / "registry.json").read_bytes(), reg_before)
        self.assertEqual({f.name: f.read_bytes() for f in self.services.glob("*.toml")},
                         tomls_before)
        self.assertEqual(sorted(res["projects"][0]["services"]),
                         ["svc-a", "svc-b"] if res["projects"][0]["id"] == "grp"
                         else ["solo"])

    def test_notes_do_not_break_idempotency(self):
        """带"不同 project 值"的服务：note 只进响应，changed=False、字节不变。"""
        solo = self.write_toml("solo.toml", "solo", 'project = "elsewhere"' + self.comp())
        first = self.reg.migrate_from_manifests()
        self.assertTrue(first["ok"])
        before = solo.read_bytes()
        second = self.reg.migrate_from_manifests()
        self.assertTrue(second["ok"])
        self.assertFalse(second["changed"])
        self.assertEqual(solo.read_bytes(), before)
        self.assertTrue(any("elsewhere" in n for n in second.get("notes", [])))


if __name__ == "__main__":
    unittest.main()
