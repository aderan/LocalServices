"""Run an explicit, temporary readiness check before committing a registration."""

from __future__ import annotations

import ipaddress
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import manifest, procs
from .controller import Controller, STARTUP_TIMEOUT as START_TIMEOUT, tail


def local_address(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_url(url: str) -> tuple[str | None, int]:
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not local_address(parsed.hostname)
            or parsed.username is not None):
        raise ValueError("运行验证需要本机 HTTP(S) 地址")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def validate(project: manifest.Project, components: list[manifest.Component],
             repair: bool, deadline: float) -> None:
    if project.observe_only or project.autostart or project.adapter:
        raise ValueError("此服务的运行方式需要手动登记和验证")
    if not components or (not repair and project.generators()):
        raise ValueError("含生成任务的服务需要手动登记和验证")
    for comp in components:
        if comp.run != "local" or comp.build or any("{build}" in arg for arg in comp.cmd):
            raise ValueError(f"{comp.id} 需要容器或构建步骤，请手动验证")
        if comp.install and not (comp.install_marker and
                                 (project.comp_cwd(comp) / comp.install_marker).exists()):
            raise ValueError(f"{comp.id} 需要先安装依赖")
        if not comp.port or comp.kind != "server":
            raise ValueError(f"{comp.id} 缺少可验证的监听端口")
        if comp.ready and comp.ready.http and local_url(comp.ready.http)[1] != comp.port:
            raise ValueError(f"{comp.id} 的探针与监听端口不一致")
        if comp.ready and comp.ready.tcp:
            host, _, port = comp.ready.tcp.rpartition(":")
            if not local_address(host) or int(port) != comp.port:
                raise ValueError(f"{comp.id} 的 TCP 探针需要指向本机监听端口")
    if project.open and local_url(project.open)[1] not in {comp.port for comp in components}:
        raise ValueError("打开地址与组件监听端口不一致")
    for dep in project.dependencies:
        if not dep.required:
            continue
        if dep.probe.http:
            local_url(dep.probe.http)
        if dep.probe.tcp and not local_address(dep.probe.tcp.rpartition(":")[0]):
            raise ValueError(f"{dep.name} 的探针需要指向本机")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not procs.probe(dep.probe, min(2, remaining))[0]:
            raise ValueError(f"必要依赖 {dep.name} 未就绪")


def wait_ready(process: subprocess.Popen, port: int, probe: manifest.Probe, deadline: float) -> None:
    detail = "等待进程监听端口"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError(f"试启动进程已退出（{process.returncode}）")
        listener = procs.listeners().get(port)
        if listener is not None:
            if listener not in procs.descendants(process.pid, procs.snapshot()):
                raise ValueError(f"端口 {port} 由其他进程监听")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ok, detail = procs.probe(probe, min(probe.timeout, remaining))
            if ok and process.poll() is None:
                return
        time.sleep(.1)
    raise ValueError(f"端口 {port} 试启动超时：{detail}")


def run(definition: bytes, record: Path, *, repair: bool = False) -> dict:
    """Trial the same normalized definition the registry will save, without launchd."""
    processes = []
    logs = []
    checks = []
    with tempfile.TemporaryDirectory(prefix="ls-trial-") as tmp:
        try:
            config = Path(tmp) / "service.toml"
            config.write_bytes(definition)
            project = manifest.load_file(config)
            components = Controller._order(project)
            deadline = time.monotonic() + START_TIMEOUT
            validate(project, components, repair, deadline)
            occupied = procs.listeners()
            for comp in components:
                if comp.port in occupied or procs.tcp_ok(f"127.0.0.1:{comp.port}", .15):
                    raise ValueError(f"端口 {comp.port} 已被占用")
            for comp in components:
                if time.monotonic() >= deadline:
                    raise ValueError("已达到试启动时限")
                log = Path(tmp) / (comp.id + ".log")
                logs.append(log)
                with log.open("wb") as stream:
                    process = subprocess.Popen(comp.cmd, cwd=project.comp_cwd(comp),
                                               env=manifest.component_env(project, comp),
                                               stdin=subprocess.DEVNULL, stdout=stream,
                                               stderr=stream, start_new_session=True)
                processes.append(process)
                probe = comp.ready or manifest.Probe(tcp=f"127.0.0.1:{comp.port}")
                wait_ready(process, comp.port, probe, min(deadline, time.monotonic() + comp.start_timeout))
                checks.append({"component": comp.id, "port": comp.port})
            if project.open:
                _, port = local_url(project.open)
                index = next(i for i, comp in enumerate(components) if comp.port == port)
                wait_ready(processes[index], port, manifest.Probe(http=project.open), deadline)
            if any(process.poll() is not None for process in processes):
                raise ValueError("试启动组件在验证完成前退出")
            result = {"state": "passed", "checks": checks,
                      "skipped": [g.id for g in project.generators()]}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            result = {"state": "failed", "error": str(error), "checks": checks}
        finally:
            for process in reversed(processes):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        output = "\n".join(f"[{log.stem}]\n{tail(log, 20)}" for log in logs)
        if output:
            with record.open("a", encoding="utf-8") as stream:
                stream.write(output + "\n")
    return result
