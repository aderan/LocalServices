"""Locove 本地 DNS responder：只把已登记的 *.locove 名字解析到回环，其余 NXDOMAIN。

- 运行为普通用户的 launchd agent，监听 127.0.0.1:<DNS_PORT>（非特权高端口，
  绝不占 53、不改全局 DNS）；split DNS 由 /etc/resolver/<suffix> 把该后缀定向过来。
- 事实来源 = locove.routes_file()（daemon 收敛生成的单一事实），每次查询按 mtime 重读。
- 已登记名：A → 127.0.0.1；AAAA → NOERROR 空答案（NODATA，客户端回落 IPv4）；
  未登记名：NXDOMAIN。绝不为任意 hostname 回答回环地址。
- UDP + TCP（2 字节长度前缀）双协议；畸形查询回复 FORMERR，不崩溃、不打日志刷屏。
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from pathlib import Path

TYPE_A, TYPE_AAAA, TYPE_SOA, TYPE_OPT = 1, 28, 6, 41
CLASS_IN = 1
RCODE_OK, RCODE_FORMERR, RCODE_NXDOMAIN = 0, 1, 3
TTL = 5
LOOPBACK = "127.0.0.1"


class Zone:
    """已登记主机集合（读 routes.json；坏文件按空处理，不影响其他名字的回答）。"""

    def __init__(self, routes_path: Path):
        self.path = routes_path
        self._lock = threading.Lock()
        self._hosts: frozenset[str] = frozenset()
        self._mtime: float | None = None

    def refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            with self._lock:
                self._hosts, self._mtime = frozenset(), None
            return
        with self._lock:
            if mtime == self._mtime:
                return
        try:
            import json
            data = json.loads(self.path.read_text(encoding="utf-8"))
            hosts = frozenset((data.get("routes") or {}).keys())
        except (OSError, ValueError):
            hosts = frozenset()
            mtime = None
        with self._lock:
            self._hosts, self._mtime = hosts, mtime

    def contains(self, qname: str) -> bool:
        with self._lock:
            return qname in self._hosts


def _name_wire(labels: list[str]) -> bytes:
    out = b""
    for lab in labels:
        raw = lab.encode("idna") if not lab.isascii() else lab.encode("ascii")
        out += bytes([min(len(raw), 63)]) + raw
    return out + b"\x00"


def _skip_name(msg: bytes, off: int) -> int:
    while True:
        if off >= len(msg):
            raise ValueError("truncated name")
        n = msg[off]
        if n & 0xC0 == 0xC0:
            return off + 2
        off += 1
        if n == 0:
            return off
        off += n


def parse_query(msg: bytes) -> tuple[int, str, int]:
    """返回 (id, qname 小写点分, qtype)；畸形输入抛 ValueError。"""
    if len(msg) < 12:
        raise ValueError("short")
    qid = struct.unpack("!H", msg[:2])[0]
    qdcount = struct.unpack("!H", msg[4:6])[0]
    if qdcount < 1:
        raise ValueError("no question")
    off = 12
    end = _skip_name(msg, off)
    labels: list[str] = []
    i = off
    while i < end - 1:
        n = msg[i]
        labels.append(msg[i + 1:i + 1 + n].decode("ascii", "replace").lower())
        i += 1 + n
    qname = ".".join(labels)
    if end + 4 > len(msg):
        raise ValueError("truncated type")
    qtype, _qclass = struct.unpack("!HH", msg[end:end + 4])
    return qid, qname, qtype


def build_response(query: bytes, zone: Zone) -> bytes:
    """构造应答；未识别问题（EDNS OPT 等）按 FORMERR 之外的常规路径处理。"""
    qid, qname, qtype = parse_query(query)
    header_flags = 0x8180  # QR=1, AA=1, RD=1(回显), RA=0
    off = 12
    end = _skip_name(query, off)
    question = query[off:end + 4]

    registered = zone.contains(qname)
    rcode = RCODE_OK
    answer = b""
    authority = b""
    ancount = 0
    nscount = 0

    if not registered:
        rcode = RCODE_NXDOMAIN
    elif qtype == TYPE_A:
        answer = (b"\xc0\x0c" + struct.pack("!HHIH", TYPE_A, CLASS_IN, TTL, 4)
                  + socket.inet_aton(LOOPBACK))
        ancount = 1
    elif qtype == TYPE_AAAA:
        pass  # NODATA：已登记但没有 AAAA，客户端回落 A 记录
    elif qtype == TYPE_SOA:
        # 对后缀本身的 SOA 查询给出极小权威应答，避免解析器反复重试
        soa_name = qname
        mname = _name_wire([soa_name.split(".")[0] if "." in soa_name else soa_name])
        rname = _name_wire(["hostmaster", *soa_name.split(".")]) if soa_name else _name_wire(["hostmaster"])
        rdata = mname + rname + struct.pack("!IIIII", 1, 3600, 600, 86400, 60)
        answer = (b"\xc0\x0c" + struct.pack("!HHIH", TYPE_SOA, CLASS_IN, TTL, len(rdata)) + rdata)
        ancount = 1
    # 其他类型（MX/TXT/…）：已登记名按 NODATA，未登记按 NXDOMAIN（上面已定）

    flags = header_flags | rcode
    header = struct.pack("!HHHHHH", qid, flags, 1, ancount, nscount, 0)
    return header + question + answer + authority


def handle_packet(data: bytes, zone: Zone) -> bytes | None:
    zone.refresh()
    try:
        return build_response(data, zone)
    except ValueError:
        try:
            qid = struct.unpack("!H", data[:2])[0]
            return struct.pack("!HHHHHH", qid, 0x8181, 0, 0, 0, 0)  # FORMERR
        except struct.error:
            return None


def serve(port: int, routes_path: Path, host: str = "127.0.0.1") -> None:
    """阻塞式 UDP+TCP 服务（launchd agent 入口调用）。"""
    zone = Zone(routes_path)
    routes_path.parent.mkdir(parents=True, exist_ok=True)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind((host, port))
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind((host, port))
    tcp.listen(16)

    def udp_loop():
        while True:
            try:
                data, addr = udp.recvfrom(4096)
                reply = handle_packet(data, zone)
                if reply:
                    udp.sendto(reply, addr)
            except OSError:
                continue

    def tcp_client(conn: socket.socket):
        try:
            with conn:
                conn.settimeout(10)
                hdr = conn.recv(2)
                if len(hdr) < 2:
                    return
                (n,) = struct.unpack("!H", hdr)
                data = b""
                while len(data) < n:
                    chunk = conn.recv(n - len(data))
                    if not chunk:
                        return
                    data += chunk
                reply = handle_packet(data, zone)
                if reply:
                    conn.sendall(struct.pack("!H", len(reply)) + reply)
        except OSError:
            pass

    def tcp_loop():
        while True:
            try:
                conn, _ = tcp.accept()
            except OSError:
                continue
            threading.Thread(target=tcp_client, args=(conn,), daemon=True).start()

    print(f"[locove-dns] serving {host}:{port} from {routes_path}", flush=True)
    threading.Thread(target=udp_loop, daemon=True).start()
    tcp_loop()


def main() -> int:
    from . import locove, paths
    port = int(os.environ.get("LOCOVE_DNS_PORT", "5354"))
    paths.ensure()
    serve(port, locove.routes_file())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
