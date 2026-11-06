"""可选域名模块的按需启用引擎（D3，2026-11-06）。

流程（enable）：预检（只读）→ 用户级件安装/更新（无需授权，launchd agent 属当前
用户）→ 本地验证 →（resolver 未安装时）经 osascript 管理员**当次**授权安装仅
`.locove` 的 split-DNS resolver → 端到端验证 → 全部通过才写 use_domains=true。

- 取消授权：回滚本次新装的用户级件，系统零残留，继续直连（use_domains 不变）。
- 失败：报告具体原因（端口被占/caddy 缺失/resolver 他人所有/验证失败），不保存
  启用成功；既有安装件保持可诊断。
- 绝不保存管理员密码或长期权限：osascript 每次弹窗，用户可以拒绝。
- 系统件只经 scripts/install-locove-system.sh --resolver / uninstall 脚本触碰，
  且只处理带本工具管理标记的文件（locove_resolver 归属守卫，S2/S3）。

disable：关设置 + 停止 DNS/代理 agent（plists 与 resolver 保留，状态如实展示）。
uninstall：经授权只删确认归属的配置；业务进程、数据、登记一律不动。"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import time
from pathlib import Path

from . import locove
from .locove_resolver import owned

REPO = Path(__file__).resolve().parent.parent

# 进度面（GET /api/locove 合并返回；enable 在后台线程执行）。
# ok 三态：None=进行中/未知，True=操作成功，False=失败（error 给出原因）。
_progress: dict = {"active": False, "op": None, "step": None, "steps": [],
                   "at": 0.0, "error": None, "ok": None, "finished_at": None}


def progress() -> dict:
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in _progress.items()}


def _step(name: str, detail: str = "") -> None:
    _progress.update({"step": name, "at": time.time()})
    _progress["steps"].append({"step": name, "detail": detail, "at": time.time()})


def _run(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(REPO))


# ---------- 管理员当次授权（标准 macOS 机制；不保存凭据） ----------

def _applescript_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def admin_run(script_args: list[str], prompt: str, timeout: float = 600.0) -> dict:
    """经 osascript 弹出系统管理员授权执行一次脚本。用户取消 → reason=cancelled。"""
    cmd = " ".join(shlex.quote(a) for a in script_args)
    apple = (f'do shell script "{_applescript_quote(cmd)}" '
             f'with administrator privileges with prompt "{_applescript_quote(prompt)}"')
    try:
        r = _run(["/usr/bin/osascript", "-e", apple], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "error": "管理员授权等待超时"}
    if r.returncode == 0:
        return {"ok": True, "stdout": r.stdout}
    err = (r.stderr or r.stdout or "").strip()
    if "User canceled" in err or "-128" in err or "用户取消" in err:
        return {"ok": False, "reason": "cancelled", "error": err}
    return {"ok": False, "reason": "auth-failed", "error": err[:600]}


# ---------- 预检（零副作用） ----------

def _listener_name(port: int) -> str | None:
    """目标端口上的监听进程名（lsof 只读）；失败/无监听 → None。"""
    try:
        r = _run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=8)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[0]:
                return parts[0]
    except Exception:  # noqa: BLE001 — 观测失败按未知处理
        pass
    return None


def preflight(port: int | None = None) -> dict:
    """启用前只读检查：端口、caddy、resolver 归属、既有安装件、旧 80 件。"""
    from .registry import Registry
    settings = Registry().locove_settings()
    port = int(port or settings.get("domain_port") or locove.proxy_port())
    if not Registry.locove_port_valid(port):
        return {"ok": False, "port": port, "reason": "invalid-port",
                "error": f"端口 {port} 非法：需 1024–65535 且不与控制面端口冲突"
                         f"（{sorted(Registry.LOCOVE_PORT_FORBIDDEN)}）"}
    checks: dict = {"port": port}
    busy = locove._port_open("127.0.0.1", port)
    listener = _listener_name(port) if busy else None
    # 我们的 Caddy agent 在目标端口上（重复启用/端口未变）不算冲突；
    # 其他任何监听者都按冲突报告，绝不抢占未知进程的端口。
    checks["port_free"] = (not busy) or listener == "caddy"
    checks["port_listener"] = listener
    checks["caddy"] = locove.module_status(settings).get("caddy_available", False)
    checks["resolver_state"] = locove._resolver_state(locove.resolver_file())
    checks["installed"] = {k: locove.agent_plist(k.replace("_agent", "")).exists()
                           for k in ("dns_agent", "proxy_agent")}
    checks["legacy_80"] = locove.legacy_80_present()
    if not checks["port_free"]:
        return {"ok": False, **checks, "reason": "port-conflict",
                "error": f"端口 {port} 已被进程 {listener!r} 监听；请在设置中更换域名端口"}
    if not checks["caddy"]:
        return {"ok": False, **checks, "reason": "caddy-missing",
                "error": "未找到 caddy（数据面代理）。安装：brew install caddy"}
    if checks["resolver_state"] in ("foreign", "unreadable"):
        return {"ok": False, **checks, "reason": f"resolver-{checks['resolver_state']}",
                "error": f"/etc/resolver/{locove.suffix()} 不属于本工具（或不可读），"
                         "拒绝接管他人配置；请人工核对后重试"}
    return {"ok": True, **checks}


# ---------- 验证 ----------

def _wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if locove._port_open("127.0.0.1", port):
            return True
        time.sleep(0.4)
    return False


def _resolves(host: str, timeout: float = 5.0) -> bool:
    """系统解析（走 /etc/resolver split DNS）能否把已登记名字解到回环。"""
    import threading
    result: dict = {}

    def probe():
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            result["ok"] = any(i[4][0] == "127.0.0.1" for i in infos)
        except OSError:
            result["ok"] = False

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout)
    return bool(result.get("ok"))


def _probe_host() -> str | None:
    routes = (locove.read_routes() or {}).get("routes") or {}
    hosts = sorted(routes)
    return hosts[0] if hosts else None


# ---------- enable / disable / uninstall ----------

def enable(port: int | None = None) -> dict:
    """按需启用。成功条件：用户级件运行 + resolver 就绪 + 系统解析 + 代理监听。"""
    if _progress.get("active"):
        return {"ok": False, "reason": "busy", "error": "域名模块操作进行中，请稍候"}
    _progress.update({"active": True, "op": "enable", "step": None, "steps": [],
                      "error": None, "ok": None, "finished_at": None})
    try:
        res = _enable_inner(port)
        _progress["ok"] = bool(res.get("ok"))
        return res
    except Exception as e:  # noqa: BLE001 — 任何意外都如实报告；设置只在成功末尾写入，
        # 失败路径不改写（此前若已启用则保持原设置，状态机如实呈现 failed/available）。
        # str(e) 可能为空（如 StopIteration()）——补异常类型，绝不把失败伪装成无错
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        _progress["error"] = detail
        _progress["ok"] = False
        return {"ok": False, "reason": "error", "error": detail, "progress": progress()}
    finally:
        _progress.update({"active": False, "finished_at": time.time()})


def _enable_inner(port: int | None) -> dict:
    from .registry import Registry
    _step("preflight")
    pre = preflight(port)
    if not pre.get("ok"):
        _progress["error"] = pre.get("error", "")
        return {"ok": False, "reason": pre.get("reason"), "error": pre.get("error"),
                "preflight": pre, "progress": progress()}
    port = pre["port"]
    fresh_install = not (pre["installed"]["dns_agent"] or pre["installed"]["proxy_agent"])

    _step("install-agents", f"端口 {port}")
    r = _run(["bash", str(REPO / "scripts" / "install-locove-system.sh"),
              "--user", "--port", str(port)], timeout=120)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:600]
        if fresh_install:
            _rollback_agents()  # 新装失败不留半套
        _progress["error"] = detail
        return {"ok": False, "reason": "agents-install-failed", "error": detail,
                "progress": progress()}

    _step("verify-local")
    dns_up = _wait_port(locove.dns_port(), timeout=20)
    proxy_up = _wait_port(port, timeout=20)
    if not (dns_up and proxy_up):
        detail = f"DNS {'就绪' if dns_up else '未响应'} · 代理(:{port}) {'就绪' if proxy_up else '未响应'}"
        if fresh_install:
            _rollback_agents()
        _progress["error"] = detail
        return {"ok": False, "reason": "local-verify-failed", "error": detail,
                "progress": progress()}

    resolver_state = locove._resolver_state(locove.resolver_file())
    if resolver_state == "absent":
        _step("install-resolver", "需要一次管理员授权（仅 .locove 解析配置）")
        auth = admin_run(["bash", str(REPO / "scripts" / "install-locove-system.sh"), "--resolver"],
                         prompt=f"Locove／洛湾：安装仅针对 .{locove.suffix()} 的本地解析配置")
        if not auth.get("ok"):
            if fresh_install:
                _rollback_agents()  # 取消零安装副作用：回到未安装
            detail = {"cancelled": "已取消管理员授权（未安装任何系统件），继续直连",
                      "auth-failed": f"管理员授权失败：{auth.get('error', '')}",
                      "timeout": "等待管理员授权超时，已中止（未安装系统件）"}.get(
                          auth.get("reason"), auth.get("error", ""))
            _progress["error"] = detail
            return {"ok": False, "reason": auth.get("reason"), "error": detail,
                    "progress": progress()}
    else:
        _step("install-resolver", "已安装（归属本工具），无需授权")

    _step("verify-end-to-end")
    host = _probe_host()
    resolution_ok = _resolves(host) if host else None
    if host and not resolution_ok:
        detail = (f"系统解析仍未把 {host} 指向回环（resolver 生效需要几秒；"
                  "可稍后在设置页诊断，或检查 /etc/resolver 与本地 DNS）")
        _progress["error"] = detail
        # 不回滚：安装件均健康，仅系统解析尚未生效——如实报告失败，允许重试
        return {"ok": False, "reason": "resolution-failed", "error": detail,
                "progress": progress()}

    _step("enable-settings")
    st = Registry().locove_settings_update({"use_domains": True, "domain_port": port,
                                            "domains_enabled_at": time.time()})
    _step("done", f"http://<服务名>.{locove.suffix()}:{port}")
    return {"ok": True, "settings": st, "port": port, "resolution_probe": host,
            "state": locove.module_status(st).get("state"), "progress": progress()}


def _rollback_agents() -> None:
    """仅在「本次全新安装」的取消/失败路径调用：撤掉用户级件，系统零残留。"""
    _step("rollback-agents")
    try:
        _run(["bash", str(REPO / "scripts" / "uninstall-locove-system.sh"), "--user-only"],
             timeout=60)
    except Exception:  # noqa: BLE001 — 回滚失败如实暴露在进度面
        _progress["steps"].append({"step": "rollback-agents", "detail": "回滚失败（人工检查）",
                                   "at": time.time()})


def disable() -> dict:
    """关闭域名：回直连。停止 DNS/代理 agent 与路由收敛；安装件与 resolver 保留。"""
    from .registry import Registry
    _progress.update({"active": True, "op": "disable", "step": None, "steps": [],
                      "error": None, "ok": None, "finished_at": None})
    try:
        _step("disable-settings")
        st = Registry().locove_settings_update({"use_domains": False})
        _step("stop-agents")
        uid = os.getuid()
        for job in ("dns", "proxy"):
            subprocess.run(["/bin/launchctl", "bootout", f"gui/{uid}/com.mybrain.ls.locove.{job}"],
                           capture_output=True, text=True, timeout=15)
        _step("done", "直连模式；已安装件保留（可在设置页查看/卸载）")
        _progress["ok"] = True
        return {"ok": True, "settings": st, "state": locove.module_status(st).get("state"),
                "progress": progress()}
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        _progress["error"] = detail
        _progress["ok"] = False
        return {"ok": False, "reason": "error", "error": detail, "progress": progress()}
    finally:
        _progress.update({"active": False, "finished_at": time.time()})


def uninstall(keep_resolver: bool = True) -> dict:
    """卸载域名模块：用户级件必删；resolver 与旧 80 系统件经管理员当次授权删除
    （归属守卫生效；业务进程/数据/登记不动）。keep_resolver=True 时 resolver 保留。"""
    from .registry import Registry
    _progress.update({"active": True, "op": "uninstall", "step": None, "steps": [],
                      "error": None, "ok": None, "finished_at": None})
    try:
        _step("disable")
        try:
            Registry().locove_settings_update({"use_domains": False})
        except Exception:  # noqa: BLE001 — 设置失败不阻塞件清理
            pass
        _step("remove-user-parts")
        r = _run(["bash", str(REPO / "scripts" / "uninstall-locove-system.sh"), "--user-only"],
                 timeout=60)
        removed_resolver = False
        if not keep_resolver:
            st = locove._resolver_state(locove.resolver_file())
            if st == "ours":
                _step("remove-resolver", "需要一次管理员授权")
                auth = admin_run(["bash", str(REPO / "scripts" / "uninstall-locove-system.sh"),
                                  "--resolver-only"],
                                 prompt=f"Locove／洛湾：移除 .{locove.suffix()} 本地解析配置")
                if auth.get("ok"):
                    removed_resolver = True
                else:
                    _progress["error"] = f"resolver 未移除：{auth.get('reason')}"
            elif st == "absent":
                removed_resolver = True
        remaining = []
        if not removed_resolver and locove._resolver_state(locove.resolver_file()) != "absent":
            remaining.append(f"/etc/resolver/{locove.suffix()}（保留）")
        if locove.legacy_80_present():
            remaining.append("旧 80 系统转发器（另行迁移：scripts/retire-locove-80.sh）")
        _step("done", "; ".join(remaining) or "全部移除")
        _progress["ok"] = r.returncode == 0
        return {"ok": r.returncode == 0, "kept": remaining,
                "state": locove.module_status({"use_domains": False}).get("state"),
                "progress": progress()}
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        _progress["error"] = detail
        _progress["ok"] = False
        return {"ok": False, "reason": "error", "error": detail, "progress": progress()}
    finally:
        _progress.update({"active": False, "finished_at": time.time()})


# ---------- 兼容导入（owned 供外部核对归属语义时使用） ----------

__all__ = ["progress", "preflight", "enable", "disable", "uninstall", "admin_run", "owned"]
