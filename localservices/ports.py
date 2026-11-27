"""TCP endpoint evidence shared by startup, registration and repair."""

import re
from urllib.parse import urlsplit

from . import locove, manifest, paths, procs


def _matching_url_host(url: str | None, port: int | None) -> str | None:
    if url:
        try:
            parts = urlsplit(url)
            if parts.hostname and (parts.port or (443 if parts.scheme == "https" else 80)) == port:
                return parts.hostname
        except ValueError:
            return None
    return None


def target_hosts(component: manifest.Component, opening: str | None) -> list[str]:
    hosts = []
    if component.ready:
        if component.ready.tcp:
            host, _, endpoint_port = component.ready.tcp.rpartition(":")
            if host and endpoint_port == str(component.port):
                hosts.append(host.strip("[]"))
        http_host = _matching_url_host(component.ready.http, component.port)
        if http_host and http_host not in hosts:
            hosts.append(http_host)
    return hosts or [_matching_url_host(opening, component.port) or "127.0.0.1"]


def target_host(component: manifest.Component, opening: str | None) -> str:
    return target_hosts(component, opening)[0]


def component_listeners(listen: dict[int, list[procs.TCPListener]], component: manifest.Component,
                        opening: str | None) -> list[procs.TCPListener]:
    return list(dict.fromkeys(row for host in target_hosts(component, opening)
                              for row in procs.matching_listeners(listen, host, component.port)))


def occupancy(component: str, host: str, port: int, listeners: list[procs.TCPListener]) -> dict:
    return {
        "kind": "occupied", "component": component, "host": host, "port": port,
        "listener_pid": listeners[0].pid,
        "listeners": [{"pid": row.pid, "address": row.address, "family": row.family} for row in listeners],
    }


def readiness(project: manifest.Project, component: manifest.Component, pid: int,
              processes: dict, listen: dict[int, list[procs.TCPListener]]) -> dict | None:
    if not component.port:
        return None
    port = component.port
    owned = procs.descendants(pid, processes)
    missing = []
    for host in target_hosts(component, project.open):
        matches = procs.matching_listeners(listen, host, port)
        foreign = [row for row in matches if row.pid not in owned]
        if foreign:
            return occupancy(component.id, host, port, foreign)
        if not matches:
            missing.append(host)
    if not missing:
        return None
    host = missing[0]
    actual = sorted(p for p, rows in listen.items() if any(row.pid in owned for row in rows))
    kind = "wrong_address" if port in actual else "wrong_port" if actual else "not_listening"
    return {"kind": kind, "component": component.id, "host": host, "port": port, "actual_ports": actual}


def published_listeners(state: dict) -> dict[int, list[procs.TCPListener]]:
    published: dict[int, list[procs.TCPListener]] = {}
    for target, bindings in state.get("published_ports", {}).items():
        if not target.endswith("/tcp"):
            continue
        for binding in bindings or ():
            host, port = binding.get("HostIp", ""), str(binding.get("HostPort", ""))
            if port.isdigit():
                published.setdefault(int(port), []).append(procs.TCPListener(0, host))
    return published


def published_port_issue(project: manifest.Project, component: manifest.Component, state: dict,
                         published: dict[int, list[procs.TCPListener]]) -> dict | None:
    if not component.port:
        return None
    port = component.port
    host = next((host for host in target_hosts(component, project.open)
                 if not procs.matching_listeners(published, host, port)), None)
    if host is None:
        return None
    kind = "wrong_address" if port in published else "wrong_port" if published else "unpublished"
    return {"kind": kind, "component": component.id, "host": host, "port": port,
            "actual_ports": sorted(published), "container": state.get("container")}


def loopback_host(listen: dict[int, list[procs.TCPListener]], port: int,
                  owned: set[int], preferred: str) -> str | None:
    candidates = ("::1", "127.0.0.1") if preferred == "::1" else ("127.0.0.1", "::1")
    for host in candidates:
        matches = procs.matching_listeners(listen, host, port)
        if matches and all(row.pid in owned for row in matches):
            return host
    return None


def describe(issue: dict) -> str:
    port = issue["port"]
    host = issue["host"]
    if issue["kind"] == "occupied":
        return f"端口 {port} 被其他进程占用（pid {issue['listener_pid']}）"
    if issue["kind"] == "bind_failed":
        return f"本次启动报告端口 {port} 绑定冲突，当前占用方待核实"
    if issue["kind"] == "unpublished":
        return f"容器未发布宿主机 TCP 端口 {port}"
    if issue["kind"] == "wrong_address":
        return f"端口 {port} 未使用预期监听地址 {host}"
    if issue["kind"] == "wrong_port":
        actual = "、".join(map(str, issue["actual_ports"]))
        return f"预期监听端口 {port}，实际监听端口 {actual}"
    return f"进程尚未监听端口 {port}"


BIND_FAILURE = re.compile(
    r"\bEADDRINUSE\b|address already in use|port is already allocated|ports? (?:are|is) not available",
    re.IGNORECASE,
)


def bind_failure(project: manifest.Project, component: manifest.Component, output: str) -> dict | None:
    if component.port and BIND_FAILURE.search(output):
        return {"kind": "bind_failed", "component": component.id, "port": component.port,
                "host": target_host(component, project.open)}
    return None


def control_ports(settings: dict | None = None) -> set[int]:
    return {paths.daemon_port(), locove.dns_port(), locove.admin_port(),
            locove.proxy_port(), locove.effective_port(settings=settings)}


def suggest(port: int, occupied: set[int]) -> int | None:
    for candidates in (range(max(port + 1, 1024), 65536), range(1024, port)):
        for candidate in candidates:
            if candidate not in occupied:
                return candidate
    return None


def repair_strategy(project: manifest.Project, component: manifest.Component) -> tuple[str, int] | None:
    """Find an explicit registration field that controls the listener."""
    if (project.observe_only or project.autostart or project.adapter or project.options or project.uses_docker()
            or len(project.components) != 1 or component.kind != "server" or not component.cmd
            or component.build or any("{build}" in arg for arg in component.cmd)
            or (component.install and not (component.install_marker
                and (project.comp_cwd(component) / component.install_marker).is_file()))):
        return None
    cmd, old = component.cmd, str(component.port)
    launcher = cmd[0].rsplit("/", 1)[-1]
    if launcher in ("sh", "bash", "zsh", "fish", "dash", "ksh"):
        return None
    flags = [(i, arg) for i, arg in enumerate(cmd[1:], 1)
             if arg in ("--port", "-p") or arg.startswith(("--port=", "-p="))]
    assigned = component.env.get("PORT")
    if cmd[1:3] == ["-m", "http.server"] and launcher.startswith("python") and cmd[3:4] == [old]:
        return ("cmd", 3) if not flags and assigned is None else None
    if not flags:
        return ("env", 0) if assigned == old else None
    if len(flags) == 1 and assigned is None:
        i, arg = flags[0]
        if arg == f"--port={old}":
            return "cmd_inline", i
        if (arg == "--port" or (arg == "-p" and any(
                token.rsplit("/", 1)[-1] in ("next", "vite", "astro", "nuxt") for token in cmd[:i]
        ))) and cmd[i + 1:i + 2] == [old]:
            return "cmd", i + 1
    return None


def repair_action(project: manifest.Project, issues: list[dict]) -> str | None:
    issues = [issue for issue in issues if issue["kind"] != "not_listening"]
    if not issues:
        return None
    if (len(issues) == 1 and issues[0]["kind"] in ("occupied", "wrong_port")
            and not any(owner["same_project"] for owner in issues[0].get("owners", []))
            and repair_strategy(project, project.component(issues[0]["component"]))):
        return "auto"
    return "prompt"


def project_prompt(source: str, service: str, issues: list[dict], *, rejected_port: int | None = None) -> str:
    lines = [f"请在项目目录 {source} 修复洛湾登记服务 {service} 的监听配置。"]
    for issue in issues:
        address = issue["host"]
        if ":" in address:
            address = f"[{address}]"
        fact = describe(issue) if issue["kind"] != "reserved" else "登记端口已被其他服务预留"
        details = [f"{owner['container']}（容器）" if owner.get("container") else owner["service"]
                   for owner in issue.get("owners", []) if owner.get("container") or owner.get("service")]
        if issue.get("reserved_by"):
            details.append(issue["reserved_by"])
        if issue.get("container"):
            details.append(issue["container"])
        if details:
            fact += f"（{'、'.join(details)}）"
        suggested = issue.get("suggested_port")
        suggestion = f"，建议改用端口 {suggested}" if suggested else ""
        lines.append(f"- 组件 {issue['component']} 预期监听 {address}:{issue['port']}，{fact}{suggestion}。")
    if rejected_port:
        lines.append(f"洛湾已尝试端口 {rejected_port}，启动验证未通过。请确认项目是否实际使用了指定端口。")
    lines.append(
        "查明实际生效的启动命令或项目配置，做最小修改，让受影响组件支持指定空闲端口。"
        "检查相关健康探针、前端代理、回调和 Compose 宿主机映射，并保留当前占用方和其他运行中的服务。"
        "用项目原有启动方式验证进程归属、实际监听地址及健康检查。"
        "最后列出洛湾登记需要更新的组件、启动命令或环境变量、端口、探针和打开地址。"
    )
    return "\n".join(lines)
