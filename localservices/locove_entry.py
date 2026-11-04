"""Locove 80 入口转发器（最小特权）：只做 127.0.0.1:80 → 127.0.0.1:<proxy_port> 的 TCP 字节转发。

- 以 root LaunchDaemon 运行（唯一需要低端口权限的组件）；绑定成功后立即降权到 nobody。
- 无配置文件、无 shell、无动态上游：目标端口编译期缺省 + 只读环境变量（安装时固定）。
- 只监听回环；局域网不可见。代理未就绪时连接立即关闭（浏览器显示连接被拒，不挂起）。
"""

from __future__ import annotations

import os
import socket
import threading

BUF = 65536
IDLE_TIMEOUT = 300.0


def drop_privileges() -> None:
    """绑定 80 后降权；降权失败必须退出（继续以 root 跑违背最小特权承诺）。"""
    if os.geteuid() != 0:
        return
    try:
        os.setgroups([])
        os.setgid(4294967294 - 256)  # nobody gid (-2)
        os.setuid(4294967294 - 256)  # nobody uid (-2)
    except OSError as e:
        raise SystemExit(f"[locove-entry] privilege drop failed, refusing to run as root: {e}")


def pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(BUF)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            a.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, target: tuple[str, int]) -> None:
    try:
        with client:
            client.settimeout(IDLE_TIMEOUT)
            try:
                upstream = socket.create_connection(target, timeout=10.0)
            except OSError:
                # 上游不可达：排空并半关闭后再让 with 收尾——带着未读数据直接 close
                # 会发 RST，浏览器把它当成异常断连而不是干净的"服务未运行"
                try:
                    client.shutdown(socket.SHUT_WR)
                    client.settimeout(0.5)
                    while client.recv(4096):
                        pass
                except OSError:
                    pass
                return
            upstream.settimeout(IDLE_TIMEOUT)
            with upstream:
                t1 = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
                t2 = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
                t1.start()
                t2.start()
                t1.join()
                t2.join()
    except OSError:
        pass


def make_listener(listen_port: int, host: str = "127.0.0.1") -> socket.socket:
    """root 阶段绑定低端口（绑定成功后才允许降权）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, listen_port))
    srv.listen(64)
    return srv


def serve(listen_port: int, target_port: int, host: str = "127.0.0.1",
          listener: socket.socket | None = None) -> None:
    """listener：root 预绑定的监听套接字（LaunchDaemon 路径）；缺省自行绑定（测试/普通端口）。"""
    srv = listener or make_listener(listen_port, host)
    target = ("127.0.0.1", target_port)
    print(f"[locove-entry] {host}:{listen_port} -> 127.0.0.1:{target_port}"
          + (" (pre-bound as root, serving unprivileged)" if listener else ""), flush=True)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(conn, target), daemon=True).start()


def main() -> int:
    listen = int(os.environ.get("LOCOVE_ENTRY_PORT", "80"))
    target = int(os.environ.get("LOCOVE_ENTRY_TARGET", "47890"))
    listener = None
    if os.geteuid() == 0:
        # 顺序必须是：root 绑定低端口 → 降权 → 用已绑定套接字服务。
        # 先降权再绑定会 EACCES（LaunchDaemon 崩溃循环的真实教训）。
        listener = make_listener(listen)
        drop_privileges()
    serve(listen, target, listener=listener)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
