"""Locove DNS responder 协议行为：已登记名 A=回环、AAAA=NODATA、未登记 NXDOMAIN、
UDP/TCP 双协议、畸形包 FORMERR、routes.json 热更新。"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices.locove_dns import (  # noqa: E402
    RCODE_NXDOMAIN, RCODE_OK, Zone, build_response, handle_packet, parse_query, serve,
)


def encode_name(name: str) -> bytes:
    out = b""
    for lab in name.split("."):
        b = lab.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"


def make_query(qid: int, name: str, qtype: int) -> bytes:
    return (struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
            + encode_name(name) + struct.pack("!HH", qtype, 1))


def parse_name(msg: bytes, off: int) -> tuple[str, int]:
    labels = []
    while True:
        n = msg[off]
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", msg[off:off + 2])[0] & 0x3FFF
            name, _ = parse_name(msg, ptr)
            labels.append(name)
            return ".".join(l for l in labels if l), off + 2
        off += 1
        if n == 0:
            return ".".join(labels), off
        labels.append(msg[off:off + n].decode())
        off += n


def answer_records(msg: bytes) -> list[tuple[str, int, bytes]]:
    _, qend = parse_name(msg, 12)
    off = qend + 4
    an = struct.unpack("!H", msg[6:8])[0]
    out = []
    for _ in range(an):
        name, off = parse_name(msg, off)
        rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", msg[off:off + 10])
        off += 10
        out.append((name, rtype, msg[off:off + rdlen]))
        off += rdlen
    return out


class ZoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-dns-")
        self.addCleanup(self.tmp.cleanup)
        self.routes = Path(self.tmp.name) / "routes.json"
        self.routes.parent.mkdir(parents=True, exist_ok=True)

    def write_routes(self, hosts):
        self.routes.write_text(json.dumps({"routes": {h: {} for h in hosts}}), encoding="utf-8")

    def test_zone_reads_and_hot_reloads(self):
        zone = Zone(self.routes)
        self.write_routes(["a.locove"])
        zone.refresh()
        self.assertTrue(zone.contains("a.locove"))
        self.assertFalse(zone.contains("b.locove"))
        self.write_routes(["a.locove", "b.locove"])
        zone.refresh()
        self.assertTrue(zone.contains("b.locove"))

    def test_zone_bad_file_means_empty(self):
        self.routes.write_text("{not json", encoding="utf-8")
        zone = Zone(self.routes)
        zone.refresh()
        self.assertFalse(zone.contains("a.locove"))


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-dns-")
        self.addCleanup(self.tmp.cleanup)
        self.routes = Path(self.tmp.name) / "routes.json"
        self.routes.write_text(json.dumps({"routes": {"buildbase.locove": {}}}), encoding="utf-8")
        self.zone = Zone(self.routes)
        self.zone.refresh()

    def test_registered_a_returns_loopback(self):
        q = make_query(7, "buildbase.locove", 1)
        r = build_response(q, self.zone)
        qid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", r[:12])
        self.assertEqual(qid, 7)
        self.assertEqual(flags & 0x000F, RCODE_OK)
        self.assertEqual(an, 1)
        recs = answer_records(r)
        self.assertEqual(recs[0][1], 1)
        self.assertEqual(socket.inet_ntoa(recs[0][2]), "127.0.0.1")

    def test_registered_aaaa_is_nodata(self):
        r = build_response(make_query(8, "buildbase.locove", 28), self.zone)
        an = struct.unpack("!H", r[6:8])[0]
        self.assertEqual(struct.unpack("!H", r[2:4])[0] & 0x000F, RCODE_OK)
        self.assertEqual(an, 0)

    def test_unknown_name_nxdomain(self):
        r = build_response(make_query(9, "nope.locove", 1), self.zone)
        self.assertEqual(struct.unpack("!H", r[2:4])[0] & 0x000F, RCODE_NXDOMAIN)
        self.assertEqual(struct.unpack("!H", r[6:8])[0], 0)

    def test_other_qtype_registered_is_nodata(self):
        # Safari/Chrome 的 HTTPS(SVCB) 查询：已登记名 NOERROR 空答案
        r = build_response(make_query(10, "buildbase.locove", 65), self.zone)
        self.assertEqual(struct.unpack("!H", r[2:4])[0] & 0x000F, RCODE_OK)
        self.assertEqual(struct.unpack("!H", r[6:8])[0], 0)

    def test_malformed_packet_formerr(self):
        r = handle_packet(b"\x00\x01\x00", self.zone)
        self.assertIsNotNone(r)
        self.assertEqual(struct.unpack("!H", r[2:4])[0] & 0x000F, 1)

    def test_parse_query_roundtrip(self):
        qid, name, qtype = parse_query(make_query(3, "X.Locove.LOCOVE", 1))
        self.assertEqual((qid, name, qtype), (3, "x.locove.locove", 1))


class ServeTest(unittest.TestCase):
    """真实 UDP/TCP socket 上的端到端（回环 + 临时端口）。

    类级共享一个 server：routes 文件生命周期覆盖全部用例——服务器对已删除的
    routes.json 会正确收敛为 NXDOMAIN（ZoneTest 已覆盖），不能让清理互相干扰。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ls-dns-")
        cls.routes = Path(cls.tmp.name) / "routes.json"
        cls.routes.write_text(json.dumps({"routes": {"app.locove": {}}}), encoding="utf-8")
        cls.port = 53000 + (os.getpid() % 1000)
        cls.thread = threading.Thread(target=serve, args=(cls.port, cls.routes), daemon=True)
        cls.thread.start()
        import time as _t
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                return
            except OSError:
                _t.sleep(0.02)
        raise AssertionError("dns test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_udp_query(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(make_query(42, "app.locove", 1), ("127.0.0.1", self.port))
        data, _ = s.recvfrom(4096)
        self.assertEqual(struct.unpack("!H", data[:2])[0], 42)
        self.assertEqual(answer_records(data)[0][1], 1)

    def test_udp_unknown_nxdomain(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(make_query(43, "ghost.locove", 1), ("127.0.0.1", self.port))
        data, _ = s.recvfrom(4096)
        self.assertEqual(struct.unpack("!H", data[2:4])[0] & 0x000F, RCODE_NXDOMAIN)

    def test_tcp_query(self):
        payload = make_query(44, "app.locove", 1)
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as s:
            s.sendall(struct.pack("!H", len(payload)) + payload)
            hdr = s.recv(2)
            (n,) = struct.unpack("!H", hdr)
            data = b""
            while len(data) < n:
                data += s.recv(n - len(data))
        self.assertEqual(struct.unpack("!H", data[:2])[0], 44)
        self.assertEqual(answer_records(data)[0][1], 1)


if __name__ == "__main__":
    unittest.main()
