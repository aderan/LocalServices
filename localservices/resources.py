"""Docker/Compose 资源盘点（CONTRACTS §1 Resource + V2.6 资源 v2）：只读，
绝不 create/stop/pull/切换 context。

- 盘点用 `ps -a`（含已停止容器），字段在 V1 基础上新增 state/container_id/bind_mounts/context，
  并按清单（dependencies[].container + run=compose 组件）推导 `consumers`。
- 实际 docker 调用统一经 dockerctx（显式 --context，环境清除 DOCKER_HOST/CONTEXT/CONFIG）。
  测试仍可 monkeypatch 模块内 `_run`：它收到 `["docker", <sub>, …]` 形式的 argv
  （与 V1 契约一致），默认实现把子命令交给 dockerctx 执行。
- docker 不可达（未安装/超时/守护进程关闭）时返回 {"ok": False, "error": ...}，
  不抛异常、不阻塞其余 API。注册时的依赖 resource 引用（container:<name>）据此校验。
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import compose, dockerctx, manifest

TIMEOUT = 10.0  # 每条 docker 命令的超时上限


def _run(cmd: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    """模块内唯一的 subprocess 出口（测试 monkeypatch 这里）。

    argv 保持 `["docker", <subcommand>, …]` 形状；默认实现剥离 "docker" 后交给
    dockerctx.run，从而获得显式 --context 与清洗过的子进程环境。
    """
    return dockerctx.run(cmd[1:], timeout=timeout)


def inventory() -> dict:
    """V2 盘点入口（daemon /api/resources 调用）。"""
    return inventory_v2()


def inventory_v2(*, include_consumers=True) -> dict:
    try:
        context = dockerctx.name()
        ps = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
        if ps.returncode != 0:
            err = (ps.stderr or "docker 调用失败").strip()
            return {"ok": False, "error": err[:300]}

        names: list[str] = []
        for line in ps.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = str(row.get("Names", "")).split(",")[0].strip()
            if name:
                names.append(name)

        by_name: dict[str, dict] = {}
        if names:
            insp = _run(["docker", "inspect", "--format", "{{json .}}", *names])
            if insp.returncode != 0:
                return {"ok": False, "error": (insp.stderr or "docker inspect 失败").strip()[:300]}
            for line in insp.stdout.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                r = _resource(d, context)
                if r:
                    by_name[r["name"]] = r

        resources = [by_name[n] for n in names if n in by_name]
        return {"ok": True, "docker_context": context,
                "resources": resources,
                "consumers": _consumers(resources, by_name) if include_consumers else {}}
    except FileNotFoundError:
        return {"ok": False, "error": "docker 未安装或不在 PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"docker 超时（>{TIMEOUT:g}s）"}
    except Exception as e:  # 盘点永不向上抛
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def environments(projects: list[manifest.Project], statuses: list[dict]) -> dict:
    """Project saved bindings and interrogate each distinct local Docker endpoint."""
    try:
        listed = dockerctx.run(["context", "ls", "--format", "{{json .}}"], timeout=4, check=True)
        contexts = [json.loads(line) for line in listed.stdout.splitlines() if line.strip()]
        discovery_error = ""
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        contexts = []
        discovery_error = str(exc)
    profiles = dockerctx.list_colima_profiles()

    groups: dict[str, dict] = {}
    context_endpoints: dict[str, str] = {}
    for row in contexts:
        context = row.get("Name", "")
        if not dockerctx.CONTEXT_NAME.fullmatch(context):
            continue
        endpoint = dockerctx.canonical_endpoint(row.get("DockerEndpoint") or "")
        context_endpoints[context] = endpoint
        group = groups.setdefault(endpoint or context, _environment(context, endpoint, profiles))
        group["contexts"].append(context)
        group["context_present"] = True

    # Colima removes its generated context on stop. Keep the profile discoverable.
    for record in profiles:
        profile = record.get("name")
        if not isinstance(profile, str) or not dockerctx.CONTEXT_NAME.fullmatch(profile):
            continue
        endpoint = dockerctx.colima_endpoint(profile)
        if dockerctx.colima_profile(endpoint, profiles) != profile:
            continue
        context = dockerctx.colima_context(profile)
        group = groups.setdefault(endpoint, _environment(context, endpoint, profiles))
        if context not in context_endpoints:
            group["contexts"].append(context)
            context_endpoints[context] = endpoint
        group["profile_stopped"] = record.get("status") == "Stopped"

    observed = {p["id"]: p for p in statuses}
    for project in projects:
        if not project.uses_docker():
            continue
        binding = project.docker or {}
        context = binding.get("context") or dockerctx.name()
        endpoint = binding.get("endpoint") or context_endpoints.get(context, "")
        group = groups.setdefault(endpoint or context, _environment(context, endpoint, profiles))
        status = observed.get(project.id, {})
        group["services"].append({
            "id": project.id, "name": project.name, "context": context, "state": status.get("state", "unknown"),
            "label": status.get("label") or "未知",
            "binding_changed": bool(binding) and context_endpoints.get(context) != endpoint,
        })

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(_inspect_environment, groups.values()))
    by_service = {p.id: p for p in projects}
    for row in rows:
        for container in row["containers"]:
            container["consumers"] = []
            for service in row["services"]:
                if service["binding_changed"]:
                    continue
                project = by_service[service["id"]]
                dependency = any(d.container == container["name"] for d in project.dependencies)
                component = any(c.run == "compose" and c.compose_service == container["compose_service"]
                                and compose.project_name(project, c) == container["compose_project"]
                                for c in project.components)
                if dependency or component:
                    container["consumers"].append({"id": project.id, "name": project.name,
                                                   "project": project.project or project.id,
                                                   "state": service["state"],
                                                   "label": service["label"],
                                                   "via": "dependency" if dependency else "compose"})
    return {"ok": True, "default_context": dockerctx.name(), "environments": rows, "error": discovery_error}


def _environment(context: str, endpoint: str, profiles: list[dict]) -> dict:
    profile = dockerctx.colima_profile(endpoint, profiles)
    provider = "colima" if profile else "docker"
    if endpoint == f"unix://{Path.home() / '.docker/run/docker.sock'}":
        provider = "docker-desktop"
    elif endpoint.startswith("unix:///") and "podman" in Path(endpoint).name:
        provider = "podman"
    return {"context": context, "endpoint": endpoint, "contexts": [], "provider": provider,
            "profile": profile, "services": [], "context_present": False}


def _inspect_environment(group: dict) -> dict:
    endpoint = group["endpoint"]
    group.update(state="unknown", containers=[], error="")
    if not group["context_present"] or not endpoint:
        if group.get("profile_stopped"):
            group["state"] = "stopped"
        return group
    if not dockerctx.is_local(endpoint):
        group["state"] = "remote"
        return group
    context = group["context"]
    try:
        with dockerctx.bound({"context": context, "endpoint": endpoint}):
            result = dockerctx.run(["info", "--format", "{{.ServerVersion}}"], timeout=4)
            if result.returncode:
                group.update(state="unreachable", error=(result.stderr or result.stdout).strip())
                return group
            group["state"] = "ready"
            inv = inventory_v2(include_consumers=False)
            group["containers"] = inv.get("resources", [])
            group["error"] = inv.get("error", "")
    except (OSError, dockerctx.BindingError, subprocess.SubprocessError) as exc:
        group["error"] = str(exc)
    return group


def container_state(container: str) -> str | None:
    """单个容器的状态（ps -a 语义：已停止容器也可见）；不存在/不可达 → None。

    只读助手，注册校验（container: 引用）等调用方可防御性使用。
    """
    try:
        r = _run(["docker", "container", "inspect", "--format", "{{.State.Status}}", container])
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _consumers(resources: list[dict], by_name: dict[str, dict]) -> dict[str, list[dict]]:
    """谁在用这些资源：由清单（dependencies[].container + run=compose 组件）推导。

    清单加载失败（损坏/权限等）→ 空 consumers，绝不让盘点整体失败。
    """
    try:
        projects = manifest.load_all()
    except Exception:
        return {}
    out: dict[str, list[dict]] = {}

    def running(name: str) -> bool:
        return by_name.get(name, {}).get("state") == "running"

    for p in projects.values():
        if p.docker and p.docker["context"] != dockerctx.name():
            continue
        for dep in p.dependencies:
            if dep.container and dep.container in by_name:
                out.setdefault(f"container:{dep.container}", []).append(
                    {"service": p.id, "via": "dependency", "running": running(dep.container)})
        for c in p.components:
            if c.run != "compose" or not c.compose_service:
                continue
            compose_project = c.compose_project or p.comp_cwd(c).name
            for r in resources:
                if r.get("compose_project") == compose_project and r.get("compose_service") == c.compose_service:
                    out.setdefault(r["id"], []).append(
                        {"service": p.id, "via": "compose", "running": r.get("state") == "running"})
    return out


def _resource(d: dict, context: str = "") -> dict | None:
    name = str(d.get("Name", "")).lstrip("/")
    if not name:
        return None
    state = d.get("State", {}) or {}
    config = d.get("Config", {}) or {}
    labels = config.get("Labels") or {}
    ports: dict[str, list[dict]] = {}
    for key, bindings in (d.get("NetworkSettings", {}) or {}).get("Ports", {}).items():
        mapped = [{"host_ip": b.get("HostIp", ""), "host_port": int(b.get("HostPort", 0))}
                  for b in (bindings or [])]
        if mapped:
            ports[key] = mapped
    mounts = d.get("Mounts") or []
    volumes = sorted({m.get("Name") for m in mounts
                      if m.get("Type") == "volume" and m.get("Name")})
    bind_mounts = [{"source": m.get("Source", ""), "target": m.get("Destination", "")}
                   for m in mounts if m.get("Type") == "bind"]
    return {
        "id": f"container:{name}",
        "kind": "docker-container",
        "name": name,
        "image": config.get("Image", ""),
        "state": state.get("Status", "unknown"),
        "health": (state.get("Health") or {}).get("Status") or "none",
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "ports": ports,
        "volumes": volumes,
        # V2.6 新字段
        "container_id": d.get("Id", ""),
        "bind_mounts": bind_mounts,
        "context": context,
    }
