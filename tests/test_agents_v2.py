"""V2.10 分析上下文（Agent/UX 线）：handle_analyze 的 project_id 校验、上下文组装、
prompt 注入与脱敏保证。假 CLI 驱动（模式沿 tests/test_agents_invoke.py）。"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from localservices.agents import invoke


def _service(sid="demo-web", cmd=None, cwd=".", port=None, source_dir="."):
    comp = {"id": "main", "kind": "server", "cmd": cmd or ["make", "dev"], "cwd": cwd,
            "name": None, "shell_script": None, "port": port, "ready": None,
            "required": True, "start_timeout": None, "after": [], "hot": False,
            "watch": [], "restart_on": [], "install": [], "build": [],
            "install_when": [], "install_marker": None, "auto_update": True,
            "external_cmd": None}
    return {
        "id": sid, "name": sid,
        "description": None, "open": None, "source_dir": source_dir,
        "components": [comp], "dependencies": [], "resource_refs": [],
        "evidence": {"summary": "from Makefile", "files": ["Makefile:3 dev"]},
        "unresolved": [],
    }


FAKE_RESOURCES = {"ok": True, "docker_context": "colima", "resources": [
    {"id": "container:pg", "kind": "docker-container", "name": "pg", "image": "postgres:16",
     "state": "running", "compose_project": "demo", "compose_service": "db",
     "ports": {"5432/tcp": [{"host_ip": "0.0.0.0", "host_port": 5434}]},
     "volumes": ["demo_pgdata"]},
    {"id": "container:redis", "kind": "docker-container", "name": "redis", "image": "redis:7",
     "state": "exited", "ports": {}, "volumes": []},
]}


class AnalyzeV2Env(unittest.TestCase):
    """隔离环境：LOCALSERVICES_HOME/SERVICES/USER_SERVICES 全部指向临时目录，
    绝不触碰真实 ~/Library/Application Support/LocalServices。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ls-agents-v2-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dirA = self.root / "dirA"
        self.bin = self.root / "bin"
        self.payloads = self.root / "payloads"
        self.home = self.root / "home"
        self.user_services = self.home / "services"
        self.services = self.root / "builtin-services"
        for d in (self.dirA, self.bin, self.payloads, self.home, self.user_services,
                  self.services, self.root / "codex-home", self.root / "claude-cfg"):
            d.mkdir(parents=True)
        (self.dirA / "Makefile").write_text("dev:\n\tnpm run dev\n")
        env = {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),        # 不加载仓库内置模板
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
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

    # ---------- 夹具 ----------

    def make_project(self, pid="demoproj", name="DemoProj"):
        from localservices.registry import Registry
        proj, created = Registry().ensure_project(pid, name, [{"path": str(self.dirA)}], "codex")
        self.assertTrue(created, "夹具项目应首次创建")
        return proj

    def write_service_toml(self, sid="demo-app", pid="demoproj", port=8123):
        toml = f'''
id = "{sid}"
name = "{sid} App"
description = "fixture"
source = "{self.dirA}"
project = "{pid}"

[[components]]
id = "api"
name = "API"
cmd = ["make", "dev"]
cwd = "."
port = {port}
env = {{ SECRET_TOKEN = "super-secret-do-not-leak" }}

[[components]]
id = "worker"
name = "Worker"
cmd = ["make", "worker"]
cwd = "."
after = ["api"]
'''
        p = self.user_services / f"{sid}.toml"
        p.write_text(toml, encoding="utf-8")
        return p

    def write_overrides(self, sid="demo-app"):
        data = {"schemaVersion": 1, "services": {sid: {"fields": {
            "name": "我的应用", "components[api].port": 8123, "api_key": "sk-live-123456"}}}}
        self.home.joinpath("overrides.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_fake_codex(self, payload_text):
        p = self.payloads / "good.json"
        p.write_text(payload_text, encoding="utf-8")
        body = (
            'out=""; prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "-o" ]; then out="$a"; fi\n'
            '  prev="$a"\n'
            "done\n"
            f'if [ -n "$out" ]; then cat "{p}" > "$out"; fi\n'
            'echo "exec finished"\n'
        )
        f = self.bin / "codex"
        f.write_text(f"#!/bin/sh\n{body}")
        f.chmod(0o755)

    def _wait_job(self, job_id, timeout=30):
        from localservices import agent_api
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, body = agent_api.handle_job_get(job_id)
            if code == 200 and body.get("state") in ("done", "failed"):
                return body
            time.sleep(0.05)
        self.fail(f"job {job_id} 未在 {timeout}s 内结束")


class BuildProjectContextTest(AnalyzeV2Env):
    """build_project_context：项目/服务/覆盖层/资源摘要（V2.10）。"""

    def test_unknown_project_returns_none(self):
        from localservices import agent_api
        self.assertIsNone(agent_api.build_project_context("missing-proj"))

    def test_context_shape_services_overrides_resources(self):
        from localservices import agent_api
        self.make_project()
        self.write_service_toml()
        self.write_overrides()
        with patch("localservices.resources.inventory", return_value=FAKE_RESOURCES):
            ctx = agent_api.build_project_context("demoproj")
        self.assertEqual(ctx["project"]["id"], "demoproj")
        self.assertEqual(ctx["project"]["name"], "DemoProj")
        self.assertEqual(ctx["project"]["directories"][0]["path"], str(self.dirA.resolve()))
        svc = next(s for s in ctx["services"] if s["id"] == "demo-app")
        self.assertEqual(svc["name"], "demo-app App")
        self.assertEqual(svc["source"], str(self.dirA))   # manifest 层保留书写路径（未 resolve）
        self.assertIn({"component": "api", "port": 8123}, svc["ports"])   # worker 无端口不出现
        self.assertEqual(svc["components"][0]["cmd_summary"], "make dev")
        self.assertEqual(ctx["overrides"]["demo-app"]["name"], "我的应用")
        self.assertEqual(ctx["overrides"]["demo-app"]["components[api].port"], 8123)
        self.assertEqual(ctx["resources"]["docker_context"], "colima")
        names = [i["name"] for i in ctx["resources"]["items"]]
        self.assertEqual(names, ["pg", "redis"])                          # 含已停止容器
        self.assertEqual(ctx["resources"]["items"][0]["volumes"], ["demo_pgdata"])

    def test_override_secret_key_redacted(self):
        from localservices import agent_api
        self.make_project()
        self.write_service_toml()
        self.write_overrides()
        ctx = agent_api.build_project_context("demoproj")
        self.assertEqual(ctx["overrides"]["demo-app"]["api_key"], "***")

    def test_resources_degrade_on_error_and_old_shape(self):
        from localservices import agent_api
        self.make_project()
        self.write_service_toml()
        with patch("localservices.resources.inventory", side_effect=RuntimeError("docker boom")):
            ctx = agent_api.build_project_context("demoproj")
        self.assertEqual(ctx["resources"], {})
        with patch("localservices.resources.inventory", return_value={"ok": False, "error": "x"}):
            ctx = agent_api.build_project_context("demoproj")
        self.assertEqual(ctx["resources"], {})

    def test_no_overrides_file_means_empty(self):
        from localservices import agent_api
        self.make_project()
        self.write_service_toml()
        ctx = agent_api.build_project_context("demoproj")
        self.assertEqual(ctx["overrides"], {})


class HandleAnalyzeProjectTest(AnalyzeV2Env):
    """/api/analyze 的 project_id 校验与任务落盘。"""

    def test_unknown_project_id_is_400(self):
        from localservices import agent_api
        code, body = agent_api.handle_analyze(
            {"directory": str(self.dirA), "agent": "codex", "project_id": "missing-proj"})
        self.assertEqual(code, 400)
        self.assertIn("未知项目", body["error"])

    def test_bad_project_id_type_is_400(self):
        from localservices import agent_api
        for bad in ("", 123, []):
            code, body = agent_api.handle_analyze(
                {"directory": str(self.dirA), "agent": "codex", "project_id": bad})
            self.assertEqual(code, 400, bad)

    def test_job_file_persists_project_id(self):
        from localservices import agent_api
        self.make_project()
        self.write_fake_codex(json.dumps({"services": [_service()]}))
        code, body = agent_api.handle_analyze(
            {"directory": str(self.dirA), "agent": "codex", "project_id": "demoproj"})
        self.assertEqual(code, 202)
        job = self._wait_job(body["job_id"])
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["project_id"], "demoproj")
        on_disk = json.loads(
            (self.home / "jobs" / f"{body['job_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["project_id"], "demoproj")

    def test_job_without_project_id_has_null(self):
        from localservices import agent_api
        self.write_fake_codex(json.dumps({"services": [_service()]}))
        code, body = agent_api.handle_analyze({"directory": str(self.dirA), "agent": "codex"})
        self.assertEqual(code, 202)
        job = self._wait_job(body["job_id"])
        self.assertIn("project_id", job)
        self.assertIsNone(job["project_id"])


class PromptContextTest(AnalyzeV2Env):
    """prompt 注入：目标项目上下文区块 + 脱敏保证（.env/env 值绝不进 prompt）。"""

    def _run_with_spy(self, **kwargs):
        self.make_project()
        self.write_service_toml()
        self.write_overrides()
        self.write_fake_codex(json.dumps({"services": [_service()]}))
        captured = {}
        original = invoke.CodexAdapter.build_argv  # patch 前拿原函数，避免自递归

        def spy(self, directory, prompt_file, schema_path, timeout, last_message_path=None):
            captured["prompt"] = Path(prompt_file).read_text()
            return original(self, directory, prompt_file, schema_path, timeout, last_message_path)

        from localservices import agent_api
        with patch.object(invoke.CodexAdapter, "build_argv", spy), \
             patch("localservices.resources.inventory", return_value=FAKE_RESOURCES):
            code, body = agent_api.handle_analyze(
                {"directory": str(self.dirA), "agent": "codex", "project_id": "demoproj",
                 **kwargs})
            self.assertEqual(code, 202)
            self._wait_job(body["job_id"])
        self.assertTrue(captured.get("prompt"), "spy 未捕获 prompt")
        return captured["prompt"]

    def test_prompt_contains_project_context(self):
        prompt = self._run_with_spy()
        self.assertIn("目标项目上下文", prompt)
        self.assertIn("ANALYSIS ONLY", prompt)                 # 只读规则头部保留
        self.assertIn("DemoProj", prompt)
        self.assertIn("demo-app", prompt)
        self.assertIn("8123", prompt)
        self.assertIn("我的应用", prompt)                        # 覆盖层值（清单层字段）
        self.assertIn("components[api].port", prompt)
        self.assertIn("pg", prompt)                            # 可复用容器名
        self.assertIn("demo_pgdata", prompt)                   # 可复用卷名
        self.assertIn("prefer proposing an UPDATE", prompt)    # 倾向更新既有 id

    def test_redaction_env_values_never_in_prompt(self):
        prompt = self._run_with_spy()
        self.assertNotIn("super-secret-do-not-leak", prompt)   # 组件 env 值
        self.assertNotIn("sk-live-123456", prompt)             # 敏感键覆盖值

    def test_no_context_without_project_id(self):
        self.write_fake_codex(json.dumps({"services": [_service()]}))
        captured = {}
        original = invoke.CodexAdapter.build_argv

        def spy(self, directory, prompt_file, schema_path, timeout, last_message_path=None):
            captured["prompt"] = Path(prompt_file).read_text()
            return original(self, directory, prompt_file, schema_path, timeout, last_message_path)

        from localservices import agent_api
        with patch.object(invoke.CodexAdapter, "build_argv", spy):
            code, body = agent_api.handle_analyze({"directory": str(self.dirA), "agent": "codex"})
            self.assertEqual(code, 202)
            self._wait_job(body["job_id"])
        self.assertNotIn("目标项目上下文", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
