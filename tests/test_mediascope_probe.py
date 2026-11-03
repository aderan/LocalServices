"""MediaScope busy 探测回归（2026-11-03 线上事故）：

/api/tasks 返回全量任务史且无过滤参数（464 条 ≈ 500KB 且只增不减）。
默认 256KB 截断后 http_get 返回 200+空 body，旧代码 json.loads(b"") 抛 ValueError
→ busy() 报“任务状态无法解析”→ stop/restart/自动更新三路全部永久被卡
（实际 0 个进行中任务，dashboard 空闲）。
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from localservices.adapters import mediascope  # noqa: E402

# 响应体按 path 存 bytes；测试里改 ROUTES 即可切换场景
ROUTES: dict[str, bytes] = {}
PAD = {"v": "x" * 1200}  # 单条 ≈1.2KB，与真实 tasks_payload 同量级


def _tasks_body(n: int, active: int = 0) -> bytes:
    tasks = [{"task_id": f"t{i}", "status": ("running" if i < active else "completed"), **PAD}
             for i in range(n)]
    return json.dumps({"tasks": tasks}).encode()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = ROUTES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class FakeProj:
    """mediascope.Adapter 只读 project.open。"""

    def __init__(self, port: int) -> None:
        self.open = f"http://127.0.0.1:{port}/"


OVERVIEW_IDLE = json.dumps({"rooms": [{"phase": "idle", "finishing": False}]}).encode()


class MediaScopeProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.adapter = mediascope.Adapter(FakeProj(cls.port))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_1_history_over_256kb_is_idle(self):
        # 事故现场：>256KB 全终态任务史。修复前 busy() 报“任务状态无法解析”卡死更新
        ROUTES["/api/tasks"] = _tasks_body(300)  # ≈360KB
        ROUTES["/api/live/overview"] = OVERVIEW_IDLE
        self.assertGreater(len(ROUTES["/api/tasks"]), 256 * 1024)
        self.assertIsNone(self.adapter.busy())

    def test_2_active_task_defers(self):
        ROUTES["/api/tasks"] = _tasks_body(300, active=2)
        ROUTES["/api/live/overview"] = OVERVIEW_IDLE
        self.assertIn("2 个任务进行中", self.adapter.busy())

    def test_3_live_recording_defers(self):
        ROUTES["/api/tasks"] = _tasks_body(5)
        ROUTES["/api/live/overview"] = json.dumps(
            {"rooms": [{"phase": "recording", "finishing": False}]}).encode()
        self.assertIn("1 路直播录制中", self.adapter.busy())

    def test_4_oversize_even_8mb_defers_with_explicit_reason(self):
        # 超过 8MB 探测上限：仍保守延后，但理由必须点明响应异常（不再是“无法解析”）
        ROUTES["/api/tasks"] = _tasks_body(8000)  # ≈9.6MB
        ROUTES["/api/live/overview"] = OVERVIEW_IDLE
        reason = self.adapter.busy()
        self.assertIsNotNone(reason)
        self.assertIn("响应异常", reason)
        self.assertIn("exceeds", reason)

    def test_5_unparseable_body_defers(self):
        ROUTES["/api/tasks"] = b"<html>maintenance</html>"
        ROUTES["/api/live/overview"] = OVERVIEW_IDLE
        self.assertEqual(self.adapter.busy(), "任务状态无法解析，暂缓更新")

    def test_6_dashboard_down_is_idle(self):
        down = mediascope.Adapter(FakeProj(1))  # 1 端口必拒连
        self.assertIsNone(down.busy())


if __name__ == "__main__":
    unittest.main()
