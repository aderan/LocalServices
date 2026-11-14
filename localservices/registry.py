"""工作台注册权威（CONTRACTS §1/§2/§7）：Project/Directory/收藏、服务 TOML 写入、草稿与迁移。

- registry.json（{"schemaVersion":1,"revision":N,"projects":[...]}）是工作台结构唯一权威；
  服务定义仍是 TOML，注册写入 user_services_dir()（与内置 services/ 同 schema）。
- 一切落盘位置由 paths.py 提供，测试用 LOCALSERVICES_* 重定向。
- 并发模型：进程内 threading.RLock + 进程间 fcntl.flock(<home>/registry.lock)；
  每次修改都在锁内重读磁盘再合并保存（daemon 与 CLI 可能同时写），写入一律 tmp + os.replace。
- 注册校验全部通过后才落盘：任何失败保证 registry.json 与 services 目录字节不变。
"""

from __future__ import annotations

import copy
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import tomllib
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from . import manifest, overrides, paths, resources, tombstones

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COMPONENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
AGENTS = ("codex", "claude", "none")
REGISTRANTS = ("gui", "cli", "skill")
DISPOSITIONS = ("stop", "keep-external")
INTENTS = ("stopped", "running")

SERVICE_KEYS = {"id", "name", "description", "source", "open", "autostart", "adapter",
                "observe_only", "draft", "project_hint", "options", "components", "dependencies", "evidence",
                # V3 公开入口（进 TOML）
                "endpoints",
                # V2.3 覆盖层 / V2.7 compose 确认（payload-only，不进 TOML）
                "edited", "accept_new", "confirmed_provision"}
COMP_KEYS = {"id", "kind", "name", "cmd", "shell_script", "cwd", "env", "env_files", "port", "ready",
             "required", "start_timeout", "after", "hot", "watch", "restart_on", "ignore", "build",
             "install", "install_when", "install_marker", "auto_update", "external_cmd",
             "external_cwd", "external_cwd_exact", "debounce",
             # V2.7 运行适配（manifest v2 向后兼容字段）
             "run", "compose_service", "compose_project", "compose_files", "provision"}
READY_KEYS = {"http", "tcp", "json_ok", "timeout", "interval"}
DEP_KEYS = {"name", "probe", "affects", "managed", "note", "required", "container", "volume", "resource"}
PROBE_KEYS = {"http", "tcp", "json_ok", "timeout", "interval"}
COMPONENT_KINDS = ("server", "worker", "generator")

# TOML 输出时组件/依赖字段的固定顺序（只写显式提供的字段，缺省由加载端补齐）
_COMP_ORDER = ["id", "kind", "name", "cwd", "cmd", "env", "env_files", "port", "ready", "required",
               "start_timeout", "after", "hot", "watch", "restart_on", "ignore", "build", "install",
               "install_when", "install_marker", "auto_update", "external_cmd", "external_cwd",
               "external_cwd_exact", "debounce", "run", "compose_service", "compose_project",
               "compose_files", "provision"]
_DEP_ORDER = ["name", "probe", "affects", "managed", "note", "required", "container", "volume"]
_ENDPOINT_ORDER = ["id", "host", "component", "open_path"]

_lock = threading.RLock()


class RegistryError(Exception):
    """可映射为 HTTP 状态的错误（404/400/409/500 等）。"""

    def __init__(self, message: str, status: int = 400, details: list[str] | None = None, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message}
        if details:
            self.payload["details"] = details
        self.payload.update(extra)


# ---------------------------------------------------------------- TOML emitter
# 标准库只有 tomllib（只读）；这里实现一个只覆盖清单所需类型的小 emitter：
# str/int/float/bool/list[str]/list[dict]/dict（内联表），产出必须能被 tomllib 原样解析。

def _toml_str(v: str) -> str:
    out = (v.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    out = "".join(ch if ch >= " " else f"\\u{ord(ch):04x}" for ch in out)
    return f'"{out}"'


def _toml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    raise ValueError(f"unsupported TOML scalar type: {type(v).__name__}")


def _toml_array(values: list) -> str:
    return "[" + ", ".join(_toml_scalar(v) for v in values) + "]"


def _toml_inline(d: dict) -> str:
    return "{ " + ", ".join(f"{k} = {_toml_scalar(v)}" for k, v in d.items() if v is not None) + " }"


def dumps_toml(data: dict) -> str:
    """清单用 TOML 序列化：标量/内联表逐行，list[dict] 输出为 [[section]] 表数组。"""
    head: list[str] = []
    sections: list[tuple[str, list]] = []
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            sections.append((k, v))
        elif isinstance(v, list):
            head.append(f"{k} = {_toml_array(v)}")
        elif isinstance(v, dict):
            head.append(f"{k} = {_toml_inline(v)}")
        else:
            head.append(f"{k} = {_toml_scalar(v)}")
    blocks = ["\n".join(head)] if head else []
    for name, rows in sections:
        for row in rows:
            items = []
            order = [k for k in _order_for(name) if k in row] + [k for k in row if k not in _order_for(name)]
            for k in order:
                v = row[k]
                if v is None:
                    continue
                if isinstance(v, list):
                    items.append(f"{k} = {_toml_array(v)}")
                elif isinstance(v, dict):
                    items.append(f"{k} = {_toml_inline(v)}")
                else:
                    items.append(f"{k} = {_toml_scalar(v)}")
            blocks.append(f"[[{name}]]\n" + "\n".join(items))
    return "\n\n".join(blocks) + "\n"


def _order_for(section: str) -> list[str]:
    return _COMP_ORDER if section == "components" else _DEP_ORDER


# ---------------------------------------------------------------- 通用小工具

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _dir_id(path: str) -> str:
    return "d-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:6]


def _resolved(p) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


def _inside(child: Path, root: Path) -> bool:
    if child == root:
        return True
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _current_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _quiet_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _restore_inplace(path: Path, data: bytes) -> None:
    """恢复已替换文件的原字节：r+b truncate+write+fsync，不新分配空间（V2.4）。"""
    try:
        with open(path, "r+b") as f:
            f.truncate(0)
            f.seek(0)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # 尽力而为：调用方仍会抛出原始异常


def _atomic_commit(writes: list[tuple[Path, bytes]], after=None) -> None:
    """多文件事务提交（V2.4）：全部临时文件写好+fsync（经由 _write_bytes_atomic，
    保持单一失败注入接缝）→ 统一 rename → 执行 after 回调（如 registry _save）。
    任一阶段失败：已 rename 的文件用**原地写回**恢复原字节（新建文件删除），
    未 rename 的临时文件清理，原始异常向上抛——持续 I/O 故障下保持一致或整体失败。
    同一目标路径出现两次 = 调用方编程错误（第二次 rename 必然 FileNotFoundError），
    在任何写盘之前拒绝（R03）。"""
    writes = list(writes)
    seen: set[Path] = set()
    for path, _data in writes:
        if path in seen:
            raise RuntimeError(
                f"_atomic_commit 检测到重复目标路径 {path}：同一事务内每个文件只能写一次；"
                "多服务变更需先在内存合并出整份文件（如 overrides.plan_restore_many）")
        seen.add(path)
    staged: list[tuple[Path, Path, bytes | None]] = []
    try:
        for path, data in writes:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            _write_bytes_atomic(tmp, data)
            staged.append((path, tmp, _current_bytes(path)))
    except BaseException:
        for _p, tmp, _o in staged:
            _quiet_unlink(tmp)
        raise
    replaced: list[tuple[Path, bytes | None]] = []
    try:
        for path, tmp, orig in staged:
            os.replace(tmp, path)
            replaced.append((path, orig))
        if after is not None:
            after()
    except BaseException:
        for path, orig in replaced:
            if orig is None:
                _quiet_unlink(path)
            else:
                _restore_inplace(path, orig)
        for _p, tmp, _o in staged:
            _quiet_unlink(tmp)
        raise


@contextmanager
def _file_lock():
    """进程内 RLock + 进程间 flock；锁内重读磁盘是调用方的责任。"""
    with _lock:
        lockfile = paths.home() / "registry.lock"
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        with open(lockfile, "a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _diff_definitions(old: dict, new: dict) -> list[dict]:
    """字段级差异（dry_run 用）：顶层标量 + 按组件 id / 依赖 name 对齐的明细，old/new 原样呈现。"""
    out: list[dict] = []
    for k in ("name", "description", "source", "open", "autostart", "observe_only", "adapter", "options"):
        if old.get(k) != new.get(k) and not (old.get(k) is None and new.get(k) is None):
            out.append({"field": k, "old": old.get(k), "new": new.get(k)})
    oldc = {c.get("id"): c for c in old.get("components", []) if isinstance(c, dict)}
    newc = {c.get("id"): c for c in new.get("components", []) if isinstance(c, dict)}
    for cid in sorted(set(oldc) | set(newc), key=str):
        if cid not in oldc:
            out.append({"field": f"components[{cid}]", "old": None, "new": "新增组件"})
        elif cid not in newc:
            out.append({"field": f"components[{cid}]", "old": "（移除）", "new": None})
        else:
            for f in sorted(set(oldc[cid]) | set(newc[cid])):
                ov, nv = oldc[cid].get(f), newc[cid].get(f)
                if ov != nv and not (ov is None and nv is None):
                    out.append({"field": f"components[{cid}].{f}", "old": ov, "new": nv})
    # V3：endpoints 按 host 对齐
    olde = {e.get("host"): e for e in old.get("endpoints", []) if isinstance(e, dict)}
    newe = {e.get("host"): e for e in new.get("endpoints", []) if isinstance(e, dict)}
    for h in sorted(set(olde) | set(newe), key=str):
        if h not in olde:
            out.append({"field": f"endpoints[{h}]", "old": None, "new": "新增公开入口"})
        elif h not in newe:
            out.append({"field": f"endpoints[{h}]", "old": "（移除）", "new": None})
        else:
            for f in ("component", "open_path"):
                ov, nv = olde[h].get(f), newe[h].get(f)
                if ov != nv and not (ov is None and nv is None):
                    out.append({"field": f"endpoints[{h}].{f}", "old": ov, "new": nv})
    oldd = {d.get("name"): d for d in old.get("dependencies", []) if isinstance(d, dict)}
    newd = {d.get("name"): d for d in new.get("dependencies", []) if isinstance(d, dict)}
    for dn in sorted(set(oldd) | set(newd), key=str):
        if dn not in oldd:
            out.append({"field": f"dependencies[{dn}]", "old": None, "new": "新增依赖"})
        elif dn not in newd:
            out.append({"field": f"dependencies[{dn}]", "old": "（移除）", "new": None})
        elif oldd[dn] != newd[dn]:
            out.append({"field": f"dependencies[{dn}]", "old": oldd[dn], "new": newd[dn]})
    return out


def _identity(source: Path, comps) -> tuple:
    """注册身份 = resolve(source) + 排序后 cwd 集合 + 排序后 cmd argv 集合（CONTRACTS §2）。"""
    cwds = sorted(str((source / (c.cwd or ".")).resolve()) for c in comps)
    cmds = sorted(tuple(c.cmd) for c in comps)
    return (str(source), tuple(cwds), tuple(cmds))


def _find(data: dict, pid: str) -> dict | None:
    for p in data.get("projects", []):
        if p.get("id") == pid:
            return p
    return None


def _revision_gate(proj: dict | None, revision: int) -> dict | None:
    if proj is not None and revision != proj.get("revision", 0):
        return {"ok": False, "error": "revision conflict",
                "current_revision": proj.get("revision", 0), "details": []}
    return None


def _new_project(pid: str, name: str, origin: str = "user") -> dict:
    now = _now()
    return {"id": pid, "name": name, "origin": origin, "directories": [], "services": [],
            "favorite": False, "autostart": False, "agent": None,
            "created_at": now, "updated_at": now, "revision": 0}


_COMP_FIELDS = ("id", "kind", "name", "cmd", "cwd", "env", "env_files", "port", "ready", "required",
                "start_timeout", "after", "hot", "watch", "restart_on", "ignore", "build", "install",
                "install_when", "install_marker", "auto_update", "external_cmd", "external_cwd",
                "external_cwd_exact", "debounce", "run", "compose_service", "compose_project",
                "compose_files", "provision")
_DEP_FIELDS = ("name", "probe", "affects", "managed", "note", "required", "container", "volume")


def _comp_key(c, source: Path) -> tuple:
    """内容等价比较键：cwd 归一为 resolve 后绝对路径，list/dict 转为可比对的元组。"""
    vals = []
    for f in _COMP_FIELDS:
        v = getattr(c, f)
        if f == "cwd":
            p = Path(v or ".")
            v = str((p if p.is_absolute() else source / p).resolve())
        elif isinstance(v, list):
            v = tuple(v)
        elif isinstance(v, dict):
            v = tuple(sorted((k, tuple(x) if isinstance(x, list) else x) for k, x in v.items()))
        elif v is None:
            v = ""
        vals.append(v)
    return tuple(vals)


def _dep_key(d) -> tuple:
    vals = []
    for f in _DEP_FIELDS:
        v = getattr(d, f)
        if isinstance(v, list):
            v = tuple(v)
        elif v is None:
            v = ""
        vals.append(v)
    return tuple(vals)


def _service_key(source: Path, d: dict) -> tuple:
    """从 TOML 层 dict 计算内容等价键（写入端与既有清单端共用同一归一规则）。"""
    comps = sorted((manifest._component(dict(c)) for c in d.get("components", [])),
                   key=lambda c: c.id)
    deps = sorted((manifest.Dependency(**{**dict(x), "probe": manifest.Probe(**x["probe"])})
                   for x in d.get("dependencies", [])), key=lambda x: x.name)
    return (
        d.get("name", ""), d.get("description", ""), str(_resolved(d.get("source", "."))),
        d.get("open") or "", bool(d.get("autostart", False)), bool(d.get("observe_only", False)),
        d.get("adapter") or "",
        tuple(sorted(_comp_key(c, source) for c in comps)),
        tuple(sorted(_dep_key(x) for x in deps)),
        # V3：endpoints 变化是真实内容变化（域名/落点/组件引用），不能被判为 unchanged
        tuple(sorted((e.get("host"), e.get("component"), e.get("open_path"))
                     for e in _endpoint_rows(d))),
    )


def _endpoint_rows(d: dict) -> list[dict]:
    """等价键用 endpoints 行（TOML dict 与 Project 序列化结果同形）。"""
    raw = d.get("endpoints")
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _insert_project_line(text: str, pid: str) -> str:
    """在第一个 [[section]] 之前插入顶层 project= 键（追加到文件尾会落入最后一个表）。"""
    line = f'project = "{pid}"\n'
    lines = text.splitlines(keepends=True)
    for i, l in enumerate(lines):
        if l.lstrip().startswith("[["):
            if i and not lines[i - 1].strip():
                return "".join(lines[:i - 1]) + line + "".join(lines[i - 1:])
            return "".join(lines[:i]) + line + "\n" + "".join(lines[i:])
    return text + ("" if text.endswith("\n") else "\n") + line


_PROJECT_LINE_RE = re.compile(r'^project\s*=\s*"[^"]*"\s*(?:#.*)?$')


def _set_project_field(text: str, pid: str) -> str:
    """把清单 TOML 顶层 project = 值改写为目标项目（R10：成员表与 TOML 归属必须一致）。

    已有键只替换该行的值（其余行字节不动）；没有键则按 _insert_project_line 插入。
    只认第一个 [[section]] 之前的顶层键，避免误改组件段内的同名字段。
    """
    replacement = f'project = "{pid}"\n'
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    replaced = False
    in_head = True
    for l in lines:
        if in_head and not replaced and _PROJECT_LINE_RE.match(l):
            out.append(replacement)
            replaced = True
            continue
        if l.lstrip().startswith("["):
            in_head = False  # 顶层区域结束：不再匹配 project=，避免误改段内同名字段
        out.append(l)
    if replaced:
        return "".join(out)
    return _insert_project_line(text, pid)


# ---------------------------------------------------------------- Registry

def _archived_endpoint_hosts(sid: str, parsed: dict) -> set[str]:
    """归档定义将生效的公开域名（显式 endpoints，否则按稳定 id 派生）；恢复预检用。"""
    proj = manifest.Project(
        id=str(parsed.get("id") or sid), name=str(parsed.get("name") or sid),
        source=Path(os.path.expanduser(str(parsed.get("source") or "/tmp"))),
        open=parsed.get("open"),
        components=[manifest._component(dict(c)) for c in parsed.get("components", []) or []])
    return {e.host for e in manifest._derive_endpoints(proj, parsed.get("endpoints", []) or [])}


class Registry:
    """工作台注册权威。实例无内部缓存（每次读盘），单例与多实例同样安全。

    registry.json 缺失即视为空注册表（惰性加载）。"""

    # ---------- 读取 ----------

    def _load(self) -> dict:
        path = paths.registry_file()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = None  # 惰性空库（首次运行）
        except OSError as e:
            # 权限等读失败：fail-closed——绝不当空库（空库会随后续保存无备份覆盖真实记录）
            raise RegistryError(f"registry.json 不可读（{e}），拒绝按空注册表处理：{path}",
                                status=500) from e
        corrupt = False
        if raw is None:
            data = {}
        else:
            try:
                data = json.loads(raw)
            except ValueError:
                corrupt = True
                data = {}
            else:
                if not isinstance(data, dict):
                    corrupt = True  # 合法 JSON 但非对象（如数组）：与损坏同路径
                    data = {}
        if corrupt:
            # 损坏文件不能当空库覆盖：先整体备份，再从空开始（原记录保留在备份里）
            backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                os.replace(path, backup)
                print(f"[registry] registry.json 损坏（非 JSON 对象），已备份到 {backup}",
                      file=sys.stderr)
            except OSError as e:
                # 备份失败绝不能继续当空库（否则下次写入覆盖原始记录）
                raise RegistryError(f"registry.json 损坏且备份失败（{e}），请先人工处理 {path}") from e
        data.setdefault("schemaVersion", SCHEMA_VERSION)
        data.setdefault("revision", 0)
        data.setdefault("projects", [])
        return data

    def _save(self, data: dict) -> None:
        data["schemaVersion"] = SCHEMA_VERSION
        _write_bytes_atomic(paths.registry_file(),
                            json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def data(self) -> dict:
        """全量只读快照。读失败（fail-closed）时返回带 error 的标记对象——
        绝不伪装成空注册表（空 projects 会让调用方误判并覆盖真实记录）。"""
        try:
            return copy.deepcopy(self._load())
        except RegistryError as e:
            return {"ok": False, "error": e.payload.get("error", "registry.json 不可读")}

    def projects(self) -> list[dict]:
        return copy.deepcopy(self._load()["projects"])

    def project(self, pid: str) -> dict | None:
        p = _find(self._load(), pid)
        return copy.deepcopy(p) if p else None

    # ---------- Project / Directory ----------

    def ensure_project(self, pid: str | None, name: str, directories: list[dict] | None,
                       agent: str | None, origin: str = "user", autostart: bool | None = None,
                       favorite: bool | None = None) -> tuple[dict, bool]:
        """创建或更新工作台；返回 (project, created)。"""
        with _file_lock():
            data = self._load()
            derived = pid or _slug(name or "")
            if not derived or not ID_RE.fullmatch(derived):
                raise RegistryError(f"项目 id 非法：{derived!r}")
            proj = _find(data, derived)
            if proj is None and (not isinstance(name, str) or not name.strip()):
                # 仅创建时要求 name；更新已有项目允许缺省（GUI 开关只发 {id, autostart} 等）
                raise RegistryError("项目 name 不能为空")
            if agent is not None and agent not in AGENTS:
                raise RegistryError(f"agent 必须是 {'/'.join(AGENTS)}")
            for flag, key in ((autostart, "autostart"), (favorite, "favorite")):
                if flag is not None and not isinstance(flag, bool):
                    raise RegistryError(f"{key} 必须是布尔值")
            dirs = _norm_directories(directories)
            created = proj is None
            changed = created
            if created:
                proj = _new_project(derived, name.strip(), origin=origin)
                data["projects"].append(proj)
            else:
                if isinstance(name, str) and name.strip() and proj.get("name") != name.strip():
                    proj["name"] = name.strip()
                    changed = True
                if agent and proj.get("agent") != agent:
                    proj["agent"] = agent
                    changed = True
                if autostart is not None and proj.get("autostart") != autostart:
                    proj["autostart"] = autostart
                    changed = True
                if favorite is not None and proj.get("favorite") != favorite:
                    proj["favorite"] = favorite
                    changed = True
            for rp, role in dirs:
                changed |= _add_directory_entry(proj, rp, role)
            if changed:
                proj["revision"] = proj.get("revision", 0) + 1
                proj["updated_at"] = _now()
                data["revision"] = data.get("revision", 0) + 1
                self._save(data)
            return copy.deepcopy(proj), created

    def rename_project(self, pid: str, name: str) -> dict:
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            if not isinstance(name, str) or not name.strip():
                raise RegistryError("项目 name 不能为空")
            proj["name"] = name.strip()
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1
            self._save(data)
            return copy.deepcopy(proj)

    def add_directory(self, pid: str, path: str, role: str = "source") -> dict:
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            rp = str(_resolved(path))
            if not Path(rp).is_dir():
                raise RegistryError(f"目录不存在：{path}", details=[str(path)])
            if not _add_directory_entry(proj, rp, role):
                return copy.deepcopy(proj)  # 符号链接 resolve 后已登记：幂等
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1
            self._save(data)
            return copy.deepcopy(proj)

    def remove_directory(self, pid: str, dir_id: str) -> dict:
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            dirs = proj.get("directories", [])
            hit = next((d for d in dirs if d.get("id") == dir_id), None)
            if hit is None:
                raise RegistryError(f"未知目录 {dir_id}", status=404)
            dirs.remove(hit)  # 解除关联不删文件
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1
            self._save(data)
            return copy.deepcopy(proj)

    def set_favorite(self, pid: str, value: bool) -> dict:
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            proj["favorite"] = bool(value)
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1
            self._save(data)
            return copy.deepcopy(proj)

    def set_autostart(self, pid: str, value: bool) -> dict:
        """V2.9 专用端点：只切换 autostart，不需要 name。"""
        if not isinstance(value, bool):
            raise RegistryError("value 必须是布尔值")
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            proj["autostart"] = value
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1
            self._save(data)
            return copy.deepcopy(proj)

    def autostart_map(self) -> dict[str, bool]:
        """{pid: autostart} 只读助手（V2.9：Runtime 引擎 Store.intent 初始缺省用）。"""
        return {p.get("id"): bool(p.get("autostart", False))
                for p in self._load().get("projects", []) if p.get("id")}

    # ---------- 草稿 ----------

    def save_draft(self, payload: dict) -> dict:
        draft_id = "d-" + secrets.token_hex(4)
        rec = {"id": draft_id, "saved_at": _now(), "payload": payload}
        _write_bytes_atomic(paths.drafts_dir() / f"{draft_id}.json",
                            json.dumps(rec, ensure_ascii=False, indent=1).encode("utf-8"))
        svc = payload.get("service") if isinstance(payload.get("service"), dict) else {}
        return {"ok": True, "action": "draft", "draft_id": draft_id,
                "project_id": (payload.get("project") or {}).get("id") if isinstance(payload.get("project"), dict) else None,
                "service_id": svc.get("id"), "draft_id": draft_id,
                "validation": {"warnings": []}}

    def drafts(self) -> list[dict]:
        out = []
        for f in sorted(paths.drafts_dir().glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        out.sort(key=lambda d: d.get("saved_at", ""), reverse=True)
        return out

    def draft(self, draft_id: str) -> dict | None:
        if not DRAFT_ID_RE.fullmatch(draft_id or ""):
            return None
        try:
            return json.loads((paths.drafts_dir() / f"{draft_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def delete_draft(self, draft_id: str) -> dict:
        if not DRAFT_ID_RE.fullmatch(draft_id or ""):
            raise RegistryError(f"非法草稿 id {draft_id!r}")
        f = paths.drafts_dir() / f"{draft_id}.json"
        if not f.exists():
            raise RegistryError(f"未知草稿 {draft_id}", status=404)
        f.unlink()
        return {"ok": True, "draft_id": draft_id}

    # ---------- 注册（CONTRACTS §2） ----------

    def register_service(self, payload: dict, dry_run: bool = False, *, trial=None) -> dict:
        try:
            return self._register(payload, dry_run=dry_run, trial=trial)
        except RegistryError as e:
            out = dict(e.payload)
            out.setdefault("details", [])
            return out

    def _register(self, payload: dict, dry_run: bool = False, *, trial=None) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload 必须是对象", "details": []}
        if payload.get("schemaVersion") != 1:
            return {"ok": False, "error": "schemaVersion 必须为 1", "details": []}
        revision = payload.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return {"ok": False, "error": "revision 必须为非负整数", "details": []}
        svc = payload.get("service")
        if not isinstance(svc, dict):
            return {"ok": False, "error": "service 必须是对象", "details": []}
        unknown = set(svc) - SERVICE_KEYS
        if unknown:
            return {"ok": False, "error": f"service 含未知字段：{sorted(unknown)}", "details": []}
        for key in ("edited", "accept_new"):  # V2.3 payload-only 字段
            v = svc.get(key)
            if v is not None and not _is_str_list(v):
                return {"ok": False, "error": f"service.{key} 必须是字符串数组", "details": []}
        if svc.get("confirmed_provision") is not None and not isinstance(svc.get("confirmed_provision"), bool):
            return {"ok": False, "error": "service.confirmed_provision 必须是布尔值", "details": []}

        comps_in = svc.get("components")
        comps_in = comps_in if isinstance(comps_in, list) else []
        incomplete = (bool(svc.get("draft"))
                      or not isinstance(svc.get("name"), str) or not svc.get("name", "").strip()
                      or not isinstance(svc.get("source"), str) or not svc.get("source", "").strip()
                      or not comps_in
                      or not all(_component_complete(c) for c in comps_in))
        if incomplete:
            if trial is not None:
                raise RegistryError("候选定义不完整，无法试启动", details=_missing_fields(svc, comps_in))
            if dry_run:
                # V2.4：dry_run 零写入——草稿只由非 dry_run 请求产生
                return {"ok": True, "action": "incomplete",
                        "missing": _missing_fields(svc, comps_in), "draft_id": None}
            return self.save_draft(payload)  # 草稿绝不进 user_services_dir

        warnings: list[str] = []
        norm = self._validate(payload, warnings)  # 全部校验通过才继续
        sid, pid, src, toml = (norm["sid"], norm["pid"], norm["src"], norm["toml"])

        with _file_lock():
            data = self._load()
            try:
                loaded = manifest.load_all()
            except Exception as e:  # 现有清单坏：宁可拒绝也不叠加写入
                return {"ok": False, "error": f"无法加载现有清单：{e}", "details": []}

            # V2.3（R06 修订）：合并优先级 = 本次用户编辑（edited 路径）> 旧人工覆盖 > 模型候选。
            # 覆盖层先于比较/写盘生效，但 edited 列出的路径旁路旧覆盖（本次 payload 值直接生效）；
            # 成功后 plan(edited=...) 把最终值写回覆盖层。accept_new 在此清除指定覆盖 → dry_run diff = 实际
            eff_fields = overrides.effective_fields(sid, svc.get("accept_new"), svc.get("edited"))
            toml = overrides.apply_fields(toml, eff_fields)
            comp_objs = [manifest._component(dict(c)) for c in toml.get("components", [])]

            existing = loaded.get(sid)
            proj = _find(data, pid)
            ident = _identity(src, comp_objs)
            identity_changed, identity_note = False, None

            # 身份判定先行：unchanged / duplicate 都不落盘、不检查端口与 revision
            if existing is not None:
                owner_pid = existing.project or next(
                    (x["id"] for x in data.get("projects", []) if sid in x.get("services", [])), None)
                if owner_pid and owner_pid != pid:
                    return {"ok": False, "error": f"服务 id {sid} 已属于项目 {owner_pid}", "details": []}
                ex_src = _resolved(existing.source)
                identity_changed = (str(src) != str(ex_src)
                                    or {str((src / (c.cwd or ".")).resolve()) for c in comp_objs}
                                    != {str((ex_src / (c.cwd or ".")).resolve())
                                        for c in existing.components})
                identity_note = ("注册身份变化（source 或组件 cwd 集合不同），已按 revision 门控执行更新"
                                 if identity_changed else None)
                toml["registered"] = norm["registered"]
                final_def = (self._merge_existing(Path(existing.file), toml)
                             if existing.file else toml)
                # 比较对象 = 实际将写盘的合并定义（merge 保留未提供的顶层键，预览 = 实际）
                if trial is None and ident == _identity(ex_src, existing.components) and self._content_equal(final_def, existing):
                    if dry_run:
                        return {"ok": True, "action": "diff", "registration_action": "unchanged",
                                "service_id": sid, "project_id": pid,
                                "revision": (proj or {}).get("revision", 0), "diff": [],
                                "identity_changed": identity_changed, "identity_note": identity_note,
                                "draft_id": None, "validation": {"warnings": warnings}}
                    return _ok("unchanged", pid, sid, (proj or {}).get("revision", 0), warnings)
                gate = _revision_gate(proj, revision)
                if gate:
                    return gate
                action = "updated"
            else:
                dup = None
                for lp in loaded.values():
                    if lp.id == sid:
                        continue
                    in_proj = bool(proj and lp.id in proj.get("services", [])) or lp.project == pid
                    if in_proj and _identity(_resolved(lp.source), lp.components) == ident:
                        dup = lp.id
                        break
                if dup and trial is not None:
                    raise RegistryError(f"已有相同服务 {dup}，请在原服务上运行验证")
                if dup:
                    if dry_run:
                        # V2.4：dry_run 与实际一致——返回 duplicate 动作 + 字段级 diff，不写盘
                        old_def = {}
                        try:
                            old_def = tomllib.loads(Path(loaded[dup].file).read_text(encoding="utf-8"))
                        except (OSError, ValueError, TypeError):
                            old_def = {}
                        return {"ok": True, "action": "duplicate", "service_id": dup, "project_id": pid,
                                "revision": (proj or {}).get("revision", 0),
                                "diff": _diff_definitions(old_def, toml),
                                "draft_id": None, "validation": {"warnings": warnings}}
                    return _ok("duplicate", pid, dup, (proj or {}).get("revision", 0), warnings)
                gate = _revision_gate(proj, revision)
                if gate:
                    return gate
                action = "created"
                toml["registered"] = norm["registered"]
                final_def = toml

            # R5（review）：派生声明保真。既有服务无显式入口时，payload 原样带回的入口
            # （toml["endpoints"]，_validate 规范化结果）若与「按稳定规则派生」完全一致
            # （GUI 编辑往返：只改了 description 等无关字段），不固化为显式 [[endpoints]]
            # ——custom 域名形式下的路由资格不能被无关编辑改变。与派生不一致（用户真正
            # 编辑过 host/组件/落点）→ 照常写显式；既有显式入口不走此分支。dry_run diff
            # 在此之后计算，预览 = 实际。
            ep_payload = toml.get("endpoints")
            if (existing is not None and ep_payload is not None
                    and not any(not e.derived for e in existing.endpoints)):
                probe = {k: v for k, v in final_def.items() if k != "endpoints"}
                try:
                    derived_rows = manifest.endpoint_rows(probe)
                except Exception:  # noqa: BLE001 — 派生失败（如无端口组件）按显式处理
                    derived_rows = None
                if derived_rows is not None and [
                    {"id": e.get("id"), "host": e.get("host"), "component": e.get("component"),
                     "open_path": e.get("open_path")} for e in ep_payload
                ] == derived_rows:
                    final_def.pop("endpoints", None)

            # 端口冲突（锁内重读后的最新状态；更新自身端口除外）
            owners: dict[int, str] = {}
            for lp in loaded.values():
                for lc in lp.managed_components():
                    if lc.port:
                        owners[lc.port] = f"{lp.id}.{lc.id}"
            for cobj in comp_objs:
                if cobj.kind == "generator" or not cobj.port:
                    continue
                owner = owners.get(cobj.port)
                if owner and owner.split(".", 1)[0] != sid:
                    return {"ok": False, "error": f"端口 {cobj.port} 已被占用",
                            "details": [f"{owner} 与 {sid}.{cobj.id}"]}

            # V3 域名冲突（锁内重读后的最新状态；以实际将写盘的定义计算，预览 = 实际）
            host_owner: dict[str, str] = {}
            for lp in loaded.values():
                if lp.id == sid:
                    continue
                for ep in lp.endpoints:
                    host_owner[ep.host] = lp.id
            try:
                cand_rows = manifest.endpoint_rows(final_def)
            except ValueError as e:
                return {"ok": False, "error": str(e), "details": []}
            if any(r.get("__invalid__") for r in cand_rows):
                return {"ok": False, "error": "endpoints 定义无效（未知组件/重复域名）", "details": []}
            clash = sorted({str(r.get("host")) for r in cand_rows} & set(host_owner))
            if clash:
                return {"ok": False,
                        "error": "域名已被占用：" + "；".join(f"{h}（服务 {host_owner[h]}）" for h in clash),
                        "details": clash}

            # ---- 校验完成，开始落盘 ----
            existing_file = Path(existing.file) if existing is not None and existing.file else None
            builtin = (existing_file is not None
                       and existing_file.parent.resolve() == paths.services_dir().resolve())
            target = (existing_file if existing_file is not None and not builtin
                      else paths.user_services_dir() / f"{sid}.toml")
            if target != existing_file and target.exists():
                return {"ok": False, "error": f"本机清单路径已被占用：{target}", "details": []}
            text = dumps_toml(final_def).encode("utf-8")
            if trial is not None:
                trial(action, final_def, existing)
            if dry_run:
                old_def = {}
                if existing is not None and existing.file:
                    try:
                        old_def = tomllib.loads(Path(existing.file).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        old_def = {}
                return {"ok": True, "action": "diff", "registration_action": action,
                        "service_id": sid, "project_id": pid,
                        "revision": proj["revision"] if proj else 0,
                        "diff": _diff_definitions(old_def, final_def),
                        "identity_changed": identity_changed, "identity_note": identity_note,
                        "draft_id": None, "validation": {"warnings": warnings}}

            # 先在内存里完成全部 registry 变更，再走多文件事务（V2.4）：
            # TOML + overrides.json + removed.json（按需）统一提交，registry 经 after=_save；
            # 任一失败 → 已替换文件原地写回原字节（新建则删除），接口失败 = 无变化。
            if proj is None:
                proj = _new_project(pid, norm["pj_name"] or svc.get("name", pid))
                data["projects"].append(proj)
            if norm["pj_name"]:
                proj["name"] = norm["pj_name"]
            if norm["agent"]:
                proj["agent"] = norm["agent"]
            for rp, role in norm["dirs"]:
                _add_directory_entry(proj, rp, role)
            if sid not in proj.get("services", []):
                proj.setdefault("services", []).append(sid)
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1

            writes: list[tuple[Path, bytes]] = [(target, text)]
            ov_bytes = overrides.plan(sid, accept=svc.get("accept_new"),
                                      edited=svc.get("edited"), final_definition=final_def)
            if ov_bytes is not None:
                writes.append((paths.overrides_file(), ov_bytes))
            if builtin:
                shadow = tombstones.apply_add_service(tombstones.base(), sid, {"builtin_shadow": True})
                writes.append((paths.removed_file(), shadow))
            elif sid in tombstones.services():
                # 同 sid 重新注册解除 tombstone（内置遮蔽语义：有内置模板则保留条目）
                rm_bytes = tombstones.plan_clear_on_reregister(sid)
                if rm_bytes is not None:
                    writes.append((paths.removed_file(), rm_bytes))
            _atomic_commit(writes, after=lambda: self._save(data))
            out = _ok(action, pid, sid, proj["revision"], warnings)
            if action == "updated":
                out["identity_changed"] = identity_changed
                out["identity_note"] = identity_note
            return out

    def _content_equal(self, toml: dict, existing: manifest.Project) -> bool:
        try:
            # V3：endpoints 两侧同规归一（未显式提供的一侧按稳定规则派生），
            # 否则「未提供 vs 已派生」永远不等，unchanged 判定被破坏
            toml = dict(toml)
            toml["endpoints"] = manifest.endpoint_rows(toml)
            existing_d = {
                "name": existing.name, "description": existing.description,
                "source": str(existing.source), "open": existing.open,
                "autostart": existing.autostart, "observe_only": existing.observe_only,
                "adapter": existing.adapter,
                "components": [dataclass_to_comp_dict(c) for c in existing.components],
                "dependencies": [dep_to_dict(d) for d in existing.dependencies],
                "endpoints": [{"id": e.id, "host": e.host, "component": e.component,
                               "open_path": e.open_path} for e in existing.endpoints],
            }
        except Exception:
            return False
        return _service_key(_resolved(toml["source"]), toml) == _service_key(_resolved(existing.source), existing_d)

    def _merge_existing(self, file: Path, toml: dict) -> dict:
        """更新既有服务：payload 定义的字段覆盖，其余顶层键（options/group 等）保留。"""
        try:
            base = tomllib.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return toml
        base.update(toml)
        return base

    # ---------- 移除登记（V2.1 / V2.2） ----------

    def remove_service(self, sid: str, disposition: str, ctl, removed_by: str = "user") -> dict:
        """按 V2.1 语义移除单个服务登记；失败 = 登记不变（幂等可重试）。

        步骤：预检/停止 → 快照（state/overrides/TOML）→ ctl.release_service（失败即 409，
        此时什么都还没写，登记原样保留）→ 归档 + tombstone + registry 去成员（多文件事务）
        → 删除用户层 TOML（builtin 模板永不修改，靠 tombstone 遮蔽）。"""
        if disposition not in DISPOSITIONS:
            raise RegistryError(f"disposition 必须是 {'/'.join(DISPOSITIONS)}")
        if disposition == "stop":
            if ctl is None:
                raise RegistryError("controller unavailable", status=503)
            self._stop_via_controller(ctl, sid)
        else:  # keep-external：绝不留下孤儿 KeepAlive job
            alive = self._launchd_alive(ctl, sid)
            if alive:
                raise RegistryError(
                    "无法保留为外部实例：仍由本工具 launchd 保活，请选择停止后移除"
                    f"（组件 {', '.join(alive)}）", status=409, components=alive)
        with _file_lock():
            try:
                loaded = manifest.load_all()
            except Exception as e:
                raise RegistryError(f"无法加载现有清单：{e}", status=500)
            p = loaded.get(sid)
            if p is None:
                raise RegistryError(f"未知服务 {sid}", status=404)
            data = self._load()
            owner = next((x for x in data.get("projects", []) if sid in x.get("services", [])), None)

            # 释放运行期对象（state 条目/run 记录/launchd 残留）——放在任何写盘之前：
            # 释放失败 → 409，登记一字节未动（幂等可重试）
            released = self._release_via_controller(ctl, sid)

            ts = time.strftime("%Y%m%d-%H%M%S")
            removed_at = _now()
            project_id = owner.get("id") if owner else p.project
            archive_name = f"{sid}-{ts}.json"
            archive_doc = {"sid": sid, "project_id": project_id, "removed_at": removed_at,
                           "disposition": disposition,
                           "toml": (Path(p.file).read_text(encoding="utf-8", errors="replace")
                                    if p.file else ""),
                           "state": self._state_snapshot(sid), "overrides": overrides.snapshot(sid)}
            entry = {"removed_at": removed_at, "removed_by": removed_by,
                     "disposition": disposition, "project_id": project_id, "archive": archive_name}

            if owner is not None:
                owner["services"] = [s for s in owner.get("services", []) if s != sid]
                owner["revision"] = owner.get("revision", 0) + 1
                owner["updated_at"] = removed_at
                data["revision"] = data.get("revision", 0) + 1

            writes: list[tuple[Path, bytes]] = [
                (paths.removed_dir() / archive_name,
                 json.dumps(archive_doc, ensure_ascii=False, indent=1).encode("utf-8")),
                (paths.removed_file(), tombstones.plan_add_service(sid, entry)),
            ]
            ov_bytes = overrides.plan_drop(sid)
            if ov_bytes is not None:
                writes.append((paths.overrides_file(), ov_bytes))
            _atomic_commit(writes, after=(lambda: self._save(data)) if owner is not None else None)

            # 用户层 TOML 删除；builtin（services_dir 内随仓模板）永不触碰
            builtin = bool(p.file and _inside(Path(p.file).resolve(), paths.services_dir().resolve()))
            if not builtin and p.file and Path(p.file).exists():
                try:
                    Path(p.file).unlink()  # 只删登记，业务数据/源码永不删除
                except OSError as e:
                    raise RegistryError(f"登记已移除但清单文件删除失败：{e}", status=500) from e
            return {"ok": True, "service_id": sid, "disposition": disposition,
                    "project_id": project_id, "archived": True, "archive": archive_name,
                    "released": released, "builtin": builtin,
                    "revision": owner.get("revision") if owner else None}

    def service_remove_preview(self, sid: str, ctl) -> dict:
        """V2.1 移除预览：零副作用。"""
        try:
            loaded = manifest.load_all()
        except Exception as e:
            raise RegistryError(f"无法加载现有清单：{e}", status=500)
        p = loaded.get(sid)
        if p is None:
            raise RegistryError(f"未知服务 {sid}", status=404)
        state = "unknown"
        external: list[dict] = []
        if ctl is not None:
            try:
                snap = {x.get("id"): x for x in (ctl.snapshot() or {}).get("projects", [])}
                state = (snap.get(sid) or {}).get("state", "unknown")
            except Exception:  # noqa: BLE001
                pass
            ext_fn = getattr(ctl, "external_instances", None)
            if callable(ext_fn):
                try:
                    external = ext_fn(p) or []
                except Exception:  # noqa: BLE001
                    external = []
        alive = self._launchd_alive(ctl, sid)
        keep = [str(p.source)]
        for d in p.dependencies:
            if d.container:
                keep.append(f"container:{d.container}")
            if d.volume:
                keep.append(f"volume:{d.volume}")
        keep_external = {"enabled": not alive,
                         "reason": (None if not alive else
                                    f"组件 {', '.join(alive)} 仍由本工具 launchd 保活")}
        return {"ok": True, "service_id": sid, "name": p.name, "state": state,
                "running_components": alive, "external_instances": external, "keep": keep,
                "dispositions": {"stop": {"enabled": True}, "keep-external": keep_external},
                "restore_hint": (f"移除后登记归档于 removed/{sid}-<时间戳>.json，"
                                 f"可通过 POST /api/removed/services/{sid}/restore 恢复")}

    @staticmethod
    def _launchd_alive(ctl, sid: str) -> list[str]:
        """仍由本工具 launchd loaded/running 的组件 id；无法确认（ctl 缺失/未加载该服务）
        时返回空——只有 ctl 证实 loaded 才拒绝 keep-external。"""
        from . import launchd as _launchd
        try:
            if ctl is None:
                return []
            p = getattr(ctl, "projects", {}).get(sid)
            label = getattr(ctl, "label", None)
            if p is None or label is None:
                return []
            alive = []
            for c in p.managed_components():
                ji = _launchd.info(label(p, c))
                if ji and (ji.running or ji.loaded):
                    alive.append(c.id)
            return alive
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _release_via_controller(ctl, sid: str) -> list:
        """防御式调用 ctl.release_service（Runtime 线提供；缺省视为 no-op）。
        返回 released 组件列表；释放失败抛 409——调用方此时尚未写盘，登记保留。"""
        rel = getattr(ctl, "release_service", None)
        if rel is None:
            return []
        try:
            res = rel(sid) or {}
        except Exception as e:  # noqa: BLE001
            raise RegistryError(f"释放运行期对象失败（{e}），登记保留；可重试", status=409) from e
        if not res.get("ok"):
            raise RegistryError(
                f"释放运行期对象失败（{res.get('error', '')}），登记保留；可重试",
                status=409, errors=res.get("errors", []))
        return res.get("released", []) or []

    @staticmethod
    def _state_snapshot(sid: str) -> dict:
        try:
            data = json.loads(paths.state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entry = (data.get("projects") or {}).get(sid)
        return copy.deepcopy(entry) if isinstance(entry, dict) else {}

    @staticmethod
    def _set_state_intent(sid: str, intent: str, by: str = "restore") -> None:
        """显式写 state.json 意图（缺省 stopped；绝不自动启动）——仅作无 ctl 写入口时的回退。"""
        try:
            data = json.loads(paths.state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        projects = data.setdefault("projects", {})
        entry = projects.get(sid) if isinstance(projects.get(sid), dict) else {}
        entry.update({"intent": intent, "intent_at": time.time(), "intent_by": by})
        projects[sid] = entry
        _write_bytes_atomic(paths.state_file(),
                            json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    @staticmethod
    def _apply_restore_intent(ctl, sid: str, intent: str) -> None:
        """恢复流程设置意图（V3：统一写入口）。优先走 ctl.set_intent_direct——daemon 内
        Controller 的 Store 持有内存缓存，绕过它会制造“文件刚写就被旧缓存覆盖”的竞态；
        无该入口（旧桩/独立进程）时回退为原子直写文件。"""
        writer = getattr(ctl, "set_intent_direct", None)
        if callable(writer):
            writer(sid, intent, by="restore")
            return
        Registry._set_state_intent(sid, intent)

    @staticmethod
    def _stop_via_controller(ctl, sid: str) -> None:
        try:
            ctl.reload()
        except Exception:
            pass
        res = {}
        try:
            res = ctl.stop(sid) or {}
        except Exception as e:
            raise RegistryError(f"停止失败：{e}", status=409)
        if not res.get("ok") and "未知项目" in str(res.get("error", "")):
            return  # 控制器尚未加载该服务（未运行）：直接移除登记
        if not res.get("ok") and not res.get("accepted"):
            raise RegistryError(f"停止失败：{res.get('error', '')}", status=409)
        if res.get("accepted"):
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    ops = ctl.ops
                    if sid not in ops:
                        break
                except Exception:
                    break
                time.sleep(0.2)
        # 操作结束 ≠ 停止成功：确认没有组件仍在 launchd 中运行，否则不移除登记
        # （避免"界面已删、进程还在"的半删除状态）
        from . import launchd as _launchd
        try:
            ctl.reload()
            p = ctl.projects.get(sid)
        except Exception as e:
            # 确认停止状态失败时宁可拒绝移除（fail-closed），不做未确认的删除
            raise RegistryError(f"无法确认停止状态（{e}），登记未移除；可重试", status=409) from e
        if p is not None:
            still = [c.id for c in p.managed_components()
                     if (ji := _launchd.info(ctl.label(p, c))) and (ji.running or ji.loaded)]
            if still:
                raise RegistryError(
                    f"停止未完成（组件 {', '.join(still)} 仍在运行），登记未移除；"
                    "可重试或选择「保留为外部实例」", status=409)

    # ---------- Project 移除 / 恢复 / removed 列表（V2.2） ----------

    def remove_project_preview(self, pid: str, ctl) -> dict:
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            try:
                loaded = manifest.load_all()
            except Exception as e:
                raise RegistryError(f"无法加载现有清单：{e}", status=500)
            return self._project_remove_preview(proj, loaded, ctl)

    def _project_remove_preview(self, proj: dict, loaded: dict, ctl) -> dict:
        warnings: list[str] = []
        services = []
        snap: dict[str, dict] = {}
        if ctl is not None:
            try:
                snap = {x.get("id"): x for x in (ctl.snapshot() or {}).get("projects", [])}
            except Exception:  # noqa: BLE001
                pass
        for sid in proj.get("services", []):
            p = loaded.get(sid)
            if p is None:
                warnings.append(f"成员 {sid} 当前未加载（可能已移除），仅移除登记关联")
                continue
            alive = self._launchd_alive(ctl, sid)
            external: list[dict] = []
            if ctl is not None:
                ext_fn = getattr(ctl, "external_instances", None)
                if callable(ext_fn):
                    try:
                        external = ext_fn(p) or []
                    except Exception:  # noqa: BLE001
                        external = []
            services.append({
                "id": sid, "name": p.name,
                "state": (snap.get(sid) or {}).get("state", "unknown"),
                "external_instances": external,
                "dispositions": {"stop": {"enabled": True},
                                 "keep-external": {"enabled": not alive,
                                                   "reason": (None if not alive else
                                                              f"组件 {', '.join(alive)} 仍由本工具 launchd 保活")}},
            })
        resources = self._resources_referenced(proj, loaded, warnings)
        return {"ok": True, "project": {"id": proj["id"], "name": proj.get("name")},
                "services": services,
                "directories": copy.deepcopy(proj.get("directories", [])),
                "resources_referenced": resources,
                "kept": sorted({str(loaded[sid].source) for sid in proj.get("services", [])
                                if sid in loaded} |
                               {d.get("path", "") for d in proj.get("directories", [])} - {""}),
                "warnings": warnings}

    def _resources_referenced(self, proj: dict, loaded: dict, warnings: list[str]) -> list[dict]:
        """成员服务依赖引用的容器/卷；shared=其他项目的服务也在引用。"""
        member_ids = set(proj.get("services", []))
        refs: dict[str, list[str]] = {}
        for lp in loaded.values():
            names: list[str] = []
            for d in lp.dependencies:
                if d.container:
                    names.append(f"container:{d.container}")
                if d.volume:
                    names.append(f"volume:{d.volume}")
            for c in lp.components:
                if getattr(c, "run", "local") == "compose":
                    names.append(f"compose:{c.compose_service}@{c.compose_project or ''}")
            for n in names:
                refs.setdefault(n, []).append(lp.id)
        if not refs:
            return []
        inv: dict = {}
        try:
            inv = resources.inventory()
        except Exception as e:  # noqa: BLE001
            inv = {"ok": False, "error": str(e)}
        states: dict[str, str] = {}
        if inv.get("ok"):
            for r in inv.get("resources", []):
                states[f"container:{r.get('name')}"] = r.get("state", "unknown")
        else:
            warnings.append(f"资源状态未验证（Docker 不可达：{inv.get('error', '')}）")
        out = []
        for name, consumers in sorted(refs.items()):
            if not (set(consumers) & member_ids):
                continue  # F-6：只列本 Project 成员引用的资源；其他项目的资源不属于本预览
            others = [s for s in consumers if s not in member_ids]
            out.append({"name": name,
                        "state": states.get(name, "unknown"),
                        "shared": bool(others),
                        "consumers": sorted(consumers)})
        return out

    def remove_project(self, pid: str, disposition: str, ctl, dry_run: bool = False,
                       removed_by: str = "user") -> dict:
        """V2.2：逐成员按 V2.1 处置；任一成员停止/预检失败 → 整体中止，全部登记保留。"""
        if disposition not in DISPOSITIONS:
            raise RegistryError(f"disposition 必须是 {'/'.join(DISPOSITIONS)}")
        with _file_lock():
            data = self._load()
            proj = _find(data, pid)
            if proj is None:
                raise RegistryError(f"未知项目 {pid}", status=404)
            try:
                loaded = manifest.load_all()
            except Exception as e:
                raise RegistryError(f"无法加载现有清单：{e}", status=500)
            if dry_run:
                return self._project_remove_preview(proj, loaded, ctl)

            members = [sid for sid in proj.get("services", []) if sid in loaded]
            if disposition == "stop" and members and ctl is None:
                raise RegistryError("controller unavailable", status=503)

            # 1) 停止/预检阶段：任一失败 → 整体中止（已停止进程不回拉，登记全保留）
            stopped: list[str] = []
            remaining: list[dict] = []
            for sid in members:
                try:
                    if disposition == "stop":
                        self._stop_via_controller(ctl, sid)
                    else:
                        alive = self._launchd_alive(ctl, sid)
                        if alive:
                            raise RegistryError(
                                "无法保留为外部实例：仍由本工具 launchd 保活，请选择停止后移除"
                                f"（组件 {', '.join(alive)}）", status=409)
                    stopped.append(sid)
                except RegistryError as e:
                    remaining.append({"id": sid, "reason": str(e)})
            if remaining:
                return {"ok": False, "project_id": pid, "stopped": stopped, "remaining": remaining}

            # 2) 释放运行期对象（失败 → 整体中止；此时尚未写盘，登记全保留）
            released: dict[str, list] = {}
            for sid in members:
                try:
                    released[sid] = self._release_via_controller(ctl, sid)
                except RegistryError as e:
                    return {"ok": False, "project_id": pid, "stopped": stopped,
                            "remaining": [{"id": sid, "reason": str(e)}]}

            # 3) 全部成员通过 → 归档 + tombstone + 项目删除（多文件事务）
            ts = time.strftime("%Y%m%d-%H%M%S")
            removed_at = _now()
            writes: list[tuple[Path, bytes]] = []
            rm_data = tombstones.base()
            services_out: list[dict] = []
            for sid in members:
                p = loaded[sid]
                archive_name = f"{sid}-{ts}.json"
                archive_doc = {"sid": sid, "project_id": pid, "removed_at": removed_at,
                               "disposition": disposition,
                               "toml": (Path(p.file).read_text(encoding="utf-8", errors="replace")
                                        if p.file else ""),
                               "state": self._state_snapshot(sid),
                               "overrides": overrides.snapshot(sid)}
                entry = {"removed_at": removed_at, "removed_by": removed_by,
                         "disposition": disposition, "project_id": pid, "archive": archive_name}
                writes.append((paths.removed_dir() / archive_name,
                               json.dumps(archive_doc, ensure_ascii=False, indent=1).encode("utf-8")))
                tombstones.apply_add_service(rm_data, sid, entry)
                services_out.append({"id": sid, "archive": archive_name,
                                     "released": released.get(sid, []),
                                     "builtin": bool(p.file and _inside(
                                         Path(p.file).resolve(), paths.services_dir().resolve()))})
            proj_entry = {"removed_at": removed_at, "removed_by": removed_by,
                          "disposition": disposition,
                          "services": list(proj.get("services", [])),
                          "project": copy.deepcopy(proj)}
            writes.append((paths.removed_file(),
                           tombstones.apply_add_project(rm_data, pid, proj_entry)))
            ov_bytes = overrides.plan_drop_many(members)
            if ov_bytes is not None:
                writes.append((paths.overrides_file(), ov_bytes))

            data["projects"].remove(proj)
            data["revision"] = data.get("revision", 0) + 1
            _atomic_commit(writes, after=lambda: self._save(data))

            # 4) 删除用户层 TOML（builtin 模板永不触碰）
            for sid in members:
                p = loaded[sid]
                builtin = bool(p.file and _inside(Path(p.file).resolve(),
                                                  paths.services_dir().resolve()))
                if not builtin and p.file and Path(p.file).exists():
                    try:
                        Path(p.file).unlink()
                    except OSError as e:
                        raise RegistryError(f"登记已移除但清单文件删除失败：{e}", status=500) from e
            return {"ok": True, "project_id": pid, "disposition": disposition,
                    "stopped": stopped, "services": services_out,
                    "directories": copy.deepcopy(proj.get("directories", []))}

    def removed_list(self) -> dict:
        """V2.2 GET /api/removed：只列当前未登记（load_all 不含该 id）的移除条目。
        项目条目同理过滤：registry.json 中已重建同 id 项目（或全部成员已重新登记）的
        stale 条目不再显示——否则会建议"恢复"一个已在运行的项目（真实 Chat 使用发现）。"""
        rm = tombstones._load_full()
        try:
            loaded = manifest.load_all()
        except Exception:  # noqa: BLE001
            loaded = {}
        try:
            live_projects = {p.get("id") for p in self._load().get("projects", [])}
        except RegistryError:
            live_projects = None  # 注册表读失败时保守显示全部（恢复入口宁可多给）
        services = [{"sid": sid, "project_id": e.get("project_id"),
                     "removed_at": e.get("removed_at"), "disposition": e.get("disposition"),
                     "archive": e.get("archive")}
                    for sid, e in rm.get("services", {}).items() if sid not in loaded]
        projects = [{"pid": pid, "removed_at": e.get("removed_at"),
                     "disposition": e.get("disposition"),
                     "services": list(e.get("services", []))}
                    for pid, e in rm.get("projects", {}).items()
                    if not (live_projects is not None and pid in live_projects)
                    and not all(sid in loaded for sid in e.get("services", []) if sid)]
        return {"ok": True, "services": services, "projects": projects}

    def restore_service(self, sid: str, body: dict, ctl) -> dict:
        """V2.2：归档 TOML 写回 user_services_dir（即使原是 builtin）+ 恢复登记 +
        state intent 显式设为所选值（缺省 stopped，绝不自动启动）。
        R10：目标项目（body.project_id）≠ 归档 TOML 的 project 字段时，
        同一事务把 TOML 归属重写为目标项目——成员表与清单归属一致。"""
        body = body if isinstance(body, dict) else {}
        with _file_lock():
            rm = tombstones._load_full()
            entry = rm.get("services", {}).get(sid)
            if not isinstance(entry, dict):
                raise RegistryError(f"未找到服务 {sid} 的移除记录", status=404)
            try:
                loaded = manifest.load_all()
            except Exception as e:
                raise RegistryError(f"无法加载现有清单：{e}", status=500)
            if sid in loaded:
                raise RegistryError(f"服务 id {sid} 已登记（先移除再恢复）", status=409)
            archive_path = paths.removed_dir() / str(entry.get("archive") or "")
            if not archive_path.is_file():
                cands = sorted(paths.removed_dir().glob(f"{sid}-*.json"))
                if not cands:
                    raise RegistryError(f"服务 {sid} 的归档文件缺失", status=404)
                archive_path = cands[-1]
            try:
                doc = json.loads(archive_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise RegistryError(f"归档 {archive_path.name} 无法读取：{e}", status=500) from e
            toml_text = str(doc.get("toml") or "")
            try:
                parsed = tomllib.loads(toml_text)
            except ValueError as e:
                raise RegistryError(f"归档 TOML 无法解析：{e}", status=500) from e

            intent = body.get("intent") or "stopped"
            if intent not in INTENTS:
                raise RegistryError("intent 必须是 stopped/running")
            # 端口冲突预检（写盘前，失败不动任何文件）
            owners: dict[int, str] = {}
            for lp in loaded.values():
                for lc in lp.managed_components():
                    if lc.port:
                        owners[lc.port] = f"{lp.id}.{lc.id}"
            for c in parsed.get("components", []) or []:
                port = c.get("port") if isinstance(c, dict) else None
                if port and owners.get(port, "").split(".", 1)[0] not in ("", sid):
                    raise RegistryError(f"端口 {port} 已被占用", status=409,
                                        details=[f"{owners[port]} 与 {sid}.{c.get('id')}"])

            # V3 域名冲突预检（写盘前）：归档 endpoints（显式或按 id 派生）不得占用现存服务域名
            try:
                arch_hosts = _archived_endpoint_hosts(sid, parsed)
            except ValueError as e:
                raise RegistryError(f"归档定义无效：{e}", status=500)
            host_owner = {ep.host: lp.id for lp in loaded.values() for ep in lp.endpoints}
            hclash = sorted(arch_hosts & set(host_owner))
            if hclash:
                raise RegistryError(
                    "域名已被占用：" + "；".join(f"{h}（服务 {host_owner[h]}）" for h in hclash),
                    status=409, details=hclash)

            target_pid = body.get("project_id") or doc.get("project_id") or sid
            if not isinstance(target_pid, str) or not ID_RE.fullmatch(target_pid or ""):
                raise RegistryError(f"project_id 非法：{target_pid!r}")
            data = self._load()
            proj = _find(data, target_pid)
            recreated = False
            if proj is None:
                archived_proj = ((rm.get("projects", {}).get(target_pid) or {}).get("project")
                                 or {})
                name = archived_proj.get("name") or parsed.get("name") or target_pid
                proj = _new_project(target_pid, name,
                                    origin=archived_proj.get("origin") or "user")
                for key in ("directories", "agent"):
                    if archived_proj.get(key):
                        proj[key] = copy.deepcopy(archived_proj[key])
                for key in ("favorite", "autostart"):
                    if isinstance(archived_proj.get(key), bool):
                        proj[key] = archived_proj[key]
                data["projects"].append(proj)
                recreated = True
            else:
                rev = body.get("revision")
                if isinstance(rev, int) and not isinstance(rev, bool) and rev != proj.get("revision", 0):
                    raise RegistryError("revision conflict", status=409,
                                        current_revision=proj.get("revision", 0))
            if sid not in proj.get("services", []):
                proj.setdefault("services", []).append(sid)
            proj["revision"] = proj.get("revision", 0) + 1
            proj["updated_at"] = _now()
            data["revision"] = data.get("revision", 0) + 1

            # R10：目标项目 ≠ 归档 TOML 声明的 project → 同一事务重写 project 字段
            #（查询层优先 manifest.project：成员表与 TOML 归属必须一致；相同则保持原字节）
            out_text = toml_text
            if parsed.get("project") != target_pid:
                out_text = _set_project_field(toml_text, target_pid)
            writes: list[tuple[Path, bytes]] = [
                (paths.user_services_dir() / f"{sid}.toml", out_text.encode("utf-8"))]
            # tombstone：有内置模板 → 保留条目（持续遮蔽 builtin）；纯用户层 id → 解除
            builtin_shadow = tombstones.has_builtin(sid)
            if not builtin_shadow:
                rm_bytes = tombstones.plan_remove_service(sid)
                if rm_bytes is not None:
                    writes.append((paths.removed_file(), rm_bytes))
            ov_bytes = overrides.plan_restore(sid, doc.get("overrides") or {})
            if ov_bytes is not None:
                writes.append((paths.overrides_file(), ov_bytes))
            _atomic_commit(writes, after=lambda: self._save(data))
            self._apply_restore_intent(ctl, sid, intent)  # 绝不自动启动
            return {"ok": True, "service_id": sid, "project_id": target_pid, "intent": intent,
                    "archive": archive_path.name, "builtin_shadow": builtin_shadow,
                    "project_recreated": recreated}

    def restore_project(self, pid: str, body: dict, ctl) -> dict:
        """V2.2：恢复项目条目（归档的原 project 对象）+ 全部成员服务。

        R02：写盘前对**全部**成员归档做端口/服务 id/注册身份预检（与当前有效清单及
        成员相互之间），任何冲突 → 零写入 409。R03：成员覆盖层合并为一次 overrides.json
        写入。R10：成员 TOML 的 project 字段统一重绑定为本项目。"""
        body = body if isinstance(body, dict) else {}
        with _file_lock():
            rm = tombstones._load_full()
            entry = rm.get("projects", {}).get(pid)
            if not isinstance(entry, dict):
                raise RegistryError(f"未找到项目 {pid} 的移除记录", status=404)
            try:
                loaded = manifest.load_all()
            except Exception as e:
                raise RegistryError(f"无法加载现有清单：{e}", status=500)
            if _find(self._load(), pid) is not None:
                raise RegistryError(f"项目 id {pid} 已存在（先移除再恢复）", status=409)
            members = [s for s in entry.get("services", []) if isinstance(s, str)]
            members = list(dict.fromkeys(members))  # 防御去重：同目标 TOML 只能写一次
            intent = body.get("intent") or "stopped"
            if intent not in INTENTS:
                raise RegistryError("intent 必须是 stopped/running")
            # 预检：同 id 已登记 → 409；归档缺失 → 404（写盘前失败 = 无变化）
            conflicts = [s for s in members if s in loaded]
            if conflicts:
                raise RegistryError(f"成员 {', '.join(conflicts)} 已登记，无法整项目恢复",
                                    status=409, details=conflicts)
            archives: dict[str, dict] = {}
            for sid in members:
                svc_entry = rm.get("services", {}).get(sid)
                path = (paths.removed_dir() / str((svc_entry or {}).get("archive") or "")
                        if svc_entry else None)
                if path is None or not path.is_file():
                    cands = sorted(paths.removed_dir().glob(f"{sid}-*.json"))
                    if not cands:
                        raise RegistryError(f"服务 {sid} 的归档文件缺失", status=404)
                    path = cands[-1]
                try:
                    archives[sid] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as e:
                    raise RegistryError(f"归档无法读取（{sid}）：{e}", status=500) from e

            # R02 预检：写盘前一次性验证**全部**成员归档定义——监听端口（成员之间 +
            # 与当前有效清单）、服务 id、注册身份。任何冲突 → 零写入 409，逐条列出冲突对象
            #（此前逐成员恢复后写盘成功、load_all 全局 duplicate listen port 崩溃）。
            reg_snap = self._load()
            pid_members = {s for x in reg_snap.get("projects", [])
                           if x.get("id") == pid for s in x.get("services", [])}
            owners: dict[int, str] = {}
            for lp in loaded.values():
                for lc in lp.managed_components():
                    if lc.port:
                        owners[lc.port] = f"{lp.id}.{lc.id}"
            conflicts = []
            parsed_defs: dict[str, dict] = {}
            seen_ids: set[str] = set()
            seen_ports: dict[int, str] = {}
            seen_idents: dict[tuple, str] = {}
            host_owner = {ep.host: lp.id for lp in loaded.values() for ep in lp.endpoints}
            seen_hosts: dict[str, str] = {}
            for sid in members:
                try:
                    parsed = tomllib.loads(str(archives[sid].get("toml") or ""))
                except ValueError as e:
                    raise RegistryError(f"归档 TOML 无法解析（{sid}）：{e}", status=500) from e
                parsed_defs[sid] = parsed
                inner = str(parsed.get("id") or sid)
                if inner != sid:
                    conflicts.append(f"{sid}: 归档 TOML 声明的 id {inner!r} 与移除记录不一致")
                if inner in loaded:
                    conflicts.append(f"服务 id {inner} 已登记")
                elif inner in seen_ids:
                    conflicts.append(f"服务 id {inner} 在恢复成员中重复")
                seen_ids.add(inner)
                try:
                    ahosts = _archived_endpoint_hosts(sid, parsed)
                except ValueError as e:
                    conflicts.append(f"{sid}: 归档定义无效（{e}）")
                    ahosts = set()
                for h in sorted(ahosts):
                    if h in host_owner:
                        conflicts.append(f"域名 {h} 已被服务 {host_owner[h]} 占用（与 {sid} 冲突）")
                    elif h in seen_hosts:
                        conflicts.append(f"域名 {h} 在恢复成员 {seen_hosts[h]} 与 {sid} 之间重复")
                    seen_hosts[h] = sid
                try:
                    comps = [manifest._component(dict(c))
                             for c in parsed.get("components", []) or [] if isinstance(c, dict)]
                except TypeError as e:
                    raise RegistryError(f"归档 TOML 组件无法解析（{sid}）：{e}", status=500) from e
                ident = _identity(_resolved(parsed.get("source", ".")), comps)
                for other_sid, other_ident in seen_idents.items():
                    if other_ident == ident:
                        conflicts.append(f"{sid} 与 {other_sid} 注册身份相同（source/命令重叠）")
                seen_idents[sid] = ident
                for lp in loaded.values():
                    if ((lp.project == pid or lp.id in pid_members)
                            and _identity(_resolved(lp.source), lp.components) == ident):
                        conflicts.append(f"{sid} 与已登记的 {lp.id} 注册身份相同")
                for cobj in comps:
                    if cobj.kind == "generator" or not cobj.port:
                        continue
                    if cobj.port in owners:
                        conflicts.append(
                            f"端口 {cobj.port} 已被 {owners[cobj.port]} 占用（与 {sid}.{cobj.id} 冲突）")
                    elif cobj.port in seen_ports:
                        conflicts.append(
                            f"端口 {cobj.port} 同时用于 {seen_ports[cobj.port]} 与 {sid}.{cobj.id}")
                    else:
                        seen_ports[cobj.port] = f"{sid}.{cobj.id}"
            if conflicts:
                raise RegistryError(f"项目 {pid} 无法恢复：存在登记冲突", status=409,
                                    details=conflicts)

            data = self._load()
            archived_proj = entry.get("project") or {}
            proj = _new_project(pid, archived_proj.get("name") or pid,
                                origin=archived_proj.get("origin") or "user")
            for key in ("directories", "agent"):
                if archived_proj.get(key):
                    proj[key] = copy.deepcopy(archived_proj[key])
            for key in ("favorite", "autostart"):
                if isinstance(archived_proj.get(key), bool):
                    proj[key] = archived_proj[key]
            proj["services"] = list(members)
            proj["revision"] = (archived_proj.get("revision") or 0) + 1
            proj["updated_at"] = _now()
            data["projects"].append(proj)
            data["revision"] = data.get("revision", 0) + 1

            rm_data = tombstones.base()
            rm_bytes = tombstones.apply_remove_project(rm_data, pid)
            for sid in members:
                if tombstones.has_builtin(sid):
                    continue  # 内置遮蔽语义：保留条目
                b = tombstones.apply_remove_service(rm_data, sid)
                if b is not None:
                    rm_bytes = b
            writes: list[tuple[Path, bytes]] = []
            if rm_bytes is not None:
                writes.append((paths.removed_file(), rm_bytes))
            ov_items: list[tuple[str, dict]] = []
            for sid in members:
                text = str(archives[sid].get("toml") or "")
                if parsed_defs[sid].get("project") != pid:
                    # R10：归档 TOML 的 project 字段与本项目不一致 → 同一事务重写（归属一致）
                    text = _set_project_field(text, pid)
                writes.append((paths.user_services_dir() / f"{sid}.toml", text.encode("utf-8")))
                ov_items.append((sid, archives[sid].get("overrides") or {}))
            # R03：全部成员的覆盖层合并为**一次** overrides.json 写入
            #（同一事务同一目标路径只能写一次；逐成员 plan 会让 rename 二次失败）
            ov_bytes = overrides.plan_restore_many(ov_items)
            if ov_bytes is not None:
                writes.append((paths.overrides_file(), ov_bytes))
            _atomic_commit(writes, after=lambda: self._save(data))
            for sid in members:
                self._apply_restore_intent(ctl, sid, intent)
            return {"ok": True, "project_id": pid, "intent": intent, "services": members,
                    "project": copy.deepcopy(proj)}

    # ---------- 迁移（CONTRACTS §7，一次性且幂等） ----------

    def migrate_from_manifests(self) -> dict:
        ts = time.strftime("%Y%m%d-%H%M%S")
        with _file_lock():
            try:
                loaded = manifest.load_all()
            except Exception as e:
                return {"ok": False, "error": f"无法加载清单：{e}"}

            notes: list[str] = []
            # V2.5：tombstone 服务完全跳过 = 标记存在且当前未登记（load_all 不含）。
            # load_all 已按 V2.0 修订过滤（遮蔽的 builtin 不出现；用户层同 id 文件正常加载
            # → 属"已登记"，正常迁移）。
            tombstoned = {sid for sid in tombstones.services() if sid not in loaded}

            groups: dict[str, list] = {}
            desired: list[dict] = []
            for p in loaded.values():
                if p.id in tombstoned:
                    notes.append(f"{p.id}: 已在移除标记中，迁移跳过")
                    continue
                if p.group:
                    groups.setdefault(p.group, []).append(p)
                else:
                    desired.append({"id": p.id, "name": p.name, "services": [p.id], "sources": [p.source]})
            for group, members in groups.items():
                members.sort(key=lambda p: (p.group_order or 0, p.id))
                desired.append({"id": group, "name": members[0].group_name or group,
                                "services": [m.id for m in members], "sources": [m.source for m in members]})
            desired.sort(key=lambda d: d["id"])

            # registry 现状先读：V2.5 判定"sid 已属其他项目"要在 TOML 插键之前完成
            reg_path = paths.registry_file()
            old_bytes = reg_path.read_bytes() if reg_path.exists() else None
            data = self._load()
            assigned: dict[str, str] = {}
            for x in data.get("projects", []):
                for sid in x.get("services", []):
                    assigned.setdefault(sid, x["id"])

            # 1) 需要追加 project= 的 TOML；内置清单在用户目录生成副本。
            #    V2.5：TOML 已声明**不同** project 值 → 跳过写入并记 note（绝不插第二个顶层键）；
            #    registry 已把 sid 指派给其他项目 → 同样不写映射键（不挪归属）。
            toml_writes: list[tuple[Path, bytes | None, bytes]] = []
            shadows = tombstones.base()
            shadow_bytes = None
            builtin_dir = paths.services_dir().resolve()
            declared: dict[str, str | None] = {}  # sid → TOML 声明的 project（无键=None）
            for spec in desired:
                for sid in spec["services"]:
                    file = Path(loaded[sid].file)
                    orig = file.read_bytes()
                    parsed = tomllib.loads(orig.decode("utf-8"))
                    declared[sid] = parsed.get("project")
                    if parsed.get("project") == spec["id"]:
                        continue
                    if "project" in parsed:
                        notes.append(f"{sid}: TOML 已声明 project={parsed['project']!r}"
                                     f"（迁移映射为 {spec['id']!r}），不插入第二键、不挪归属")
                        continue
                    if assigned.get(sid) not in (None, spec["id"]):
                        notes.append(f"{sid}: registry 已指派给项目 {assigned[sid]}，"
                                     f"不写入 project={spec['id']!r}、不挪归属")
                        continue
                    new = _insert_project_line(orig.decode("utf-8"), spec["id"]).encode("utf-8")
                    if file.parent.resolve() == builtin_dir:
                        file = paths.user_services_dir() / f"{sid}.toml"
                        if file.exists():
                            return {"ok": False, "error": f"本机清单路径已被占用：{file}"}
                        orig = None
                        shadow_bytes = tombstones.apply_add_service(shadows, sid, {"builtin_shadow": True})
                    toml_writes.append((file, orig, new))

            # 2) registry 合并（保留用户已有的项目与目录，仅补齐迁移映射）。
            #    V2.5：sid 已属其他项目（TOML 声明或 registry 指派）→ 不挪归属、不双写成员。
            registry_changed = False
            for spec in desired:
                homed: list[tuple[str, object]] = []  # (sid, source) 本项目实际接纳的成员
                for sid in spec["services"]:
                    decl = declared.get(sid)
                    if decl is not None and decl != spec["id"]:
                        continue  # note 已在上一轮记录
                    assigned = next((x["id"] for x in data.get("projects", [])
                                     if sid in x.get("services", [])), None)
                    if assigned is not None and assigned != spec["id"]:
                        notes.append(f"{sid}: 已属于项目 {assigned}（registry 指派），"
                                     f"不加入 {spec['id']}、不双写成员")
                        continue
                    homed.append((sid, loaded[sid].source))
                if not homed and not any(x["id"] == spec["id"] for x in data["projects"]):
                    continue  # 全员归属他处且项目尚不存在：不为空壳建项目
                proj = _find(data, spec["id"])
                if proj is None:
                    proj = _new_project(spec["id"], spec["name"], origin="migrated")
                    data["projects"].append(proj)
                    registry_changed = True
                if proj.get("origin") == "migrated" and proj.get("name") != spec["name"]:
                    proj["name"] = spec["name"]
                    registry_changed = True
                for sid, src in homed:
                    rp = str(Path(src).resolve())
                    if not any(d.get("path") == rp for d in proj.get("directories", [])):
                        proj.setdefault("directories", []).append(
                            {"id": _dir_id(rp), "path": rp, "role": "source"})
                        registry_changed = True
                    if sid not in proj.get("services", []):
                        proj.setdefault("services", []).append(sid)
                        registry_changed = True

            mapping = [{"id": x["id"], "name": x.get("name"),
                        "services": list(x.get("services", [])),
                        "directories": copy.deepcopy(x.get("directories", []))}
                       for x in sorted(data["projects"], key=lambda p: p["id"])]

            if not toml_writes and not registry_changed:
                return {"ok": True, "changed": False, "notes": notes, "projects": mapping}

            # 3) 备份 + 写入；任何失败经原地写回恢复原字节（与 _atomic_commit 同一恢复机制）
            artifacts: list[Path] = []  # 备份/标记文件（失败时删除）
            replaced: list[tuple[Path, bytes | None]] = []  # 已写文件及其原字节
            new_reg = (json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
                       if registry_changed else None)
            try:
                for file, orig, new in toml_writes:
                    _write_bytes_atomic(file, new)
                    replaced.append((file, orig))
                if shadow_bytes is not None:
                    removed_file = paths.removed_file()
                    original = _current_bytes(removed_file)
                    _write_bytes_atomic(removed_file, shadow_bytes)
                    replaced.append((removed_file, original))
                if old_bytes is not None:
                    bak = reg_path.with_name(f"registry.json.bak-{ts}")
                    bak.write_bytes(old_bytes)
                    artifacts.append(bak)
                elif registry_changed:
                    marker = paths.home() / "registry.json.migration-backup"
                    if not marker.exists():
                        marker.write_bytes(b"")
                        artifacts.append(marker)
                if new_reg is not None:
                    _write_bytes_atomic(reg_path, new_reg)
                    replaced.append((reg_path, old_bytes))
            except Exception as e:
                for path, orig in replaced:
                    if orig is None:
                        _quiet_unlink(path)
                    else:
                        _restore_inplace(path, orig)
                for path in artifacts:
                    _quiet_unlink(path)
                if old_bytes is None and reg_path.exists() and (reg_path, old_bytes) not in replaced:
                    _quiet_unlink(reg_path)
                elif old_bytes is not None and (reg_path, old_bytes) not in replaced:
                    _restore_inplace(reg_path, old_bytes)
                return {"ok": False, "error": f"迁移失败，已恢复原状：{e}"}
            return {"ok": True, "changed": True, "notes": notes, "projects": mapping}

    # ---------- 校验辅助 ----------

    def _norm_endpoints(self, raw, comp_objs, sid: str, details: list[str]) -> list[dict] | None:
        """V3：payload service.endpoints 规范化。None = 未提供（加载端派生缺省入口）。
        host 缺省：第一个入口用 <sid>.<suffix>（锚定稳定 id），其余必须显式。"""
        if raw is None:
            return None
        if not isinstance(raw, list):
            details.append("service.endpoints 必须是数组")
            return []
        comp_ids = {c.id for c in comp_objs}
        out: list[dict] = []
        seen: set[str] = set()
        for i, d in enumerate(raw):
            if not isinstance(d, dict):
                details.append(f"endpoints[{i}] 必须是对象")
                continue
            unknown = set(d) - set(_ENDPOINT_ORDER) - {"derived"}  # derived 是 definition 元数据，回传时忽略
            if unknown:
                details.append(f"endpoints[{i}] 含未知字段：{sorted(unknown)}")
                continue
            host_in = d.get("host")
            if host_in is None:
                if i == 0:
                    host_in = f"{sid}.{manifest.domain_suffix()}"
                else:
                    details.append(f"endpoints[{i}] 缺少 host（仅第一个入口可用服务 id 缺省域名）")
                    continue
            if not isinstance(host_in, str):
                details.append(f"endpoints[{i}].host 必须是字符串")
                continue
            try:
                host = manifest._norm_host(host_in)
            except ValueError as e:
                details.append(str(e))
                continue
            if host in seen:
                details.append(f"endpoints[{i}] host 重复：{host}")
                continue
            seen.add(host)
            comp = d.get("component")
            if comp is None:
                details.append(f"endpoints[{i}]（{host}）缺少 component")
                continue
            if comp not in comp_ids:
                details.append(f"endpoints[{i}]（{host}）引用未知组件 {comp!r}")
                continue
            open_path = d.get("open_path", "/")
            if not isinstance(open_path, str):
                details.append(f"endpoints[{i}]（{host}）open_path 必须是字符串")
                continue
            out.append({"id": str(d.get("id") or ("default" if i == 0 else f"ep-{i + 1}")),
                        "host": host, "component": comp,
                        "open_path": manifest._norm_open_path(open_path)})
        return out

    def set_language(self, language: object) -> str:
        if not isinstance(language, str) or language not in ("zh-CN", "en"):
            raise RegistryError("language must be zh-CN or en")
        with _file_lock():
            data = self._load()
            settings = data.setdefault("settings", {})
            if not isinstance(settings, dict):
                raise RegistryError("registry.settings must be an object", status=500)
            if settings.get("language") != language:
                settings["language"] = language
                self._save(data)
        return language

    # ---------- Locove 域名设置（V3.5：默认直连 IP；D3：可选 5670 域名模块） ----------

    LOCOVE_FORMS = ("suffix", "custom")
    # 业务代理端口禁区：控制面 47800、Caddy admin 47891、本地 DNS 5354；
    # <1024 需 root 绑定，违背「普通用户运行」前提。缺省 5670。
    LOCOVE_PORT_FORBIDDEN = {47800, 47891, 5354}
    LOCOVE_PORT_DEFAULT = 5670

    @classmethod
    def locove_port_valid(cls, port: object) -> bool:
        return (isinstance(port, int) and not isinstance(port, bool)
                and 1024 <= port <= 65535 and port not in cls.LOCOVE_PORT_FORBIDDEN)

    def locove_settings(self) -> dict:
        """{"use_domains", "domain_form", "domain_port"}；缺省全部关闭/后缀式/5670。"""
        with _file_lock():
            data = self._load()
        st = data.get("settings") or {}
        port = st.get("domain_port", self.LOCOVE_PORT_DEFAULT)
        return {"use_domains": bool(st.get("use_domains", False)),
                "domain_form": st.get("domain_form", "suffix") if st.get("domain_form") in ("suffix", "custom") else "suffix",
                "domain_port": port if self.locove_port_valid(port) else self.LOCOVE_PORT_DEFAULT,
                "domains_enabled_at": float(st.get("domains_enabled_at") or 0) or None}

    def locove_settings_update(self, body: dict) -> dict:
        body = body if isinstance(body, dict) else {}
        use = body.get("use_domains")
        form = body.get("domain_form")
        port = body.get("domain_port")
        enabled_at = body.get("domains_enabled_at")
        if use is not None and not isinstance(use, bool):
            raise RegistryError("use_domains 必须是布尔值")
        if form is not None and form not in self.LOCOVE_FORMS:
            raise RegistryError(f"domain_form 必须是 {'/'.join(self.LOCOVE_FORMS)}")
        if port is not None and not self.locove_port_valid(port):
            raise RegistryError(f"domain_port 必须是 1024–65535 的整数且不得使用控制面端口"
                                f"（{sorted(self.LOCOVE_PORT_FORBIDDEN)}）；缺省 {self.LOCOVE_PORT_DEFAULT}")
        if enabled_at is not None and not isinstance(enabled_at, (int, float)):
            raise RegistryError("domains_enabled_at 必须是数字时间戳（启用流程内部写入）")
        # 开启域名跳转必须同时有明确形式（缺省视为 suffix，显式传 null 拒绝）
        with _file_lock():
            data = self._load()
            st = data.setdefault("settings", {})
            if not isinstance(st, dict):
                raise RegistryError("registry.settings 损坏（不是对象），拒绝覆盖", status=500)
            if use is not None:
                st["use_domains"] = use
            if form is not None:
                st["domain_form"] = form
            if port is not None:
                st["domain_port"] = port
            if enabled_at is not None:
                st["domains_enabled_at"] = float(enabled_at)
            st["updated_at"] = _now()
            self._save(data)
        return self.locove_settings()

    def _validate(self, payload: dict, warnings: list[str]) -> dict:
        details: list[str] = []
        svc = payload["service"]
        pj = payload.get("project") or {}
        if not isinstance(pj, dict):
            raise RegistryError("project 必须是对象", details=["project"])
        pj_id = pj.get("id")
        if pj_id is not None and (not isinstance(pj_id, str) or not ID_RE.fullmatch(pj_id)):
            details.append(f"project.id 非法：{pj_id!r}")
        pj_name = pj.get("name")
        if pj_name is not None and (not isinstance(pj_name, str) or not pj_name.strip()):
            details.append("project.name 必须是非空字符串")
        agent = pj.get("agent")
        if agent is not None and agent not in AGENTS:
            details.append(f"project.agent 必须是 {'/'.join(AGENTS)}")
        by = payload.get("by")
        if by is not None and by not in REGISTRANTS:
            by = None

        dirs = _norm_directories(payload.get("directories"), details)

        for key in ("description", "open", "adapter"):
            v = svc.get(key)
            if v is not None and not isinstance(v, str):
                details.append(f"service.{key} 必须是字符串")
        for key in ("autostart", "observe_only"):
            v = svc.get(key)
            if v is not None and not isinstance(v, bool):
                details.append(f"service.{key} 必须是布尔值")
        hint = svc.get("project_hint")
        if hint is not None and (not isinstance(hint, str) or not ID_RE.fullmatch(hint)):
            details.append(f"service.project_hint 非法：{hint!r}")
        confirmed = svc.get("confirmed_provision")

        src = _resolved(svc["source"])
        if not src.is_dir():
            details.append(f"source 目录不存在：{svc['source']}")
        sid = svc.get("id")
        if sid is None:
            sid = pj_id or hint or _slug(svc["name"])
        if not isinstance(sid, str) or not ID_RE.fullmatch(sid or ""):
            details.append(f"service.id 非法：{sid!r}（须匹配 ^[a-z0-9][a-z0-9-]*$）")
            sid = sid if isinstance(sid, str) else "invalid"
        pid = pj_id or hint or sid
        _raise_details(details)

        # 目录越界防护：cwd 必须 ==/位于 source 内，或 ==/位于 Project 已登记（含本次声明）目录内
        roots = [src] + [Path(rp) for rp, _ in dirs]
        reg = self._load()
        proj = _find(reg, pid)
        if proj:
            roots += [Path(d["path"]) for d in proj.get("directories", []) if Path(d["path"]).is_dir()]

        comps: list[dict] = []
        seen_ids: set[str] = set()
        ports_seen: dict[int, str] = {}
        for c in svc["components"]:
            nc = _norm_component(c, details)
            if nc is None:
                continue
            if nc["id"] in seen_ids:
                details.append(f"组件 id 重复：{nc['id']}")
                continue
            seen_ids.add(nc["id"])
            if nc.get("provision") == "up" and not confirmed:
                details.append(f"组件 {nc['id']} provision=up 需要显式确认"
                               "（service.confirmed_provision=true，允许 compose 创建容器/拉取镜像）")
            cwd = Path(nc.get("cwd", "."))
            cwd_abs = (cwd if cwd.is_absolute() else src / cwd).resolve()
            if not cwd_abs.is_dir():
                details.append(f"组件 {nc['id']} cwd 不存在：{nc.get('cwd', '.')}")
            elif not any(_inside(cwd_abs, r) for r in roots):
                details.append(f"组件 {nc['id']} cwd 超出声明的目录范围：{cwd_abs}")
            comp_cwd = Path(nc.get("cwd", ".") or ".")
            _check_cmd(nc, src, details, warnings, comp_cwd=comp_cwd)
            if nc.get("port") is not None:
                if nc["port"] in ports_seen:
                    details.append(f"端口 {nc['port']} 同时用于 {ports_seen[nc['port']]} 与 {nc['id']}")
                else:
                    ports_seen[nc["port"]] = nc["id"]
            _check_ready_port(nc, details)
            nc.pop("_shell_script", None)
            comps.append(nc)
        _raise_details(details)

        deps: list[dict] = []
        for d in svc.get("dependencies") or []:
            nd = _norm_dependency(d, details, warnings)
            if nd is not None:
                deps.append(nd)
        _raise_details(details)

        comp_objs = [manifest._component(dict(c)) for c in comps]
        # V3 依赖图：未知 after 引用 / 环在提交前失败（与加载端同一校验）
        try:
            manifest.validate_component_graph(
                manifest.Project(id=sid, name=svc["name"], source=src, components=comp_objs))
        except ValueError as e:
            details.append(str(e))
        _raise_details(details)

        # V3 公开入口：显式 endpoints 规范化进 TOML；未提供时由加载端按稳定规则派生
        endpoints_out = self._norm_endpoints(svc.get("endpoints"), comp_objs, sid, details)
        _raise_details(details)

        toml = {"id": sid, "name": svc["name"], "source": str(src)}
        for key in ("description", "open", "adapter"):
            v = svc.get(key)
            # 空 description 视为"未提供"：更新时经 merge 保留用户手写的旧描述
            # （GUI 恒携带该字段，重新识别没有描述时发 ""——不能借此清空人工描述）
            if v is not None and (key != "description" or v != ""):
                toml[key] = v
        for key in ("autostart", "observe_only"):
            if svc.get(key) is not None:
                toml[key] = svc[key]
        toml["project"] = pid
        toml["components"] = [{k: c[k] for k in _COMP_ORDER if k in c} for c in comps]
        toml["dependencies"] = [{k: d[k] for k in _DEP_ORDER if k in d} for d in deps]
        if endpoints_out is not None:
            toml["endpoints"] = endpoints_out

        evidence = svc.get("evidence") or {}
        ev_agent = evidence.get("agent") if isinstance(evidence, dict) else None
        summary = evidence.get("summary") if isinstance(evidence, dict) else ""
        registered = {"at": _now(), "by": by or "cli",
                      "agent": ev_agent or agent or "none",
                      "evidence_summary": summary if isinstance(summary, str) else ""}

        return {"sid": sid, "pid": pid, "src": src, "comps": comp_objs, "toml": toml,
                "dirs": [(str(src), "source")] + dirs, "pj_name": (pj_name or "").strip() or None,
                "agent": agent, "registered": registered}


def _ok(action: str, pid: str, sid: str, revision: int, warnings: list[str]) -> dict:
    return {"ok": True, "action": action, "project_id": pid, "service_id": sid,
            "revision": revision, "draft_id": None, "validation": {"warnings": warnings}}


def _raise_details(details: list[str]) -> None:
    if details:
        raise RegistryError(details[0], details=details)


def _component_complete(c) -> bool:
    if not isinstance(c, dict):
        return False
    if not isinstance(c.get("id"), str) or not c.get("id"):
        return False
    cmd = c.get("cmd")
    has_cmd = isinstance(cmd, list) and bool(cmd) and all(isinstance(x, str) for x in cmd)
    has_script = isinstance(c.get("shell_script"), str) and bool(c.get("shell_script"))
    if has_cmd or has_script:
        return True
    # V2.7：run=compose 组件无本地 cmd；缺 compose_service 由 _validate 给出精确错误
    return c.get("run") == "compose"


def _missing_fields(svc: dict, comps_in: list) -> list[str]:
    """不完整 payload 的缺项清单（dry_run action=incomplete 用）。"""
    missing: list[str] = []
    if bool(svc.get("draft")):
        missing.append("service.draft")
    if not isinstance(svc.get("name"), str) or not svc.get("name", "").strip():
        missing.append("service.name")
    if not isinstance(svc.get("source"), str) or not svc.get("source", "").strip():
        missing.append("service.source")
    if not comps_in:
        missing.append("service.components")
    for c in comps_in:
        cid = c.get("id") if isinstance(c, dict) else None
        label = cid if isinstance(cid, str) and cid else "<缺失 id>"
        if not isinstance(cid, str) or not cid:
            missing.append(f"components[{label}].id")
        elif not _component_complete(c):
            missing.append(f"components[{label}].cmd")
    return missing


def _norm_directories(raw, details: list[str] | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not raw:
        return out
    if not isinstance(raw, list):
        if details is not None:
            details.append("directories 必须是数组")
        return out
    for d in raw:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"].strip():
            if details is not None:
                details.append(f"目录项非法：{d!r}")
            continue
        role = d.get("role", "source")
        if not isinstance(role, str) or not role.strip():
            role = "source"
        rp = _resolved(d["path"])
        if not rp.is_dir():
            if details is not None:
                details.append(f"目录不存在：{d['path']}")
            continue
        if all(rp != Path(p) for p, _ in out):
            out.append((str(rp), role))
    return out


def _add_directory_entry(proj: dict, resolved_path: str, role: str) -> bool:
    if any(d.get("path") == resolved_path for d in proj.get("directories", [])):
        return False
    proj.setdefault("directories", []).append(
        {"id": _dir_id(resolved_path), "path": resolved_path, "role": role})
    return True


def _norm_component(c: dict, details: list[str]) -> dict | None:
    cid = c.get("id") if isinstance(c.get("id"), str) else None
    label = cid or "<缺失 id>"
    unknown = set(c) - COMP_KEYS
    if unknown:
        details.append(f"组件 {label} 含未知字段：{sorted(unknown)}")
        return None
    if not cid or not COMPONENT_ID_RE.fullmatch(cid):
        details.append(f"组件 id 非法：{cid!r}")
        return None
    out: dict = {"id": cid}
    if "kind" in c and c["kind"] is not None:
        if c["kind"] not in COMPONENT_KINDS:
            details.append(f"组件 {cid} kind 非法：{c['kind']!r}")
            return None
        out["kind"] = c["kind"]
    if c.get("name") is not None:
        if not isinstance(c["name"], str):
            details.append(f"组件 {cid} name 必须是字符串")
            return None
        out["name"] = c["name"]
    if c.get("cwd") not in (None, ""):
        if not isinstance(c["cwd"], str):
            details.append(f"组件 {cid} cwd 必须是字符串")
            return None
        out["cwd"] = c["cwd"]
    cmd = c.get("cmd")
    run = c.get("run")
    if run not in (None, "local", "compose"):
        details.append(f"组件 {cid} run 必须是 local/compose：{run!r}")
        return None
    if run is not None:
        out["run"] = run
    if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
        out["cmd"] = list(cmd)
    elif isinstance(c.get("shell_script"), str) and c["shell_script"]:
        # shell 入口必须指向工程内已存在脚本（禁止字符串拼接隐式 shell）
        out["cmd"] = ["/bin/sh", c["shell_script"]]
        out["_shell_script"] = c["shell_script"]
    elif run != "compose":
        details.append(f"组件 {cid} 缺少 cmd 或 shell_script")
        return None
    if c.get("port") is not None:
        if isinstance(c["port"], bool) or not isinstance(c["port"], int) or not 1 <= c["port"] <= 65535:
            details.append(f"组件 {cid} port 必须是 1–65535 的整数")
            return None
        out["port"] = c["port"]
    if c.get("ready") is not None:
        r = c["ready"]
        if not isinstance(r, dict) or set(r) - READY_KEYS:
            details.append(f"组件 {cid} ready 结构非法")
            return None
        nr: dict = {}
        for k in ("http", "tcp", "json_ok"):
            if r.get(k) is not None:
                if not isinstance(r[k], str):
                    details.append(f"组件 {cid} ready.{k} 必须是字符串")
                    return None
                nr[k] = r[k]
        for k in ("timeout", "interval"):
            if r.get(k) is not None:
                if isinstance(r[k], bool) or not isinstance(r[k], (int, float)):
                    details.append(f"组件 {cid} ready.{k} 必须是数值")
                    return None
                nr[k] = float(r[k])
        out["ready"] = nr
    if c.get("env") is not None:
        env = c["env"]
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            details.append(f"组件 {cid} env 必须是 str→str 映射")
            return None
        out["env"] = dict(env)
    for key in ("env_files", "after", "watch", "restart_on", "ignore", "build", "install", "install_when"):
        if c.get(key) is not None:
            if not _is_str_list(c[key]):
                details.append(f"组件 {cid} {key} 必须是字符串数组")
                return None
            if c[key]:
                out[key] = list(c[key])
    for key in ("required", "hot", "auto_update", "external_cwd_exact"):
        if c.get(key) is not None:
            if not isinstance(c[key], bool):
                details.append(f"组件 {cid} {key} 必须是布尔值")
                return None
            out[key] = c[key]
    for key in ("start_timeout", "debounce"):
        if c.get(key) is not None:
            if isinstance(c[key], bool) or not isinstance(c[key], (int, float)):
                details.append(f"组件 {cid} {key} 必须是数值")
                return None
            out[key] = float(c[key])
    for key in ("install_marker", "external_cmd", "external_cwd"):
        if c.get(key) is not None:
            if not isinstance(c[key], str):
                details.append(f"组件 {cid} {key} 必须是字符串")
                return None
            out[key] = c[key]
    # V2.7 运行适配字段（run=compose 无本地 cmd；manifest v2 向后兼容）
    if run == "compose":
        if not isinstance(c.get("compose_service"), str) or not c.get("compose_service"):
            details.append(f"组件 {cid} run=compose 需要 compose_service")
            return None
    for key in ("compose_service", "compose_project"):
        if c.get(key) is not None:
            if not isinstance(c[key], str):
                details.append(f"组件 {cid} {key} 必须是字符串")
                return None
            out[key] = c[key]
    if c.get("compose_files") is not None:
        if not _is_str_list(c["compose_files"]):
            details.append(f"组件 {cid} compose_files 必须是字符串数组")
            return None
        out["compose_files"] = list(c["compose_files"])
    if c.get("provision") is not None:
        if c["provision"] not in ("existing", "up"):
            details.append(f"组件 {cid} provision 必须是 existing/up")
            return None
        out["provision"] = c["provision"]
    return out


def _norm_dependency(d: dict, details: list[str], warnings: list[str]) -> dict | None:
    if not isinstance(d, dict):
        details.append(f"依赖项非法：{d!r}")
        return None
    unknown = set(d) - DEP_KEYS
    if unknown:
        details.append(f"依赖 {d.get('name')} 含未知字段：{sorted(unknown)}")
        return None
    if not isinstance(d.get("name"), str) or not d["name"].strip():
        details.append("依赖 name 不能为空")
        return None
    probe = d.get("probe")
    if not isinstance(probe, dict) or set(probe) - PROBE_KEYS:
        details.append(f"依赖 {d['name']} probe 结构非法")
        return None
    if not probe.get("tcp") and not probe.get("http"):
        details.append(f"依赖 {d['name']} probe 至少需要 tcp 或 http 之一")
        return None
    np: dict = {}
    for k in ("tcp", "http", "json_ok"):
        if probe.get(k) is not None:
            if not isinstance(probe[k], str):
                details.append(f"依赖 {d['name']} probe.{k} 必须是字符串")
                return None
            np[k] = probe[k]
    if probe.get("timeout") is not None:
        if isinstance(probe["timeout"], bool) or not isinstance(probe["timeout"], (int, float)):
            details.append(f"依赖 {d['name']} probe.timeout 必须是数值")
            return None
        np["timeout"] = float(probe["timeout"])
    out: dict = {"name": d["name"], "probe": np}
    if d.get("affects") is not None:
        if not _is_str_list(d["affects"]):
            details.append(f"依赖 {d['name']} affects 必须是字符串数组")
            return None
        out["affects"] = list(d["affects"])
    for key in ("managed", "required"):
        if d.get(key) is not None:
            if not isinstance(d[key], bool):
                details.append(f"依赖 {d['name']} {key} 必须是布尔值")
                return None
            out[key] = d[key]
    for key in ("note", "container", "volume"):
        if d.get(key) is not None:
            if not isinstance(d[key], str):
                details.append(f"依赖 {d['name']} {key} 必须是字符串")
                return None
            out[key] = d[key]
    resource = d.get("resource")
    if resource is not None:
        if not isinstance(resource, str) or not resource.startswith("container:") or not resource.removeprefix("container:"):
            details.append(f"依赖 {d['name']} resource 必须形如 container:<name>")
            return None
        name = resource.removeprefix("container:")
        # 映射到既有 Dependency.container 字段（manifest schema 的既有引用方式）
        out["container"] = out.get("container") or name
        _check_resource_ref(d["name"], resource, name, details, warnings)
    return out


def _check_resource_ref(dep_name: str, ref: str, name: str, details: list[str], warnings: list[str]) -> None:
    try:
        inv = resources.inventory()
    except Exception as e:  # 盘点本身崩溃按不可达处理
        inv = {"ok": False, "error": str(e)}
    if not inv.get("ok"):
        warnings.append(f"依赖 {dep_name} 的资源引用 {ref} 未验证（Docker 不可达：{inv.get('error', '')}）")
        return
    res = {r.get("name"): r for r in inv.get("resources", [])}
    if name not in res:
        details.append(f"依赖 {dep_name} 引用的资源不存在：{ref}")
        return
    state = res[name].get("state", "unknown")
    if state in ("exited", "created", "paused"):
        # V2.6：已停止容器仍是合法引用（启动时按需恢复），但要显式告知，不能静默当运行中；
        # unknown（旧盘点形状未带 state）不告警
        warnings.append(f"依赖 {dep_name} 的容器 {name} 当前状态为 {state}（已停止，启动时将按需恢复）")


def _check_cmd(nc: dict, src: Path, details: list[str], warnings: list[str], comp_cwd: Path | None = None) -> None:
    cid = nc["id"]
    if nc.get("run") == "compose":
        return  # V2.7：compose 组件由 docker compose 启动，无本地 cmd 可解析
    script = nc.get("_shell_script")
    if script is not None:
        p = (src / script).resolve()
        if not p.is_file() or not _inside(p, src):
            details.append(f"组件 {cid} shell_script 必须是 source 内已存在文件：{script}")
            return
        warnings.append(f"组件 {cid} 使用 shell_script，由 /bin/sh 执行 {script}")
    arg0 = nc["cmd"][0]
    if arg0.startswith("{build}"):
        warnings.append(f"组件 {cid} cmd 使用 {{build}} 占位符，未验证可执行")
        return
    if _resolve_cmd0(arg0, src, comp_cwd):
        return
    details.append(f"组件 {cid} cmd[0] 无法解析：{arg0}")


def _resolve_cmd0(arg0: str, src: Path, comp_cwd: Path | None = None) -> bool:
    p = Path(os.path.expanduser(arg0))
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(src / p)
        candidates.append(src / "node_modules" / ".bin" / p.name)
        # 组件 cwd 相对（monorepo 前端标准形态：cwd=apps/web + node_modules/.bin/vite；
        # 运行时 runner 在 comp cwd 下 execvpe，注册校验必须与运行时同源解析）
        if comp_cwd is not None:
            base = comp_cwd if comp_cwd.is_absolute() else src / comp_cwd
            candidates.append(base / p)
            candidates.append(base / "node_modules" / ".bin" / p.name)
    for d in manifest.DEFAULT_PATH.split(":"):
        if d:
            candidates.append(Path(d) / p.name if not p.is_absolute() else p)
    return any(c.is_file() and os.access(c, os.X_OK) for c in candidates)


def _check_ready_port(nc: dict, details: list[str]) -> None:
    ready = nc.get("ready")
    if not ready:
        return
    cid = nc["id"]
    port = nc.get("port")
    ports: list[int] = []
    if ready.get("http"):
        try:
            parts = urllib.parse.urlsplit(ready["http"])
            p = parts.port
            if p is None:
                p = 443 if (parts.scheme or "http").lower() == "https" else 80
            ports.append(p)
        except ValueError:
            details.append(f"组件 {cid} ready.http URL 非法：{ready['http']}")
            return
    if ready.get("tcp"):
        host, _, p = ready["tcp"].rpartition(":")
        try:
            ports.append(int(p))
        except ValueError:
            details.append(f"组件 {cid} ready.tcp 必须形如 host:port：{ready['tcp']}")
            return
    if port is None:
        if ports:
            details.append(f"组件 {cid} 声明了 ready 探针端口但未声明 port")
        return
    for p in ports:
        if p != port:
            details.append(f"组件 {cid} ready 探针端口 {p} 与组件端口 {port} 不一致")


def dataclass_to_comp_dict(c) -> dict:
    """Component dataclass → TOML 层 dict（内容比较用，含默认值）。"""
    d = dataclasses.asdict(c)
    ready = d.pop("ready", None)
    out = {k: v for k, v in d.items() if k in COMP_KEYS and k != "shell_script"}
    if ready is not None:
        out["ready"] = {k: v for k, v in ready.items() if v is not None}
    return {k: v for k, v in out.items() if v not in (None, [], {}) or k in ("id", "cmd")}


def dep_to_dict(d) -> dict:
    dd = dataclasses.asdict(d)
    probe = dd.pop("probe", {})
    return {k: v for k, v in dd.items() if k in _DEP_FIELDS and v not in (None, [], "")} | (
        {"probe": {k: v for k, v in probe.items() if v is not None}} if probe else {})
