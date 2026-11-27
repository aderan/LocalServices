"""Locove 可选域名访问面（D2/D3）：路由事实（routes.json）、Caddy 配置生成/校验/激活/回退、分层健康。

职责边界（Goal V3.4 + D2/D3 修订）：
- DNS（locove_dns.py）只把已登记名字解析到回环；未知名 NXDOMAIN（高端口 5354）。
- 数据面由 Caddy 承载：直接监听回环业务端口（缺省 5670，普通用户可绑定，
  不经 80 转发器）；本模块生成 Caddyfile，先 `caddy validate` 再优雅 reload；
  任一步失败保留上一次有效配置（文件与运行态都不动），错误可诊断。
- 整个模块默认不安装不运行（D2）；启用/关闭/卸载流程见 locove_optin.py（D3）。
- 上游端口永远来自登记组件的实际监听（内部端口是实现细节，不进入用户可见地址）。

routes.json 是 DNS/代理/UI 共享的单一事实，由 daemon 周期收敛（sync，仅在
域名模块开启时运行）。"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from . import paths

DEFAULT_PROXY_PORT, DEFAULT_DNS_PORT, DEFAULT_ENTRY_PORT, DEFAULT_ADMIN_PORT = 5670, 5354, 80, 47891


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


def launchagents_dir() -> Path:
    """用户 LaunchAgents 目录（测试经 LOCOVE_LAUNCHAGENTS 重定向）。"""
    return Path(os.environ.get("LOCOVE_LAUNCHAGENTS",
                               str(Path.home() / "Library" / "LaunchAgents")))


def agent_plist(job: str) -> Path:
    """job ∈ {dns, proxy}；这些 plist 只由本工具的安装/启用流程写入。"""
    return launchagents_dir() / f"com.mybrain.ls.locove.{job}.plist"


LEGACY_ENTRY_PLIST = "/Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist"


def legacy_80_present() -> bool:
    """旧 80 系统转发器（V3.4 时代）是否仍登记——迁移面事实，不代表当前数据面。"""
    return Path(LEGACY_ENTRY_PLIST).exists()


def routes_file() -> Path:
    return locove_dir() / "routes.json"


def caddyfile() -> Path:
    return locove_dir() / "Caddyfile"


def suffix() -> str:
    from . import manifest
    return manifest.domain_suffix()


def effective_port(port: int | None = None, settings: dict | None = None) -> int:
    """业务代理端口（D3）的唯一裁决：显式参数 > settings.domain_port > 环境覆盖 >
    缺省 5670。所有渲染/落盘/健康检查共用，避免各调用方拼出不同端口。"""
    if port is not None:
        return int(port)
    if settings and settings.get("domain_port"):
        return int(settings["domain_port"])
    return proxy_port()


# ---------- 期望路由（登记 × 运行事实） ----------

@dataclass
class Route:
    host: str
    service: str
    component: str
    upstream_port: int | None
    state: str  # proxy | unavailable
    loopback_host: bool = False
    loopback_origin: bool = False
    upstream_host: str = "127.0.0.1"

    def to_json(self) -> dict:
        row = {"service": self.service, "component": self.component,
               "upstream_port": self.upstream_port, "state": self.state}
        if self.upstream_host != "127.0.0.1":
            row["upstream_host"] = self.upstream_host
        if self.loopback_host:
            row["loopback_host"] = True
        if self.loopback_origin:
            row["loopback_origin"] = True
        return row


def effective_endpoints(p, settings: dict | None) -> list:
    """V3.5：域名形式=custom → 只保留显式登记入口（derived 派生不算）；
    缺省 suffix → 派生+显式全部参与（向后兼容 V3.0–V3.4 行为）。"""
    eps = p.endpoints
    if (settings or {}).get("domain_form") == "custom":
        eps = [e for e in eps if not getattr(e, "derived", False)]
    return eps


def _component_states(st: dict) -> dict:
    return {c.get("id"): c for c in (st.get("components") or []) if isinstance(c, dict)}


def _vite_command(command: list[str]) -> bool:
    if not command:
        return False
    executable = Path(command[0]).name
    return executable == "vite" or (
        executable == "node" and len(command) > 1 and command[1].endswith("/vite/bin/vite.js")
    )


def _vite_component(project, component) -> bool:
    if not component or component.run != "local" or not component.cmd:
        return False
    command = component.cmd
    if _vite_command(command):
        return True
    manager = Path(command[0]).name
    if manager not in {"npm", "pnpm", "yarn"} or len(command) < 2:
        return False
    if command[1] in {"run", "run-script"}:
        if len(command) < 3:
            return False
        script = command[2]
    elif manager != "npm" or command[1] == "start":
        script = command[1]
    else:
        return False
    try:
        package = json.loads((project.comp_cwd(component) / "package.json").read_text())
        scripts = package.get("scripts") if isinstance(package, dict) else None
        value = scripts.get(script) if isinstance(scripts, dict) else None
        return isinstance(value, str) and _vite_command(shlex.split(value))
    except (OSError, ValueError):
        return False


def desired_routes(projects: dict, snapshot: dict | None = None,
                   settings: dict | None = None) -> dict:
    """由清单 + 共享快照推导期望路由。快照缺失时按 unavailable 收敛（不猜测运行态）。

    逐 endpoint 判定（review R2）：服务总体状态（如 partial）不代言单个入口——
    只看该 endpoint 目标组件的事实：外部实例有已验证归属的实际端口 → 转发实际端口；
    组件自身 ready → 转发其端口；其余（stopped/starting/unhealthy/快照缺组件事实）→
    unavailable（503），绝不把流量转发给已停止组件的端口——它可能已被无关程序占用。
    """
    snap_projects = {p.get("id"): p for p in (snapshot or {}).get("projects", [])}
    loopback_endpoints = (settings or {}).get("loopback_endpoints", {})
    routes: dict[str, Route] = {}
    for pid, p in projects.items():
        st = snap_projects.get(pid, {})
        comp_states = _component_states(st)
        ext_ports = st.get("external_ports") or {}
        for e in effective_endpoints(p, settings):
            comp = p.component_by_id(e.component)
            port = ext_ports.get(e.component) or (comp.port if comp else None)
            ext_port = ext_ports.get(e.component)
            cs = comp_states.get(e.component)
            upstream = cs.get("upstream_host", "127.0.0.1") if cs else "127.0.0.1"
            if ext_port:
                # 外部实例：快照 external_ports 是「组件 -> 实际监听端口」，仅经归属+就绪
                # 验证（Controller._adoptable）后才会出现；用实际端口作上游
                state, port = "proxy", ext_port
            else:
                if cs is not None and cs.get("state") == "ready" and cs.get("running", True):
                    port = cs.get("port") or (comp.port if comp else None)
                    state = "proxy" if port else "unavailable"
                else:
                    state = "unavailable"
            if upstream not in ("127.0.0.1", "::1"):
                state = "unavailable"
            loopback_origin = state == "proxy" and loopback_endpoints.get(e.host) == {
                "service": pid, "component": e.component}
            routes[e.host] = Route(host=e.host, service=pid, component=e.component,
                                   upstream_port=port, state=state, upstream_host=upstream or "127.0.0.1",
                                   loopback_host=loopback_origin or (state == "proxy" and _vite_component(p, comp)),
                                   loopback_origin=loopback_origin)
    return routes


def routes_payload(routes: dict[str, Route], port: int | None = None,
                   settings: dict | None = None) -> dict:
    p = effective_port(port, settings)
    return {"schemaVersion": 1, "suffix": suffix(), "generated_at": time.time(),
            "proxy_port": p, "domain_port": p,
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

_FORBIDDEN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>访问被拒绝 · Locove</title>
<style>:root{{color-scheme:light dark}}body{{font:16px system-ui,sans-serif;margin:0;padding:clamp(24px,8vw,96px)}}
main{{max-width:560px}}h1{{font-size:1.5em}}p{{line-height:1.6}}a{{display:inline-block;margin-top:.5em;
padding:.55em .9em;border-radius:6px;background:#1656ca;color:white;text-decoration:none}}</style></head>
<body><main><h1>应用返回 403</h1><p><strong>{host}</strong></p>
<p>如果直连地址可以打开，应用可能只接受本机地址。你可以在 Locove 的入口设置中检查并开启兼容。</p>
<a href="{link}">入口设置</a></main></body></html>"""


def _forbidden_response(route: Route) -> list[str]:
    query = urlencode({"service": route.service, "host": route.host})
    link = f"http://127.0.0.1:{paths.daemon_port()}/endpoint-settings?{query}#p-{route.service}"
    body = _FORBIDDEN_PAGE.format(host=escape(route.host), link=escape(link))
    return [
        "\t\t@entry_forbidden status 403",
        "\t\thandle_response @entry_forbidden {",
        "\t\t\troute {",
        "\t\t\t\theader @entry_navigation {",
        '\t\t\t\t\tContent-Type "text/html; charset=utf-8"',
        "\t\t\t\t\tCache-Control no-store",
        "\t\t\t\t\tX-Frame-Options DENY",
        '\t\t\t\t\tContent-Security-Policy "default-src \'none\'; style-src \'unsafe-inline\'; frame-ancestors \'none\'"',
        "\t\t\t\t}",
        f"\t\t\t\trespond @entry_navigation `{body}` 403",
        "\t\t\t\tcopy_response",
        "\t\t\t}",
        "\t\t}",
    ]


def render_caddyfile(routes: dict[str, Route], port: int | None = None,
                     settings: dict | None = None) -> str:
    """按精确 Host 生成站点；无匹配 Host 落到 catch-all 404（不代理任何未知主机）。

    所有站点与 catch-all 经全局 default_bind 只监听回环（review R1）——admin 的
    127.0.0.1 不约束业务 listener，必须显式绑定；否则数据面会开在所有网卡上。
    业务端口（缺省 5670）由 effective_port 统一裁决。"""
    public_port = effective_port(port, settings)
    parts = [
        "{",
        f"\tadmin 127.0.0.1:{admin_port()}",
        "\tauto_https off",
        f"\thttp_port {public_port}",
        "\tdefault_bind 127.0.0.1 ::1",
        "\tgrace_period 3s",
        "}",
    ]
    for host, r in sorted(routes.items()):
        parts.append(f'\nhttp://{host} {{')
        if r.state == "proxy" and r.upstream_port:
            authority_host = "[::1]" if r.upstream_host == "::1" else "127.0.0.1"
            upstream = f"{authority_host}:{r.upstream_port}"
            indent = "\t"
            if r.loopback_origin:
                authority = host if public_port == 80 else f"{host}:{public_port}"
                origin = f'http://{authority}'
                # Validate the public origin before translating it for a loopback-only dashboard.
                parts.extend([
                    f'\t@public_origin header Origin "{origin}"',
                    "\t@foreign_origin {",
                    "\t\theader Origin *",
                    f'\t\tnot header Origin "{origin}"',
                    "\t}",
                    "\t@write_without_origin {",
                    "\t\tnot method GET HEAD OPTIONS",
                    f'\t\tnot header Origin "{origin}"',
                    "\t}",
                    "\t@cross_site_fetch {",
                    "\t\theader Sec-Fetch-Site *-site",
                    "\t\tnot header Sec-Fetch-Mode navigate",
                    "\t}",
                    "\troute {",
                    '\t\trespond @foreign_origin "Forbidden origin" 403',
                    '\t\trespond @write_without_origin "Origin required" 403',
                    '\t\trespond @cross_site_fetch "Forbidden cross-site request" 403',
                    f"\t\trequest_header @public_origin Origin http://{upstream}",
                ])
                indent = "\t\t"
            else:
                parts.extend([
                    "\t@entry_navigation {",
                    "\t\tmethod GET",
                    "\t\theader Accept *text/html*",
                    "\t\tnot header Origin *",
                    '\t\texpression `{http.request.header.Sec-Fetch-Mode} in ["", "navigate"] '
                    '&& {http.request.header.Sec-Fetch-Dest} in ["", "document"]`',
                    "\t}",
                ])
            parts.append(f"{indent}reverse_proxy {upstream} {{")
            if r.loopback_host:
                # Caddy has already matched the public Host and forwards it as X-Forwarded-Host.
                parts.append(f"{indent}\theader_up Host {{upstream_hostport}}")
            parts.append(f"{indent}\tflush_interval -1")
            if not r.loopback_origin:
                parts.extend(_forbidden_response(r))
            parts.append(f"{indent}}}")
            if r.loopback_origin:
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


def _caddy_control_enabled() -> bool:
    """隔离守卫：LOCALSERVICES_HOME 被重定向（测试/验收脚本的隔离 daemon）且未显式
    指定 admin 端口时，禁止一切对 Caddy 二进制的调用——隔离进程只能写自己 home 下的
    配置文件，绝不 reload 任何运行中的实例（防止测试配置污染生产代理）。"""
    if "LOCOVE_ADMIN_PORT" in os.environ:
        return True
    return not os.environ.get("LOCALSERVICES_HOME")


def apply_routes(routes: dict[str, Route], port: int | None = None,
                 settings: dict | None = None) -> dict:
    """把期望路由落为运行态。顺序：生成 → validate → 写 routes.json/Caddyfile → reload；
    validate 失败不动任何文件；reload 失败回滚两个文件到旧内容（运行中的 Caddy 从未
    接受坏配置，保持上次有效路由）。Caddy 尚未运行（admin 端口无响应）时只落盘
    （pending 激活，代理由 launchd 守护，起来后读到落盘配置即生效）。"""
    locove_dir().mkdir(parents=True, exist_ok=True)
    cfg = render_caddyfile(routes, port, settings)
    payload = routes_payload(routes, port, settings)
    old_routes = read_routes()
    old_cfg = None
    try:
        old_cfg = caddyfile().read_text(encoding="utf-8")
    except OSError:
        pass
    if _caddy_control_enabled():
        # 校验通过才允许替换任何文件（坏配置零写入）；隔离守卫路径只落盘自己的 home
        try:
            _validate(cfg)
        except ActivationError as e:
            return {"ok": False, "changed": False, "error": str(e)}
    routes_file().write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    caddyfile().write_text(cfg, encoding="utf-8")
    if not _caddy_control_enabled():
        return {"ok": True, "changed": True, "hosts": len(routes), "pending": True, "isolated": True}
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


_last_applied = 0.0
_FORCE_REAPPLY = float(os.environ.get("LOCOVE_FORCE_REAPPLY", "60"))


def gated_sync(settings: dict | None, projects: dict, snapshot: dict | None) -> dict | None:
    """daemon 周期入口（D2）：仅在用户开启域名时收敛路由；未开启（默认/未安装）时
    不运行任何网络收敛、不写任何 locove 文件，返回 None。"""
    if not (settings or {}).get("use_domains"):
        return None
    return sync(projects, snapshot, settings=settings)


def sync(projects: dict, snapshot: dict | None, settings: dict | None = None,
         port: int | None = None) -> dict:
    """daemon 周期调用（仅域名模块开启时，D2）：期望 == 已落盘 → 无操作；否则走
    apply_routes（端口经 effective_port 统一裁决）。

    另外每 _FORCE_REAPPLY 秒强制重激活一次：运行中的 Caddy 若被外部配置覆盖
    （漂移），落盘事实没变也照样收敛回来——重启/漂移后自动重建一致配置。"""
    global _last_applied
    routes = desired_routes(projects, snapshot, settings)
    current = (read_routes() or {}).get("routes")
    want = routes_payload(routes, port, settings)["routes"]
    now = time.time()
    if current == want and now - _last_applied < _FORCE_REAPPLY:
        return {"ok": True, "changed": False, "hosts": len(routes)}
    res = apply_routes(routes, port, settings)
    if res.get("ok"):
        _last_applied = now
    return res


# ---------- 分层健康（DNS / 入口 / 代理 / 应用各自观测） ----------

def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


SCUTIL = "/usr/sbin/scutil"  # 注意：scutil 在 sbin，不在 bin（写错会静默变成假绿）


def _scutil_proxy() -> str:
    try:
        r = subprocess.run([SCUTIL, "--proxy"], capture_output=True,
                           text=True, timeout=3)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — 观测失败按未知处理，不炸健康接口
        return ""


def proxy_bypass_status() -> dict:
    """Observe the effective system proxy settings for local HTTP URLs."""
    text = _scutil_proxy()
    if not text.lstrip().startswith("<dictionary>"):
        return {"ok": True, "enabled": None, "detail": "系统代理状态未知（scutil 不可读）"}
    sfx = suffix()
    if re.search(r"^  ProxyAuto(?:Config|Discovery)Enable : 1$", text, re.M):
        return {"ok": False, "enabled": True, "automatic": True,
                "detail": f"系统使用自动代理配置，请在代理客户端中配置 .{sfx} 直连"}
    enabled = re.search(r"^  (?:HTTP|SOCKS)Enable : 1$", text, re.M) is not None
    exceptions = re.search(r"^  ExceptionsList : <array> \{\n(.*?)^  \}", text, re.M | re.S)
    entries = re.findall(r"^    \d+ : (.+)$", exceptions[1], re.M) if exceptions else []
    if not enabled:
        return {"ok": True, "enabled": False, "detail": "系统代理未启用（无需绕行）"}
    if f"*.{sfx}" in entries:
        return {"ok": True, "enabled": True, "detail": f"*.{sfx} 已在系统代理绕行表"}
    return {"ok": False, "enabled": True,
            "entries": entries,
            "detail": f"系统代理绕行表缺 *.{sfx}。在工作台设置中点击「添加系统代理绕过」，"
                      "或运行 lsctl locove-bypass。代理客户端重新应用配置时可能覆盖此规则。"}


# ---------- 可选域名模块状态机（D2/D3：默认不安装；启用才运行） ----------

def resolver_file() -> Path:
    """生产为 /etc/resolver/<suffix>；测试经 LOCOVE_RESOLVER_DIR 重定向。"""
    return Path(os.environ.get("LOCOVE_RESOLVER_DIR", "/etc/resolver")) / suffix()


def _resolver_state(path: Path) -> str:
    """absent（未安装）/ ours / foreign / unreadable——与 locove_resolver 的归属判定一致。"""
    if not path.exists():
        return "absent"
    from .locove_resolver import owned
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unreadable"
    return "ours" if owned(content) else "foreign"


# 启用后端口/依赖就绪的宽限期：期间未就绪报「启动中」而非「失败」
ENABLE_GRACE = float(os.environ.get("LOCOVE_ENABLE_GRACE", "45"))

MODULE_STATES = ("not_installed", "installed_off", "starting", "available", "failed")


def module_status(settings: dict | None = None) -> dict:
    """可选域名模块的分层观测（纯只读，不猜运行态）。

    state：not_installed（用户级件与 resolver 均未安装）/ installed_off（有安装件
    但 use_domains=false——直连）/ starting（刚启用，宽限期内）/ available（DNS、
    代理、resolver 全就绪）/ failed（启用且过宽限期仍缺层）。一层失败不掩盖其他层；
    旧 80 转发器以 legacy_80_present 单独报告，不参与新数据面健康。"""
    settings = settings or {}
    port = effective_port(settings=settings)
    enabled = bool(settings.get("use_domains"))
    installed = {
        "dns_agent": agent_plist("dns").exists(),
        "proxy_agent": agent_plist("proxy").exists(),
        "resolver": _resolver_state(resolver_file()) == "ours",
    }
    resolver_state = _resolver_state(resolver_file())
    dns_ok = _port_open("127.0.0.1", dns_port())
    proxy_ok = _port_open("127.0.0.1", port)
    data = read_routes()

    if not any(installed.values()) and resolver_state == "absent":
        state = "not_installed"
    elif not enabled:
        state = "installed_off"
    elif dns_ok and proxy_ok and installed["resolver"]:
        state = "available"
    else:
        enabled_at = float(settings.get("domains_enabled_at") or 0)
        state = "starting" if (enabled_at and time.time() - enabled_at < ENABLE_GRACE) else "failed"

    if state == "not_installed":
        dns_detail = proxy_detail = "域名模块未安装（可选功能，默认直连不受影响）"
    elif not dns_ok:
        dns_detail = "本地 DNS 未运行（launchd agent com.mybrain.ls.locove.dns）"
    else:
        dns_detail = "正常"
    if state == "not_installed":
        proxy_detail = "域名模块未安装（可选功能，默认直连不受影响）"
    elif not proxy_ok:
        proxy_detail = f"Caddy 未监听 127.0.0.1:{port}（agent 未启动或端口冲突）"
    else:
        proxy_detail = f"正常（127.0.0.1:{port}）"
    if resolver_state == "ours":
        resolver_detail = "正常（仅 .{s} 定向到本地 DNS）".format(s=suffix())
    elif resolver_state == "foreign":
        resolver_detail = f"/etc/resolver/{suffix()} 属于他人配置，本工具不接管；启用将失败"
    elif resolver_state == "unreadable":
        resolver_detail = f"/etc/resolver/{suffix()} 不可读，无法确认归属"
    elif enabled:
        resolver_detail = f"/etc/resolver/{suffix()} 未安装（启用流程会请求一次管理员授权）"
    else:
        resolver_detail = "未安装（启用时经管理员当次授权安装）"

    return {
        "suffix": suffix(),
        "domain_port": port,
        "state": state,
        "enabled": enabled,
        "installed": installed,
        "resolver_state": resolver_state,
        "proxy_bypass": proxy_bypass_status(),
        "dns": {"ok": dns_ok if state != "not_installed" else None, "detail": dns_detail},
        "proxy": {"ok": proxy_ok if state != "not_installed" else None, "detail": proxy_detail},
        "resolver_installed": installed["resolver"],
        "resolver": {"ok": installed["resolver"] if state != "not_installed" else None,
                     "detail": resolver_detail},
        "legacy_80": legacy_80_present(),
        "hosts": len((data or {}).get("routes", {})),
        "routes": (data or {}).get("routes", {}),
        "caddy_available": shutil.which(caddy_bin()) is not None or Path(caddy_bin()).exists(),
    }


# 向后兼容旧名（V3.4 时代的 80 入口分层已由 module_status 的 legacy_80/代理层取代）
def entry_status(settings: dict | None = None) -> dict:
    return module_status(settings)
