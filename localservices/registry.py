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
import threading
import time
import tomllib
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from . import manifest, paths, resources

SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COMPONENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
AGENTS = ("codex", "claude", "none")
REGISTRANTS = ("gui", "cli", "skill")
DISPOSITIONS = ("stop", "keep-external")

SERVICE_KEYS = {"id", "name", "description", "source", "open", "autostart", "adapter",
                "observe_only", "draft", "project_hint", "components", "dependencies", "evidence"}
COMP_KEYS = {"id", "kind", "name", "cmd", "shell_script", "cwd", "env", "env_files", "port", "ready",
             "required", "start_timeout", "after", "hot", "watch", "restart_on", "ignore", "build",
             "install", "install_when", "install_marker", "auto_update", "external_cmd",
             "external_cwd", "external_cwd_exact", "debounce"}
READY_KEYS = {"http", "tcp", "json_ok", "timeout", "interval"}
DEP_KEYS = {"name", "probe", "affects", "managed", "note", "required", "container", "volume", "resource"}
PROBE_KEYS = {"http", "tcp", "json_ok", "timeout"}
COMPONENT_KINDS = ("server", "worker", "generator")

# TOML 输出时组件/依赖字段的固定顺序（只写显式提供的字段，缺省由加载端补齐）
_COMP_ORDER = ["id", "kind", "name", "cwd", "cmd", "env", "env_files", "port", "ready", "required",
               "start_timeout", "after", "hot", "watch", "restart_on", "ignore", "build", "install",
               "install_when", "install_marker", "auto_update", "external_cmd", "external_cwd",
               "external_cwd_exact", "debounce"]
_DEP_ORDER = ["name", "probe", "affects", "managed", "note", "required", "container", "volume"]

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
                "external_cwd_exact", "debounce")
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
    )


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


# ---------------------------------------------------------------- Registry

class Registry:
    """工作台注册权威。实例无内部缓存（每次读盘），单例与多实例同样安全。

    registry.json 缺失即视为空注册表（惰性加载）。"""

    # ---------- 读取 ----------

    def _load(self) -> dict:
        try:
            data = json.loads(paths.registry_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schemaVersion", SCHEMA_VERSION)
        data.setdefault("revision", 0)
        data.setdefault("projects", [])
        return data

    def _save(self, data: dict) -> None:
        data["schemaVersion"] = SCHEMA_VERSION
        _write_bytes_atomic(paths.registry_file(),
                            json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def data(self) -> dict:
        return copy.deepcopy(self._load())

    def projects(self) -> list[dict]:
        return copy.deepcopy(self._load()["projects"])

    def project(self, pid: str) -> dict | None:
        p = _find(self._load(), pid)
        return copy.deepcopy(p) if p else None

    # ---------- Project / Directory ----------

    def ensure_project(self, pid: str | None, name: str, directories: list[dict] | None,
                       agent: str | None, origin: str = "user") -> tuple[dict, bool]:
        """创建或更新工作台；返回 (project, created)。"""
        with _file_lock():
            data = self._load()
            derived = pid or _slug(name or "")
            if not derived or not ID_RE.fullmatch(derived):
                raise RegistryError(f"项目 id 非法：{derived!r}")
            if not isinstance(name, str) or not name.strip():
                raise RegistryError("项目 name 不能为空")
            if agent is not None and agent not in AGENTS:
                raise RegistryError(f"agent 必须是 {'/'.join(AGENTS)}")
            dirs = _norm_directories(directories)
            proj = _find(data, derived)
            created = proj is None
            changed = created
            if created:
                proj = _new_project(derived, name.strip(), origin=origin)
                data["projects"].append(proj)
            else:
                if name.strip() and proj.get("name") != name.strip():
                    proj["name"] = name.strip()
                    changed = True
                if agent and proj.get("agent") != agent:
                    proj["agent"] = agent
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

    def register_service(self, payload: dict) -> dict:
        try:
            return self._register(payload)
        except RegistryError as e:
            out = dict(e.payload)
            out.setdefault("details", [])
            return out

    def _register(self, payload: dict) -> dict:
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

        comps_in = svc.get("components")
        comps_in = comps_in if isinstance(comps_in, list) else []
        incomplete = (bool(svc.get("draft"))
                      or not isinstance(svc.get("name"), str) or not svc.get("name", "").strip()
                      or not isinstance(svc.get("source"), str) or not svc.get("source", "").strip()
                      or not comps_in
                      or not all(_component_complete(c) for c in comps_in))
        if incomplete:
            return self.save_draft(payload)  # 草稿绝不进 user_services_dir

        warnings: list[str] = []
        norm = self._validate(payload, warnings)  # 全部校验通过才继续
        sid, pid, src, comp_objs, toml = (norm["sid"], norm["pid"], norm["src"], norm["comps"], norm["toml"])

        with _file_lock():
            data = self._load()
            try:
                loaded = manifest.load_all()
            except Exception as e:  # 现有清单坏：宁可拒绝也不叠加写入
                return {"ok": False, "error": f"无法加载现有清单：{e}", "details": []}

            existing = loaded.get(sid)
            proj = _find(data, pid)
            ident = _identity(src, comp_objs)

            # 身份判定先行：unchanged / duplicate 都不落盘、不检查端口与 revision
            if existing is not None:
                owner_pid = existing.project or next(
                    (x["id"] for x in data.get("projects", []) if sid in x.get("services", [])), None)
                if owner_pid and owner_pid != pid:
                    return {"ok": False, "error": f"服务 id {sid} 已属于项目 {owner_pid}", "details": []}
                if (_identity(_resolved(existing.source), existing.components) == ident
                        and self._content_equal(toml, existing)):
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
                if dup:
                    return _ok("duplicate", pid, dup, (proj or {}).get("revision", 0), warnings)
                gate = _revision_gate(proj, revision)
                if gate:
                    return gate
                action = "created"

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

            # ---- 校验完成，开始落盘（先 TOML 后 registry，均为原子写） ----
            toml["registered"] = norm["registered"]
            if existing is not None and existing.file:
                merged = self._merge_existing(Path(existing.file), toml)
                target, text = Path(existing.file), dumps_toml(merged)
            else:
                target, text = paths.user_services_dir() / f"{sid}.toml", dumps_toml(toml)
            _write_bytes_atomic(target, text.encode("utf-8"))

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
            self._save(data)
            return _ok(action, pid, sid, proj["revision"], warnings)

    def _content_equal(self, toml: dict, existing: manifest.Project) -> bool:
        try:
            existing_d = {
                "name": existing.name, "description": existing.description,
                "source": str(existing.source), "open": existing.open,
                "autostart": existing.autostart, "observe_only": existing.observe_only,
                "adapter": existing.adapter,
                "components": [dataclass_to_comp_dict(c) for c in existing.components],
                "dependencies": [dep_to_dict(d) for d in existing.dependencies],
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

    # ---------- 移除登记 ----------

    def remove_service(self, sid: str, disposition: str, ctl) -> dict:
        if disposition not in DISPOSITIONS:
            raise RegistryError(f"disposition 必须是 {'/'.join(DISPOSITIONS)}")
        if disposition == "stop":
            if ctl is None:
                raise RegistryError("controller unavailable", status=503)
            self._stop_via_controller(ctl, sid)
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
            if p.file and Path(p.file).exists():
                Path(p.file).unlink()  # 只删登记，业务数据/源码永不删除
            if owner is not None:
                owner["services"] = [s for s in owner.get("services", []) if s != sid]
                owner["revision"] = owner.get("revision", 0) + 1
                owner["updated_at"] = _now()
                data["revision"] = data.get("revision", 0) + 1
                self._save(data)
            return {"ok": True, "service_id": sid, "disposition": disposition,
                    "project_id": owner.get("id") if owner else None,
                    "revision": owner.get("revision") if owner else None}

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

    # ---------- 迁移（CONTRACTS §7，一次性且幂等） ----------

    def migrate_from_manifests(self) -> dict:
        ts = time.strftime("%Y%m%d-%H%M%S")
        with _file_lock():
            try:
                loaded = manifest.load_all()
            except Exception as e:
                return {"ok": False, "error": f"无法加载清单：{e}"}

            groups: dict[str, list] = {}
            desired: list[dict] = []
            for p in loaded.values():
                if p.group:
                    groups.setdefault(p.group, []).append(p)
                else:
                    desired.append({"id": p.id, "name": p.name, "services": [p.id], "sources": [p.source]})
            for group, members in groups.items():
                members.sort(key=lambda p: (p.group_order or 0, p.id))
                desired.append({"id": group, "name": members[0].group_name or group,
                                "services": [m.id for m in members], "sources": [m.source for m in members]})
            desired.sort(key=lambda d: d["id"])

            # 1) 需要追加 project= 的 TOML（内置 services/ 与用户目录一视同仁）
            toml_writes: list[tuple[Path, bytes, bytes]] = []
            for spec in desired:
                for sid in spec["services"]:
                    file = Path(loaded[sid].file)
                    orig = file.read_bytes()
                    parsed = tomllib.loads(orig.decode("utf-8"))
                    if parsed.get("project") == spec["id"]:
                        continue
                    new = _insert_project_line(orig.decode("utf-8"), spec["id"]).encode("utf-8")
                    toml_writes.append((file, orig, new))

            # 2) registry 合并（保留用户已有的项目与目录，仅补齐迁移映射）
            reg_path = paths.registry_file()
            old_bytes = reg_path.read_bytes() if reg_path.exists() else None
            data = self._load()
            registry_changed = False
            for spec in desired:
                proj = _find(data, spec["id"])
                if proj is None:
                    proj = _new_project(spec["id"], spec["name"], origin="migrated")
                    data["projects"].append(proj)
                    registry_changed = True
                if proj.get("origin") == "migrated" and proj.get("name") != spec["name"]:
                    proj["name"] = spec["name"]
                    registry_changed = True
                for src in spec["sources"]:
                    rp = str(Path(src).resolve())
                    if not any(d.get("path") == rp for d in proj.get("directories", [])):
                        proj.setdefault("directories", []).append(
                            {"id": _dir_id(rp), "path": rp, "role": "source"})
                        registry_changed = True
                for sid in spec["services"]:
                    if sid not in proj.get("services", []):
                        proj.setdefault("services", []).append(sid)
                        registry_changed = True

            mapping = [{"id": x["id"], "name": x.get("name"),
                        "services": list(x.get("services", [])),
                        "directories": copy.deepcopy(x.get("directories", []))}
                       for x in sorted(data["projects"], key=lambda p: p["id"])]

            if not toml_writes and not registry_changed:
                return {"ok": True, "changed": False, "projects": mapping}

            # 3) 备份 + 写入；任何失败恢复原字节
            artifacts: list[Path] = []  # 备份/标记文件（失败时删除）
            new_reg = (json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
                       if registry_changed else None)
            try:
                for file, _, new in toml_writes:
                    _write_bytes_atomic(file, new)
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
            except Exception as e:
                for file, orig, _ in toml_writes:
                    try:
                        file.write_bytes(orig)  # 已写/未写文件一律恢复原字节
                    except OSError:
                        pass
                for path in artifacts:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                try:
                    if old_bytes is None and reg_path.exists():
                        reg_path.unlink()
                    elif old_bytes is not None:
                        reg_path.write_bytes(old_bytes)
                except OSError:
                    pass
                return {"ok": False, "error": f"迁移失败，已恢复原状：{e}"}
            return {"ok": True, "changed": True, "projects": mapping}

    # ---------- 校验辅助 ----------

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
            cwd = Path(nc.get("cwd", "."))
            cwd_abs = (cwd if cwd.is_absolute() else src / cwd).resolve()
            if not cwd_abs.is_dir():
                details.append(f"组件 {nc['id']} cwd 不存在：{nc.get('cwd', '.')}")
            elif not any(_inside(cwd_abs, r) for r in roots):
                details.append(f"组件 {nc['id']} cwd 超出声明的目录范围：{cwd_abs}")
            _check_cmd(nc, src, details, warnings)
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
        toml = {"id": sid, "name": svc["name"], "source": str(src)}
        for key in ("description", "open", "adapter"):
            if svc.get(key) is not None:
                toml[key] = svc[key]
        for key in ("autostart", "observe_only"):
            if svc.get(key) is not None:
                toml[key] = svc[key]
        toml["project"] = pid
        toml["components"] = [{k: c[k] for k in _COMP_ORDER if k in c} for c in comps]
        toml["dependencies"] = [{k: d[k] for k in _DEP_ORDER if k in d} for d in deps]

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
    return has_cmd or has_script


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
    if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
        out["cmd"] = list(cmd)
    elif isinstance(c.get("shell_script"), str) and c["shell_script"]:
        # shell 入口必须指向工程内已存在脚本（禁止字符串拼接隐式 shell）
        out["cmd"] = ["/bin/sh", c["shell_script"]]
        out["_shell_script"] = c["shell_script"]
    else:
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
    names = {r.get("name") for r in inv.get("resources", [])}
    if name not in names:
        details.append(f"依赖 {dep_name} 引用的资源不存在：{ref}")


def _check_cmd(nc: dict, src: Path, details: list[str], warnings: list[str]) -> None:
    cid = nc["id"]
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
    if _resolve_cmd0(arg0, src):
        return
    details.append(f"组件 {cid} cmd[0] 无法解析：{arg0}")


def _resolve_cmd0(arg0: str, src: Path) -> bool:
    p = Path(os.path.expanduser(arg0))
    candidates: list[Path] = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(src / p)
        candidates.append(src / "node_modules" / ".bin" / p.name)
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
