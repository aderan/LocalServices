"""迁移测试（CONTRACTS §7）：group→Project、无 group 各自成 Project、source→Directory、
TOML 追加 project=（含内置目录）、二次执行幂等且字节不变、失败恢复原字节、不动 state.json。
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

from localservices import manifest, paths  # noqa: E402
from localservices import registry as reg  # noqa: E402


class MigrationBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsmig-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        for d in (self.home, self.services, self.user_services):
            d.mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.reg = reg.Registry()
        self.write_manifests()

    def write_manifests(self):
        src_a = self.root / "srcA"
        src_a2 = self.root / "srcA2"
        src_b = self.root / "srcB"
        for d in (src_a, src_a2, src_b):
            d.mkdir()
        # link-b -> srcB：迁移登记 Directory 时应 resolve 为真实路径
        os.symlink(src_b, self.root / "link-b")
        (self.services / "explore-x.toml").write_text(
            f'id = "explore-x"\nname = "Explore X"\nsource = "{src_a}"\n'
            f'group = "ui-atlas"\ngroup_name = "UI-Atlas 工作区"\ngroup_order = 0\n'
            f'[[components]]\nid = "api"\ncmd = ["true"]\nport = 47901\n')
        (self.services / "ui-atlas-x.toml").write_text(
            f'id = "ui-atlas-x"\nname = "UI-Atlas X"\nsource = "{src_a2}"\n'
            f'group = "ui-atlas"\ngroup_name = "UI-Atlas 工作区"\ngroup_order = 1\n'
            f'[[components]]\nid = "web"\ncmd = ["true"]\nport = 47902\n')
        (self.services / "solo.toml").write_text(
            f'id = "solo"\nname = "Solo"\nsource = "{self.root}/link-b"\n'
            f'[[components]]\nid = "web"\ncmd = ["true"]\nport = 47903\n')
        return {"srcA": src_a, "srcA2": src_a2, "srcB": src_b}

    def toml_bytes(self):
        return {f.name: f.read_bytes() for f in sorted(self.services.glob("*.toml"))}


class FirstMigrationTest(MigrationBase):
    def test_mapping_and_toml_project_field(self):
        srcs = {"explore-x": (self.root / "srcA").resolve(),
                "ui-atlas-x": (self.root / "srcA2").resolve(),
                "solo": (self.root / "srcB").resolve()}
        before = {pid: {(c.id, c.port) for c in p.managed_components()} for pid, p in manifest.load_all().items()}

        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["changed"])

        data = self.reg.data()
        self.assertEqual(data["schemaVersion"], 1)
        by_id = {p["id"]: p for p in data["projects"]}
        self.assertEqual(set(by_id), {"ui-atlas", "solo"})
        grp = by_id["ui-atlas"]
        self.assertEqual(grp["name"], "UI-Atlas 工作区")
        self.assertEqual(grp["origin"], "migrated")
        self.assertEqual(grp["services"], ["explore-x", "ui-atlas-x"])  # 按 group_order 排序
        self.assertEqual({d["path"] for d in grp["directories"]},
                         {str(srcs["explore-x"]), str(srcs["ui-atlas-x"])})
        self.assertEqual({d["role"] for d in grp["directories"]}, {"source"})
        solo = by_id["solo"]
        self.assertEqual(solo["services"], ["solo"])
        self.assertEqual([d["path"] for d in solo["directories"]], [str(srcs["solo"])])  # symlink 已 resolve

        # TOML 追加 project= 且仍可被清单引擎加载；id/端口不变；state.json 未被创建
        loaded = manifest.load_all()
        self.assertEqual(loaded["explore-x"].project, "ui-atlas")
        self.assertEqual(loaded["ui-atlas-x"].project, "ui-atlas")
        self.assertEqual(loaded["solo"].project, "solo")
        for pid, ports in before.items():
            self.assertEqual({(c.id, c.port) for c in loaded[pid].managed_components()}, ports)
            self.assertEqual(str(Path(loaded[pid].source).resolve()), str(srcs[pid]))
        self.assertFalse(paths.state_file().exists())
        # registry.json 不存在时的首迁移标记
        self.assertTrue((self.home / "registry.json.migration-backup").exists())
        # 映射明细供证据
        mapped = {p["id"]: p["services"] for p in res["projects"]}
        self.assertEqual(mapped["ui-atlas"], ["explore-x", "ui-atlas-x"])
        self.assertEqual(mapped["solo"], ["solo"])

    def test_project_line_lands_at_top_level(self):
        self.reg.migrate_from_manifests()
        # project= 必须位于第一个 [[components]] 之前，而不是落入最后一个组件表
        text = (self.services / "solo.toml").read_text()
        self.assertLess(text.index('project = "solo"'), text.index("[[components]]"))
        import tomllib
        self.assertEqual(tomllib.loads(text)["project"], "solo")
        self.assertEqual(tomllib.loads(text)["components"][0]["id"], "web")


class IdempotentTest(MigrationBase):
    def test_second_run_changes_nothing(self):
        self.reg.migrate_from_manifests()
        reg_before = (self.home / "registry.json").read_bytes()
        toml_before = self.toml_bytes()
        res = self.reg.migrate_from_manifests()
        self.assertTrue(res["ok"])
        self.assertFalse(res["changed"])
        self.assertEqual((self.home / "registry.json").read_bytes(), reg_before)
        self.assertEqual(self.toml_bytes(), toml_before)


class FailureTest(MigrationBase):
    def test_midway_failure_restores_original_bytes(self):
        original = self.toml_bytes()
        real_write = reg._write_bytes_atomic
        calls = {"n": 0}

        def flaky(path, data):
            if str(path).endswith(".toml"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated write failure")
            return real_write(path, data)

        with patch.object(reg, "_write_bytes_atomic", side_effect=flaky):
            res = self.reg.migrate_from_manifests()
        self.assertFalse(res["ok"])
        self.assertIn("恢复", res["error"])
        self.assertEqual(self.toml_bytes(), original)  # 已写/未写文件一律恢复原字节
        self.assertFalse((self.home / "registry.json").exists())
        self.assertFalse((self.home / "registry.json.migration-backup").exists())


if __name__ == "__main__":
    unittest.main()
