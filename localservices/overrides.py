"""人工覆盖层（CONTRACTS V2.3）。

overrides.json = {"schemaVersion": 1, "services": {<sid>: {"fields": {<dotted path>: value},
"updated_at": ...}}}。路径形如 `name`、`description`、`open`（清单顶层）与
`components[<cid>].<field>`（组件字段）。注册合并时覆盖层优先生效：payload
`service.accept_new` 显式清除指定覆盖，`service.edited` 在注册成功后把用户编辑的
最终值写入覆盖层——因此 dry_run 的 diff 在应用覆盖后计算，预览 = 实际。

本模块自带原子写与进程内锁；注册事务需要把 overrides.json 并入同一原子提交时，
用 `plan_*` 变体拿到新文件字节（不落盘），再由调用方随其余文件一起提交
（`commit` / Registry._atomic_commit）。
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

from . import paths

SCHEMA_VERSION = 1

_lock = threading.RLock()

# 顶层可覆盖字段（V2.3 契约限定；source/id 等身份字段绝不可覆盖）
TOP_FIELDS = ("name", "description", "open")
_COMP_PATH_RE = re.compile(r"^components\[([A-Za-z0-9_-]+)\]\.([A-Za-z_][A-Za-z0-9_-]*)$")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load() -> dict:
    try:
        data = json.loads(paths.overrides_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schemaVersion": SCHEMA_VERSION, "services": {}}
    if not isinstance(data, dict):
        return {"schemaVersion": SCHEMA_VERSION, "services": {}}
    data.setdefault("schemaVersion", SCHEMA_VERSION)
    data.setdefault("services", {})
    return data


def _dump(data: dict) -> bytes:
    data["schemaVersion"] = SCHEMA_VERSION
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def _save(data: dict) -> None:
    _write_bytes_atomic(paths.overrides_file(), _dump(data))


def commit(data_bytes: bytes) -> None:
    """落盘由 plan_* 产出的整文件字节（注册事务提交用）。"""
    _write_bytes_atomic(paths.overrides_file(), data_bytes)


# ---------------------------------------------------------------- 路径读写

def _valid_path(path: str) -> bool:
    return path in TOP_FIELDS or bool(_COMP_PATH_RE.match(path or ""))


def _get_path(definition: dict, path: str):
    """从清单层定义取 dotted path 的当前值；路径不存在返回 None。"""
    if path in TOP_FIELDS:
        return definition.get(path)
    m = _COMP_PATH_RE.match(path or "")
    if not m:
        return None
    cid, field = m.group(1), m.group(2)
    for c in definition.get("components", []) or []:
        if isinstance(c, dict) and c.get("id") == cid:
            return c.get(field)
    return None


def _set_path(definition: dict, path: str, value) -> bool:
    """把值写入定义的 dotted path；未知路径/缺失组件返回 False（忽略，不报错）。"""
    if not _valid_path(path):
        return False
    if path in TOP_FIELDS:
        definition[path] = value
        return True
    m = _COMP_PATH_RE.match(path)
    cid, field = m.group(1), m.group(2)
    for c in definition.get("components", []) or []:
        if isinstance(c, dict) and c.get("id") == cid:
            c[field] = value
            return True
    return False


# ---------------------------------------------------------------- 查询

def fields(sid: str) -> dict:
    """该服务当前生效的覆盖字段（无覆盖时为空 dict）。"""
    with _lock:
        entry = _load()["services"].get(sid)
        if not isinstance(entry, dict):
            return {}
        flds = entry.get("fields")
        return dict(flds) if isinstance(flds, dict) else {}


def get(sid: str) -> dict:
    """完整条目 {"fields": {...}, "updated_at": ...}；无覆盖返回 {}。"""
    with _lock:
        entry = _load()["services"].get(sid)
        return copy.deepcopy(entry) if isinstance(entry, dict) else {}


def snapshot(sid: str) -> dict:
    """归档用快照（V2.0/V2.3：移除时随服务归档，恢复时带回）。"""
    return get(sid)


# ---------------------------------------------------------------- 应用与变更

def apply_fields(definition: dict, flds: dict) -> dict:
    """把覆盖字段应用到定义的副本上（覆盖层优先；未知路径忽略）。"""
    out = copy.deepcopy(definition)
    for path, value in (flds or {}).items():
        _set_path(out, path, value)
    return out


def apply(sid: str, definition: dict) -> dict:
    """`apply_fields(definition, fields(sid))`：注册合并入口。"""
    return apply_fields(definition, fields(sid))


def effective_fields(sid: str, accept=None) -> dict:
    """应用 accept_new 之后的生效覆盖（纯读：payload accept 清除在写盘前生效）。"""
    flds = fields(sid)
    if not accept or not flds:
        return flds
    if "*" in accept:
        return {}
    return {p: v for p, v in flds.items() if p not in set(accept)}


def plan(sid: str, accept=None, edited=(), final_definition: dict | None = None) -> bytes | None:
    """计算注册成功后的 overrides.json 新字节；无变化返回 None（不落盘）。

    accept：payload service.accept_new（["*"] 清全部）；edited：service.edited 路径列表，
    其最终值取自 final_definition（已应用覆盖、已合并既有清单的定义）。
    """
    with _lock:
        data = _load()
        entry = data["services"].get(sid)
        flds = dict(entry.get("fields") or {}) if isinstance(entry, dict) else {}
        had_entry = isinstance(entry, dict) and bool(flds)
        if accept:
            if "*" in accept:
                flds = {}
            else:
                for p in set(accept):
                    flds.pop(p, None)
        for p in edited or ():
            v = _get_path(final_definition or {}, p)
            if v is not None:
                flds[p] = v
        if flds:
            data["services"][sid] = {"fields": flds, "updated_at": _now()}
            changed = not had_entry or data["services"][sid]["fields"] != (entry or {}).get("fields")
        elif isinstance(entry, dict):
            del data["services"][sid]
            changed = True
        else:
            changed = False
        return _dump(data) if changed else None


def plan_drop(sid: str) -> bytes | None:
    """移除服务时清掉活跃覆盖层（快照已在归档里）。"""
    with _lock:
        data = _load()
        if sid in data["services"]:
            del data["services"][sid]
            return _dump(data)
        return None


def plan_drop_many(sids) -> bytes | None:
    """一次清掉多个服务的覆盖层（项目移除用：单文件一次写入）。"""
    with _lock:
        data = _load()
        hit = False
        for sid in sids:
            if sid in data["services"]:
                del data["services"][sid]
                hit = True
        return _dump(data) if hit else None


def plan_restore(sid: str, snap: dict) -> bytes | None:
    """恢复时带回归档的覆盖层（空快照清空条目）。"""
    with _lock:
        data = _load()
        flds = (snap or {}).get("fields") or {}
        if flds:
            data["services"][sid] = {"fields": dict(flds),
                                     "updated_at": (snap or {}).get("updated_at") or _now()}
        elif sid in data["services"]:
            del data["services"][sid]
        else:
            return None
        return _dump(data)


# ---------------------------------------------------------------- 即刻写入口

def record(sid: str, paths_, final_definition: dict) -> None:
    """记录用户编辑路径的最终值（等价 plan(edited=...)+commit）。"""
    bs = plan(sid, edited=paths_, final_definition=final_definition)
    if bs is not None:
        commit(bs)


def clear(sid: str, paths_=None) -> None:
    """清除指定覆盖（"*" 或路径列表；缺省全部）。"""
    accept = ["*"] if paths_ is None else (paths_ if isinstance(paths_, list) else [paths_])
    bs = plan(sid, accept=accept)
    if bs is not None:
        commit(bs)


def restore(sid: str, data: dict) -> None:
    bs = plan_restore(sid, data)
    if bs is not None:
        commit(bs)


def drop(sid: str) -> None:
    bs = plan_drop(sid)
    if bs is not None:
        commit(bs)
