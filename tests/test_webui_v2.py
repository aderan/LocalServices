"""V2.11 GUI 契约测试（Agent/UX 线）：dashboard.html 静态锚点 + 隔离真实 daemon E2E
（/api/analyze 带 project_id、多目录同名候选注册不互相覆盖）。

静态层模式沿 tests/test_webui.py（锚点断言）；JS 行为层（菜单开合、模态 Esc/Enter、
消歧 -2 后缀、accept_new、autostart 请求形状）由 scripts/verify_v2_gui.cjs 无头验证。
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


class DashboardV2StaticTest(unittest.TestCase):
    """V2.11 增删入口 / 模态 / 覆盖层 / 恢复视图的静态锚点。"""

    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    # ---- (a) ⋯ 溢出菜单 ----

    def test_menu_anchors(self):
        for anchor in ("toggleMenu(event,'svc:", "toggleMenu(event,'proj:",
                       'aria-haspopup="menu"', "function projectMenuItems",
                       "function serviceMenuItems"):
            self.assertIn(anchor, self.html)

    def test_menu_labels(self):
        # 项目 ⋯：添加服务…/重新识别/重命名/移除项目…；服务 ⋯：编辑/日志/打开目录/移除登记…
        proj = self.html[self.html.index("function projectMenuItems"):
                         self.html.index("function projectMenuItems") + 700]
        for label in ("添加服务…", "重新识别", "重命名", "移除项目…"):
            self.assertIn(f'"{label}"', proj)
        svc = self.html[self.html.index("function serviceMenuItems"):
                        self.html.index("function serviceMenuItems") + 700]
        for label in ("编辑", "日志", "打开目录", "移除登记…"):
            self.assertIn(f'"{label}"', svc)

    def test_remove_action_moved_to_menu(self):
        # 折叠区保留技术明细；移除按钮上移菜单（锚点注释留在原位）
        self.assertIn("<!-- 移除入口已移至服务 ⋯ 菜单（V2.11） -->", self.html)
        self.assertNotIn("removeBlock", self.html)
        self.assertNotIn("移除登记…</button></div>${removeBlock", self.html)

    def test_menu_keyboard_accessibility(self):
        self.assertIn('setAttribute("role", "menu")', self.html)
        self.assertIn('setAttribute("role", "menuitem")', self.html)
        self.assertIn('e.key === "Escape"', self.html)   # Esc 关闭菜单
        self.assertIn("ArrowDown", self.html)            # ↑/↓ 菜单项导航

    # ---- (b) 自绘模态系统 ----

    def test_modal_system(self):
        for anchor in ("function showModal", 'setAttribute("aria-modal", "true")',
                       'setAttribute("role", "dialog")', "焦点陷阱", "modalOpen"):
            self.assertIn(anchor, self.html)

    def test_modal_keyboard_semantics(self):
        # Esc=取消 / Enter=主操作（textarea 除外）/ Tab 焦点循环
        self.assertIn('e.key === "Escape"', self.html)
        self.assertIn('e.key === "Enter"', self.html)
        self.assertIn('t.tagName === "TEXTAREA"', self.html)
        self.assertIn('e.key === "Tab"', self.html)

    def test_poll_guard_while_modal_open(self):
        # 轮询刷新不重建内容区 DOM：uiLocked() 守卫进入 render 的 innerHTML 路径
        self.assertIn("function uiLocked", self.html)
        self.assertIn("if (uiLocked()) return;", self.html)

    def test_confirm_prompt_replaced_for_removal_rename(self):
        # 重命名/移除走自绘模态，不再 prompt()/confirm()
        self.assertNotIn("prompt(", self.html)
        rename = self.html[self.html.index("async function renameProject"):
                           self.html.index("async function renameProject") + 900]
        self.assertNotIn("confirm(", rename)
        remove = self.html[self.html.index("async function askRemoveService"):
                           self.html.index("async function askRemoveService") + 2500]
        self.assertNotIn("confirm(", remove)

    # ---- (c) remove-preview 对话框 ----

    def test_remove_preview_fetches(self):
        self.assertIn("/api/services/${sid}/remove-preview", self.html)
        self.assertIn("/api/projects/${pid}/remove-preview", self.html)

    def test_remove_dialog_content_requirements(self):
        for anchor in ("将停止的实例", "保留内容", "受影响项目", "恢复方式"):
            self.assertIn(anchor, self.html)
        # keep-external 不可用时禁用并给原因
        self.assertIn("enabled === false", self.html)
        self.assertIn("无法保留为外部实例", self.html)
        # 项目移除失败显示逐成员 stopped/remaining
        self.assertIn("res.stopped", self.html)
        self.assertIn("res.remaining", self.html)

    def test_cancel_zero_side_effects_shape(self):
        # 取消路径不触发 DELETE：DELETE 只出现在确认回调（doRemove / execProjectRemove）里
        self.assertIn('await doRemove(sid, __rmDisp)', self.html)
        self.assertIn("async function execProjectRemove", self.html)

    # ---- (d) 编辑对话框 ----

    def test_edit_dialog_flow(self):
        for anchor in ("function editService", "预览修改（dry_run）", "确认注册",
                       "function editedPaths", "payload.service.edited = editedPaths(e)",
                       "components[", "dry_run: true"):
            self.assertIn(anchor, self.html)

    # ---- (e) 多目录候选消歧（V2.10 / N04 AC-06） ----

    def test_disambiguation_is_named_function(self):
        # 必须是具名可测函数（不在 render 内联），且在联合识别批次完成时调用
        import re
        self.assertRegex(self.html, r"function disambiguateIds\(cands\)")
        self.assertIn("disambiguateIds(wiz.candidates)", self.html)
        self.assertIn("多目录同名服务已分配稳定 ID，可修改", self.html)

    def test_candidate_id_remains_editable(self):
        # (c) 同批次 id 在注册前仍可人工修改
        self.assertIn("wizCandField(${i},'id',this.value)", self.html)

    # ---- (f) 覆盖层「采用新值」 ----

    def test_accept_new_flow(self):
        for anchor in ("wizAcceptNew", "采用新值", "你的覆盖优先生效",
                       "/api/services/${res.service_id}/overrides",
                       "svc.accept_new"):
            self.assertIn(anchor, self.html)

    # ---- (g) autostart 专用端点 ----

    def test_autostart_dedicated_endpoint(self):
        self.assertIn("/api/projects/${pid}/autostart", self.html)
        # 不再走无名 /api/projects upsert 携带 autostart
        self.assertNotIn("autostart: value", self.html)

    # ---- (h) 已移除登记视图 ----

    def test_removed_view(self):
        for anchor in ("已移除登记", "nav-removed", "function viewRemoved",
                       "function loadRemoved", "function restoreService",
                       "function restoreProject",
                       "/api/removed/services/${sid}/restore",
                       "/api/removed/projects/${pid}/restore",
                       "removedIntents"):
            self.assertIn(anchor, self.html)
        # 恢复意图缺省 stopped（绝不自动带回 running）
        restore = self.html[self.html.index("async function restoreService"):
                            self.html.index("async function restoreService") + 600]
        self.assertIn('"stopped"', restore)


class DaemonV2EndToEndTest(unittest.TestCase):
    """隔离真实 daemon（绝不碰 47800 / 真实数据目录）上的 V2 E2E。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ls-webui-v2-")
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
                   PATH="/usr/bin:/bin")   # 无 codex/claude：分析任务快速失败，不真调用
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

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "X-LocalServices-Token": self.token}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {}

    def _register(self, sid: str, source: Path, port: int, revision: int, project: str):
        payload = {
            "schemaVersion": 1, "revision": revision, "by": "test",
            "project": {"id": project, "name": project, "agent": "codex"},
            "directories": [{"path": str(source), "role": "source"}],
            "service": {
                "id": sid, "name": sid, "source": str(source),
                "evidence": {"agent": "none", "summary": "v2 e2e fixture"},
                "components": [{"id": "main", "kind": "server", "name": "main",
                                "cmd": ["/bin/sleep", "100"], "cwd": ".",
                                "port": port,
                                "ready": {"http": f"http://127.0.0.1:{port}/"}}]}}
        return self.call("POST", "/api/register", payload)

    # ---- /api/analyze 带 project_id（V2.10） ----

    def test_analyze_with_project_id_persists_to_job_file(self):
        code, res = self._register("v2e2e-app", self.src, 18260, 0, "v2e2e")
        self.assertEqual(code, 200, res)
        code, res = self.call("POST", "/api/analyze",
                              {"directory": str(self.src), "agent": "codex",
                               "project_id": "v2e2e"})
        self.assertEqual(code, 202, res)
        self.assertTrue(res.get("ok"))
        job_id = res["job_id"]
        job_file = self.home / "jobs" / f"{job_id}.json"
        self.assertTrue(job_file.is_file(), "job 文件必须落盘")
        job = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(job["project_id"], "v2e2e")
        self.assertEqual(job["kind"], "analyze")

    def test_analyze_unknown_project_id_is_400(self):
        code, res = self.call("POST", "/api/analyze",
                              {"directory": str(self.src), "agent": "codex",
                               "project_id": "no-such-project"})
        self.assertEqual(code, 400)
        self.assertIn("未知项目", res.get("error", ""))

    # ---- 多目录同名候选：消歧后的批次注册出两条服务（N04 AC-06） ----
    # JS 侧 -2/-3 后缀分配与可编辑性由 scripts/verify_v2_gui.cjs 行为级验证；
    # 这里锁服务端后果：按向导消歧后的 id 批量注册 → 两条都 created，第一条不被覆盖。

    def test_wizard_batch_disambiguated_ids_register_two_services(self):
        root = Path(self.tmp.name)
        dirA, dirB = root / "dirA", root / "dirB"
        dirA.mkdir(exist_ok=True)
        dirB.mkdir(exist_ok=True)
        # 向导消歧后的批次：第一个目录保持 dup-app，第二个目录自动得到 dup-app-2
        code1, res1 = self._register("dup-app", dirA, 18261, 0, "dupproj")
        self.assertEqual(code1, 200, res1)
        self.assertEqual(res1.get("action"), "created", res1)
        code2, res2 = self._register("dup-app-2", dirB, 18262, res1.get("revision", 1), "dupproj")
        self.assertEqual(code2, 200, res2)
        self.assertEqual(res2.get("action"), "created", res2)
        code, reg = self.call("GET", "/api/registry")
        ids = [s["id"] for s in reg.get("services", [])]
        self.assertIn("dup-app", ids)
        self.assertIn("dup-app-2", ids)

    def test_same_id_different_source_would_overwrite_without_disambiguation(self):
        # 反例（为什么必须消歧）：未消歧时第二个目录沿用同 id + 不同 source
        # → registry 按“同 id 更新”处理（identity_changed 的 update 原语合法），
        # 向导侧靠稳定后缀避免这种静默覆盖。本测试自建项目，不依赖执行顺序。
        root = Path(self.tmp.name)
        dirA, dirC = root / "ovwA", root / "ovwC"
        dirA.mkdir(exist_ok=True)
        dirC.mkdir(exist_ok=True)
        code1, res1 = self._register("ovw-app", dirA, 18264, 0, "ovwproj")
        self.assertEqual(code1, 200, res1)
        self.assertEqual(res1.get("action"), "created", res1)
        code, res = self._register("ovw-app", dirC, 18265, res1.get("revision", 1), "ovwproj")
        self.assertEqual(code, 200, res)
        self.assertIn(res.get("action"), ("updated", "created"))  # 更新原语本身合法
        # 但 registry 里仍然只有一条 ovw-app（没有第二条服务）
        code, reg2 = self.call("GET", "/api/registry")
        self.assertEqual([s["id"] for s in reg2["services"]].count("ovw-app"), 1)


if __name__ == "__main__":
    unittest.main()
