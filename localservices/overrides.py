"""人工覆盖层（CONTRACTS V2.3）。

overrides.json = {"schemaVersion": 1, "services": {<sid>: {"fields": {<dotted path>: value},
"updated_at": ...}}}。路径形如 `name`、`description`、`open`（清单顶层）与
`components[<cid>].<field>[.<subfield>...]`（组件字段，支持任意嵌套，如
`components[api].ready.http`、`components[api].env.DB_HOST`）。

合并优先级（R06 修订）：**本次用户编辑（payload service.edited 列出的路径）> 旧人工覆盖
> 模型候选**——注册应用覆盖前先跳过本次 edited 的路径（用户编辑直接生效），注册成功后
把 edited 路径的最终值写回覆盖层（覆盖旧值）。payload `service.accept_new` 显式清除
指定覆盖。dry_run 的 diff 用同一合并计算，预览 = 实际。

本模块自带原子写与进程内锁；注册事务需要把 overrides.json 并入同一原子提交时，
用 `plan_*` 变体拿到新文件字节（不落盘），再由调用方随其余文件一起提交
（`commit` / Registry._atomic_commit）。纯函数（apply_fields/_get_path/_set_path）
与 GUI 共用同一嵌套路径契约。
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
# 嵌套路径的根容器（成员按 id/name 定位，不得凭空发明成员）
_SECTION_ROOTS = ("components", "dependencies")
_MISSING = object()
# 单段 token：`name`、`components[api]`、`[KEY]`（env 键等）
_TOKEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)?(?:\[([A-Za-z0-9_-]+)\])?$")


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

def _parse_path(path: str) -> list[tuple[str | None, str | None]] | None:
    """dotted path → [(name, index|None), ...]。

    `name` → [("name", None)]；`components[api].ready.http` →
    [("components","api"), ("ready",None), ("http",None)]。任一段非法 → None。
    """
    tokens: list[tuple[str | None, str | None]] = []
    for part in (path or "").split("."):
        m = _TOKEN_RE.match(part)
        if not m or (m.group(1) is None and m.group(2) is None):
            return None
        tokens.append((m.group(1), m.group(2)))
    return tokens or None


def _valid_path(path: str) -> bool:
    """顶层白名单字段，或 components/dependencies 下的任意嵌套路径（至少一层字段）。"""
    if path in TOP_FIELDS:
        return True
    toks = _parse_path(path or "")
    if not toks or len(toks) < 2:
        return False
    name, idx = toks[0]
    return idx is not None and name in _SECTION_ROOTS


def _descend(cur, name: str | None, idx: str | None):
    """按 token 下潜一层；list 按 id/name 定位成员，dict 按键取值。缺路 → _MISSING。"""
    if name is not None:
        if not isinstance(cur, dict) or name not in cur:
            return _MISSING
        cur = cur[name]
    if idx is not None:
        if isinstance(cur, dict):
            return cur[idx] if idx in cur else _MISSING
        if isinstance(cur, list):
            for item in cur:
                if isinstance(item, dict) and idx in (item.get("id"), item.get("name")):
                    return item
        return _MISSING
    return cur


def _descend_set(cur, name: str | None, idx: str | None):
    """set 路径的下潜：缺失的 dict 中间层补建；components/dependencies 成员不得凭空发明。"""
    if name is not None:
        if not isinstance(cur, dict):
            return _MISSING
        if name not in cur:
            if name in _SECTION_ROOTS:
                return _MISSING
            cur[name] = {}
        cur = cur[name]
    if idx is not None:
        if isinstance(cur, dict):
            if idx not in cur:
                cur[idx] = {}
            return cur[idx]
        if isinstance(cur, list):
            for item in cur:
                if isinstance(item, dict) and idx in (item.get("id"), item.get("name")):
                    return item
        return _MISSING
    return cur


def _get_path(definition: dict, path: str):
    """从清单层定义取 dotted path（任意嵌套）的当前值；路径不存在返回 None。"""
    toks = _parse_path(path)
    if not toks:
        return None
    cur = definition
    for name, idx in toks:
        cur = _descend(cur, name, idx)
        if cur is _MISSING:
            return None
    return cur


def _set_path(definition: dict, path: str, value) -> bool:
    """把值写入定义的 dotted path（任意嵌套）；未知路径/缺失成员返回 False（忽略，不报错）。"""
    toks = _parse_path(path) if _valid_path(path) else None
    if not toks:
        return False
    cur = definition
    for name, idx in toks[:-1]:
        cur = _descend_set(cur, name, idx)
        if cur is _MISSING:
            return False
    name, idx = toks[-1]
    if name is not None and idx is not None:
        # `env[KEY]` 形式的最后一段：定位/补建 name 容器后按键写值
        if not isinstance(cur, dict):
            return False
        child = cur.get(name)
        if not isinstance(child, dict):
            if name in cur:
                return False  # 已有标量：不覆盖成容器
            child = cur[name] = {}
        child[idx] = value
        return True
    if name is not None:
        if not isinstance(cur, dict):
            return False
        cur[name] = value
        return True
    if idx is not None and isinstance(cur, dict):
        cur[idx] = value
        return True
    return False  # list 成员不得整体替换


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
    """把覆盖字段应用到定义的副本上（覆盖层优先；未知路径忽略；支持嵌套 dotted path）。"""
    out = copy.deepcopy(definition)
    for path, value in (flds or {}).items():
        _set_path(out, path, value)
    return out


def apply(sid: str, definition: dict) -> dict:
    """`apply_fields(definition, fields(sid))`：注册合并入口。"""
    return apply_fields(definition, fields(sid))


def effective_fields(sid: str, accept=None, edited=()) -> dict:
    """应用 accept_new 与**本次 edited 路径旁路**之后的生效覆盖（纯读）。

    合并优先级（R06）：本次用户编辑（edited）> 旧人工覆盖 > 模型候选——
    edited 列出的路径本次直接用 payload 值，旧覆盖不参与比较/写盘；
    随后 `plan(edited=...)` 会把这些路径的最终值写回覆盖层（覆盖旧值）。
    """
    flds = fields(sid)
    if not flds:
        return {}
    if accept:
        if "*" in accept:
            return {}
        flds = {p: v for p, v in flds.items() if p not in set(accept)}
    if edited:
        flds = {p: v for p, v in flds.items() if p not in set(edited)}
    return flds


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


def plan_restore_many(items) -> bytes | None:
    """整项目恢复：把多个成员的归档覆盖层**合并进同一份** overrides.json（R03）。

    一次事务只能写同一目标路径一次——逐成员 plan_restore 会把 overrides.json
    重复加入 _atomic_commit；这里一次累计全部变更，单次写入。无变化返回 None。
    """
    with _lock:
        data = _load()
        changed = False
        for sid, snap in items:
            flds = (snap or {}).get("fields") or {}
            if flds:
                data["services"][sid] = {"fields": dict(flds),
                                         "updated_at": (snap or {}).get("updated_at") or _now()}
                changed = True
            elif sid in data["services"]:
                del data["services"][sid]
                changed = True
        return _dump(data) if changed else None


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
