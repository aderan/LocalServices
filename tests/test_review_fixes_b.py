"""V2 复核修复回归（B 线）：R01 秘密边界 + R07 schema/本地校验半边。

R01：嵌套 env/argv/URL 中的假秘密标记绝不进入项目上下文与最终 prompt。
     边界设在模型请求构造处（prompt 字符串），不只 UI/日志。
R07：run=compose 候选通过 schema 与本地校验进入 services[]；
     provision=up 无确认被拒（识别阶段不允许隐式授权创建容器）。

全部用假 CLI 与假标记驱动（模式沿 tests/test_agents_invoke.py），
绝不发送真实模型请求、不触碰真实 ~/Library。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localservices.agents import invoke

MARKER = "FAKE-SECRET-MARKER-123"


def _component(cid="main", cmd=None, cwd=".", port=None):
    comp = {"id": cid, "kind": "server", "cmd": cmd or ["make", "dev"], "cwd": cwd,
            "name": None, "shell_script": None, "port": port, "ready": None,
            "required": True, "start_timeout": None, "after": [], "hot": False,
            "watch": [], "restart_on": [], "install": [], "build": [],
            "install_when": [], "install_marker": None, "auto_update": True,
            "external_cmd": None}
    return comp


def _service(sid="demo-web", cmd=None, cwd=".", port=None, source_dir="."):
    return {
        "id": sid, "name": sid,
        "description": None, "open": None, "source_dir": source_dir,
        "components": [_service_comp(sid, cmd, cwd, port)], "dependencies": [],
        "resource_refs": [],
        "evidence": {"summary": "from Makefile", "files": ["Makefile:3 dev"]},
        "unresolved": [],
    }


def _service_comp(sid, cmd, cwd, port):
    return _component(cmd=cmd, cwd=cwd, port=port)


def _compose_component(cid="main", compose_service="web", provision=None,
                       compose_files=None, compose_project=None, run="compose", cmd=None):
    """run=compose 组件：无 cmd（可选字段），带 compose 运行参数。"""
    comp = {"id": cid, "kind": "server", "name": None, "cwd": ".",
            "shell_script": None, "port": None, "ready": None, "required": True,
            "start_timeout": None, "after": [], "hot": False, "watch": [],
            "restart_on": [], "install": [], "build": [], "install_when": [],
            "install_marker": None, "auto_update": True, "external_cmd": None,
            "run": run, "compose_service": compose_service,
            "compose_project": compose_project, "compose_files": compose_files or []}
    if provision is not None:
        comp["provision"] = provision
    if cmd is not None:
        comp["cmd"] = cmd
    return comp


def _compose_service(sid="compose-web", confirmed=None, **comp_kw):
    svc = {
        "id": sid, "name": sid,
        "description": None, "open": None, "source_dir": ".",
        "components": [_compose_component(**comp_kw)], "dependencies": [],
        "resource_refs": [],
        "evidence": {"summary": "compose service", "files": ["docker-compose.yml:3 web"]},
        "unresolved": [],
    }
    if confirmed is not None:
        svc["confirmed_provision"] = confirmed
    return svc


class FixBEnv(unittest.TestCase):
    """隔离环境（沿 test_agents_v2.AnalyzeV2Env）：全部路径指向临时目录。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ls-review-fix-b-")
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
            "LOCALSERVICES_SERVICES": str(self.services),
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

    def write_service_toml(self, sid="demo-app", pid="demoproj"):
        """api=普通组件；db-cli 命令行携带敏感参数与 token URL；worker 供嵌套覆盖用。"""
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
port = 8123

[[components]]
id = "db-cli"
name = "DB CLI"
cmd = ["psql", "--password=hunter2-secret-987", "https://db.example.dev/cb?token={MARKER}"]
cwd = "."

[[components]]
id = "worker"
cmd = ["make", "worker"]
cwd = "."
after = ["api"]
'''
        p = self.user_services / f"{sid}.toml"
        p.write_text(toml, encoding="utf-8")
        return p

    def write_overrides_raw(self, fields, sid="demo-app"):
        data = {"schemaVersion": 1, "services": {sid: {"fields": fields}}}
        self.home.joinpath("overrides.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def leaky_override_fields(self):
        return {
            "name": "我的应用",
            "components[api].port": 8123,
            # R01 复现主体：路径名不含敏感词 → 旧实现整个 dict 直通
            "components[api].env": {"DB_PASSWORD": MARKER, "DB_HOST": "127.0.0.1",
                                    "PGPORT": 5432},
            "open": f"https://x.demo.dev/cb?token={MARKER}",
            "api_key": "sk-live-123456",
            # 深层嵌套：列表 × dict × env 形态
            "components[worker].build": [{"env": {"NESTED_API_TOKEN": MARKER}}],
            "components[worker].note": {"deep": ["plain-value", {"DB_PASSWORD": MARKER}]},
        }

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

    def run_with_prompt_capture(self, **kwargs):
        """假 CLI + build_argv spy：返回 (run_analysis 结果, 实际写入 prompt 文件的字符串)。"""
        self.write_fake_codex(json.dumps({"services": [_service()]}))
        captured = {}
        original = invoke.CodexAdapter.build_argv  # patch 前拿原函数，避免自递归

        def spy(self_, directory, prompt_file, schema_path, timeout, last_message_path=None):
            captured["prompt"] = Path(prompt_file).read_text()
            return original(self_, directory, prompt_file, schema_path, timeout,
                            last_message_path)

        with patch.object(invoke.CodexAdapter, "build_argv", spy):
            r = invoke.run_analysis("codex", str(self.dirA), timeout=30, **kwargs)
        self.assertTrue(captured.get("prompt"), "spy 未捕获 prompt")
        return r, captured["prompt"]


# ---------------------------------------------------------------- R01 上下文脱敏

class R01ContextTest(FixBEnv):
    """build_project_context：递归脱敏，env 只留名字与非敏感值，argv/URL 同样清理。"""

    def _ctx(self):
        from localservices import agent_api
        self.make_project()
        self.write_service_toml()
        self.write_overrides_raw(self.leaky_override_fields())
        return agent_api.build_project_context("demoproj")

    def test_nested_env_override_masked(self):
        ctx = self._ctx()
        env = ctx["overrides"]["demo-app"]["components[api].env"]
        self.assertEqual(env, {"DB_PASSWORD": "***", "DB_HOST": "127.0.0.1",
                               "PGPORT": 5432})

    def test_no_marker_anywhere_in_context(self):
        dump = json.dumps(self._ctx(), ensure_ascii=False)
        self.assertNotIn(MARKER, dump)
        self.assertNotIn("hunter2-secret-987", dump)      # cmd --password= 值
        self.assertNotIn("sk-live-123456", dump)          # 敏感键覆盖值

    def test_deeply_nested_lists_of_dicts_masked(self):
        ctx = self._ctx()
        dump = json.dumps(ctx, ensure_ascii=False)
        self.assertNotIn(MARKER, dump)
        ov = ctx["overrides"]["demo-app"]
        self.assertEqual(ov["components[worker].build"],
                         [{"env": {"NESTED_API_TOKEN": "***"}}])
        self.assertEqual(ov["components[worker].note"],
                         {"deep": ["plain-value", {"DB_PASSWORD": "***"}]})

    def test_url_token_in_override_masked(self):
        ov = self._ctx()["overrides"]["demo-app"]
        self.assertEqual(ov["open"], "https://x.demo.dev/cb?token=***")

    def test_cmd_summary_argv_scrubbed(self):
        ctx = self._ctx()
        svc = next(s for s in ctx["services"] if s["id"] == "demo-app")
        by_id = {c["id"]: c["cmd_summary"] for c in svc["components"]}
        self.assertEqual(by_id["api"], "make dev")
        self.assertIn("--password=***", by_id["db-cli"])
        self.assertIn("db.example.dev/cb?token=***", by_id["db-cli"])
        self.assertNotIn(MARKER, by_id["db-cli"])

    def test_benign_values_survive(self):
        ctx = self._ctx()
        dump = json.dumps(ctx, ensure_ascii=False)
        self.assertIn("我的应用", ctx["overrides"]["demo-app"]["name"])
        self.assertIn("127.0.0.1", dump)
        self.assertIn("5432", dump)                        # PGPORT 非敏感保留
        svc = next(s for s in ctx["services"] if s["id"] == "demo-app")
        self.assertIn({"component": "api", "port": 8123}, svc["ports"])
        self.assertIn(str(self.dirA), dump)                # source 路径保留


# ---------------------------------------------------------------- R01 prompt 边界

class R01PromptBoundaryTest(FixBEnv):
    """invoke 侧最后一道防线：实际发给假 CLI 的 prompt 绝不含假秘密标记。"""

    def test_context_marker_never_in_prompt(self):
        # 直接构造带标记的上下文（模拟上游任何缺口），只靠 invoke 层兜底
        ctx = {"project": {"id": "p1", "name": "Proj", "directories": []},
               "services": [],
               "overrides": {"demo-app": {
                   "components[api].env": {"DB_PASSWORD": MARKER, "DB_HOST": "127.0.0.1"},
                   "open": f"https://x.demo.dev/cb?token={MARKER}"}},
               "resources": {"items": [{"name": "pg", "image": "postgres:16"}]}}
        _, prompt = self.run_with_prompt_capture(project_context=ctx)
        self.assertNotIn(MARKER, prompt)
        self.assertIn("127.0.0.1", prompt)      # 非敏感值仍在（脱敏≠删光）
        self.assertIn("demo-app", prompt)
        self.assertIn("postgres:16", prompt)
        self.assertIn("***", prompt)

    def test_evidence_marker_never_in_prompt(self):
        (self.dirA / "Makefile").write_text(
            f"dev:\n\t./serve --api-key={MARKER} --port 8123\n")
        _, prompt = self.run_with_prompt_capture()
        self.assertNotIn(MARKER, prompt)
        self.assertIn("--api-key=***", prompt)  # 证据行保留但已脱敏
        self.assertIn("--port 8123", prompt)

    def test_focus_marker_never_in_prompt(self):
        _, prompt = self.run_with_prompt_capture(focus=f"check token={MARKER} please")
        self.assertNotIn(MARKER, prompt)
        self.assertIn("token=***", prompt)


# ---------------------------------------------------------------- R07 compose 半边

class R07ComposeValidationTest(FixBEnv):
    """schema + 本地校验：run=compose 候选合法进入 services[]，授权缺省被拒。"""

    def _run(self, services):
        self.write_fake_codex(json.dumps({"services": services}))
        return invoke.run_analysis("codex", str(self.dirA), timeout=30)

    def test_compose_candidate_passes_and_fields_forwarded(self):
        r = self._run([_compose_service("compose-web", compose_service="web",
                                        compose_files=["docker-compose.yml"])])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["unresolved"], [], r["unresolved"])
        self.assertEqual([s["id"] for s in r["services"]], ["compose-web"])
        comp = r["services"][0]["components"][0]
        self.assertEqual(comp["run"], "compose")
        self.assertEqual(comp["compose_service"], "web")
        self.assertEqual(comp["compose_files"], ["docker-compose.yml"])
        self.assertNotIn("cmd", comp)                    # compose 无 cmd 不报错

    def test_compose_cmd_absent_not_flagged(self):
        r = self._run([_compose_service("cw")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"][0]["id"], "cw")
        self.assertFalse([u for u in r["unresolved"] if "cmd" in u], r["unresolved"])

    def test_compose_missing_compose_service_rejected(self):
        r = self._run([_compose_service("no-svc", compose_service=None)])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"], [])
        self.assertTrue(any("compose_service" in u for u in r["unresolved"]),
                        r["unresolved"])

    def test_provision_up_without_confirmation_rejected(self):
        r = self._run([_compose_service("up-noauth", provision="up")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"], [])
        self.assertTrue(any("识别阶段不允许隐式授权创建容器" in u for u in r["unresolved"]),
                        r["unresolved"])

    def test_provision_up_with_confirmation_passes(self):
        r = self._run([_compose_service("up-authed", provision="up", confirmed=True)])
        self.assertTrue(r["ok"], r)
        self.assertEqual([s["id"] for s in r["services"]], ["up-authed"])
        self.assertEqual(r["services"][0]["confirmed_provision"], True)
        self.assertEqual(r["services"][0]["components"][0]["provision"], "up")

    def test_compose_files_escape_rejected(self):
        r = self._run([_compose_service("esc", compose_files=["../outside.yml"])])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"], [])
        self.assertTrue(any("compose_files" in u for u in r["unresolved"]),
                        r["unresolved"])

    def test_invalid_run_value_rejected_by_schema(self):
        r = self._run([_compose_service("k8s", run="k8s")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"], [])
        self.assertTrue(any("不在枚举" in u for u in r["unresolved"]), r["unresolved"])

    def test_local_run_cmd_still_required_and_validated(self):
        r = self._run([_compose_service("local-nocmd", run="local")])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["services"], [])
        self.assertTrue(any("cmd 为空" in u for u in r["unresolved"]), r["unresolved"])
        r = self._run([_compose_service("local-badcmd", run="local",
                                        cmd=["definitely-not-a-cmd-xyz"])])
        self.assertEqual(r["services"], [])
        self.assertTrue(any("无法解析" in u for u in r["unresolved"]), r["unresolved"])

    def test_classic_local_candidate_untouched(self):
        # 无 run/compose 字段的旧形状候选照常通过（向后兼容）
        r = self._run([_service("classic-web")])
        self.assertTrue(r["ok"], r)
        self.assertEqual([s["id"] for s in r["services"]], ["classic-web"])
        self.assertEqual(r["unresolved"], [])

    def test_prompt_teaches_compose_option(self):
        _, prompt = self.run_with_prompt_capture()
        self.assertIn('run": "compose"', prompt)
        self.assertIn("compose_service", prompt)
        self.assertIn("provision", prompt)


if __name__ == "__main__":
    unittest.main()
