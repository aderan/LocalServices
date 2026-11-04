"""Locove 无端口访问面：路由事实（routes.json）、Caddy 配置生成/校验/激活/回退、分层健康。

职责边界（Goal V3.4）：
- DNS（locove_dns.py）只把已登记名字解析到回环；未知名 NXDOMAIN。
- 入口转发（locove_entry.py，root）只做 127.0.0.1:80 → 127.0.0.1:<proxy_port> 的字节转发。
- 数据面由 Caddy 承载：本模块生成 Caddyfile，先 `caddy validate` 再优雅 reload；
  任一步失败保留上一次有效配置（文件与运行态都不动），错误可诊断。
- 上游端口永远来自登记组件的实际监听（内部端口是实现细节，不进入用户可见地址）。

routes.json 是 DNS/代理/UI 共享的单一事实，由 daemon 周期收敛（sync）。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

DEFAULT_PROXY_PORT, DEFAULT_DNS_PORT, DEFAULT_ENTRY_PORT, DEFAULT_ADMIN_PORT = 47890, 5354, 80, 47891


def _env_port(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def proxy_port() -> int:
    return _env_port("LOCOVE_PROXY_PORT", DEFAULT_PROXY_PORT)


def dns_port() -> int:
    return _env_port("LOCOVE_DNS_PORT", DEFAULT_DNS_PORT)


def entry_port() -> int:
    return _env_port("LOCOVE_ENTRY_PORT", DEFAULT_ENTRY_PORT)


def admin_port() -> int:
    return _env_port("LOCOVE_ADMIN_PORT", DEFAULT_ADMIN_PORT)

PROXY_STATES = ("proxy", "unavailable")  # 路由模式：转发上游 / 明确不可用页


def caddy_bin() -> str:
    return os.environ.get("LOCOVE_CADDY_BIN", "/opt/homebrew/bin/caddy")


def locove_dir() -> Path:
    return paths.home() / "locove"


def routes_file() -> Path:
    return locove_dir() / "routes.json"


def caddyfile() -> Path:
    return locove_dir() / "Caddyfile"


def suffix() -> str:
    from . import manifest
    return manifest.domain_suffix()


# ---------- 期望路由（登记 × 运行事实） ----------

@dataclass
class Route:
    host: str
    service: str
    component: str
    upstream_port: int | None
    state: str  # proxy | unavailable

    def to_json(self) -> dict:
        return {"service": self.service, "component": self.component,
                "upstream_port": self.upstream_port, "state": self.state}


def desired_routes(projects: dict, snapshot: dict | None = None) -> dict:
    """由清单 + 共享快照推导期望路由。快照缺失时按 unavailable 收敛（不猜测运行态）。

    外部实例：快照 external_ports 已是「组件 -> 实际监听端口」（V2/V3 接线，见
    Controller.project_status），就绪的外部实例用实际端口作上游；未就绪/未知 → unavailable。
    """
    snap_projects = {p.get("id"): p for p in (snapshot or {}).get("projects", [])}
    routes: dict[str, Route] = {}
    for pid, p in projects.items():
        st = snap_projects.get(pid, {})
        healthy = st.get("state") in ("available", "partial", "external")
        ext_ports = st.get("external_ports") or {}
        for e in p.endpoints:
            comp = p.component_by_id(e.component)
            port = ext_ports.get(e.component) or (comp.port if comp else None)
            state = "proxy" if (healthy and port) else "unavailable"
            routes[e.host] = Route(host=e.host, service=pid, component=e.component,
                                   upstream_port=port, state=state)
    return routes


def routes_payload(routes: dict[str, Route]) -> dict:
    return {"schemaVersion": 1, "suffix": suffix(), "generated_at": time.time(),
            "proxy_port": proxy_port(),
            "routes": {h: r.to_json() for h, r in sorted(routes.items())}}


def read_routes() -> dict | None:
    try:
        data = json.loads(routes_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ---------- Caddyfile ----------

_UNAVAILABLE_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>服务未运行 · Locove</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;background:#1c1c1e;color:#e8e8ea;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
div{{text-align:center}}h1{{font-size:17px;font-weight:600}}p{{color:#98989d;font-size:13px}}</style></head>
<body><div><h1>{host} 未运行</h1><p>服务已停止或未就绪。在 Locove／洛湾 工作台启动后即可访问。</p></div></body></html>"""


def render_caddyfile(routes: dict[str, Route]) -> str:
    """按精确 Host 生成站点；无匹配 Host 落到 catch-all 404（不代理任何未知主机）。"""
    parts = [
        "{",
        f"\tadmin 127.0.0.1:{admin_port()}",
        "\tauto_https off",
        f"\thttp_port {proxy_port()}",
        "\tgrace_period 3s",
        "}",
    ]
    for host, r in sorted(routes.items()):
        parts.append(f'\nhttp://{host} {{')
        if r.state == "proxy" and r.upstream_port:
            parts.append(f"\treverse_proxy 127.0.0.1:{r.upstream_port} {{")
            parts.append("\t\tflush_interval -1")
            parts.append("\t}")
        else:
            body = _UNAVAILABLE_PAGE.format(host=host)
            parts.append(f"\trespond `{body}` 503 {{")
            parts.append("\t\tclose")
            parts.append("\t}")
        parts.append("}")
    parts.append('\nhttp:// {')
    parts.append('\trespond "unknown host (Locove)" 404 {')
    parts.append("\t\tclose")
    parts.append("\t}")
    parts.append("}")
    return "\n".join(parts) + "\n"


# ---------- 激活（校验 → 写盘 → reload；失败保持上次有效状态） ----------

class ActivationError(Exception):
    pass


def _run(cmd: list[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _validate(cfg_text: str) -> None:
    """caddy validate 通过才允许替换当前配置。"""
    tmp = locove_dir() / f".validate.{os.getpid()}.caddyfile"
    try:
        tmp.write_text(cfg_text, encoding="utf-8")
        r = _run([caddy_bin(), "validate", "--config", str(tmp)])
        if r.returncode != 0:
            raise ActivationError(f"caddy validate 失败：{(r.stderr or r.stdout).strip()[:600]}")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _reload() -> None:
    r = _run([caddy_bin(), "reload", "--config", str(caddyfile())])
    if r.returncode != 0:
        raise ActivationError(f"caddy reload 失败：{(r.stderr or r.stdout).strip()[:600]}")


def apply_routes(routes: dict[str, Route]) -> dict:
    """把期望路由落为运行态。顺序：生成 → validate → 写 routes.json/Caddyfile → reload；
    validate 失败不动任何文件；reload 失败回滚两个文件到旧内容（运行中的 Caddy 从未
    接受坏配置，保持上次有效路由）。Caddy 尚未运行（admin 端口无响应）时只落盘
    （pending 激活，代理由 launchd 守护，起来后读到落盘配置即生效）。"""
    locove_dir().mkdir(parents=True, exist_ok=True)
    cfg = render_caddyfile(routes)
    payload = routes_payload(routes)
    old_routes = read_routes()
    old_cfg = None
    try:
        old_cfg = caddyfile().read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        _validate(cfg)
    except ActivationError as e:
        return {"ok": False, "changed": False, "error": str(e)}
    routes_file().write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    caddyfile().write_text(cfg, encoding="utf-8")
    if not _port_open("127.0.0.1", admin_port()):
        return {"ok": True, "changed": True, "hosts": len(routes), "pending": True}
    try:
        _reload()
    except ActivationError as e:
        # 回滚文件（运行态本来就没变）；下次 sync 在代理恢复后重试
        if old_routes is not None:
            routes_file().write_text(json.dumps(old_routes, ensure_ascii=False, indent=1), encoding="utf-8")
        elif old_cfg is None and not routes:
            routes_file().unlink(missing_ok=True)
        if old_cfg is not None:
            caddyfile().write_text(old_cfg, encoding="utf-8")
        elif old_cfg is None and not routes:
            caddyfile().unlink(missing_ok=True)
        return {"ok": False, "changed": True, "error": str(e)}
    return {"ok": True, "changed": True, "hosts": len(routes)}


def sync(projects: dict, snapshot: dict | None) -> dict:
    """daemon 周期调用：期望 == 已落盘 → 无操作；否则走 apply_routes。"""
    routes = desired_routes(projects, snapshot)
    current = (read_routes() or {}).get("routes")
    want = routes_payload(routes)["routes"]
    if current == want:
        return {"ok": True, "changed": False, "hosts": len(routes)}
    return apply_routes(routes)


# ---------- 分层健康（DNS / 入口 / 代理 / 应用各自观测） ----------

def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def entry_status() -> dict:
    """四层健康各报各的：dns=名字服务在答，proxy=Caddy 在监听，entry=80 入口在听，
    config=激活态与文件一致。任何一层失败都给出面向用户的原因，不互相掩盖。"""
    dns_ok = _port_open("127.0.0.1", dns_port())
    proxy_ok = _port_open("127.0.0.1", proxy_port())
    entry_ok = _port_open("127.0.0.1", entry_port())
    installed_resolver = Path("/etc/resolver", suffix()).exists()
    data = read_routes()
    if entry_ok and not proxy_ok:
        entry_detail = "80 入口在监听，但代理端口无响应（Caddy 未运行？）"
    elif entry_ok:
        entry_detail = "正常"
    else:
        entry_detail = ("80 未监听：系统转发器未安装或未运行（需要一次管理员授权安装，"
                        "见 scripts/install-locove-system.sh）")
    if not dns_ok:
        dns_detail = "本地 DNS 未运行（launchd agent com.mybrain.ls.locove.dns）"
    elif not installed_resolver:
        dns_detail = f"DNS 在答，但 /etc/resolver/{suffix()} 未安装（需要管理员授权）"
    else:
        dns_detail = "正常"
    return {
        "suffix": suffix(),
        "dns": {"ok": dns_ok and installed_resolver, "detail": dns_detail},
        "entry": {"ok": entry_ok and proxy_ok, "detail": entry_detail},
        "proxy": {"ok": proxy_ok, "detail": "正常" if proxy_ok else "Caddy 未运行（launchd agent com.mybrain.ls.locove.proxy）"},
        "resolver_installed": installed_resolver,
        "hosts": len((data or {}).get("routes", {})),
        "routes": (data or {}).get("routes", {}),
        "caddy_available": shutil.which(caddy_bin()) is not None or Path(caddy_bin()).exists(),
    }
