"""scanner 只读证据提取：真实入口文件被提炼，噪音被剪掉，限额生效。"""
import unittest
from pathlib import Path
import tempfile

from localservices.agents import scanner


class ScannerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "sub" / "deeper").mkdir(parents=True)
        (self.root / "node_modules" / "left-pad").mkdir(parents=True)
        (self.root / ".hidden").mkdir()
        (self.root / "node_modules" / "left-pad" / "package.json").write_text('{"name":"left-pad"}')
        (self.root / ".hidden" / "Makefile").write_text("secret:\n\techo hidden\n")
        (self.root / "Makefile").write_text(
            "# dev targets\n"
            ".PHONY: dev build\n"
            "dev:\n\tnpm run dev\n"
            "build:\n\tnpm run build\n"
            "sources-manager:\n\tpython3 -m localservices.daemon\n"
            "install:\n\tnpm install\n"
        )
        (self.root / "package.json").write_text(
            '{"name": "demo-app", "scripts": {"dev": "vite", "build": "tsc", "test": "vitest"}}'
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'demo'\n"
            "[project.scripts]\ndemo = 'demo.cli:main'\nother = 'demo.x:y'\n"
            "[tool.ruff]\nline-length = 100\n"
            "[tool.pytest.ini_options]\naddopts = '-q'\n"
        )
        (self.root / "docker-compose.yml").write_text(
            "version: '3'\n"
            "services:\n"
            "  web:\n    build: .\n"
            "  db:\n    image: postgres:16\n"
            "volumes:\n  data:\n"
        )
        (self.root / "README.md").write_text(
            "# Demo\n\nDev setup:\n\n```sh\nnpm install\nmake dev\n```\n\n## Notes\nplain text\n"
        )
        (self.root / ".env.example").write_text(
            "# comment\nSECRET_KEY=supersecret\nPORT=8000\nDATABASE_URL=postgres://user:pw@localhost/x\n"
        )
        (self.root / "sub" / "Makefile").write_text("inner:\n\techo inner\n")
        (self.root / "sub" / "deeper" / "package.json").write_text('{"name":"deep","scripts":{"dev":"x"}}')
        (self.root / "bigfile.bin").write_bytes(b"x" * (scanner.MAX_FILE_BYTES + 10))

    def _kinds(self, result):
        return {e["kind"] for e in result["entries"]}

    def _by_kind(self, result, kind):
        return [e for e in result["entries"] if e["kind"] == kind]

    def test_extracts_all_evidence_kinds(self):
        r = scanner.scan_directory(self.root)
        kinds = self._kinds(r)
        self.assertIn("makefile", kinds)
        self.assertIn("package_json", kinds)
        self.assertIn("pyproject", kinds)
        self.assertIn("compose", kinds)
        self.assertIn("readme", kinds)
        self.assertIn("env_example", kinds)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["directory"], str(self.root.resolve()))

    def test_makefile_targets_and_interesting_lines(self):
        r = scanner.scan_directory(self.root)
        d = self._by_kind(r, "makefile")[0]["detail"]
        for t in ("dev", "build", "sources-manager", "install"):
            self.assertIn(t, d)

    def test_package_and_pyproject_details(self):
        r = scanner.scan_directory(self.root)
        self.assertIn("demo-app", self._by_kind(r, "package_json")[0]["detail"])
        py = self._by_kind(r, "pyproject")[0]["detail"]
        self.assertIn("demo", py)          # project.scripts 名
        self.assertIn("tool.ruff", py)

    def test_readme_keeps_documented_local_browser_entry(self):
        (self.root / "README.md").write_text(
            "# Demo\nOpen [the admin page](http://localhost:8123/admin).\n")
        result = scanner.scan_directory(self.root)
        self.assertIn("http://localhost:8123/admin", self._by_kind(result, "readme")[0]["detail"])

    def test_compose_services_top_level_keys(self):
        r = scanner.scan_directory(self.root)
        d = self._by_kind(r, "compose")[0]["detail"]
        self.assertIn("web", d)
        self.assertIn("db", d)
        self.assertNotIn("data", d)  # volumes 顶层键不混入

    def test_env_values_masked(self):
        r = scanner.scan_directory(self.root)
        d = self._by_kind(r, "env_example")[0]["detail"]
        self.assertIn("SECRET_KEY", d)
        self.assertIn("DATABASE_URL", d)
        self.assertNotIn("supersecret", d)
        self.assertNotIn("user:pw", d)

    def test_noise_directories_skipped(self):
        r = scanner.scan_directory(self.root)
        paths = " ".join(e["path"] for e in r["entries"])
        self.assertNotIn("node_modules/left-pad", paths.replace("node_modules 存在", ""))
        self.assertNotIn(".hidden", paths)
        hidden = [e for e in r["entries"] if ".hidden" in e["path"]]
        self.assertFalse(hidden)

    def test_install_hint_recorded(self):
        r = scanner.scan_directory(self.root)
        hints = self._by_kind(r, "install_hint")
        self.assertTrue(any("node_modules" in h["path"] for h in hints))

    def test_depth_limit(self):
        r = scanner.scan_directory(self.root, max_depth=1)
        paths = [e["path"] for e in r["entries"]]
        self.assertNotIn(Path("sub") / "Makefile", [Path(p) for p in paths])
        r2 = scanner.scan_directory(self.root, max_depth=2)
        self.assertTrue(any(e["path"] == "sub/Makefile" for e in r2["entries"]))

    def test_max_files_truncates(self):
        r = scanner.scan_directory(self.root, max_files=3)
        self.assertTrue(r["truncated"])

    def test_big_files_not_read(self):
        # bigfile.bin 不是证据文件，本来不产生条目；这里保证 >1MB 的 Makefile 也不会被读内容
        big_root = self.root / "bigcase"
        big_root.mkdir()
        (big_root / "Makefile").write_bytes(b"target:\n" + b"x" * (scanner.MAX_FILE_BYTES + 10))
        r = scanner.scan_directory(big_root)
        self.assertFalse([e for e in r["entries"] if e["kind"] == "makefile"])

    def test_missing_directory_raises(self):
        with self.assertRaises(ValueError):
            scanner.scan_directory(self.root / "nope")


if __name__ == "__main__":
    unittest.main()
