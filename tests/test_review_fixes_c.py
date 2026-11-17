"""V2 复核修复（fix line C）契约测试：R05 / R06-GUI / R07-GUI / R08 / R09。

静态锚点层沿 tests/test_webui_v2.py 模式；行为层（页面真实 JS 在桩 DOM 上执行）由
scripts/verify_v2_gui.cjs 第 10-14 节验证。另含一个隔离真实 daemon E2E，锁定
R05 依赖的 GET /api/services/<sid>/definition 响应形状（完整字段保真）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "localservices" / "web" / "dashboard.html"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ReviewFixCStaticTest(unittest.TestCase):
    """dashboard.html 静态锚点（R05/R06-GUI/R07-GUI/R08/R09）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    # ---- R05：编辑对话框不得丢失服务定义 ----

    def test_r05_edit_fetches_full_definition(self):
        # 编辑入口先取完整定义（V2 端点），不再从快照空初始化 cmd/port/probe
        self.assertIn("/api/services/${sid}/definition", self.html)
        self.assertIn("function editService", self.html)
        # 不再以空 cmd 初始化组件（旧缺陷锚点必须消失）
        self.assertNotIn("cmd: [], port: null, probe: \"\"", self.html)

    def test_r05_full_fidelity_submit_function(self):
        # 具名“完整保真提交”构造函数：从未编辑字段零变化的全量定义出发
        self.assertIn("function editFullService", self.html)
        self.assertIn("editFullService(e)", self.html)
        # 编辑对话框仍走 dry_run → 确认注册，edited 路径入覆盖层（既有契约保持）
        self.assertIn("预览修改（dry_run）", self.html)
        self.assertIn("payload.service.edited = editedPaths(e)", self.html)

    def test_r05_prefilled_inputs_argv_per_line(self):
        # cmd 用 textarea 预填（argv 每行一个），port/探针按定义预填
        self.assertIn("editComp(${i}, 'cmd', this.value)", self.html)
        self.assertIn('esc(c.cmd.join("\\n"))', self.html)
        self.assertIn("editComp(${i}, 'port', this.value)", self.html)
        self.assertIn("editComp(${i}, 'probe', this.value)", self.html)
        # 未在对话框展示的组件（generator）原样保留
        self.assertIn("kind === \"generator\"", self.html)

    def test_r05_compose_component_rides_through(self):
        # compose 组件保持 run=compose 及其字段，不被重建为本地 server：只有 local 组件替换 cmd
        edit_full = self.html[self.html.index("function editFullService"):
                              self.html.index("function editFullService") + 1600]
        self.assertIn('=== "local"', edit_full)
        self.assertIn("out.cmd", edit_full)
        self.assertIn("kind === \"generator\"", edit_full)

    # ---- R06-GUI：本次编辑优先 + 嵌套 dotted path 契约 ----

    def test_r06_nested_dotted_paths(self):
        # 嵌套路径按字段逐个产出：components[x].cmd / .port / .ready.http / .env.KEY
        self.assertIn("function serviceEditedPaths", self.html)
        self.assertIn('".ready.http"', self.html)
        self.assertIn('".env."', self.html)
        self.assertIn("function editedPaths", self.html)
        self.assertIn("function candidateEditedPaths", self.html)

    def test_r06_candidate_register_carries_edited(self):
        # 候选注册流同样携带 edited（本次编辑优先于旧覆盖）
        cand = self.html[self.html.index("function candPayload"):
                         self.html.index("function candPayload") + 3400]
        self.assertIn("edited", cand)

    def test_r06_override_priority_caption(self):
        # 文案语义：你的覆盖优先生效，但本次编辑优先；未编辑且命中覆盖的字段才可「采用新值」
        self.assertIn("你的覆盖优先生效", self.html)
        self.assertIn("本次编辑优先", self.html)
        # 命中覆盖且本次已编辑 → 不再渲染采用新值按钮（isEdited 门控存在）
        self.assertIn("!isEdited", self.html)

    # ---- R07-GUI：candPayload 不得丢弃 compose 参数 ----

    def test_r07_compose_fields_forwarded(self):
        cand = self.html[self.html.index("function candPayload"):
                         self.html.index("function candPayload") + 3400]
        for anchor in ('run = "compose"', "compose_service", "compose_project",
                       "compose_files", "provision", "confirmed_provision"):
            self.assertIn(anchor, cand)

    def test_r07_compose_review_ui(self):
        # 候选卡：compose 字段可编辑 + 确认勾选（由我确认）+ 不要求 cmd
        self.assertIn("允许创建容器/拉镜像", self.html)
        self.assertIn("wizConfirmProvision", self.html)
        self.assertIn("function isComposeCand", self.html)
        self.assertIn("function effectiveProvision", self.html)

    def test_r07_provision_up_warning(self):
        # provision=up 未勾选 → 注册前可见警告（后台 400 之前 GUI 先解释）
        self.assertIn("provision=up", self.html)

    # ---- R09：候选消歧不得产生重复 ID ----

    def test_r09_disambiguate_occupied_set(self):
        dis = self.html[self.html.index("function disambiguateIds(cands, w = wiz)"):
                        self.html.index("function disambiguateIds(cands, w = wiz)") + 1600]
        # 已占用集合 = 全部已登记 id ∪ 批内候选 id；分配时从占用集中消费
        self.assertIn("occupied", dis)
        self.assertIn("registry", dis)

    def test_r09_identity_match_keeps_id(self):
        dis = self.html[self.html.index("function disambiguateIds(cands, w = wiz)"):
                        self.html.index("function disambiguateIds(cands, w = wiz)") + 1600]
        # 与已登记服务同运行身份（复用/再识别更新路径）→ 保持原 id，绝不改名
        self.assertIn("sameSource", dis)
        self.assertIn("再识别", dis)

    # ---- R08：资源视图消费者/归属/复用 ----

    def test_r08_resources_columns(self):
        res = self.html.split("function viewResources()", 1)[1].split("function draftSummary(", 1)[0]
        for anchor in ("引用服务", "bind_mounts", "跨项目共享", "已停止", "已绑定服务", "容器状态无法验证"):
            self.assertIn(anchor, res)
        self.assertIn("stateLabel(s)", res)

    def test_r08_wizard_reuse_binding(self):
        # 候选审核依赖行：复用选择下拉（/api/resources），不要求手填 container/volume
        self.assertIn("复用选择", self.html)
        self.assertIn("不绑定", self.html)
        self.assertIn("function wizResourceOptions", self.html)
        self.assertIn("function wizBindDependency", self.html)
        self.assertIn("（匹配：", self.html)
        self.assertIn("function wizLoadResources", self.html)


class DefinitionEndpointShapeTest(unittest.TestCase):
    """R05 依赖的 GET /api/services/<sid>/definition：隔离真实 daemon 上锁定响应形状。

    注册一个多组件服务（worker + compose + env/build/install/after + 依赖 container/volume），
    断言定义端点返回全部清单层字段——GUI 编辑据此做“未编辑字段零变化”的全保真提交。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ls-def-shape-")
        root = Path(cls.tmp.name)
        cls.home = root / "home"
        cls.services = root / "builtin"
        (cls.home / "services").mkdir(parents=True)
        cls.services.mkdir()
        cls.src = root / "src"
        cls.src.mkdir()
        cls.port = free_port()
        env = dict(os.environ,
                   LOCALSERVICES_HOME=str(cls.home),
                   LOCALSERVICES_SERVICES=str(cls.services),
                   LOCALSERVICES_USER_SERVICES=str(cls.home / "services"),
                   LOCALSERVICES_PORT=str(cls.port),
                   PATH="/usr/bin:/bin")
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "localservices.daemon"],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.token = ""
        base = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                raise AssertionError("daemon exited during startup")
            try:
                with urllib.request.urlopen(f"{base}/api/ping", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        else:
            cls.tearDownClass()
            raise AssertionError("daemon did not come up")
        token_file = cls.home / "token"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if token_file.exists():
                cls.token = token_file.read_text().strip()
                if len(cls.token) >= 32:
                    break
            time.sleep(0.1)
        cls.base = base
        payload = {
            "schemaVersion": 1, "revision": 0, "by": "test",
            "project": {"id": "defproj", "name": "DefProj", "agent": "codex"},
            "directories": [{"path": str(cls.src), "role": "source"}],
            "service": {
                "id": "def-svc", "name": "Def", "description": "d",
                "open": f"http://127.0.0.1:{cls.port}/",
                "source": str(cls.src),
                "evidence": {"agent": "none", "summary": "definition shape fixture"},
                "components": [
                    {"id": "api", "kind": "server", "name": "api",
                     "cmd": ["/bin/sleep", "100"], "cwd": ".", "port": cls.port,
                     "ready": {"http": f"http://127.0.0.1:{cls.port}/"},
                     "env": {"NODE_ENV": "prod"}, "build": ["make", "build"],
                     "install": ["npm", "ci"], "after": ["worker"]},
                    {"id": "worker", "kind": "worker", "name": "worker",
                     "cmd": ["/bin/sleep", "200"], "cwd": ".", "env": {"QUEUE": "nats"}},
                    {"id": "stack", "kind": "server", "name": "stack", "cwd": ".",
                     "run": "compose", "compose_service": "web",
                     "compose_project": "demo", "compose_files": ["docker-compose.yml"],
                     "provision": "existing"},
                ],
                "dependencies": [
                    {"name": "db", "probe": {"tcp": "127.0.0.1:5432"},
                     "container": "def-db", "volume": "def-data"}]}}
        req = urllib.request.Request(
            base + "/api/register", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-LocalServices-Token": cls.token},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
            if r.status != 200 or not body.get("ok"):
                raise AssertionError(f"fixture register failed: {body}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if getattr(cls, "tmp", None):
            cls.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, headers={"X-LocalServices-Token": self.token}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_definition_returns_full_fidelity_fields(self):
        code, res = self._get("/api/services/def-svc/definition")
        self.assertEqual(code, 200, res)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("service_id"), "def-svc")
        self.assertIn("definition", res)
        self.assertIn("revision", res)
        d = res["definition"]
        self.assertEqual(d["id"], "def-svc")
        self.assertEqual(d["name"], "Def")
        self.assertEqual(d["source"], str(self.src.resolve()))
        comps = {c["id"]: c for c in d["components"]}
        # 本地 server：cmd/port/ready/env/build/install/after 全保留
        api = comps["api"]
        self.assertEqual(api["cmd"], ["/bin/sleep", "100"])
        self.assertEqual(api["port"], self.port)
        self.assertEqual(api["env"], {"NODE_ENV": "prod"})
        self.assertEqual(api["build"], ["make", "build"])
        self.assertEqual(api["install"], ["npm", "ci"])
        self.assertEqual(api["after"], ["worker"])
        # worker 保持 kind=worker 与自己的 env/cmd
        worker = comps["worker"]
        self.assertEqual(worker["kind"], "worker")
        self.assertEqual(worker["cmd"], ["/bin/sleep", "200"])
        self.assertEqual(worker["env"], {"QUEUE": "nats"})
        # compose 组件保留 run/compose_* / provision，且不带 cmd
        stack = comps["stack"]
        self.assertEqual(stack.get("run"), "compose")
        self.assertEqual(stack.get("compose_service"), "web")
        self.assertEqual(stack.get("compose_project"), "demo")
        self.assertEqual(stack.get("compose_files"), ["docker-compose.yml"])
        self.assertEqual(stack.get("provision"), "existing")
        self.assertNotIn("cmd", stack)
        # 依赖保留 container/volume（R08 复用绑定的既有值来源）
        dep = d["dependencies"][0]
        self.assertEqual(dep["name"], "db")
        self.assertEqual(dep.get("container"), "def-db")
        self.assertEqual(dep.get("volume"), "def-data")

    def test_definition_unknown_service_is_404(self):
        code, res = self._get("/api/services/no-such-svc/definition")
        self.assertEqual(code, 404)
        self.assertFalse(res.get("ok", True))


if __name__ == "__main__":
    unittest.main()
