"""Controller: run intent, launchd reconcile, readiness, ownership, code updates, generators.

One Controller instance lives in the daemon. All UI (dashboard, menubar, CLI) reads the
same `snapshot()`; there is no other status source.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import adapters, launchd, manifest, paths, procs, runner, sourcewatch
from .manifest import Component, Project

STATE_LABELS = {
    "stopped": "已停止",
    "starting": "启动中",
    "available": "可用",
    "partial": "部分可用",
    "failed": "故障",
    "stopping": "停止中",
    "unknown": "未知",
    "conflict": "冲突",
    "external": "外部运行",
}
SETTLE = 3.0  # seconds a source change must be quiet before applying
SECRET_RE = re.compile(
    r"(?i)((?:token|secret|password|passwd|api[_-]?key|authorization|cookie|session)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"'&,;]{6,})"
)
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}")
URL_CRED_RE = re.compile(r"([?&](?:token|key|secret|auth|sig|signature|code)=)[^&\s\"']+", re.I)
DSN_RE = re.compile(r"(://[^:/\s@]+:)[^@\s]+(@)")
FRAGMENT_RE = re.compile(r"(#(?:t|token|key|access_token)=)[^\s&\"']+", re.I)
LONG_HEX_RE = re.compile(r"\b[0-9a-f]{40,}\b", re.I)


def redact(text: str) -> str:
    text = FRAGMENT_RE.sub(lambda m: m.group(1) + "***", text)
    text = LONG_HEX_RE.sub("***", text)
    text = SECRET_RE.sub(lambda m: m.group(1) + "***", text)
    text = BEARER_RE.sub(lambda m: m.group(1) + "***", text)
    text = URL_CRED_RE.sub(lambda m: m.group(1) + "***", text)
    text = DSN_RE.sub(lambda m: m.group(1) + "***" + m.group(2), text)
    return text


def tail(path: Path, lines: int = 80) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return redact("\n".join(data.splitlines()[-lines:]))


def log_path(project: str, comp: str) -> Path:
    return paths.logs_dir() / f"{project}.{comp}.log"


def rotate(path: Path, limit: int = 5 * 1024 * 1024) -> None:
    """Rotate large logs; logs may hold service-printed secrets on disk, so keep them 0600."""
    try:
        if path.stat().st_size > limit:
            os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    os.chmod(path, 0o600)


class Store:
    """Persistent run intent + bookkeeping (state.json)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"projects": {}}
        try:
            self.data = json.loads(paths.state_file().read_text())
        except (OSError, ValueError):
            pass
        self.data.setdefault("projects", {})

    def save(self) -> None:
        with self.lock:
            p = paths.state_file()
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
            os.replace(tmp, p)

    def proj(self, pid: str) -> dict:
        with self.lock:
            return self.data["projects"].setdefault(pid, {})

    def intent(self, project: Project) -> str:
        with self.lock:
            d = self.proj(project.id)
            if "intent" not in d:
                d["intent"] = "running" if project.autostart else "stopped"
                d["intent_at"] = time.time()
                d["intent_by"] = "default"
                self.save()
            return d["intent"]

    def set_intent(self, pid: str, intent: str, by: str = "user") -> None:
        with self.lock:
            d = self.proj(pid)
            d.update({"intent": intent, "intent_at": time.time(), "intent_by": by})
            self.save()


class Controller:
    def __init__(self):
        paths.ensure()
        self.store = Store()
        self.lock = threading.RLock()
        self.projects: dict[str, Project] = {}
        self.adapters: dict[str, adapters.Adapter] = {}
        self.manifest_sig = None
        self.manifest_error = ""
        self.ops: dict[str, dict] = {}  # project -> {"kind","since"}
        self.errors: dict[str, dict] = {}  # project -> {"component","message","at"}
        self.gen_state: dict[str, dict] = {}  # "proj.comp" -> {...}
        self.update_state: dict[str, dict] = {}  # "proj.comp" -> {"pending","reason"}
        self._snapshot: dict = {"projects": [], "generated_at": 0}
        self._cwd_cache: tuple[float, dict] = (0.0, {})
        self._probe_cache: dict[str, float] = {}
        self.fsevents: dict = {"ok": None, "at": 0.0, "detail": ""}
        self._refresh_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
        self.reload()

    # ---------- manifests ----------
    def reload(self) -> None:
        d = paths.services_dir()
        sig = tuple((f.name, f.stat().st_mtime) for f in sorted(d.glob("*.toml")))
        if sig == self.manifest_sig:
            return
        try:
            projects = manifest.load_all(d)
            ads = {pid: adapters.load(p) for pid, p in projects.items()}
        except Exception as e:  # keep last good manifests
            self.manifest_error = f"清单错误：{e}"
            self.manifest_sig = sig
            return
        with self.lock:
            self.projects, self.adapters = projects, ads
            self.manifest_sig, self.manifest_error = sig, ""

    # ---------- helpers ----------
    def label(self, p: Project, c: Component) -> str:
        return launchd.label_for(p.id, c.id)

    def _plist_env(self) -> dict[str, str]:
        env = {"PATH": manifest.DEFAULT_PATH, "HOME": str(Path.home()), "PYTHONDONTWRITEBYTECODE": "1"}
        for k, v in os.environ.items():
            if k.startswith("LOCALSERVICES_"):
                env[k] = v
        return env

    def cwds(self, max_age: float = 15.0) -> dict[int, str]:
        t, cache = self._cwd_cache
        if time.time() - t > max_age:
            cache = procs.all_cwds()
            self._cwd_cache = (time.time(), cache)
        return cache

    def our_pids(self, ps: dict) -> set[int]:
        pids: set[int] = set()
        for p in self.projects.values():
            for c in p.managed_components():
                ji = launchd.info(self.label(p, c))
                if ji.pid:
                    pids |= procs.descendants(ji.pid, ps)
        return pids

    def conflicts(self, p: Project, ps: dict | None = None, fresh: bool = True) -> list[str]:
        """Instances not started by us that would collide with starting p."""
        ps = ps or procs.snapshot()
        ours = self.our_pids(ps)
        listen = procs.listeners()
        out = []
        cwds = None
        for c in p.managed_components():
            if c.port and c.port in listen and listen[c.port] not in ours:
                pid = listen[c.port]
                cmd = ps[pid].cmd[:120] if pid in ps else "?"
                out.append(f"{c.name or c.id}：端口被非本工具进程占用（pid {pid}：{cmd}）")
                continue
            if c.external_cmd:
                cwds = cwds or self.cwds(0 if fresh else 15)
                want = str((p.source / (c.external_cwd or c.cwd)).resolve())
                rx = re.compile(c.external_cmd)
                for pid, pr in ps.items():
                    if pid in ours or pid == os.getpid():
                        continue
                    pcwd = cwds.get(pid, "")
                    if (pcwd == want or pcwd.startswith(want + "/")) and rx.search(pr.cmd):
                        out.append(f"{c.name or c.id}：已有非本工具启动的实例（pid {pid}：{pr.cmd[:120]}）")
                        break
        return out

    def _set_error(self, pid: str, comp: str, msg: str) -> None:
        self.errors[pid] = {"component": comp, "message": msg, "at": time.time()}

    def _run_step(self, p: Project, c: Component, cmd: list[str], what: str, timeout: float) -> tuple[bool, str]:
        env = manifest.component_env(p, c)
        lp = log_path(p.id, c.id)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "ab") as log:
            log.write(f"[localservices] {time.strftime('%Y-%m-%d %H:%M:%S')} {what}: {' '.join(cmd)}\n".encode())
            log.flush()
            try:
                r = subprocess.run(cmd, cwd=p.comp_cwd(c), env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                log.write(f"[localservices] {what} timed out\n".encode())
                return False, f"{what}超时"
            except OSError as e:
                return False, f"{what}无法执行：{e}"
            log.write(r.stdout)
        if r.returncode != 0:
            out = "\n".join(redact(r.stdout.decode("utf-8", "replace")).strip().splitlines()[-12:])
            return False, f"{what}失败（exit {r.returncode}）\n{out}"
        return True, ""

    def _maybe_install(self, p: Project, c: Component, force: bool = False) -> tuple[bool, str]:
        if not c.install:
            return True, ""
        key = f"install.{c.id}"
        h = sourcewatch.files_hash(p.comp_cwd(c), c.install_when) if c.install_when else "static"
        st = self.store.proj(p.id)
        marker_ok = (p.comp_cwd(c) / c.install_marker).exists() if c.install_marker else True
        if not force and marker_ok and key not in st and c.install_marker:
            st[key] = h  # existing install predates us: adopt it, don't reinstall on first start
            self.store.save()
        if not force and marker_ok and st.get(key) == h:
            return True, ""
        ok, err = self._run_step(p, c, c.install, "安装依赖", 900)
        if ok:
            st[key] = h
            self.store.save()
        return ok, err

    def _build(self, p: Project, c: Component) -> tuple[bool, str]:
        if not c.build:
            return True, ""
        t0 = time.time()
        ok, err = self._run_step(p, c, [a.replace("{build}", str(paths.build_dir())) for a in c.build], "编译", 600)
        st = self.store.proj(p.id)
        st[f"build.{c.id}"] = {"ok": ok, "at": t0, "error": err}
        self.store.save()
        return ok, err

    def _bootstrap(self, p: Project, c: Component) -> tuple[bool, str]:
        label = self.label(p, c)
        lp = log_path(p.id, c.id)
        rotate(lp)
        launchd.write_plist(label, p.id, c.id, lp, self._plist_env())
        runner.reset_record(p.id, c.id)
        return launchd.bootstrap(label)

    def _ready(self, p: Project, c: Component, cached: bool = True) -> tuple[bool, str]:
        ok, detail = (True, "")
        if c.ready:
            key = f"{p.id}.{c.id}"
            hit = self._probe_cache.get(key)
            if cached and hit and c.ready.interval and time.time() - hit < c.ready.interval:
                ok = True
            else:
                # status path: short timeout so one slow dev server can't stall the shared snapshot;
                # start path (cached=False) uses the manifest timeout to let first compiles finish
                ok, detail = procs.probe(c.ready, None if not cached else min(c.ready.timeout, 4.0))
                if ok:
                    self._probe_cache[key] = time.time()
                else:
                    self._probe_cache.pop(key, None)
        if ok:
            extra = self.adapters[p.id].component_health(c)
            if extra is not None:
                ok, detail = extra
        return ok, detail

    def _bootout_wait(self, p: Project, c: Component, timeout: float = 30.0) -> tuple[bool, str]:
        """Stop our job and wait until its whole process tree is gone (slow dev-server shutdowns
        would otherwise look like a foreign instance on restart). Only pids that descended from
        our own launchd job are ever signalled."""
        label = self.label(p, c)
        ji = launchd.info(label)
        tree = procs.descendants(ji.pid, procs.snapshot()) if ji.pid else set()
        ok, err = launchd.bootout(label)

        def alive() -> set[int]:
            ps = procs.snapshot()
            return {pid for pid in tree if pid in ps}
        if not procs.wait_until(lambda: not alive(), timeout):
            for pid in alive():
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
            procs.wait_until(lambda: not alive(), 5)
        return ok, err

    def _order(self, p: Project) -> list[Component]:
        comps = p.managed_components()
        done: list[Component] = []
        names = {c.id for c in comps}
        pending = list(comps)
        while pending:
            progressed = False
            for c in list(pending):
                if all(a in {d.id for d in done} or a not in names for a in c.after):
                    done.append(c)
                    pending.remove(c)
                    progressed = True
            if not progressed:
                done.extend(pending)
                break
        return done

    # ---------- operations ----------
    def _begin(self, pid: str, kind: str) -> bool:
        with self.lock:
            if pid in self.ops:
                return False
            self.ops[pid] = {"kind": kind, "since": time.time()}
            return True

    def _end(self, pid: str) -> None:
        with self.lock:
            self.ops.pop(pid, None)

    def start(self, pid: str, open_after: bool = False) -> dict:
        p = self.projects.get(pid)
        if not p:
            return {"ok": False, "error": f"未知项目 {pid}"}
        if p.observe_only:
            return {"ok": False, "error": "该项目仅观察，不由本工具启动"}
        cur = self.ops.get(pid)
        if cur:
            return {"ok": True, "accepted": False, "message": f"{STATE_LABELS.get(cur['kind'], cur['kind'])}，请稍候"}
        snap = self.project_status(p)
        if snap["state"] == "available":
            if open_after:
                self.open(pid)
            return {"ok": True, "accepted": False, "message": "已在运行"}
        if not self._begin(pid, "starting"):
            return {"ok": True, "accepted": False, "message": "启动中，请稍候"}
        self.store.set_intent(pid, "running")
        threading.Thread(target=self._do_start, args=(p, open_after), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "启动中"}

    def _do_start(self, p: Project, open_after: bool, only: list[str] | None = None, skip_build: bool = False,
                  by_user: bool = True) -> None:
        try:
            self.errors.pop(p.id, None)
            ps = procs.snapshot()
            mine = self.our_pids(ps)
            conf = self.conflicts(p, ps)
            if conf:
                self._set_error(p.id, "", "无法启动：" + "；".join(conf))
                if by_user:
                    self.store.set_intent(p.id, "stopped", by="conflict")
                # automatic start (autostart / re-login): keep intent, retry after the guard period
                return
            for c in self._order(p):
                if only and c.id not in only:
                    continue
                ji = launchd.info(self.label(p, c))
                if ji.running and ji.pid in mine:
                    ok, _ = self._ready(p, c)
                    if ok:
                        continue
                if not skip_build:
                    ok, err = self._maybe_install(p, c)
                    if ok:
                        ok, err = self._build(p, c)
                    if not ok:
                        self._set_error(p.id, c.id, err)
                        return
                if ji.loaded:
                    self._bootout_wait(p, c)
                ok, err = self._bootstrap(p, c)
                if not ok:
                    self._set_error(p.id, c.id, f"launchd 拒绝启动：{err}")
                    return
                if not self._wait_ready(p, c):
                    return
            for g in p.generators():
                self._run_generator(p, g, reason="启动")
            if open_after:
                self.open(p.id)
        except Exception as e:  # noqa: BLE001
            self._set_error(p.id, "", f"内部错误：{e}\n{traceback.format_exc(limit=3)}")
        finally:
            self._end(p.id)

    def _wait_ready(self, p: Project, c: Component) -> bool:
        end = time.time() + c.start_timeout
        label = self.label(p, c)
        detail = ""
        while time.time() < end:
            rec = runner.read_record(p.id, c.id)
            if rec.get("gave_up"):
                self._set_error(p.id, c.id, f"{rec.get('error')}\n{tail(log_path(p.id, c.id), 15)}")
                return False
            ok, detail = self._ready(p, c, cached=False)
            if ok:
                return True
            ji = launchd.info(label)
            if not ji.loaded:
                self._set_error(p.id, c.id, "进程未在 launchd 中运行")
                return False
            time.sleep(1.0)
        self._set_error(p.id, c.id, f"就绪检查超时（{int(c.start_timeout)}s）：{detail}\n{tail(log_path(p.id, c.id), 15)}")
        return False

    def stop(self, pid: str) -> dict:
        p = self.projects.get(pid)
        if not p or p.observe_only:
            return {"ok": False, "error": "不可停止"}
        busy = self.adapters[pid].busy()
        if busy:
            return {"ok": False, "error": f"有活动任务，暂不停止：{busy}"}
        if not self._begin(pid, "stopping"):
            return {"ok": True, "accepted": False, "message": "操作进行中，请稍候"}
        self.store.set_intent(pid, "stopped")
        threading.Thread(target=self._do_stop, args=(p,), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "停止中"}

    def _do_stop(self, p: Project) -> None:
        try:
            for c in reversed(self._order(p)):
                ok, err = self._bootout_wait(p, c)
                if not ok:
                    self._set_error(p.id, c.id, f"停止失败：{err}")
            self.errors.pop(p.id, None)
        finally:
            self._end(p.id)

    def restart(self, pid: str, components: list[str] | None = None, reason: str = "user") -> dict:
        p = self.projects.get(pid)
        if not p or p.observe_only:
            return {"ok": False, "error": "不可重启"}
        busy = self.adapters[pid].busy()
        if busy:
            return {"ok": False, "error": f"有活动任务，已延后：{busy}"}
        if not self._begin(pid, "starting"):
            return {"ok": True, "accepted": False, "message": "操作进行中，请稍候"}
        self.store.set_intent(pid, "running")
        threading.Thread(target=self._do_restart, args=(p, components, True), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "重启中"}

    def _do_restart(self, p: Project, components: list[str] | None, by_user: bool = False) -> None:
        comps = [c for c in self._order(p) if not components or c.id in components]
        # compile first so a broken build never takes down the running instance
        for c in comps:
            ok, err = self._maybe_install(p, c)
            if ok:
                ok, err = self._build(p, c)
            if not ok:
                self._set_error(p.id, c.id, err)
                self._end(p.id)
                return
        for c in reversed(comps):
            self._bootout_wait(p, c)
        self._do_start(p, False, only=[c.id for c in comps], skip_build=True, by_user=by_user)

    def open(self, pid: str) -> dict:
        p = self.projects.get(pid)
        if not p:
            return {"ok": False, "error": "未知项目"}
        url = self.adapters[pid].open_url()
        if not url:
            return {"ok": False, "error": "没有可打开的地址"}
        subprocess.Popen(["/usr/bin/open", url])
        return {"ok": True}

    def open_source(self, pid: str) -> dict:
        p = self.projects.get(pid)
        if not p:
            return {"ok": False, "error": "未知项目"}
        subprocess.Popen(["/usr/bin/open", str(p.source)])
        return {"ok": True}

    def project_action(self, pid: str, name: str) -> dict:
        p = self.projects.get(pid)
        if not p:
            return {"ok": False, "error": "未知项目"}
        ok, msg = self.adapters[pid].action(name)
        return {"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}

    def logs(self, pid: str, comp: str | None, lines: int = 200) -> dict:
        p = self.projects.get(pid)
        if not p:
            return {"ok": False, "error": "未知项目"}
        out = {}
        for c in p.components:
            if comp and c.id != comp:
                continue
            out[c.id] = tail(log_path(p.id, c.id), lines)
        return {"ok": True, "logs": out}

    # ---------- generators ----------
    def _run_generator(self, p: Project, g: Component, reason: str) -> None:
        key = f"{p.id}.{g.id}"
        st = self.gen_state.setdefault(key, {})
        t0 = time.time()
        ok, err = self._run_step(p, g, g.cmd, f"生成（{reason}）", 300)
        newest, _ = sourcewatch.newest(p.source, g.watch, g.ignore)
        st.update({"ok": ok, "at": t0, "error": err if not ok else "", "input_mtime": newest, "reason": reason})
        if ok:
            st["last_ok_at"] = t0

    def _tick_generators(self, p: Project) -> None:
        for g in p.generators():
            key = f"{p.id}.{g.id}"
            st = self.gen_state.setdefault(key, {})
            newest, rel = sourcewatch.newest(p.source, g.watch, g.ignore)
            if not st.get("at"):
                self._run_generator(p, g, "首次")
                continue
            if newest > st.get("input_mtime", 0) and time.time() - newest >= g.debounce:
                self._run_generator(p, g, f"内容变化 {rel}")

    # ---------- code updates ----------
    def _code_state(self, p: Project, c: Component, started_at: float | None) -> dict:
        root = p.comp_cwd(c)
        b = self.store.proj(p.id).get(f"build.{c.id}")
        res = {"state": "unknown", "label": "未知", "detail": ""}
        if not started_at:
            return {"state": "idle", "label": "未运行", "detail": ""}
        restart_t, restart_f = sourcewatch.newest(root, c.restart_on + c.install_when) if (c.restart_on or c.install_when) else (0.0, "")
        hot_ok = c.hot and self.fsevents.get("ok") is not False
        if hot_ok:
            code_t, code_f = 0.0, ""
        else:
            code_t, code_f = sourcewatch.newest(root, c.watch, c.ignore) if c.watch else (0.0, "")
        t, f = (restart_t, restart_f) if restart_t >= code_t else (code_t, code_f)
        if b and not b.get("ok") and b.get("at", 0) > started_at and t <= b.get("at", 0):
            res = {"state": "failed", "label": "更新失败", "detail": b.get("error", "")[:600], "changed": f}
        elif t > started_at:
            res = {"state": "pending", "label": "待更新", "detail": f"{f} 于 {time.strftime('%H:%M:%S', time.localtime(t))} 修改", "changed": f, "changed_at": t}
            if c.hot and not hot_ok:
                res["detail"] += "；系统文件事件失效，热更新不可用，改为重启应用"
        elif c.hot and not hot_ok:
            res = {"state": "synced", "label": "已同步（按重启更新）", "detail": "系统文件事件（FSEvents）失效，热更新不可用"}
        elif c.hot:
            res = {"state": "synced", "label": "已同步（热更新）", "detail": ""}
        elif c.watch:
            res = {"state": "synced", "label": "已同步", "detail": ""}
        else:
            res = {"state": "synced", "label": "随启动加载", "detail": ""}
        res["newest_change"] = max(t, 0) or None
        return res

    def _tick_updates(self, p: Project, comp_status: dict) -> None:
        if p.id in self.ops:
            return
        need = []
        for c in p.managed_components():
            cs = comp_status.get(c.id)
            if not cs or not cs.get("running"):
                continue
            code = cs["code"]
            key = f"{p.id}.{c.id}"
            if code["state"] != "pending" or not c.auto_update:
                self.update_state.pop(key, None)
                continue
            if time.time() - code.get("changed_at", 0) < SETTLE:
                continue
            need.append(c.id)
        if not need:
            return
        busy = self.adapters[p.id].busy()
        if busy:
            for cid in need:
                self.update_state[f"{p.id}.{cid}"] = {"deferred": busy, "since": time.time()}
            return
        for cid in need:
            self.update_state.pop(f"{p.id}.{cid}", None)
        if self._begin(p.id, "starting"):
            def run():
                self._do_restart(p, need)
            threading.Thread(target=run, daemon=True).start()

    # ---------- system checks ----------
    def check_fsevents(self, max_age: float = 120.0) -> None:
        """Hot reload in every dev server depends on FSEvents; verify it really delivers events."""
        if time.time() - self.fsevents["at"] < max_age:
            return
        tool = paths.home() / "bin" / "fsevents-check"
        if not tool.exists():
            self.fsevents = {"ok": None, "at": time.time(), "detail": "未安装 fsevents-check"}
            return
        probe_dir = paths.home() / "run"
        try:
            r = subprocess.run([str(tool), str(probe_dir)], capture_output=True, text=True, timeout=8)
            ok = r.returncode == 0
            detail = "" if ok else "系统文件事件（FSEvents）无响应：开发服务器热更新失效，已改为检测到源码变化后重启。修复：sudo killall fseventsd 或重启 Mac"
        except subprocess.TimeoutExpired:
            ok, detail = False, "fsevents-check 超时"
        self.fsevents = {"ok": ok, "at": time.time(), "detail": detail}

    # ---------- reconcile ----------
    def reconcile(self) -> None:
        self.reload()
        try:
            self.check_fsevents()
        except Exception as e:  # noqa: BLE001
            self.fsevents = {"ok": None, "at": time.time(), "detail": f"检查失败：{e}"}
        for p in list(self.projects.values()):
            if p.observe_only or p.id in self.ops:
                continue
            intent = self.store.intent(p)
            err = self.errors.get(p.id)
            if intent == "running" and err and time.time() - err["at"] < 120:
                continue  # a start just failed (build/install/launchd); don't hot-loop
            if intent == "running":
                for c in p.managed_components():
                    ji = launchd.info(self.label(p, c))
                    rec = runner.read_record(p.id, c.id)
                    if rec.get("gave_up") and c.watch and not ji.running:
                        changed, _ = sourcewatch.newest(p.comp_cwd(c), c.watch + c.restart_on, c.ignore)
                        if changed > float(rec.get("gave_up_at") or rec.get("started_at") or 0):
                            # source edited after we gave up: the fix may be in, try once more
                            if self._begin(p.id, "starting"):
                                threading.Thread(target=self._do_restart, args=(p, [c.id]), daemon=True).start()
                            break
                    if not ji.loaded and not rec.get("gave_up"):
                        if self._begin(p.id, "starting"):
                            threading.Thread(target=self._do_start, args=(p, False), kwargs={"by_user": False},
                                             daemon=True).start()
                        break
                try:
                    self._tick_generators(p)
                except Exception as e:  # noqa: BLE001
                    self.gen_state[f"{p.id}._"] = {"ok": False, "error": str(e)}
            else:
                for c in p.managed_components():
                    if launchd.info(self.label(p, c)).loaded:
                        self._bootout_wait(p, c)

    # ---------- status ----------
    def project_status(self, p: Project, ps: dict | None = None, listen: dict | None = None) -> dict:
        ad = self.adapters[p.id]
        base = {"id": p.id, "name": p.name, "source": str(p.source), "observe_only": p.observe_only,
                "description": p.description}
        if p.observe_only:
            try:
                ob = ad.observe()
            except Exception as e:  # noqa: BLE001
                ob = {"state": "unknown", "label": "未知", "detail": f"观察失败：{e}"}
            base.update(ob)
            base.setdefault("label", STATE_LABELS.get(base.get("state"), "未知"))
            base["code"] = {"state": "na", "label": "外部管理"}
            base["components"] = base.get("components", [])
            base["managed_by"] = "external"
            return base
        ps = ps if ps is not None else procs.snapshot()
        intent = self.store.intent(p)
        op = self.ops.get(p.id)
        comps = []
        comp_map = {}
        for c in p.components:
            if c.kind == "generator":
                gs = self.gen_state.get(f"{p.id}.{c.id}", {})
                comps.append({
                    "id": c.id, "name": c.name or c.id, "kind": "generator",
                    "state": ("idle" if intent != "running" else ("ok" if gs.get("ok") else ("failed" if gs.get("at") else "pending"))),
                    "label": ("未运行" if intent != "running" else ("最近生成成功" if gs.get("ok") else ("生成失败，保留上次有效结果" if gs.get("at") else "等待生成"))),
                    "detail": gs.get("error", ""), "last_run": gs.get("at"), "last_ok": gs.get("last_ok_at"),
                    "reason": gs.get("reason", ""), "required": False,
                })
                continue
            ji = launchd.info(self.label(p, c))
            rec = runner.read_record(p.id, c.id)
            started = rec.get("started_at") if ji.running else None
            cs = {"id": c.id, "name": c.name or c.id, "kind": c.kind, "pid": ji.pid, "running": ji.running,
                  "loaded": ji.loaded, "started_at": started, "required": c.required,
                  "last_exit": ji.last_exit, "failures": rec.get("failures", 0)}
            if ji.running and rec.get("phase") == "backoff" and rec.get("pid") == ji.pid:
                cs.update(state="backoff", running=False,
                          label=f"异常退出（第 {rec.get('failures')} 次），{int(rec.get('backoff', 0))}s 退避后重试",
                          detail=f"最近退出码 {ji.last_exit}")
                started = None
            elif ji.running:
                ok, detail = self._ready(p, c)
                age = time.time() - (started or time.time())
                if ok:
                    cs.update(state="ready", label="就绪", detail="")
                elif age < c.start_timeout:
                    cs.update(state="starting", label="启动中", detail=detail)
                else:
                    cs.update(state="unhealthy", label="未就绪", detail=detail)
            elif rec.get("gave_up") and (ji.loaded or intent == "running"):
                cs.update(state="crashed", label="已停止自动恢复", detail=rec.get("error", ""))
            elif ji.loaded:
                cs.update(state="exited", label="已退出", detail=f"退出码 {ji.last_exit}")
            else:
                cs.update(state="stopped", label="未运行", detail="")
            cs["code"] = self._code_state(p, c, started)
            upd = self.update_state.get(f"{p.id}.{c.id}")
            if upd and cs["code"]["state"] == "pending":
                cs["code"]["label"] = "待更新（已延后）"
                cs["code"]["detail"] = f"{cs['code']['detail']}；{upd['deferred']}"
            cs["log"] = str(log_path(p.id, c.id))
            comps.append(cs)
            comp_map[c.id] = cs

        deps = []
        for d in p.dependencies:
            ok, detail = procs.probe(d.probe)
            deps.append({"name": d.name, "ok": ok, "detail": detail, "affects": d.affects,
                         "managed": d.managed, "note": d.note})

        managed = [cs for cs in comps if cs["kind"] != "generator"]
        required = [cs for cs in managed if cs["required"]]
        any_running = any(cs["running"] for cs in managed)
        all_ready = all(cs["state"] == "ready" for cs in required)
        optional_bad = [cs for cs in managed if not cs["required"] and cs["state"] != "ready"]
        dep_bad = [d for d in deps if not d["ok"]]
        gen_bad = [cs for cs in comps if cs["kind"] == "generator" and cs["state"] == "failed"]
        err = self.errors.get(p.id)
        reason = ""
        if op and op["kind"] == "stopping":
            state = "stopping"
        elif op:
            state = "starting"
        elif intent != "running":
            state = "stopped"
            if any_running:
                state = "stopping"
        else:
            if all_ready:
                state = "partial" if (optional_bad or dep_bad or gen_bad) else "available"
            elif any(cs["state"] == "starting" for cs in required) and not err:
                state = "starting"
            elif any(cs["state"] == "ready" for cs in required):
                state = "partial"
            else:
                state = "failed"
        if (state in ("stopped", "failed") and err and err.get("message", "").startswith("无法启动")
                and not any_running):
            state = "conflict"
        external = []
        if state == "stopped" and not any_running and not p.observe_only:
            external = self.conflicts(p, fresh=False)
            if external:
                state = "conflict"
        problems = []
        for cs in managed:
            if intent == "running" and cs["state"] not in ("ready",):
                problems.append(f"{cs['name']}：{cs['label']}" + (f"（{cs['detail']}）" if cs.get("detail") else ""))
        for d in dep_bad:
            aff = "、".join(d["affects"]) or p.name
            problems.append(f"依赖 {d['name']} 不可用，影响 {aff}" + ("（外部管理，本工具不启动）" if not d["managed"] else ""))
        for g in gen_bad:
            problems.append(f"{g['name']}：{g['label']}")
        if err:
            reason = err["message"]
        elif external:
            reason = "检测到非本工具启动的实例，本工具不会停止它：" + "；".join(external)
        elif problems and state in ("partial", "failed"):
            reason = "；".join(problems)

        code_states = [cs["code"] for cs in managed if cs["running"]]
        if not code_states:
            code = {"state": "idle", "label": "—"}
        else:
            order = {"failed": 0, "pending": 1, "unknown": 2, "synced": 3}
            worst = min(code_states, key=lambda s: order.get(s["state"], 2))
            code = dict(worst)
            if worst["state"] == "synced" and any(s["label"].startswith("已同步（热") for s in code_states):
                code["label"] = "已同步（热更新）" if all(s["label"].startswith("已同步（热") for s in code_states) else "已同步"
            starts = [cs["started_at"] for cs in managed if cs.get("started_at")]
            code["process_started"] = min(starts) if starts else None
        base.update({
            "state": state, "label": STATE_LABELS[state], "intent": intent,
            "intent_by": self.store.proj(p.id).get("intent_by"),
            "op": op["kind"] if op else None, "reason": reason, "error": err,
            "components": comps, "dependencies": deps, "code": code,
            "can_open": bool(ad.open_url()) and state in ("available", "partial"),
            "managed_by": "localservices",
        })
        return base

    def refresh_snapshot(self, wait: float | None = None) -> dict:
        """Recompute the shared snapshot. Concurrent callers wait up to `wait` s, then get the last one."""
        if not self._refresh_lock.acquire(timeout=-1 if wait is None else wait):
            return self._snapshot
        try:
            return self._refresh()
        finally:
            self._refresh_lock.release()

    def _status_safe(self, p: Project, ps: dict) -> dict:
        try:
            return self.project_status(p, ps)
        except Exception as e:  # noqa: BLE001
            return {"id": p.id, "name": p.name, "state": "unknown", "label": "未知", "reason": f"状态探测失败：{e}",
                    "components": [], "code": {"state": "unknown", "label": "未知"}, "source": str(p.source)}

    def _refresh(self) -> dict:
        self.reload()
        ps = procs.snapshot()
        projects = list(self.projects.values())
        out = list(self._pool.map(lambda p: self._status_safe(p, ps), projects))
        for p, st in zip(projects, out):
            if not p.observe_only and st.get("intent") == "running":
                try:
                    self._tick_updates(p, {c["id"]: c for c in st.get("components", []) if c.get("kind") != "generator"})
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        system = []
        if self.fsevents.get("ok") is False:
            system.append(self.fsevents["detail"])
        snap = {"projects": out, "generated_at": time.time(), "manifest_error": self.manifest_error,
                "system_warnings": system,
                "attention": bool(system) or any(s.get("state") in ("failed", "conflict", "partial") for s in out)}
        self._snapshot = snap
        return snap

    def snapshot(self) -> dict:
        return self._snapshot
