"""daemon 注册面处理函数（CONTRACTS §2/§7 端点）：纯函数，不碰 HTTP 细节。

daemon.py 只负责路由/token/序列化；这里的函数永不 raise（意外错误兜底为 500）。
ctl 是 Controller 实例，可能为 None（单元测试）：涉及 ctl 的功能返回
{"ok": False, "error": "controller unavailable"}，其余功能照常。
Registry 为模块级单例（进程内共享；Registry 自身无缓存，读盘即时生效）。
"""

from __future__ import annotations

from . import manifest
from .registry import ID_RE, Registry, RegistryError

_registry = Registry()  # 进程内单例


def _fail(e: Exception) -> tuple[int, dict]:
    if isinstance(e, RegistryError):
        return e.status, e.payload
    return 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _register_status(res: dict) -> tuple[int, dict]:
    if res.get("ok"):
        return 200, res
    if res.get("error") == "revision conflict":
        return 409, res
    return 400, res


# ---------------------------------------------------------------- 注册

def handle_register(body: dict, ctl) -> tuple[int, dict]:
    """POST /api/register。body.dry_run=true 时只返回字段级差异，不落盘（重新识别预览）。"""
    try:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body 必须是对象"}
        return _register_status(_registry.register_service(body, dry_run=bool(body.get("dry_run"))))
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_registry_get(ctl) -> tuple[int, dict]:
    """GET /api/registry：registry + 每服务 {id,name,project,state,label,source}。"""
    try:
        if ctl is None:
            return 503, {"ok": False, "error": "controller unavailable"}
        data = _registry.data()
        if data.get("ok") is False:  # fail-closed 读失败标记（绝不伪装成空注册表）
            return 500, {"ok": False, "error": data.get("error", "registry.json 不可读")}
        loaded = manifest.load_all()
        snap: dict[str, dict] = {}
        try:
            snap = {p.get("id"): p for p in (ctl.snapshot() or {}).get("projects", [])}
        except Exception:  # noqa: BLE001
            pass
        owner: dict[str, str] = {}
        for proj in data.get("projects", []):
            for sid in proj.get("services", []):
                owner.setdefault(sid, proj["id"])
        services = []
        for sid, p in sorted(loaded.items()):
            st = snap.get(sid) or {}
            services.append({"id": sid, "name": p.name, "project": p.project or owner.get(sid),
                             "state": st.get("state", "unknown"), "label": st.get("label", "未知"),
                             "source": str(p.source)})
        return 200, {"ok": True, "registry": data, "services": services}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------------------------------------------------------- Project / Directory

def handle_project_upsert(body: dict) -> tuple[int, dict]:
    """POST /api/projects：{id?, name, directories?, agent?, autostart?, favorite?}。"""
    try:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body 必须是对象"}
        pid = body.get("id")
        if pid is not None and (not isinstance(pid, str) or not ID_RE.fullmatch(pid)):
            return 400, {"ok": False, "error": f"项目 id 非法：{pid!r}"}
        directories = body.get("directories")
        if directories is not None and not isinstance(directories, list):
            return 400, {"ok": False, "error": "directories 必须是数组"}
        proj, created = _registry.ensure_project(pid, body.get("name"), directories, body.get("agent"),
                                                  autostart=body.get("autostart"), favorite=body.get("favorite"))
        return 200, {"ok": True, "created": created, "project": proj,
                     "revision": proj.get("revision", 0)}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_directory_add(pid: str, body: dict) -> tuple[int, dict]:
    """POST /api/projects/<pid>/directories：{path, role?}。"""
    try:
        if not isinstance(body, dict) or not isinstance(body.get("path"), str) or not body["path"].strip():
            return 400, {"ok": False, "error": "path 不能为空"}
        proj = _registry.add_directory(pid, body["path"], body.get("role", "source"))
        return 200, {"ok": True, "project": proj, "revision": proj.get("revision", 0)}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_directory_remove(pid: str, dir_id: str) -> tuple[int, dict]:
    try:
        proj = _registry.remove_directory(pid, dir_id)
        return 200, {"ok": True, "project": proj, "revision": proj.get("revision", 0)}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_favorite(pid: str, body: dict) -> tuple[int, dict]:
    try:
        if not isinstance(body, dict) or not isinstance(body.get("value"), bool):
            return 400, {"ok": False, "error": "value 必须是布尔值"}
        proj = _registry.set_favorite(pid, body["value"])
        return 200, {"ok": True, "project": proj, "revision": proj.get("revision", 0)}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------------------------------------------------------- 移除登记

def handle_service_remove(sid: str, disposition: str, ctl) -> tuple[int, dict]:
    """DELETE /api/services/<sid>?disposition=stop|keep-external。"""
    try:
        if disposition not in ("stop", "keep-external"):
            return 400, {"ok": False, "error": f"disposition 必须是 stop/keep-external：{disposition!r}"}
        if disposition == "stop" and ctl is None:
            return 503, {"ok": False, "error": "controller unavailable"}
        res = _registry.remove_service(sid, disposition, ctl)
        return (200 if res.get("ok") else 409), res
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------------------------------------------------------- 草稿

def handle_drafts() -> tuple[int, dict]:
    try:
        return 200, {"ok": True, "drafts": _registry.drafts()}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_draft_register(draft_id: str, body: dict, ctl) -> tuple[int, dict]:
    """POST /api/drafts/<id>/register：草稿 + 补全字段合并后走正式注册；成功删草稿。"""
    try:
        draft = _registry.draft(draft_id)
        if draft is None:
            return 404, {"ok": False, "error": f"未知草稿 {draft_id}"}
        merged = _deep_merge(draft.get("payload") or {}, body if isinstance(body, dict) else {})
        # 转正即注册：draft 标志在此端点无意义，防止携带 draft:true 的草稿再次落草稿
        if isinstance(merged.get("service"), dict):
            merged["service"]["draft"] = False
        res = _registry.register_service(merged)
        if res.get("ok") and res.get("action") != "draft":
            _registry.delete_draft(draft_id)
            return 200, res
        if res.get("action") == "draft":  # 补全后仍缺必要字段：保留原草稿
            return 400, {"ok": False, "error": "草稿仍缺少必要字段，未能转正",
                         "draft_id": draft_id, "result": res}
        return _register_status(res)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_draft_delete(draft_id: str) -> tuple[int, dict]:
    try:
        return 200, _registry.delete_draft(draft_id)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


# ---------------------------------------------------------------- V2 移除/恢复/覆盖（CONTRACTS V2.1–V2.9）


def handle_service_definition(sid: str) -> tuple[int, dict]:
    """GET /api/services/<sid>/definition（R05）：当前完整定义（payload 形状，含全部组件字段）。

    GUI 编辑对话框据此初始化，保证"不编辑的字段零变化"；只返回清单层字段，不含凭据类
    env 值的额外放大（值本就存在于本机清单中，接口仍走 token 鉴权）。
    """
    try:
        try:
            loaded = manifest.load_all()
        except Exception as e:  # noqa: BLE001
            return 500, {"ok": False, "error": f"无法加载清单：{e}"}
        p = loaded.get(sid)
        if p is None:
            return 404, {"ok": False, "error": f"未知服务 {sid}"}

        def comp_dict(c) -> dict:
            d = {"id": c.id, "kind": c.kind}
            if c.name:
                d["name"] = c.name
            if c.run != "local":
                d["run"] = c.run
                d["compose_service"] = c.compose_service
                if c.compose_project:
                    d["compose_project"] = c.compose_project
                if c.compose_files:
                    d["compose_files"] = list(c.compose_files)
                d["provision"] = c.provision
            else:
                d["cmd"] = list(c.cmd)
            d["cwd"] = c.cwd
            if c.env:
                d["env"] = dict(c.env)
            if c.env_files:
                d["env_files"] = list(c.env_files)
            if c.port is not None:
                d["port"] = c.port
            if c.ready is not None:
                r = {}
                for k in ("http", "tcp", "json_ok", "timeout", "interval"):
                    v = getattr(c.ready, k)
                    if v is not None and not (k in ("timeout", "interval") and v in (0,)):
                        r[k] = v
                if r:
                    d["ready"] = r
            if not c.required:
                d["required"] = False
            if c.start_timeout != 30.0:
                d["start_timeout"] = c.start_timeout
            if c.after:
                d["after"] = list(c.after)
            if c.hot:
                d["hot"] = True
            if c.watch:
                d["watch"] = list(c.watch)
            if c.restart_on:
                d["restart_on"] = list(c.restart_on)
            if c.ignore:
                d["ignore"] = list(c.ignore)
            if c.build:
                d["build"] = list(c.build)
            if c.install:
                d["install"] = list(c.install)
            if c.install_when:
                d["install_when"] = list(c.install_when)
            if c.install_marker:
                d["install_marker"] = c.install_marker
            if not c.auto_update:
                d["auto_update"] = False
            if c.external_cmd:
                d["external_cmd"] = c.external_cmd
            if c.external_cwd:
                d["external_cwd"] = c.external_cwd
            if c.external_cwd_exact:
                d["external_cwd_exact"] = True
            return d

        def dep_dict(x) -> dict:
            d = {"name": x.name, "probe": {k: v for k, v in
                 (("tcp", x.probe.tcp), ("http", x.probe.http), ("json_ok", x.probe.json_ok)) if v}}
            if x.affects:
                d["affects"] = list(x.affects)
            if x.managed:
                d["managed"] = True
            if x.note:
                d["note"] = x.note
            if x.required:
                d["required"] = True
            if x.container:
                d["container"] = x.container
            if x.volume:
                d["volume"] = x.volume
            return d

        svc = {"id": p.id, "name": p.name, "source": str(p.source)}
        if p.description:
            svc["description"] = p.description
        if p.open:
            svc["open"] = p.open
        if p.autostart:
            svc["autostart"] = True
        if p.observe_only:
            svc["observe_only"] = True
        if p.adapter:
            svc["adapter"] = p.adapter
        svc["components"] = [comp_dict(c) for c in p.components]
        svc["dependencies"] = [dep_dict(x) for x in p.dependencies]
        return 200, {"ok": True, "service_id": sid, "definition": svc,
                     "project_id": p.project,
                     "revision": _registry.project(p.project or sid) or {}}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_service_remove_preview(sid: str, ctl) -> tuple[int, dict]:
    """GET /api/services/<sid>/remove-preview（V2.1）：零副作用。"""
    try:
        return 200, _registry.service_remove_preview(sid, ctl)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_project_remove_preview(pid: str, ctl) -> tuple[int, dict]:
    """GET /api/projects/<pid>/remove-preview（V2.2）：零副作用。"""
    try:
        return 200, _registry.remove_project_preview(pid, ctl)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_project_remove(pid: str, disposition: str, dry_run: bool, ctl) -> tuple[int, dict]:
    """DELETE /api/projects/<pid>?disposition=stop|keep-external[&dry_run=true]（V2.2）。"""
    try:
        if disposition not in ("stop", "keep-external"):
            return 400, {"ok": False, "error": f"disposition 必须是 stop/keep-external：{disposition!r}"}
        if not dry_run and disposition == "stop" and ctl is None:
            return 503, {"ok": False, "error": "controller unavailable"}
        res = _registry.remove_project(pid, disposition, ctl, dry_run=bool(dry_run))
        if res.get("ok"):
            return 200, res
        return 409, res  # 成员停止/预检失败 → 整体中止，登记保留
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_removed_list() -> tuple[int, dict]:
    """GET /api/removed（V2.2）：只列当前未登记的移除条目。"""
    try:
        return 200, _registry.removed_list()
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_restore_service(sid: str, body: dict, ctl) -> tuple[int, dict]:
    """POST /api/removed/services/<sid>/restore（V2.2）。"""
    try:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body 必须是对象"}
        return 200, _registry.restore_service(sid, body, ctl)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_restore_project(pid: str, body: dict, ctl) -> tuple[int, dict]:
    """POST /api/removed/projects/<pid>/restore（V2.2）。"""
    try:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "body 必须是对象"}
        return 200, _registry.restore_project(pid, body, ctl)
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_autostart(pid: str, body: dict) -> tuple[int, dict]:
    """POST /api/projects/<pid>/autostart {value:bool}（V2.9 专用端点，与 favorite 同形）。"""
    try:
        if not isinstance(body, dict) or not isinstance(body.get("value"), bool):
            return 400, {"ok": False, "error": "value 必须是布尔值"}
        proj = _registry.set_autostart(pid, body["value"])
        return 200, {"ok": True, "project": proj, "revision": proj.get("revision", 0)}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def handle_overrides_get(sid: str) -> tuple[int, dict]:
    """GET /api/services/<sid>/overrides（V2.3）：GUI 显示"你的覆盖优先生效"。"""
    try:
        from . import overrides
        return 200, {"ok": True, "sid": sid, "fields": overrides.fields(sid)}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
