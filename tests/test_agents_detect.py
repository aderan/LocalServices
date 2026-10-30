"""detect 状态机：假 CLI 覆盖 scan 分支与 verify 分类；真实 codex/claude 各 verify 一次自证。"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localservices.agents import base, detect

CODEX_VERSION = "codex-cli 9.9.9"


class DetectEnv(unittest.TestCase):
    """隔离环境：LOCALSERVICES_HOME/CODEX_HOME/CLAUDE_CONFIG_DIR 全部指向临时目录。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "runtime"
        self.bin = self.root / "bin"
        self.codex_home = self.root / "codex-home"
        self.claude_cfg = self.root / "claude-cfg"
        for d in (self.home, self.bin, self.codex_home, self.claude_cfg):
            d.mkdir(parents=True)
        env = {"LOCALSERVICES_HOME": str(self.home),
               "CODEX_HOME": str(self.codex_home),
               "CLAUDE_CONFIG_DIR": str(self.claude_cfg)}
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)

    def write_cli(self, kind: str, body: str, executable: bool = True) -> Path:
        p = self.bin / kind
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755 if executable else 0o644)
        return p

    def only_my_bin_on_path(self):
        # 假 bin 目录排最前，系统 /bin:/usr/bin 兜底（sleep/echo 可用；真实
        # codex/claude/npm 不在这两个目录），并封掉固定回落目录与 npm 全局目录
        p = patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}/bin{os.pathsep}/usr/bin"})
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("_candidate_dirs", []), ("_npm_global_prefix", None)):
            m = patch.object(detect, name, return_value=value)
            m.start()
            self.addCleanup(m.stop)


class ScanStatesTest(DetectEnv):
    def test_not_installed_when_absent_everywhere(self):
        self.only_my_bin_on_path()
        conns = detect.scan()
        self.assertEqual(conns["codex"].status, "not_installed")
        self.assertEqual(conns["claude"].status, "not_installed")
        self.assertEqual(conns["codex"].path, "")

    def test_not_executable(self):
        self.write_cli("codex", f"echo '{CODEX_VERSION}'", executable=False)
        self.only_my_bin_on_path()
        conns = detect.scan()
        self.assertEqual(conns["codex"].status, "not_executable")
        self.assertIn("执行权限", conns["codex"].detail)

    def test_config_only_without_login_signal(self):
        self.write_cli("codex", f"echo '{CODEX_VERSION}'")
        self.only_my_bin_on_path()
        conns = detect.scan()
        c = conns["codex"]
        self.assertEqual(c.status, "config_only")
        self.assertEqual(c.version, CODEX_VERSION)
        self.assertEqual(c.config_root, str(self.codex_home))
        self.assertIn("verify", c.detail)  # 无信号也不能武断 needs_login

    def test_config_only_with_codex_tokens(self):
        (self.codex_home / "auth.json").write_text(json.dumps({"tokens": {"id": "x"}}))
        self.write_cli("codex", f"echo '{CODEX_VERSION}'")
        self.only_my_bin_on_path()
        c = detect.scan()["codex"]
        self.assertEqual(c.status, "config_only")  # 有信号 ≠ available，必须真实调用
        self.assertIn("登录信号", c.detail)

    def test_config_only_with_claude_credentials_file(self):
        (self.claude_cfg / ".credentials.json").write_text("{}")
        self.write_cli("claude", "echo '2.1.308 (Claude Code)'")
        self.only_my_bin_on_path()
        c = detect.scan()["claude"]
        self.assertEqual(c.status, "config_only")
        self.assertIn("登录信号", c.detail)

    def test_scan_never_real_calls_or_writes(self):
        self.write_cli("codex", f"echo '{CODEX_VERSION}'")
        self.only_my_bin_on_path()
        detect.scan()
        self.assertFalse(base.load_connections()[0])  # scan 不落盘

    def test_custom_paths_direct_file(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        p = elsewhere / "codex"
        p.write_text(f"#!/bin/sh\necho '{CODEX_VERSION}'\n")
        p.chmod(0o755)
        self.only_my_bin_on_path()
        conns = detect.scan(custom_paths=[str(p)])
        self.assertEqual(conns["codex"].path, str(p))
        self.assertEqual(conns["codex"].status, "config_only")

    def test_version_failure_recorded_in_detail(self):
        self.write_cli("codex", "echo 'boom' >&2; exit 7")
        self.only_my_bin_on_path()
        c = detect.scan()["codex"]
        self.assertEqual(c.status, "config_only")
        self.assertEqual(c.version, "")
        self.assertIn("--version", c.detail)

    def test_verified_preserved_when_path_and_version_same(self):
        p = self.write_cli("codex", f"echo '{CODEX_VERSION}'")
        self.only_my_bin_on_path()
        seeded = base.AgentConnection(
            kind="codex", path=str(p), version=CODEX_VERSION,
            config_root=str(self.codex_home), status="available",
            detail="最小调用返回 OK", verified_at=123.0)
        base.save_connections({"codex": seeded}, "codex")
        c = detect.scan()["codex"]
        self.assertEqual(c.status, "available")
        self.assertEqual(c.verified_at, 123.0)

    def test_verified_downgraded_when_version_changes(self):
        p = self.write_cli("codex", f"echo '{CODEX_VERSION}'")
        self.only_my_bin_on_path()
        seeded = base.AgentConnection(
            kind="codex", path=str(p), version="codex-cli 0.1.0",
            config_root=str(self.codex_home), status="available",
            verified_at=123.0)
        base.save_connections({"codex": seeded}, "codex")
        c = detect.scan()["codex"]
        self.assertEqual(c.status, "config_only")
        self.assertIn("版本变化", c.detail)


class VerifyFakeTest(DetectEnv):
    """verify 的四种归类，全部打在假 CLI 上（不产生真实模型调用）。"""

    def codex_cli(self, exec_body: str) -> Path:
        return self.write_cli(
            "codex",
            f'if [ "$1" = "exec" ]; then\n{exec_body}\nelse echo "{CODEX_VERSION}"; fi')

    def test_verify_available(self):
        self.codex_cli("echo OK")
        self.only_my_bin_on_path()
        conn = detect.verify("codex", timeout=10)
        self.assertEqual(conn.status, "available")
        self.assertGreater(conn.verified_at, 0)
        saved, _ = base.load_connections()
        self.assertEqual(saved["codex"].status, "available")  # 结果落盘

    def test_verify_needs_login(self):
        self.codex_cli('echo "Error: unauthorized, please log in" >&2; exit 1')
        self.only_my_bin_on_path()
        conn = detect.verify("codex", timeout=10)
        self.assertEqual(conn.status, "needs_login")
        self.assertIn("unauthorized", conn.detail)

    def test_verify_call_failed(self):
        self.codex_cli('echo "boom" >&2; exit 3')
        self.only_my_bin_on_path()
        conn = detect.verify("codex", timeout=10)
        self.assertEqual(conn.status, "call_failed")
        self.assertIn("boom", conn.detail)

    def test_verify_timeout_kills_group(self):
        self.codex_cli('sleep 30 & echo $! > "$0.pid"; sleep 30')
        # $0 在 sh 里是脚本路径，借它记录子进程 pid
        self.only_my_bin_on_path()
        script = self.bin / "codex"
        pidfile = Path(str(script) + ".pid")
        conn = detect.verify("codex", timeout=2)
        self.assertEqual(conn.status, "call_failed")
        self.assertIn("超时", conn.detail)
        pid = int(pidfile.read_text().strip())
        import subprocess as sp
        import time as _t
        deadline = _t.time() + 10
        r = sp.run(["/bin/ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True)
        while _t.time() < deadline and r.stdout.strip():
            _t.sleep(0.1)
            r = sp.run(["/bin/ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True)
        self.assertFalse(r.stdout.strip(), "超时后子进程必须已被杀掉")

    def test_verify_without_binary_short_circuits(self):
        self.only_my_bin_on_path()
        conn = detect.verify("claude", timeout=10)
        self.assertEqual(conn.status, "not_installed")
        self.assertIn("无法验证", conn.detail)

    def test_verify_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            detect.verify("gemini")


class RealVerifyTest(unittest.TestCase):
    """真实现场自证：对已登录的 codex / claude 各跑一次最小真实调用。
    只重定向 LOCALSERVICES_HOME（结果不污染真实缓存），CODEX_HOME/CLAUDE_CONFIG_DIR
    保持真实环境。"""

    def _real(self, kind: str):
        if not shutil.which(kind):
            self.skipTest(f"{kind} not installed")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"LOCALSERVICES_HOME": str(Path(tmp.name) / "runtime")})
        env.start()
        self.addCleanup(env.stop)
        conn = detect.verify(kind, timeout=60)
        # 断言 available，否则必须带上真实 detail 供诊断
        self.assertIn(
            conn.status, ("available", "needs_login", "call_failed", "config_only"),
            f"{kind}: {conn.detail}")
        return conn

    def test_real_verify_codex(self):
        conn = self._real("codex")
        if conn.status != "available":
            self.fail(f"codex verify 未达 available: status={conn.status} detail={conn.detail}")
        self.assertEqual(conn.version, detect._run_version(conn.path)[0])

    def test_real_verify_claude(self):
        conn = self._real("claude")
        if conn.status != "available":
            self.fail(f"claude verify 未达 available: status={conn.status} detail={conn.detail}")


if __name__ == "__main__":
    unittest.main()
