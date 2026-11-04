"""review R3：/etc/resolver/<suffix> 所有权保护。

安装：不存在 → 写入（带管理标记）；本工具旧文件 → 幂等替换；他人配置 → 拒绝且零改动。
卸载：本工具文件 → 删除；他人配置 → 拒绝删除，保持原样。
全部用隔离目录替身，不碰真实 /etc/resolver。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import locove_resolver as lr  # noqa: E402

FOREIGN = "# 由内网 DNS 工具管理\nnameserver 10.0.0.2\nport 53\n"
# 2026-11-04 之前安装器写的内容（无 managed-by 标记）——是本工具自己的文件
LEGACY = ("# Locove／洛湾 split DNS：仅把 .locove 交给本地 DNS（由 LocalServices 管理）\n"
          "nameserver 127.0.0.1\nport 5354\nsearch_order 1\n")


class ResolverOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-resolver-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def path(self, suffix="locove"):
        return self.dir / suffix

    def test_install_writes_owned_file(self):
        res = lr.install(self.dir, 5354)
        self.assertTrue(res["ok"])
        body = self.path().read_text()
        self.assertIn(lr.MARKER, body)
        self.assertIn("nameserver 127.0.0.1", body)
        self.assertIn("port 5354", body)

    def test_install_idempotent_over_own_file(self):
        lr.install(self.dir, 5354)
        res = lr.install(self.dir, 5355)  # 端口变化 → 正常升级替换
        self.assertTrue(res["ok"])
        self.assertIn("port 5355", self.path().read_text())

    def test_install_refuses_foreign_content_and_keeps_it(self):
        self.path().write_text(FOREIGN)
        res = lr.install(self.dir, 5354)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "foreign")
        self.assertIn("拒绝覆盖", res["error"])
        self.assertEqual(self.path().read_text(), FOREIGN)  # 他人配置零改动

    def test_uninstall_removes_owned_file(self):
        lr.install(self.dir, 5354)
        res = lr.uninstall(self.dir)
        self.assertTrue(res["ok"])
        self.assertFalse(self.path().exists())

    def test_uninstall_absent_is_ok(self):
        res = lr.uninstall(self.dir)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "absent")

    def test_uninstall_refuses_foreign_file(self):
        self.path().write_text(FOREIGN)
        res = lr.uninstall(self.dir)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "foreign")
        self.assertEqual(self.path().read_text(), FOREIGN)  # 绝不删除他人配置

    def test_legacy_own_file_upgrades_and_uninstalls(self):
        """旧版安装器写的文件（无标记）：升级替换 + 卸载删除都应认领，不被自己的检查挡住。"""
        self.path().write_text(LEGACY)
        res = lr.install(self.dir, 5354)
        self.assertTrue(res["ok"], res)
        self.assertIn(lr.MARKER, self.path().read_text())
        res = lr.uninstall(self.dir)
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.path().exists())

    def test_suffix_isolates_files(self):
        lr.install(self.dir, 5354, suffix="lab")
        self.assertTrue((self.dir / "lab").exists())
        res = lr.uninstall(self.dir, suffix="locove")
        self.assertTrue(res["ok"])  # 不同后缀互不干扰
        self.assertTrue((self.dir / "lab").exists())

    def test_cli_exit_codes(self):
        self.assertEqual(lr.main(["install", "--dir", str(self.dir), "--port", "5354"]), 0)
        (self.dir / "locove").write_text(FOREIGN)
        self.assertEqual(lr.main(["uninstall", "--dir", str(self.dir)]), 1)

    def test_no_temp_residue_after_refusal_or_success(self):
        lr.install(self.dir, 5354)
        (self.dir / "locove").write_text(FOREIGN)
        lr.install(self.dir, 5354)
        leftovers = [p for p in self.dir.iterdir() if p.name != "locove"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
