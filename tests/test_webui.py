"""Web 工作台（dashboard.html）与主窗口 App 的契约测试（N02 Desktop 线）。

三层：
1. 静态断言：dashboard.html 含工作台关键锚点（原生桥、搜索框、六个导航项、注册/分析/
   资源/草稿 API、DELETE /api/services、浅深色、focus-visible）。
2. 端到端：起隔离 daemon（LOCALSERVICES_HOME 临时目录 + 空闲端口，绝不碰 47800），
   验证 token 注入、401/403 安全模型、真实注册 200。
3. 主窗口构建冒烟：swiftc 可用时跑 menubar/build.sh 并断言 .app 产出。
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
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


class DashboardStaticTest(unittest.TestCase):
    """dashboard.html 的静态锚点（无需 daemon）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_native_bridge_pick_directory(self):
        # 原生桥：优先 WKWebView messageHandlers.lsbridge，浏览器环境退化
        self.assertIn("messageHandlers.lsbridge", self.html)
        self.assertIn("pickDirectory", self.html)
        self.assertIn("lsPickDirectory", self.html)

    def test_global_search_box(self):
        header = self.html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
        self.assertIn('id="globalSearch"', header)
        self.assertNotIn('id="refreshBtn"', header)

    def test_sidebar_navigation(self):
        # R6 IA：服务 / 按 Project 分组列表 / 需要处理 / 共享资源 / Agent 连接 / 设置（与草稿分离）
        nav = self.html.split("<nav ", 1)[1].split("</nav>", 1)[0]
        for anchor in ('id="nav-all"', 'id="navProjects"', "需要处理", "共享资源", "Agent 连接",
                       'id="nav-settings"', "已移除登记", "＋ 添加服务"):
            self.assertIn(anchor, self.html)
        self.assertIn('id="nav-settings"', nav)
        self.assertNotIn("设置 / 草稿", self.html)  # 草稿归入添加流程，不再与设置混放

    def test_api_anchors(self):
        for api in ("/api/analyze", "/api/register", "/api/jobs/", "/api/resources",
                    "/api/drafts", "/api/services/", "/api/agents/candidates"):
            self.assertIn(api, self.html)
        self.assertIn("disposition=", self.html)  # DELETE /api/services/<id>?disposition=…

    def test_project_rename_and_candidates(self):
        # AC-01：项目详情视图的重命名控件；AC-23：连接页候选去重导入（不自动启动）
        self.assertIn("renameProject", self.html)
        self.assertIn("重命名", self.html)
        self.assertIn("candidatesCard", self.html)
        self.assertIn("已知工具的项目候选", self.html)
        self.assertIn("importCandidate", self.html)
        # 去重取值必须与 /api/registry 响应结构一致（registry.registry.projects）
        self.assertIn("registry?.registry?.projects || []", self.html)
        # 导入候选只建项目：importCandidate 内不出现 start/register 调用
        ic = self.html[self.html.index("async function importCandidate"):self.html.index("async function importCandidate") + 700]
        self.assertNotIn("/start", ic)
        self.assertNotIn("/register", ic)

    def test_theme_and_a11y(self):
        self.assertIn("prefers-color-scheme", self.html)
        self.assertIn("focus-visible", self.html)
        self.assertIn("__TOKEN__", self.html)  # daemon 注入点


class DaemonEndToEndTest(unittest.TestCase):
    """隔离 daemon 上的 HTTP 契约：token、Host/Origin、注册主路径。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ls-webui-test-")
        root = Path(cls.tmp.name)
        cls.home = root / "home"
        cls.services = root / "services"
        cls.src = root / "src"
        cls.services.mkdir(parents=True)
        cls.src.mkdir()
        cls.port = free_port()
        env = dict(os.environ,
                   LOCALSERVICES_HOME=str(cls.home),
                   LOCALSERVICES_SERVICES=str(cls.services),
                   LOCALSERVICES_PORT=str(cls.port))
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "localservices.daemon"],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.token = ""
        deadline = time.monotonic() + 20
        base = f"http://127.0.0.1:{cls.port}"
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
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) and cls.proc.poll() is None:
            cls.proc.terminate()  # 只杀自己起的进程（绝不用 pkill 按名匹配）
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        if getattr(cls, "tmp", None):
            cls.tmp.cleanup()

    def call(self, method: str, path: str, body: dict | None = None, *,
             token: str | None = ..., origin: str | None = None) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json"}
        if token is ...:
            token = self.token
        if token is not None:
            headers["X-LocalServices-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode("utf-8", "replace")

    # ---- GET / 的 token 注入 ----

    def test_index_serves_dashboard_with_injected_token(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as r:
            self.assertEqual(r.status, 200)
            html = r.read().decode("utf-8")
        self.assertIn("text/html", r.headers.get("Content-Type", ""))
        self.assertNotIn("__TOKEN__", html)          # 占位符必须被替换
        self.assertIn(f'const TOKEN = "{self.token}"', html)
        self.assertIn("globalSearch", html)

    def test_dashboard_logo_loads_without_api_token(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as r:
            self.assertIn('src="/locove-logo.png"', r.read().decode("utf-8"))
        with urllib.request.urlopen(f"{self.base}/locove-logo.png", timeout=10) as r:
            self.assertEqual(r.headers.get_content_type(), "image/png")
            self.assertEqual(r.read(), (REPO_ROOT / "assets" / "locove-logo.png").read_bytes())

    # ---- 安全模型 ----

    def test_api_without_token_is_401(self):
        code, body = self.call("GET", "/api/registry", token=None)
        self.assertEqual(code, 401)
        self.assertIn("unauthorized", str(body))

    def test_language_preference_roundtrips_through_registry(self):
        endpoint = "/api/settings/language"
        self.addCleanup(self.call, "POST", endpoint, {"language": "zh-CN"})
        self.assertEqual(self.call("POST", endpoint, {"language": "en"}, token=None)[0], 401)
        self.assertEqual(self.call("POST", endpoint, {"language": "en"}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.call("POST", endpoint, {"language": "en"}),
                         (200, {"ok": True, "language": "en"}))
        self.assertEqual(self.call("POST", endpoint, {"language": "xx"})[0], 400)
        code, body = self.call("GET", "/api/registry")
        self.assertEqual(code, 200)
        self.assertEqual(body["registry"]["settings"]["language"], "en")

    def test_register_with_foreign_origin_is_403(self):
        code, body = self.call("POST", "/api/register", {"schemaVersion": 1, "service": {}},
                               origin="http://evil.example")
        self.assertEqual(code, 403)
        self.assertIn("origin", str(body))

    # ---- 真实注册主路径（浏览器同源 Origin）----

    def test_register_valid_payload_creates_service(self):
        port = free_port()
        payload = {
            "schemaVersion": 1, "revision": 0, "by": "gui",
            "project": {"id": "webui-e2e", "name": "WebUI E2E", "agent": "codex"},
            "directories": [{"path": str(self.src), "role": "source"}],
            "service": {
                "id": "webui-e2e-app", "name": "WebUI E2E App", "source": str(self.src),
                "open": f"http://127.0.0.1:{port}/",
                "evidence": {"agent": "none", "summary": "e2e"},
                "components": [{"id": "main", "kind": "server", "name": "main",
                                "cmd": ["/bin/sleep", "100"], "cwd": ".",
                                "port": port,
                                "ready": {"http": f"http://127.0.0.1:{port}/"}}]}}
        code, res = self.call("POST", "/api/register", payload, origin=self.origin)
        self.assertEqual(code, 200, res)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("action"), "created")
        self.assertEqual(res.get("service_id"), "webui-e2e-app")

        code, reg = self.call("GET", "/api/registry")
        self.assertEqual(code, 200)
        ids = [s["id"] for s in reg["services"]]
        self.assertIn("webui-e2e-app", ids)
        pids = [p["id"] for p in reg["registry"]["projects"]]
        self.assertIn("webui-e2e", pids)

    def test_draft_roundtrip_via_delete(self):
        """草稿 → 列表 → 转正（必须显式 draft:false）→ 移除登记。"""
        payload = {"schemaVersion": 1, "project": {"id": "webui-draft", "name": "DraftP"},
                   "service": {"id": "draft-app", "name": "Draft App", "source": str(self.src),
                               "draft": True, "components": [{"id": "main", "name": "main"}]}}
        code, res = self.call("POST", "/api/register", payload, origin=self.origin)
        self.assertEqual(code, 200)
        self.assertEqual(res.get("action"), "draft")
        draft_id = res.get("draft_id")
        self.assertTrue(draft_id)

        code, drafts = self.call("GET", "/api/drafts")
        self.assertEqual(code, 200)
        self.assertIn(draft_id, [d["id"] for d in drafts["drafts"]])

        port = free_port()
        complete = {"service": {"draft": False, "id": "draft-app", "name": "Draft App",
                                "components": [{"id": "main", "kind": "server",
                                                "cmd": ["/bin/sleep", "100"], "port": port,
                                                "ready": {"http": f"http://127.0.0.1:{port}/"}}]}}
        code, res = self.call("POST", f"/api/drafts/{draft_id}/register", complete, origin=self.origin)
        self.assertEqual(code, 200, res)
        self.assertEqual(res.get("action"), "created")

        code, res = self.call("DELETE", "/api/services/draft-app?disposition=keep-external",
                              origin=self.origin)
        self.assertEqual(code, 200, res)
        self.assertTrue(res.get("ok"))


class MenubarBuildTest(unittest.TestCase):
    """主窗口 App 构建冒烟：swiftc 可用才跑（CI/无 Xcode 环境跳过）。"""

    def test_build_sh_produces_app(self):
        swiftc = shutil.which("swiftc")
        if not swiftc:
            self.skipTest("swiftc 不可用（未安装 Xcode Command Line Tools）")
        app = REPO_ROOT / "menubar" / "build" / "MyBrain Services.app"
        exe = app / "Contents" / "MacOS" / "MyBrainServices"
        try:
            subprocess.run(["bash", str(REPO_ROOT / "menubar" / "build.sh")],
                           cwd=REPO_ROOT / "menubar", timeout=300, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            self.fail("menubar/build.sh 超时（>300s）")
        except subprocess.CalledProcessError as e:
            self.fail(f"menubar/build.sh 失败:\n{e.stdout.decode('utf-8', 'replace')}")
        self.assertTrue(exe.exists(), f"缺少可执行文件 {exe}")
        self.assertTrue((app / "Contents" / "Info.plist").exists())
        plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
        self.assertIn("0.2.0", plist)
        icon_name = plistlib.loads(plist.encode())["CFBundleIconFile"]
        icon = app / "Contents" / "Resources" / icon_name
        self.assertEqual(icon.read_bytes()[:4], b"icns")
        status_logo = app / "Contents" / "Resources" / "LocoveStatus.png"
        self.assertEqual(status_logo.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
