"""Required dependency preflight, separate from the application's 30-second startup budget.

Docker 语义与 dockerctx 同源（V2.6）：显式 `--context <dockerctx.name()>`、子进程环境清除
DOCKER_HOST/DOCKER_CONTEXT/DOCKER_CONFIG、endpoint 非本机 unix socket 一律拒绝操作。
Compose 组件和容器依赖共用同一套运行环境准备流程。
"""

from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
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


class RuntimeUnavailable(DependencyError):
    pass


class Infrastructure:
    def __init__(self):
        self.lock = threading.Lock()
        self.recoveries: dict[str, Future] = {}
        self.containers: dict[tuple[str, str], threading.Lock] = {}

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
        if dockerctx.active_binding():
            return dockerctx.endpoint()
        result = self._docker(
            ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}", dockerctx.name()],
            deadline)
        if result.returncode:
            endpoint = dockerctx.stopped_colima_endpoint(dockerctx.name())
            if not endpoint:
                raise DependencyError(f"无法解析 Docker 上下文 {dockerctx.name()} 的 endpoint，拒绝操作")
            return endpoint
        return dockerctx.canonical_endpoint(result.stdout.strip())

    def _restore_runtime(self, endpoint, deadline, progress):
        if self._docker(["info", "--format", "{{.ServerVersion}}"], deadline).returncode == 0:
            return
        if not endpoint.startswith(f"unix://{dockerctx.colima_root()}/"):
            raise RuntimeUnavailable(f"Docker {dockerctx.name()} 不可达，请启动对应的运行环境后重试")
        profiles = self._command(["colima", "list", "--json"], deadline)
        try:
            profile = dockerctx.colima_profile(endpoint, dockerctx.colima_profiles(profiles.stdout))
        except ValueError as exc:
            raise RuntimeUnavailable("无法确认已有 Colima 环境，未自动创建新环境") from exc
        if profiles.returncode or not profile:
            raise RuntimeUnavailable(f"Docker {dockerctx.name()} 的 Colima 实例无法确认，未自动创建新环境")
        progress(f"正在启动 Colima {profile}，应用尚未启动")
        result = self._command(["colima", "start", profile, "--activate=false"], deadline, ENVIRONMENT_TIMEOUT)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeUnavailable(f"Colima {profile} 启动失败（退出码 {result.returncode}）"
                                     + (f"：{detail}" if detail else ""))
        progress(f"等待 Colima {profile} 的 Docker Engine 就绪")
        try:
            while self._docker(["info", "--format", "{{.ServerVersion}}"], deadline).returncode:
                time.sleep(min(0.5, self._remaining(deadline)))
        except DependencyError as exc:
            raise RuntimeUnavailable(f"Colima {profile} 已启动，Docker {dockerctx.name()} 仍不可达：{exc}") from exc

    def _runtime(self, deadline, progress, allow_start):
        endpoint = self._endpoint(deadline)
        if not endpoint or not dockerctx.is_local(endpoint):
            raise RuntimeUnavailable(f"Docker {dockerctx.name()} 未指向本机默认环境，{dockerctx.REMOTE_REFUSAL}")
        with self.lock:
            future = self.recoveries.get(endpoint)
            owner = future is None and allow_start
            if owner:
                future = self.recoveries[endpoint] = Future()
        if future is None:
            if self._docker(["info", "--format", "{{.ServerVersion}}"], deadline).returncode:
                raise RuntimeUnavailable(f"Docker {dockerctx.name()} 不可达，请先启动运行环境")
            return endpoint
        if owner:
            try:
                self._restore_runtime(endpoint, deadline, progress)
            except BaseException as exc:
                future.set_exception(exc)
                raise
            else:
                future.set_result(None)
            finally:
                with self.lock:
                    del self.recoveries[endpoint]
        else:
            progress(f"等待共享 Docker 环境 {dockerctx.name()} 就绪")
            try:
                future.result(timeout=self._remaining(deadline))
            except FutureTimeout as exc:
                raise RuntimeUnavailable(f"等待 Docker {dockerctx.name()} 就绪超时") from exc
        return endpoint

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

    def ensure(self, dependencies, progress, *, docker_required=False, binding=None, start_environment=False):
        try:
            with dockerctx.bound(binding, allow_stopped_colima=True):
                self._ensure(dependencies, progress, docker_required,
                             start_environment or not binding or binding.get("autostart", False))
        except dockerctx.BindingError as exc:
            raise RuntimeUnavailable(str(exc)) from exc

    def _ensure(self, dependencies, progress, docker_required, allow_start):
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
        if not containers and not docker_required:
            return
        progress("等待共享容器环境准备")
        try:
            endpoint = self._runtime(deadline, progress, allow_start)
        except DependencyError as exc:
            raise RuntimeUnavailable(str(exc)) from exc
        for dependency in containers:
            with self.lock:
                lock = self.containers.setdefault((endpoint, dependency.container), threading.Lock())
            if not lock.acquire(timeout=self._remaining(deadline)):
                raise DependencyError(f"等待容器 {dependency.container} 就绪超时")
            try:
                self._container(dependency, deadline, progress)
            finally:
                lock.release()
