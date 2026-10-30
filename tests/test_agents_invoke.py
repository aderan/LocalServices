"""invoke 有界调用：假 CLI 驱动 good path / bad_json / 超时杀组 / 双层校验剔劣。"""
import json
import os
import subprocess as sp
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from localservices.agents import invoke


def _service(sid="demo-web", cmd=None, cwd=".", port=None, source_dir="."):
    comp = {"id": "main", "kind": "server", "cmd": cmd or ["make", "dev"], "cwd": cwd}
    if port:
        comp["port"] = port
    return {
        "id": sid, "name": sid,
        "source_dir": source_dir, "components": [comp],
        "evidence": {"summary": "from Makefile", "files": ["Makefile:3 dev"]},
    }


class InvokeEnv(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "project"
        self.bin = self.root / "bin"
        self.payloads = self.root / "payloads"
        self.home = self.root / "runtime"
        for d in (self.target, self.bin, self.payloads, self.home,
                  self.root / "codex-home", self.root / "claude-cfg"):
            d.mkdir(parents=True)
        (self.target / "Makefile").write_text("dev:\n\tnpm run dev\n")
        env = {
            "LOCALSERVICES_HOME": str(self.home),
            "CODEX_HOME": str(self.root / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-cfg"),
            "PATH": f"{self.bin}{os.pathsep}/bin{os.pathsep}/usr/bin",
        }
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.addCleanup(self._env.stop)
        from localservices.agents import detect
        for name, value in (("_candidate_dirs", []), ("_npm_global_prefix", None)):
            m = patch.object(detect, name, return_value=value)
            m.start()
            self.addCleanup(m.stop)

    def write_payload(self, name: str, text: str) -> str:
        p = self.payloads / name
        p.write_text(text)
        return str(p)

    def write_fake_codex(self, payload: str | None) -> None:
        body = (
            'out=""; prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
            '  prev="$a"\n'
            "done\n"
        )
        if payload:
            body += f'if [ -n "$out" ]; then cat "{payload}" > "$out"; fi\n'
        body += 'echo "exec finished"\n'
        p = self.bin / "codex"
        p.write_text(f"#!/bin/sh\n{body}")
        p.chmod(0o755)

    def write_fake_claude(self, payload: str | None) -> None:
        if payload:
            body = (f'python3 -c \'import json,sys; '
                    f'print(json.dumps({{"type": "result", "subtype": "success", '
                    f'"is_error": False, "result": open(sys.argv[1]).read()}}))\' '
                    f'"{payload}"\n')
        else:
            body = 'printf "not json at all"\n'
        p = self.bin / "claude"
        p.write_text(f"#!/bin/sh\n{body}")
        p.chmod(0o755)


class GoodPathTest(InvokeEnv):
    def test_codex_good_json(self):
        payload = json.dumps({"services": [_service()]})
        self.write_fake_codex(self.write_payload("good.json", payload))
        r = invoke.run_analysis("codex", str(self.target), timeout=20)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["services"]), 1)
        self.assertEqual(r["services"][0]["id"], "demo-web")
        self.assertEqual(r["evidence"]["agent"], "codex")
        self.assertGreaterEqual(r["duration"], 0)

    def test_claude_fenced_json(self):
        payload = "```json\n" + json.dumps({"services": [_service()]}) + "\n```"
        self.write_fake_claude(self.write_payload("fenced.json", payload))
        r = invoke.run_analysis("claude", str(self.target), timeout=20)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"][0]["id"], "demo-web")

    def test_focus_included_in_prompt(self):
        payload = json.dumps({"services": [_service()]})
        self.write_fake_codex(self.write_payload("good.json", payload))
        captured = {}
        original = invoke.CodexAdapter.build_argv  # patch 前拿到原函数，避免自递归

        def spy(self, directory, prompt_file, schema_path, timeout, last_message_path=None):
            captured["prompt"] = Path(prompt_file).read_text()
            return original(self, directory, prompt_file, schema_path, timeout, last_message_path)

        with patch.object(invoke.CodexAdapter, "build_argv", spy):
            invoke.run_analysis("codex", str(self.target), focus="重点找 Admin 管理端", timeout=20)
        self.assertIn("重点找 Admin 管理端", captured["prompt"])
        self.assertIn("ANALYSIS ONLY", captured["prompt"])


class BadOutputTest(InvokeEnv):
    def test_codex_no_json(self):
        self.write_fake_codex(payload=None)  # 不写 -o，stdout 只有 exec finished
        r = invoke.run_analysis("codex", str(self.target), timeout=20)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "bad_json")
        self.assertIn("exec finished", r["detail"])

    def test_claude_garbage(self):
        self.write_fake_claude(payload=None)
        r = invoke.run_analysis("claude", str(self.target), timeout=20)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "bad_json")
        self.assertIn("not json", r["detail"])

    def test_root_not_services_object(self):
        payload = json.dumps({"oops": True})
        self.write_fake_codex(self.write_payload("root.json", payload))
        r = invoke.run_analysis("codex", str(self.target), timeout=20)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "bad_json")


class TimeoutTest(InvokeEnv):
    def test_timeout_kills_process_group(self):
        pidfile = self.root / "child.pid"
        body = (
            "out=\"\"; prev=\"\"\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$prev\" = \"-o\" ]; then out=\"$a\"; fi\n"
            "  prev=\"$a\"\n"
            "done\n"
            f'sleep 30 & echo $! > "{pidfile}"\n'
            "sleep 30\n"
        )
        p = self.bin / "codex"
        p.write_text(f"#!/bin/sh\n{body}")
        p.chmod(0o755)
        t0 = time.monotonic()
        r = invoke.run_analysis("codex", str(self.target), timeout=1.5)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "timeout")
        self.assertLess(time.monotonic() - t0, 20)
        pid = int(pidfile.read_text().strip())
        r2 = sp.run(["/bin/ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and r2.stdout.strip():
            time.sleep(0.1)
            r2 = sp.run(["/bin/ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True)
        self.assertFalse(r2.stdout.strip(), "超时后子进程必须已被杀掉（进程组）")


class ValidationTest(InvokeEnv):
    def _run(self, services):
        payload = json.dumps({"services": services})
        self.write_fake_codex(self.write_payload("cands.json", payload))
        return invoke.run_analysis("codex", str(self.target), timeout=20)

    def test_cwd_escape_rejected(self):
        r = self._run([_service("bad-cwd", cwd="../../")])
        self.assertTrue(r["ok"])
        self.assertEqual(r["services"], [])
        self.assertTrue(any("bad-cwd" in u and "越出" in u for u in r["unresolved"]), r["unresolved"])

    def test_cmd_not_resolvable_rejected(self):
        r = self._run([_service("bad-cmd", cmd=["definitely-not-a-cmd-xyz"])])
        self.assertTrue(r["ok"])
        self.assertTrue(any("bad-cmd" in u and "无法解析" in u for u in r["unresolved"]), r["unresolved"])

    def test_port_out_of_range_rejected_by_schema(self):
        r = self._run([_service("bad-port", port=70000)])
        self.assertTrue(r["ok"])
        self.assertTrue(any("bad-port" in u for u in r["unresolved"]), r["unresolved"])

    def test_cmd_relative_to_source_ok(self):
        # 目标目录里放一个可执行脚本，cmd 用相对路径解析成功
        script = self.target / "run.sh"
        script.write_text("#!/bin/sh\nsleep 1\n")
        script.chmod(0o755)
        r = self._run([_service("rel-cmd", cmd=["./run.sh"])])
        self.assertEqual(r["services"][0]["id"], "rel-cmd")

    def test_mixed_good_and_bad(self):
        script = self.target / "run.sh"
        script.write_text("#!/bin/sh\nsleep 1\n")
        script.chmod(0o755)
        r = self._run([
            _service("good-1", cmd=["./run.sh"], port=8123),
            _service("bad-1", cmd=["nope-xyz"]),
            dict(_service("bad-schema"), id="bad-schema", components=[{"id": "x"}]),  # 缺 cmd
        ])
        self.assertTrue(r["ok"])
        self.assertEqual([s["id"] for s in r["services"]], ["good-1"])
        self.assertEqual(len(r["unresolved"]), 2)

    def test_extra_field_rejected_by_schema(self):
        r = self._run([dict(_service("extra"), surprise=1)])
        self.assertTrue(any("extra" in u and "额外字段" in u for u in r["unresolved"]), r["unresolved"])


class GuardTest(InvokeEnv):
    def test_missing_directory_raises(self):
        self.write_fake_codex(payload=None)
        with self.assertRaises(ValueError):
            invoke.run_analysis("codex", str(self.root / "nope"))

    def test_agent_unavailable(self):
        # bin 目录里没有 codex → agent_not_available（不是异常）
        r = invoke.run_analysis("codex", str(self.target), timeout=10)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "agent_not_available")

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            invoke.run_analysis("gemini", str(self.target))


class AgentApiTest(InvokeEnv):
    """agent_api 纯函数：202 任务流转、落盘、幂等与 400 校验。"""

    def _wait_job(self, job_id, timeout=30):
        from localservices import agent_api
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, body = agent_api.handle_job_get(job_id)
            if code == 200 and body.get("state") in ("done", "failed"):
                return body
            time.sleep(0.05)
        self.fail(f"job {job_id} 未在 {timeout}s 内结束")

    def test_analyze_job_lifecycle(self):
        from localservices import agent_api
        payload = json.dumps({"services": [_service()]})
        self.write_fake_codex(self.write_payload("good.json", payload))
        code, body = agent_api.handle_analyze(
            {"directory": str(self.target), "agent": "codex"})
        self.assertEqual(code, 202)
        self.assertTrue(body["ok"])
        job = self._wait_job(body["job_id"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["kind"], "analyze")
        self.assertTrue(job["result"]["ok"])
        self.assertEqual(job["result"]["services"][0]["id"], "demo-web")
        self.assertIn("finished_at", job)
        # 任务文件确实落盘在 jobs_dir
        job_file = self.home / "jobs" / f"{body['job_id']}.json"
        self.assertTrue(job_file.is_file())

    def test_analyze_dedup_same_dir_and_agent(self):
        from localservices import agent_api
        # 用会 sleep 的假 codex 拉长运行窗口
        p = self.bin / "codex"
        p.write_text("#!/bin/sh\nsleep 8\n")
        p.chmod(0o755)
        first = agent_api.handle_analyze({"directory": str(self.target), "agent": "codex",
                                          "timeout": 30})
        self.assertEqual(first[0], 202)
        second = agent_api.handle_analyze({"directory": str(self.target), "agent": "codex"})
        self.assertEqual(second[0], 202)
        self.assertTrue(second[1].get("existing"))
        self.assertEqual(second[1]["job_id"], first[1]["job_id"])
        job = self._wait_job(first[1]["job_id"], timeout=45)
        self.assertIn(job["state"], ("done", "failed"))

    def test_analyze_validation_errors(self):
        from localservices import agent_api
        self.assertEqual(agent_api.handle_analyze({})[0], 400)
        self.assertEqual(
            agent_api.handle_analyze({"directory": str(self.target), "agent": "gemini"})[0], 400)
        self.assertEqual(
            agent_api.handle_analyze({"directory": "relative/path", "agent": "codex"})[0], 400)
        self.assertEqual(
            agent_api.handle_analyze({"directory": str(self.root / "nope"), "agent": "codex"})[0], 400)
        code, body = agent_api.handle_analyze("not a dict")
        self.assertEqual((code, body["ok"]), (400, False))

    def test_agents_get_scan_and_verify(self):
        from localservices import agent_api
        # 先放假 codex；无缓存 → 自动 scan；无登录信号 → config_only
        fake = self.bin / "codex"
        fake.write_text('#!/bin/sh\necho "codex-cli 9.9.9"\n')
        fake.chmod(0o755)
        code, body = agent_api.handle_agents_get()
        self.assertEqual(code, 200)
        self.assertEqual(body["connections"]["codex"]["status"], "config_only")
        self.assertEqual(body["connections"]["claude"]["status"], "not_installed")
        # 缓存已落盘
        self.assertTrue((self.home / "agent-connections.json").is_file())
        # 显式 scan 带自定义 paths
        code, body = agent_api.handle_agents_scan({"paths": [str(fake)]})
        self.assertEqual(code, 200)
        self.assertEqual(body["connections"]["codex"]["path"], str(fake))
        self.assertEqual(agent_api.handle_agents_scan({"paths": "bad"})[0], 400)
        # verify（假 CLI exec 分支 echo OK）→ available
        fake.write_text('#!/bin/sh\nif [ "$1" = "exec" ]; then echo OK; else echo "codex-cli 9.9.9"; fi\n')
        fake.chmod(0o755)
        code, body = agent_api.handle_agents_verify({"kind": "codex", "timeout": 10})
        self.assertEqual(code, 200)
        self.assertEqual(body["connection"]["status"], "available")
        self.assertEqual(agent_api.handle_agents_verify({"kind": "nope"})[0], 400)
        self.assertEqual(agent_api.handle_agents_verify({"kind": "codex", "timeout": "x"})[0], 400)

    def test_job_get_not_found_and_bad_id(self):
        from localservices import agent_api
        self.assertEqual(agent_api.handle_job_get("job-nonexistent")[0], 404)
        self.assertEqual(agent_api.handle_job_get("../evil")[0], 400)


if __name__ == "__main__":
    unittest.main()
