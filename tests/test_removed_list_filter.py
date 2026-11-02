"""真实 Chat 使用发现：项目移除后以同 id 重建（或全部成员重新登记）时，
GET /api/removed 的项目条目必须过滤，否则建议"恢复"一个已在运行的项目。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localservices import paths

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TOML = """id = "{sid}"
name = "{sid}"
source = "{src}"

[[components]]
id = "web"
kind = "server"
cmd = ["/usr/bin/true"]
cwd = "."
"""


class RemovedListFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-rmlist-"))
        (self.tmp / "s").mkdir(); (self.tmp / "u").mkdir(); (self.tmp / "h").mkdir()
        (self.tmp / "src").mkdir()
        self.env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.tmp / "h"),
            "LOCALSERVICES_SERVICES": str(self.tmp / "s"),
            "LOCALSERVICES_USER_SERVICES": str(self.tmp / "u")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_removed(self, data: dict):
        paths.removed_file().write_text(json.dumps(data), encoding="utf-8")

    def test_recreated_project_entry_filtered(self):
        from localservices import tombstones
        from localservices.registry import Registry
        # 用户流程：项目 p 移除（条目入 removed.json）→ 用户以同 id 重建项目并重新登记成员
        (self.tmp / "u" / "svc.toml").write_text(TOML.format(sid="svc", src=self.tmp / "src"))
        reg = Registry()
        reg.ensure_project("p", "P", None, None)
        self._write_removed({"schemaVersion": 1, "services": {},
                             "projects": {"p": {"removed_at": "x", "disposition": "stop",
                                                "services": ["svc"],
                                                "project": {"id": "p", "name": "P"}}}})
        out = reg.removed_list()
        self.assertEqual(out["projects"], [], "已重建的项目条目不应再列出")

    def test_all_members_reregistered_filters_entry(self):
        from localservices.registry import Registry
        (self.tmp / "u" / "svc.toml").write_text(TOML.format(sid="svc", src=self.tmp / "src"))
        reg = Registry()
        reg.ensure_project("q", "Q", None, None)  # 项目 id 不同（未按原名重建）
        self._write_removed({"schemaVersion": 1, "services": {},
                             "projects": {"p": {"removed_at": "x", "disposition": "stop",
                                                "services": ["svc"],
                                                "project": {"id": "p", "name": "P"}}}})
        out = reg.removed_list()
        self.assertEqual(out["projects"], [], "全部成员已重新登记的条目不应再列出")

    def test_genuinely_removed_project_still_listed(self):
        from localservices.registry import Registry
        reg = Registry()  # 空注册表
        self._write_removed({"schemaVersion": 1, "services": {},
                             "projects": {"gone": {"removed_at": "x", "disposition": "stop",
                                                   "services": ["old"], "project": {}}}})
        out = reg.removed_list()
        self.assertEqual([p["pid"] for p in out["projects"]], ["gone"])


if __name__ == "__main__":
    unittest.main()
