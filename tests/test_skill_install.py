"""Skill 线安装器与 SKILL.md 静态契约测试（CONTRACTS §6）。

全程临时目录：LOCALSERVICES_HOME / LOCALSERVICES_SKILL_DIR（部分用例再加
LOCALSERVICES_SKILL_SRC）都指向 tmp，绝不触碰真实 ~/.codex 与 ~/Library。
真实源（仓库 skills/localservices/）在全新安装、幂等与静态断言用例中使用；
更新/冲突用例用 LOCALSERVICES_SKILL_SRC 指向临时副本，避免改仓库文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install-skill.sh"
UNINSTALL = REPO / "scripts" / "uninstall-skill.sh"
REAL_SRC = REPO / "skills" / "localservices"
SKILL_MD = REAL_SRC / "SKILL.md"
BUILD_PAYLOAD = REAL_SRC / "scripts" / "build_payload.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def backups_under(skills_parent: Path) -> list[Path]:
    return sorted(skills_parent.glob("localservices.user-bak-*"))


class SkillInstallBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-skill-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill_dir = self.root / "codex" / "skills" / "localservices"
        self.ls_home = self.root / "home"
        self.record = self.ls_home / "skill-install.json"
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_SKILL_DIR": str(self.skill_dir),
            "LOCALSERVICES_HOME": str(self.ls_home),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_script(self, script: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["/bin/bash", str(script), *args], env=env,
                              capture_output=True, text=True, timeout=120)

    def install(self, **kw) -> subprocess.CompletedProcess:
        r = self.run_script(INSTALL, **kw)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def read_record(self) -> dict:
        return json.loads(self.record.read_text())


class InstallFlowTest(SkillInstallBase):
    def test_fresh_install_files_and_record(self):
        out = self.install()
        self.assertIn(str(self.skill_dir), out.stdout)
        self.assertTrue(SKILL_MD.name and (self.skill_dir / "SKILL.md").is_file())
        for rel in ("references/payload-format.md", "scripts/build_payload.py"):
            src, dst = REAL_SRC / rel, self.skill_dir / rel
            self.assertTrue(dst.is_file(), rel)
            self.assertEqual(src.read_bytes(), dst.read_bytes(), rel)

        rec = self.read_record()
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["target"], str(self.skill_dir))
        self.assertTrue(rec["installed_at"])
        head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertIn(rec["source_commit"], (head, "unknown"))
        for rel, digest in rec["files"].items():
            self.assertEqual(sha256(self.skill_dir / rel), digest, rel)
        self.assertEqual(set(rec["files"]),
                         {"SKILL.md", "references/payload-format.md", "scripts/build_payload.py"})
        self.assertFalse(backups_under(self.skill_dir.parent), "全新安装不应有备份")

    def test_reinstall_idempotent_bytes_unchanged(self):
        self.install()
        before = {p.relative_to(self.skill_dir).as_posix(): p.read_bytes()
                  for p in self.skill_dir.rglob("*") if p.is_file()}
        out = self.install()
        self.assertIn("已更新", out.stdout)
        after = {p.relative_to(self.skill_dir).as_posix(): p.read_bytes()
                 for p in self.skill_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "重复安装必须字节不变")
        self.assertFalse(backups_under(self.skill_dir.parent), "幂等重装不应有备份")

    def test_update_after_source_change_no_backup(self):
        src = self.root / "src-skill"
        subprocess.run(["cp", "-R", str(REAL_SRC), str(src)], check=True)
        self.install(extra_env={"LOCALSERVICES_SKILL_SRC": str(src)})

        (src / "SKILL.md").write_text((src / "SKILL.md").read_text() + "\n<!-- v2 -->\n")
        (src / "references" / "new-note.md").write_text("v2 note\n")
        (src / "references" / "payload-format.md").unlink()
        out = self.install(extra_env={"LOCALSERVICES_SKILL_SRC": str(src)})

        self.assertIn("已更新", out.stdout)
        self.assertIn("<!-- v2 -->", (self.skill_dir / "SKILL.md").read_text())
        self.assertTrue((self.skill_dir / "references" / "new-note.md").is_file())
        self.assertFalse((self.skill_dir / "references" / "payload-format.md").exists(),
                         "update 模式应移除新版已删且未被用户改动的旧文件")
        self.assertFalse(backups_under(self.skill_dir.parent), "自家未改动内容更新不应备份")

    def test_third_party_dir_backed_up(self):
        self.skill_dir.mkdir(parents=True)
        sentinel = self.skill_dir / "keepme.txt"
        sentinel.write_text("第三方内容：别删我\n")
        out = self.install()

        baks = backups_under(self.skill_dir.parent)
        self.assertEqual(len(baks), 1, "第三方目录必须先备份")
        self.assertEqual((baks[0] / "keepme.txt").read_text(), "第三方内容：别删我\n")
        self.assertIn(str(baks[0]), out.stdout, "备份路径必须打印给用户")
        self.assertTrue((self.skill_dir / "SKILL.md").is_file(), "备份后应装上新版")
        self.assertEqual((self.skill_dir / "keepme.txt").read_text(), "第三方内容：别删我\n",
                         "未记账的用户文件不删")

    def test_user_modified_file_backed_up_and_preserved(self):
        self.install()
        (self.skill_dir / "SKILL.md").write_text("# 用户手改版\n")
        (self.skill_dir / "user-extra.md").write_text("用户加的\n")
        self.install()

        baks = backups_under(self.skill_dir.parent)
        self.assertEqual(len(baks), 1, "用户改过 -> 整目录备份")
        self.assertEqual((baks[0] / "SKILL.md").read_text(), "# 用户手改版\n",
                         "用户的改动必须留在备份里")
        self.assertEqual((baks[0] / "user-extra.md").read_text(), "用户加的\n")
        self.assertEqual((self.skill_dir / "SKILL.md").read_bytes(), SKILL_MD.read_bytes(),
                         "安装后目标应是新版")
        self.assertEqual((self.skill_dir / "user-extra.md").read_text(), "用户加的\n")

    def test_corrupt_record_treated_as_third_party(self):
        self.install()
        self.record.write_text("not json {{{ broken")
        self.install()
        baks = backups_under(self.skill_dir.parent)
        self.assertEqual(len(baks), 1, "记录损坏按第三方处理走备份")
        self.assertTrue((baks[0] / "SKILL.md").is_file())
        self.assertTrue((self.skill_dir / "SKILL.md").is_file())


class UninstallFlowTest(SkillInstallBase):
    def test_uninstall_clean_via_wrapper(self):
        self.install()
        out = self.run_script(UNINSTALL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(self.skill_dir.exists(), "干净卸载后目录应删除")
        self.assertIn("已卸载", out.stdout)
        self.assertFalse(self.record.exists(), "全部移除后记录应删除")

    def test_uninstall_keeps_user_modified(self):
        self.install()
        (self.skill_dir / "SKILL.md").write_text("# 用户手改版\n")
        out = self.run_script(INSTALL, "--uninstall")   # --uninstall 参数入口
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((self.skill_dir / "SKILL.md").read_text(), "# 用户手改版\n",
                         "用户改过的文件必须保留")
        self.assertFalse((self.skill_dir / "references").exists() or
                         (self.skill_dir / "references" / "payload-format.md").exists(),
                         "未改动的安装文件应删除")
        rec = self.read_record()
        self.assertEqual(set(rec["files"]), {"SKILL.md"}, "记录应只剩保留的文件")

    def test_uninstall_without_record_is_noop(self):
        self.ls_home.mkdir(parents=True)
        out = self.run_script(UNINSTALL)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("没有安装记录", out.stdout + out.stderr)


class SkillContentTest(unittest.TestCase):
    """SKILL.md 静态契约：frontmatter 与关键行为约束词。"""

    def test_frontmatter(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "frontmatter 必须在文件头")
        fm = text.split("---\n", 2)[1]
        self.assertRegex(fm, r"(?m)^name:\s*localservices\s*$")
        self.assertRegex(fm, r"(?m)^description:\s*\S")
        desc = next(l for l in fm.splitlines() if l.startswith("description:"))
        self.assertGreater(len(desc), 30, "description 应覆盖触发场景")
        for trigger in ("注册", "启动", "LocalServices"):
            self.assertIn(trigger, desc)

    def test_body_constraints(self):
        body = SKILL_MD.read_text(encoding="utf-8").split("---\n", 2)[2]
        for needle in (
            "lsctl register",          # 注册走 CLI
            "--json",                  # 机器可读输出
            "--open", "--wait",        # 启动并打开的真实调用
            "绝不直接写",              # 不写状态文件
            "Application Support/LocalServices",
            "绝不递归调用",            # 不递归调模型 CLI
            "~/.codex",                # 不动用户 codex 配置
            "draft",                   # 不确定字段走草稿
            "unresolved",
            '["make", "<target>"]',    # argv 数组示例
        ):
            self.assertIn(needle, body, f"SKILL.md 正文缺少关键约束：{needle}")

    def test_payload_reference_and_script_exist(self):
        ref = REAL_SRC / "references" / "payload-format.md"
        self.assertIn('"schemaVersion": 1', ref.read_text(encoding="utf-8"))
        self.assertIn('"cmd": ["make", "serve-api"]', ref.read_text(encoding="utf-8"))
        self.assertIn("unresolved", ref.read_text(encoding="utf-8"))
        self.assertTrue(BUILD_PAYLOAD.is_file())


class BuildPayloadTest(unittest.TestCase):
    def run_tool(self, stdin_text: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(BUILD_PAYLOAD), *args],
                              input=stdin_text, capture_output=True, text=True, timeout=30)

    def test_valid_payload_exit0(self):
        r = self.run_tool(
            '{"service":{"name":"Demo","components":[{"id":"api","kind":"server",'
            '"cmd":["make","serve"],"port":4927,"ready":{"http":"http://127.0.0.1:4927/h"}}]}}',
            "--source", "/tmp/demo", "--project-id", "demo", "--agent", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["schemaVersion"], 1)
        self.assertEqual(out["service"]["source"], "/tmp/demo")
        self.assertEqual(out["project"]["id"], "demo")
        self.assertEqual(out["project"]["agent"], "codex")

    def test_cmd_string_rejected(self):
        r = self.run_tool('{"service":{"name":"D","source":"/tmp/d","components":'
                          '[{"id":"a","cmd":"make serve"}]}}')
        self.assertEqual(r.returncode, 2)
        self.assertIn("argv", r.stderr)
        self.assertEqual(r.stdout, "", "结构错误不输出 payload")

    def test_incomplete_becomes_draft_exit3(self):
        r = self.run_tool('{"service":{"name":"Demo","source":"/tmp/d","components":[]}}')
        self.assertEqual(r.returncode, 3)
        out = json.loads(r.stdout)
        self.assertTrue(out["service"]["draft"])
        self.assertIn("components", r.stderr)

    def test_bad_json_rejected(self):
        r = self.run_tool("{oops")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
