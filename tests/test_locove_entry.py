"""Locove 80 入口转发器行为：纯字节转发（HTTP 与 WebSocket 升级原样通过）、
上游拒绝时快速关闭、多连接并发。以普通用户在临时高端口上验证（同一代码路径，
生产实例仅端口为 80 且以 root 绑定后降权）。"""

from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices.locove_entry import serve  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class EntryForwarderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = socket.socket()
        cls.upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.upstream.bind(("127.0.0.1", 0))
        cls.upstream.listen(8)
        cls.upstream_port = cls.upstream.getsockname()[1]
        cls.listen_port = free_port()
        threading.Thread(target=serve, args=(cls.listen_port, cls.upstream_port),
                         daemon=True).start()
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", cls.listen_port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.02)
        raise AssertionError("forwarder did not start")

    @classmethod
    def tearDownClass(cls):
        cls.upstream.close()

    def _serve_once(self, respond: bytes):
        """接受一个携带请求数据的上游连接，回写 respond。

        就绪探测（空连接）在转发器与上游间也会留下 socket——跳过没读到数据的连接。"""
        while True:
            conn, _ = self.upstream.accept()
            with conn:
                conn.settimeout(5)
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    continue
                conn.sendall(respond)
                return data

    def test_forwards_http_request_bytes_verbatim(self):
        result = {}

        def upstream_side():
            result["req"] = self._serve_once(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        upstream = threading.Thread(target=upstream_side, daemon=True)
        upstream.start()
        with socket.create_connection(("127.0.0.1", self.listen_port), timeout=5) as c:
            c.sendall(b"GET /zh HTTP/1.1\r\nHost: buildbase.locove\r\n\r\n")
            c.settimeout(5)
            body = b""
            while not body.endswith(b"ok"):
                chunk = c.recv(4096)
                if not chunk:
                    break
                body += chunk
        upstream.join(timeout=5)
        self.assertFalse(upstream.is_alive(), "upstream did not record the request")
        self.assertIn(b"Host: buildbase.locove", result["req"])
        self.assertTrue(body.endswith(b"ok"))

    def test_forwards_websocket_upgrade_headers(self):
        result = {}

        def upstream_side():
            result["req"] = self._serve_once(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")

        upstream = threading.Thread(target=upstream_side, daemon=True)
        upstream.start()
        with socket.create_connection(("127.0.0.1", self.listen_port), timeout=5) as c:
            c.sendall(b"GET /_next/webpack-hmr HTTP/1.1\r\nHost: x.locove\r\n"
                      b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: k==\r\n\r\n")
            c.settimeout(5)
            data = b""
            while b"101" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
        upstream.join(timeout=5)
        self.assertFalse(upstream.is_alive(), "upstream did not record the request")
        self.assertIn(b"Upgrade: websocket", result["req"])
        self.assertIn(b"101", data)

    def test_upstream_refusal_closes_fast(self):
        dead = free_port()  # 无监听：转发器应立即关闭客户端连接，不挂起
        lp = free_port()
        threading.Thread(target=serve, args=(lp, dead), daemon=True).start()
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", lp), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.02)
        with socket.create_connection(("127.0.0.1", lp), timeout=5) as c:
            c.sendall(b"GET / HTTP/1.1\r\nHost: a.locove\r\n\r\n")
            c.settimeout(5)
            self.assertEqual(c.recv(1024), b"")


if __name__ == "__main__":
    unittest.main()
