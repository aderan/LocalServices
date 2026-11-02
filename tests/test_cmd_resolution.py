"""V2 集成补充：cmd[0] 按组件 cwd 解析（monorepo 前端 node_modules/.bin 形态）。

ShiCha 重加场景暴露：旧内置清单 cmd=["node_modules/.bin/vite"] + cwd="apps/admin-web"
从未走过注册校验；注册路径原来只按 source 根解析，会拒绝这种标准 monorepo 形态。
运行时 runner 在 comp cwd 下 execvpe，校验必须与运行时同源。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from localservices.registry import _resolve_cmd0  # noqa: E402


class CmdResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-cmdres-"))
        bin_dir = self.tmp / "apps" / "web" / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        tool = bin_dir / "ls-v2-unique-tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cwd_relative_bin_resolves(self):
        self.assertTrue(_resolve_cmd0("node_modules/.bin/ls-v2-unique-tool", self.tmp, Path("apps/web")))

    def test_cwd_relative_without_cwd_context_fails(self):
        # 不带组件 cwd 时保持旧语义（source 根查找）——该目录下确实没有
        self.assertFalse(_resolve_cmd0("node_modules/.bin/ls-v2-unique-tool", self.tmp, None))

    def test_absolute_still_works(self):
        self.assertTrue(_resolve_cmd0("/usr/bin/true", self.tmp, None))


if __name__ == "__main__":
    unittest.main()
