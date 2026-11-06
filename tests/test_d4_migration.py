"""D4：旧 80 系统件迁移/退役（2026-11-06 Goal）。

- retire-locove-80.sh 默认 dry-run：只盘点零改动；--apply 无 root 明确拒绝。
- 迁移只处理确认归属的件（com.mybrain.ls.locove.* 命名）；业务进程/数据/登记/用户件不动。
- resolver 归属守卫贯穿：他人文件在任何清理路径都不被删除（脚本经 locove_resolver 模块）。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RETIRE = REPO / "scripts" / "retire-locove-80.sh"
UNINSTALL = REPO / "scripts" / "uninstall-locove-system.sh"


def run_script(script: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LOCOVE_RESOLVER_DIR", None)
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          cwd=str(REPO), env=env, timeout=60)


@unittest.skipIf(os.geteuid() == 0, "非 root 前提的用例（root 下拒绝分支不可复现）")
class RetireScriptTest(unittest.TestCase):
    def test_dryrun_reports_and_changes_nothing(self):
        before = sorted((str(p), p.stat().st_mtime) for p in
                        Path("/Library/LaunchDaemons").glob("*.plist")) if Path("/Library/LaunchDaemons").exists() else []
        r = run_script(RETIRE)
        self.assertEqual(r.returncode, 0, r.stderr)
        for marker in ("盘点", "迁移计划", "保留", "移除", "resolver"):
            self.assertIn(marker, r.stdout)
        after = sorted((str(p), p.stat().st_mtime) for p in
                       Path("/Library/LaunchDaemons").glob("*.plist")) if Path("/Library/LaunchDaemons").exists() else []
        self.assertEqual(before, after, "dry-run 不得改动任何系统 plist")

    def test_apply_without_root_refuses(self):
        r = run_script(RETIRE, "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("管理员授权", r.stderr + r.stdout)
        self.assertNotIn("removed:", r.stdout, "非 root 不得执行任何移除")

    def test_only_owned_targets_are_touched(self):
        """静态守卫：脚本里所有 rm/bootout 目标必须是本工具专属命名。"""
        src = RETIRE.read_text(encoding="utf-8")
        dangerous = re.findall(r'(?:rm|bootout)[^\n]*', src)
        self.assertTrue(dangerous)
        for line in dangerous:
            self.assertRegex(line, r"com\.mybrain\.ls\.locove|Locove|ENTRY_|LIBEXEC_DIR",
                             f"非本工具命名的目标不得出现：{line}")

    def test_removes_resolver_only_via_guarded_module(self):
        """--remove-resolver 必须经 locove_resolver 模块（归属守卫），不得直接 rm resolver。"""
        src = RETIRE.read_text(encoding="utf-8")
        self.assertIn("localservices.locove_resolver uninstall", src)
        self.assertNotRegex(src, r"rm\s+[^\n]*RESOLVER_DIR|rm\s+[^\n]*/etc/resolver/",
                            "resolver 不得被 rm 直接删除")


class UninstallScriptGuardTest(unittest.TestCase):
    def test_resolver_only_mode_preserves_foreign_file(self):
        """--resolver-only 对他人 resolver 文件零动作（S2 守卫在脚本层生效）。"""
        with tempfile.TemporaryDirectory(prefix="lsd4-") as td:
            rdir = Path(td) / "resolver"
            rdir.mkdir()
            foreign = rdir / "locove"
            foreign.write_text("# corporate VPN split dns\nnameserver 10.0.0.1\n")
            r = run_script(UNINSTALL, "--resolver-only",
                           env_extra={"LOCOVE_RESOLVER_DIR": str(rdir)})
            # 非 root：脚本应拒绝执行系统级清理并保留文件
            self.assertIn("需要 root", r.stdout + r.stderr)
            self.assertTrue(foreign.exists(), "非 root 时文件绝不动")

    def test_user_only_mode_does_not_touch_resolver(self):
        with tempfile.TemporaryDirectory(prefix="lsd4-") as td:
            rdir = Path(td) / "resolver"
            rdir.mkdir()
            f = rdir / "locove"
            f.write_text("nameserver 10.0.0.1\n")
            la = Path(td) / "LA"
            home = Path(td) / "home"
            la.mkdir()
            home.mkdir()
            r = run_script(UNINSTALL, "--user-only",
                           env_extra={"LOCOVE_RESOLVER_DIR": str(rdir),
                                      "LOCOVE_LAUNCHAGENTS": str(la),
                                      "LOCALSERVICES_HOME": str(home)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(f.exists(), "user-only 卸载不碰 resolver")
            self.assertIn("USER PARTS REMOVED", r.stdout)


if __name__ == "__main__":
    unittest.main()
