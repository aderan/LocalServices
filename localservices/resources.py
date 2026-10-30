"""Docker/Compose 资源盘点（CONTRACTS §1 Resource）：只读，绝不 create/stop/pull/切换 context。

docker 不可达（未安装/超时/守护进程关闭）时返回 {"ok": False, "error": ...}，
不抛异常、不阻塞其余 API。注册时的依赖 resource 引用（container:<name>）据此校验。
"""

from __future__ import annotations

import json
import subprocess

TIMEOUT = 10.0  # 每条 docker 命令的超时上限


def _run(cmd: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    """模块内唯一的 subprocess 出口（测试 monkeypatch 这里）。"""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def inventory() -> dict:
    try:
        ctx = _run(["docker", "context", "show"])
        ps = _run(["docker", "ps", "--format", "{{json .}}"])
        if ctx.returncode != 0 or ps.returncode != 0:
            err = (ctx.stderr or ps.stderr or "docker 调用失败").strip()
            return {"ok": False, "error": err[:300]}
        context = ctx.stdout.strip() or "default"

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
                r = _resource(d)
                if r:
                    by_name[r["name"]] = r

        return {"ok": True, "docker_context": context,
                "resources": [by_name[n] for n in names if n in by_name]}
    except FileNotFoundError:
        return {"ok": False, "error": "docker 未安装或不在 PATH"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"docker 超时（>{TIMEOUT:g}s）"}
    except Exception as e:  # 盘点永不向上抛
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _resource(d: dict) -> dict | None:
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
    volumes = sorted({m.get("Name") for m in (d.get("Mounts") or [])
                      if m.get("Type") == "volume" and m.get("Name")})
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
    }
