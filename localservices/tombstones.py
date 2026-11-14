"""用户配置层移除标记（tombstone，CONTRACTS V2.0 + V2.0 内置遮蔽修订）。

removed.json 记录用户主动移除的服务/项目；manifest.load_all 据此跳过对应**内置目录**
模板（user_services_dir 的同 id 文件始终加载——用户层重新注册接管内置模板）。
同 id 重新注册：纯用户层 id 成功后删除条目；存在内置模板的 id **保留**条目（持续遮蔽）。

读取函数（services/projects）保持既有行为：坏文件按空处理，绝不覆盖原文件。
写入端（add_*/remove_*/plan_*）由 registry.py 的移除/恢复/注册事务调用；
`plan_*` 只产出整文件字节（不落盘），供调用方并入原子提交。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

_lock = threading.RLock()


def _load() -> dict:
    try:
        data = json.loads(paths.removed_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_full() -> dict:
    data = _load()
    data.setdefault("schemaVersion", SCHEMA_VERSION)
    data.setdefault("services", {})
    data.setdefault("projects", {})
    return data


def _dump(data: dict) -> bytes:
    data["schemaVersion"] = SCHEMA_VERSION
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def commit(data_bytes: bytes) -> None:
    """落盘 plan_* 产出的整文件字节。"""
    _write_bytes_atomic(paths.removed_file(), data_bytes)


# ---------------------------------------------------------------- 读取（既有行为）

def services() -> set[str]:
    """被移除服务的 id 集合（坏文件按空处理，绝不覆盖原文件）。"""
    services = _load().get("services")
    if isinstance(services, dict):
        return set(services.keys())
    return set()


def projects() -> set[str]:
    removed = _load().get("projects")
    if isinstance(removed, dict):
        return set(removed.keys())
    return set()


def service_entry(sid: str) -> dict | None:
    entry = _load().get("services", {})
    return entry.get(sid) if isinstance(entry, dict) else None


def project_entry(pid: str) -> dict | None:
    entry = _load().get("projects", {})
    return entry.get(pid) if isinstance(entry, dict) else None


# ---------------------------------------------------------------- 写入

# 累计式计划：先 base() 取一份可变数据，apply_* 逐项叠加，返回累计后的整文件字节；
# 单发式 plan_*（内部 base+apply）适合只有一处变更的调用方。

def base() -> dict:
    """累计式计划的起点（可变 dict，含 schemaVersion/services/projects）。"""
    with _lock:
        return _load_full()


def apply_add_service(data: dict, sid: str, entry: dict) -> bytes:
    data.setdefault("services", {})[sid] = entry
    return _dump(data)


def apply_remove_service(data: dict, sid: str) -> bytes | None:
    if sid in data.get("services", {}):
        del data["services"][sid]
        return _dump(data)
    return None


def apply_add_project(data: dict, pid: str, entry: dict) -> bytes:
    data.setdefault("projects", {})[pid] = entry
    return _dump(data)


def apply_remove_project(data: dict, pid: str) -> bytes | None:
    if pid in data.get("projects", {}):
        del data["projects"][pid]
        return _dump(data)
    return None


def plan_add_service(sid: str, entry: dict) -> bytes:
    """新增/覆盖服务移除标记，返回新文件字节（总是有变化：removed_at 刷新）。"""
    return apply_add_service(base(), sid, entry)


def plan_remove_service(sid: str) -> bytes | None:
    return apply_remove_service(base(), sid)


def plan_add_project(pid: str, entry: dict) -> bytes:
    return apply_add_project(base(), pid, entry)


def plan_remove_project(pid: str) -> bytes | None:
    return apply_remove_project(base(), pid)


def plan_clear_on_reregister(sid: str) -> bytes | None:
    """重新注册成功后解除 tombstone（V2.0 修订）：存在同 id 内置模板则保留条目
    （继续遮蔽内置文件，避免与用户层文件形成 duplicate id），纯用户层 id 才删除。"""
    if has_builtin(sid):
        return None
    return plan_remove_service(sid)


def has_builtin(sid: str) -> bool:
    from . import manifest

    return any(manifest.load_file(file).id == sid for file in paths.services_dir().glob("*.toml"))


def add_service(sid: str, entry: dict) -> None:
    commit(plan_add_service(sid, entry))


def remove_service(sid: str) -> None:
    bs = plan_remove_service(sid)
    if bs is not None:
        commit(bs)


def add_project(pid: str, entry: dict) -> None:
    commit(plan_add_project(pid, entry))


def remove_project(pid: str) -> None:
    bs = plan_remove_project(pid)
    if bs is not None:
        commit(bs)
