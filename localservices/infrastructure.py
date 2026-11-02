"""Required dependency preflight, separate from the application's 30-second startup budget.

Docker 语义与 dockerctx 同源（V2.6）：显式 `--context <dockerctx.name()>`、子进程环境清除
DOCKER_HOST/DOCKER_CONTEXT/DOCKER_CONFIG、endpoint 非本机 unix socket 一律拒绝操作。
Colima default profile 的恢复行为保持 V1 不变（仅在 endpoint 确属本机 Colima 时）。
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from . import dockerctx, procs
from .manifest import DEFAULT_PATH

ENVIRONMENT_TIMEOUT = 120.0
INSPECT = '{"state":{{json .State}},"mounts":{{json .Mounts}},"ports":{{json .HostConfig.PortBindings}}}'


class DependencyError(RuntimeError):
    pass


class Infrastructure:
    def __init__(self):
        # One daemon serves all projects; serialize shared runtime/container recovery.
        self.lock = threading.Lock()

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DependencyError(f"环境准备超时（{ENVIRONMENT_TIMEOUT:g}s），应用尚未启动")
        return remaining

    def _command(self, args, deadline, timeout=10):
        executable = shutil.which(args[0], path=DEFAULT_PATH)
        if not executable:
            raise DependencyError(f"未找到本机 {args[0]}，无法恢复已有运行环境")
        env = dockerctx.scrub_env({"PATH": DEFAULT_PATH})
        try:
            return subprocess.run([executable, *args[1:]], capture_output=True, text=True,
                                  env=env, timeout=min(timeout, self._remaining(deadline)))
        except subprocess.TimeoutExpired as exc:
            raise DependencyError(f"环境准备命令超时：{args[0]} {args[1]}") from exc
        except OSError as exc:
            raise DependencyError(f"无法运行本机 {args[0]}：{exc}") from exc

    def _docker(self, args, deadline):
        # argv 来自 dockerctx（docker --context <name> …），执行仍走本模块 subprocess（测试接缝）。
        return self._command(dockerctx.argv(args), deadline)

    def _endpoint(self, deadline):
        result = self._docker(
            ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}", dockerctx.name()],
            deadline)
        if result.returncode:
            raise DependencyError(f"无法解析 Docker 上下文 {dockerctx.name()} 的 endpoint，拒绝操作")
        return result.stdout.strip()

    def _runtime(self, deadline, progress):
        endpoint = self._endpoint(deadline)
        if not dockerctx.is_local(endpoint):
            # V2.6: 远程 engine 只允许只读盘点，恢复/接管一律拒绝。
            raise DependencyError(
                f"Docker {dockerctx.name()} 上下文未指向本机默认环境（{endpoint or 'endpoint 未知'}），"
                f"{dockerctx.REMOTE_REFUSAL}，拒绝接管其他 Docker 环境")
        if self._docker(["info", "--format", "{{.ServerVersion}}"], deadline).returncode == 0:
            return
        colima_root = f"unix://{Path.home()}/.colima/"
        if not endpoint.startswith(colima_root):
            raise DependencyError("本机 Docker 引擎不可达且该上下文非 Colima 管理，未自动恢复")
        profiles = self._command(["colima", "list", "--json"], deadline)
        try:
            records = [json.loads(line) for line in profiles.stdout.splitlines() if line.strip()]
            records = [item for record in records for item in (record if isinstance(record, list) else [record])]
        except (ValueError, TypeError) as exc:
            raise DependencyError("无法确认已有 Colima 环境，未创建新环境") from exc
        if profiles.returncode or not any(p.get("name") == "default" for p in records):
            raise DependencyError("已有 Colima default 环境不存在，未自动创建新环境")
        progress("正在恢复 Colima 环境（最多 120 秒），应用尚未启动")
        result = self._command(["colima", "start", "default", "--activate=false"], deadline, ENVIRONMENT_TIMEOUT)
        if result.returncode:
            raise DependencyError("Colima 恢复失败，原环境保留；检查 Colima 日志后重试")
        while self._docker(["info", "--format", "{{.ServerVersion}}"], deadline).returncode:
            time.sleep(min(0.5, self._remaining(deadline)))

    def _inspect(self, dependency, deadline):
        result = self._docker(["container", "inspect", "--format", INSPECT, dependency.container], deadline)
        if result.returncode:
            raise DependencyError(f"现有容器 {dependency.container} 不存在或无法读取，未创建容器或数据库")
        try:
            info = json.loads(result.stdout)
            if not any(m.get("Type") == "volume" and m.get("Name") == dependency.volume
                       for m in info["mounts"]):
                raise DependencyError(f"容器 {dependency.container} 未挂载原数据卷 {dependency.volume}，拒绝启动")
            if dependency.probe.tcp:
                port = dependency.probe.tcp.rsplit(":", 1)[1]
                bindings = [b for entries in (info.get("ports") or {}).values() for b in (entries or [])]
                if not any(b.get("HostPort") == port for b in bindings):
                    raise DependencyError(f"容器 {dependency.container} 未映射预期端口 {port}，拒绝启动")
            return info["state"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DependencyError(f"无法解析容器 {dependency.container} 的状态") from exc

    def _container(self, dependency, deadline, progress):
        state = self._inspect(dependency, deadline)
        if not state.get("Running"):
            progress(f"启动原有容器 {dependency.container}（保留原数据卷）")
            result = self._docker(["start", dependency.container], deadline)
            if result.returncode:
                raise DependencyError(f"现有容器 {dependency.container} 启动失败，检查端口占用及容器日志")
        progress(f"等待 {dependency.name} 健康，应用尚未启动")
        while True:
            state = self._inspect(dependency, deadline)
            health = (state.get("Health") or {}).get("Status")
            if not state.get("Running") or health in (None, "unhealthy"):
                raise DependencyError(f"{dependency.name} 容器未运行或健康检查失败（{health or '无健康检查'}）")
            ok, _ = procs.probe(dependency.probe, min(3, self._remaining(deadline)))
            if health == "healthy" and ok:
                return
            time.sleep(min(0.5, self._remaining(deadline)))

    def ensure(self, dependencies, progress):
        required = [d for d in dependencies if d.required]
        deadline = time.monotonic() + ENVIRONMENT_TIMEOUT
        for dependency in required:
            if dependency.container:
                if not dependency.volume or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dependency.container):
                    raise DependencyError(f"{dependency.name} 缺少有效容器名称或原数据卷声明")
                continue
            progress(f"检查 {dependency.name}")
            ok, detail = procs.probe(dependency.probe, min(dependency.probe.timeout, 3, self._remaining(deadline)))
            if not ok:
                raise DependencyError(f"{dependency.name} 未就绪：{detail}")
        containers = [d for d in required if d.container]
        if not containers:
            return
        progress("等待共享容器环境准备")
        if not self.lock.acquire(timeout=self._remaining(deadline)):
            raise DependencyError("等待共享容器环境准备超时")
        try:
            self._runtime(deadline, progress)
            for dependency in containers:
                self._container(dependency, deadline, progress)
        finally:
            self.lock.release()
