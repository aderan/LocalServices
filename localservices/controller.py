"""Controller: run intent, launchd reconcile, readiness, ownership, code updates, generators.

One Controller instance lives in the daemon. All UI (dashboard, menubar, CLI) reads the
same `snapshot()`; there is no other status source.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import adapters, compose, dockerctx, launchd, manifest, paths, procs, runner, sourcewatch
from .manifest import Component, Project
from .infrastructure import DependencyError, Infrastructure

STATE_LABELS = {
    "stopped": "已停止",
    "starting": "启动中",
    "preparing": "准备环境",
    "available": "可用",
    "partial": "部分可用",
    "failed": "故障",
    "stopping": "停止中",
    "unknown": "未知",
    "conflict": "冲突",
    "external": "外部运行",
}
# Supervising launcher wrappers we climb through when attributing an external instance
# (V2.8: python -m uvicorn reloaders, uv/make/go/air/just/task wrappers on top of the V1 set).
LAUNCHER_RE = re.compile(
    r"^(\S*/)?(npm|npx|pnpm|yarn|bun|node|python3[\w.]*|python|uv|uvicorn|make|go|air|just|task)(\s|$)"
    r"|^(\S*/)?sh -c "
)
SETTLE = 3.0  # seconds a source change must be quiet before applying
STARTUP_TIMEOUT = 30.0  # one budget for preparation and readiness, shared by all components
AUTOSTART_CACHE_TTL = 30.0  # V2.9: registry autostart_map re-read at most this often
COMPOSE_STATE_TTL = 5.0  # compose inspect cache for status refreshes
COMPOSE_NAME_TTL = 10.0  # compose container-name cache for attribution
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

    def __init__(self, autostart=None):
        """`autostart`: optional callable -> {project_id: bool}（V2.9 registry 只读助手），
        决定 intent 的初始缺省；不可用/异常一律按空处理。"""
        self.lock = threading.RLock()
        self.data = {"projects": {}}
        self._autostart = autostart
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
                # V2.9: 初始缺省 = running 当且仅当 清单 autostart 或 registry 项目 autostart。
                default = bool(project.autostart) or self._registry_autostart(project.id)
                d["intent"] = "running" if default else "stopped"
                d["intent_at"] = time.time()
                d["intent_by"] = "default"
                self.save()
            return d["intent"]

    def _registry_autostart(self, pid: str) -> bool:
        if not callable(self._autostart):
            return False
        try:
            return bool(self._autostart().get(pid))
        except Exception:  # registry 不可用绝不影响 intent 缺省
            return False

    def set_intent(self, pid: str, intent: str, by: str = "user") -> None:
        with self.lock:
            d = self.proj(pid)
            d.update({"intent": intent, "intent_at": time.time(), "intent_by": by})
            self.save()


class Controller:

    def __init__(self):
        paths.ensure()
        self._autostart_cache: tuple[float, dict[str, bool]] = (0.0, {})
        self._domain_settings_cache: tuple[float, dict] = (0.0, {"use_domains": False, "domain_form": "suffix"})
        self.store = Store(autostart=self._registry_autostart_map)
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
        # compose 运行期簿记（V2.7）：本会话由我们带起的容器 / 就绪判定用过端口探针的组件 /
        # component_state 与容器名解析的短缓存（刷新周期内复用，同探针缓存）。
        self._compose_started: dict[str, float] = {}  # "pid.cid" -> ensure 成功时刻
        self._compose_probe_used: set[str] = set()
        self._compose_state_cache: dict[str, tuple[float, dict]] = {}
        self._compose_name_cache: dict[str, tuple[float, str | None]] = {}
        self.fsevents: dict = {"ok": None, "at": 0.0, "detail": ""}
        self._refresh_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
        self.infrastructure = Infrastructure()
        self.reload()

    # ---------- manifests ----------
    def reload(self, force: bool = False) -> None:
        """重载清单。mtime 签名只覆盖 *.toml 增删改；tombstone（removed.json）变化不动
        任何 TOML，因此登记面变更必须 force=True（daemon._registry_changed）。
        卸载的服务在此收敛式清理 state 条目——并发 refresh 可能带着旧清单在
        release_service 之后又经 Store.intent 懒播种写回（V2.7 收尾一致性）。"""
        dirs = paths.service_search_path()
        sig = tuple((str(d), f.name, f.stat().st_mtime) for d in dirs for f in sorted(d.glob("*.toml")))
        if sig == self.manifest_sig and not force:
            return
        try:
            projects = manifest.load_all()
            for p in projects.values():
                for c in p.components:
                    if c.kind == "generator" and c.run == "compose":
                        raise ValueError(f"{p.id}.{c.id}: generator 组件不能 run=compose")
            ads = {pid: adapters.load(p) for pid, p in projects.items()}
        except Exception as e:  # keep last good manifests
            self.manifest_error = f"清单错误：{e}"
            self.manifest_sig = sig
            return
        with self.lock:
            self.projects, self.adapters = projects, ads
            self.manifest_sig, self.manifest_error = sig, ""
            stale = [pid for pid in self.store.data.get("projects", {}) if pid not in projects]
            for pid in stale:
                self.store.data["projects"].pop(pid, None)
            if stale:
                try:
                    self.store.save()
                except OSError:
                    pass

    def domain_settings(self) -> dict:
        """V3.5 域名跳转设置（Registry.locove_settings 的短缓存读）。默认关闭。"""
        now = time.monotonic()
        ts, cached = self._domain_settings_cache
        if now - ts < AUTOSTART_CACHE_TTL:
            return cached
        cached = {"use_domains": False, "domain_form": "suffix"}
        try:
            from .registry import Registry
            cached = Registry().locove_settings()
        except Exception:  # noqa: BLE001 — 设置不可读按默认（直连）处理
            cached = {"use_domains": False, "domain_form": "suffix"}
        self._domain_settings_cache = (now, cached)
        return cached

    def _registry_autostart_map(self) -> dict[str, bool]:
        """V2.9: Registry.autostart_map()（Core 线提供），30s 缓存；任何失败按空处理。

        惰性导入 + getattr 防御：registry 尚未提供该助手时不影响任何行为。
        """
        now = time.monotonic()
        ts, cached = self._autostart_cache
        if now - ts < AUTOSTART_CACHE_TTL:
            return cached
        cached = {}
        try:
            from .registry import Registry  # 惰性：daemon 启动路径不依赖 registry
            fn = getattr(Registry(), "autostart_map", None)
            if callable(fn):
                raw = fn()
                if isinstance(raw, dict):
                    cached = {str(k): bool(v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001 — registry 读失败按无 autostart 处理
            cached = {}
        self._autostart_cache = (now, cached)
        return cached

    # ---------- helpers ----------
    def label(self, p: Project, c: Component) -> str:
        return launchd.label_for(p.id, c.id)

    def set_intent_direct(self, pid: str, intent: str, by: str = "restore") -> None:
        """运行意图唯一写入口（V3）：恢复/卸载等登记面流程也必须经 Store 更新内存+文件，
        避免直接改写 state.json 后被 Store 的下一次整份保存覆盖（意图丢失竞态）。"""
        self.store.set_intent(pid, intent, by=by)

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

    def our_pids(self, ps: dict, project: Project | None = None) -> set[int]:
        pids: set[int] = set()
        for p in ([project] if project else self.projects.values()):
            for c in p.managed_components():
                if c.run == "compose":
                    continue  # compose 组件没有 launchd job
                ji = launchd.info(self.label(p, c))
                if ji.pid:
                    pids |= procs.descendants(ji.pid, ps)
        return pids

    def _docker_container_for_port(self, port: int) -> str | None:
        """哪个容器发布了该宿主端口（dockerctx，只读）；任何失败 → None（保持进程语义）。"""
        try:
            r = dockerctx.run(["ps", "-a", "--filter", f"publish={port}", "--format", "{{json .}}"], timeout=6)
            if r.returncode != 0:
                return None
            for line in r.stdout.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                name = str(row.get("Names", "")).split(",")[0].strip()
                if name:
                    return name
        except Exception:  # noqa: BLE001 — docker 不可达时退回普通进程冲突判定
            return None
        return None

    def _compose_container_cached(self, p: Project, c: Component) -> str | None:
        """compose.container_name 的短缓存版（归属判定会反复调用）。"""
        key = f"{p.id}.{c.id}"
        hit = self._compose_name_cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < COMPOSE_NAME_TTL:
            return hit[1]
        try:
            name = compose.container_name(p, c)
        except Exception:  # noqa: BLE001
            name = None
        self._compose_name_cache[key] = (now, name)
        return name

    def _container_owner(self, name: str, exclude_pid: str) -> str | None:
        """容器属于哪个已登记项目（dependencies[].container 或 compose 组件的容器）。"""
        for other in self.projects.values():
            if other.id == exclude_pid:
                continue
            if any(d.container == name for d in other.dependencies):
                return other.id
            for oc in other.managed_components():
                if oc.run == "compose" and self._compose_container_cached(other, oc) == name:
                    return other.id
        return None

    def _docker_port_entry(self, p: Project, c: Component, port: int, holder_pid: int) -> dict | None:
        """把 docker 宿主代理持有的端口归属到容器（V2.8）。

        返回 None = 无法解析容器（调用方保持既有进程冲突语义）；
        {"skip": True} = 本项目自己的依赖/自己的 compose 容器在运行（不是外部实例，不是冲突）；
        否则为一条 docker-container 外部记录（unknown 或归属其他受管项目）。
        """
        name = self._docker_container_for_port(port)
        if not name:
            return None
        if any(d.container == name for d in p.dependencies):
            return {"skip": True}  # 本项目依赖容器在运行：依赖探测另行呈现
        if c.run == "compose" and self._compose_container_cached(p, c) == name:
            return {"skip": True}  # 本组件自己的容器已在运行（up 幂等，无需接管）
        return {"component": c.id, "name": c.name or c.id, "kind": "docker-container",
                "container": name, "pid": holder_pid, "tree": [], "ports": [port], "why": "docker",
                "cmd": f"容器 {name}（docker 端口代理 pid {holder_pid}）", "same_project": False,
                "managed_project": self._container_owner(name, p.id)}

    def external_instances(self, p: Project, ps: dict | None = None, fresh: bool = True) -> list[dict]:
        """Instances of p's components not started by us. `same_project` = runs from p's own source
        folder (can be adopted / taken over); otherwise an unrelated process holds our port."""
        ps = ps or procs.snapshot()
        ours = self.our_pids(ps, p)
        other_owners = {pid: other.id for other in self.projects.values() if other.id != p.id
                        for pid in self.our_pids(ps, other)}
        listen = procs.listeners()
        ports_of: dict[int, list[int]] = {}
        for port, pid in listen.items():
            ports_of.setdefault(pid, []).append(port)
        cwds = self.cwds(0 if fresh else 15)
        held = [listen[c.port] for c in p.managed_components() if c.port in listen and listen[c.port] not in ours]
        if any(pid not in cwds for pid in held):
            cwds = self.cwds(0)
        src = str(p.source.resolve())

        def inside(pid: int, root: str = src, exact: bool = False) -> bool:
            cwd = cwds.get(pid, "")
            return cwd == root or (not exact and cwd.startswith(root + "/"))

        out, roots = [], set()
        for c in p.managed_components():
            want = str((p.source / (c.external_cwd or c.cwd)).resolve())
            cands = []
            if c.port and c.port in listen and listen[c.port] not in ours:
                holder_pid = listen[c.port]
                holder = ps.get(holder_pid)
                if holder is not None and procs.looks_like_docker_proxy(holder.cmd):
                    # docker 宿主代理：端口实际由某容器发布，归属到容器而不是进程冲突
                    entry = self._docker_port_entry(p, c, c.port, holder_pid)
                    if entry is not None:
                        if entry.get("skip"):
                            continue
                        if not any(e.get("kind") == "docker-container" and e.get("container") == entry["container"]
                                   for e in out):
                            out.append(entry)
                        continue
                cands.append((holder_pid, "port"))
            if c.external_cmd:
                rx = re.compile(c.external_cmd)
                cands += [(pid, "cmd") for pid, pr in ps.items()
                          if pid not in ours and pid != os.getpid() and inside(pid, want, c.external_cwd_exact) and rx.search(pr.cmd)]
            for pid, why in cands:
                root = pid
                # climb through launcher wrappers (npm run dev → sh -c → node), never into shells/apps
                while root in ps:
                    par = ps[root].ppid
                    if par <= 1 or par in ours or par not in ps or not inside(par) or not LAUNCHER_RE.search(ps[par].cmd):
                        break
                    root = par
                if root in roots:
                    continue
                roots.add(root)
                tree = procs.descendants(root, ps)
                out.append({"component": c.id, "name": c.name or c.id, "pid": root, "tree": sorted(tree),
                            "ports": sorted(pt for t in tree for pt in ports_of.get(t, [])), "why": why,
                            "cmd": ps[root].cmd[:120] if root in ps else "?",
                            "same_project": inside(pid, want, True) if c.external_cwd_exact else inside(pid),
                            "managed_project": other_owners.get(pid)})
        return out

    def conflicts(self, p: Project, ps: dict | None = None, fresh: bool = True) -> list[str]:
        """Instances not started by us that would collide with starting p."""
        out: list[str] = []
        for e in self.external_instances(p, ps, fresh):
            if e.get("kind") == "docker-container":
                if e.get("same_project"):
                    continue  # 自己的容器在运行：compose up 幂等，不是冲突
                if e.get("managed_project"):
                    out.append(f"{e['name']}：端口被项目 {e['managed_project']} 的容器 {e['container']} 占用")
                else:
                    port = e["ports"][0] if e["ports"] else "?"
                    out.append(f"{e['name']}：端口 {port} 由容器 {e['container']} 发布占用（未登记）")
                continue
            out.append(f"{e['name']}：" + (f"端口被项目 {e['managed_project']} 占用" if e.get("managed_project") else
                       ("端口被非本工具进程占用" if e["why"] == "port" else "已有非本工具启动的实例"))
                       + f"（pid {e['pid']}：{e['cmd']}）")
        return out

    def _adoptable(self, p: Project, ext: list[dict]) -> tuple[bool, dict[str, int]]:
        """External instances run p from its own folder and every required server answers its probe
        on the port it actually listens on. Returns (ok, component -> port)."""
        if not ext or not all(e["same_project"] for e in ext):
            return False, {}
        ports: dict[str, int] = {}
        for c in p.managed_components():
            if not c.required or not c.port:
                continue
            mine = sorted((e for e in ext if e["component"] == c.id and e["ports"]),
                          key=lambda e: c.port not in e["ports"])
            e = mine[0] if mine else None
            if not e:
                return False, {}
            port = c.port if c.port in e["ports"] else (e["ports"][0] if e["ports"] else None)
            if not port:
                return False, {}
            if c.ready:
                pr = dataclasses.replace(c.ready, http=c.ready.http.replace(f":{c.port}", f":{port}") if c.ready.http else None,
                                         tcp=c.ready.tcp.replace(f":{c.port}", f":{port}") if c.ready.tcp else None)
                key = f"ext.{p.id}.{c.id}.{port}"
                hit = self._probe_cache.get(key)
                if not (hit and pr.interval and time.time() - hit < pr.interval):
                    ok, _ = procs.probe(pr, min(pr.timeout, 4.0))
                    if not ok:
                        return False, {}
                    self._probe_cache[key] = time.time()
            ports[c.id] = port
        return True, ports

    def _external_url(self, p: Project, ports: dict[str, int]) -> str | None:
        url = self.adapters[p.id].open_url()
        if not url:
            return None
        for c in p.managed_components():
            if c.id in ports and c.port and ports[c.id] != c.port:
                url = url.replace(f":{c.port}", f":{ports[c.id]}")
        return url

    def takeover(self, pid: str) -> dict:
        """User-confirmed: stop the external instance of this same project and run ours instead.
        Never applies to unrelated processes; deferred while the project reports active work."""
        p = self.projects.get(pid)
        if not p or p.observe_only:
            return {"ok": False, "error": "不可接管"}
        ext = self.external_instances(p)
        if not ext:
            self.store.proj(pid).pop("takeover_pending", None)
            self.store.save()
            return self.start(pid)
        foreign = [e for e in ext if not e["same_project"]]
        if foreign:
            e = foreign[0]
            if e.get("kind") == "docker-container":
                return {"ok": False, "error": f"端口 {e['ports'] or '?'} 由容器 {e['container']} 发布占用"
                        + (f"（项目 {e['managed_project']}）" if e.get("managed_project") else "（未登记）")
                        + "，本工具不会停止它"}
            return {"ok": False, "error": f"端口 {e['ports'] or '?'} 被其他程序占用（pid {e['pid']}：{e['cmd']}），"
                                         "它不是本项目的实例，本工具不会停止它"}
        busy = self.adapters[pid].busy()
        if busy:
            self.store.proj(pid)["takeover_pending"] = time.time()
            self.store.save()
            return {"ok": True, "accepted": False, "message": f"有活动任务（{busy}），空闲后自动接管"}
        if not self._begin(pid, "starting"):
            return {"ok": True, "accepted": False, "message": "操作进行中，请稍候"}
        self.store.set_intent(pid, "running")
        threading.Thread(target=self._do_takeover, args=(p, ext), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "接管中：停止外部实例并由本工具启动"}

    def _do_takeover(self, p: Project, ext: list[dict]) -> None:
        try:
            self.store.proj(p.id).pop("takeover_pending", None)
            self.store.save()
            ps = procs.snapshot()
            tree = {t for e in ext for t in e["tree"] if t in ps}
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for t in tree:
                    try:
                        os.kill(t, sig)
                    except OSError:
                        pass
                if procs.wait_until(lambda: not (tree & set(procs.snapshot())), 20 if sig == signal.SIGTERM else 5):
                    break
            self.errors.pop(p.id, None)
        except Exception as e:  # noqa: BLE001
            self._set_error(p.id, "", f"接管失败：{e}")
            self._end(p.id)
            return
        self._do_start(p, False)
        err = self.errors.get(p.id)
        if err and err["message"].startswith("无法启动"):
            err["message"] = ("外部实例停止后又被它的启动方重新拉起，需在启动它的程序（如 IDE / agent 会话）中关闭："
                              + err["message"].removeprefix("无法启动："))

    def _set_error(self, pid: str, comp: str, msg: str) -> None:
        self.errors[pid] = {"component": comp, "message": msg, "at": time.time()}

    def _run_step(self, p: Project, c: Component, cmd: list[str], what: str, timeout: float) -> tuple[bool, str]:
        if self.ops.get(p.id, {}).get("deadline") is not None:
            timeout = min(timeout, self._startup_remaining(p))
        env = manifest.component_env(p, c)
        lp = log_path(p.id, c.id)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "ab") as log:
            log.write(f"[localservices] {time.strftime('%Y-%m-%d %H:%M:%S')} {what}: {' '.join(cmd)}\n".encode())
            log.flush()
            try:
                proc = subprocess.Popen(cmd, cwd=p.comp_cwd(c), env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                output, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Dependency installers/build tools can spawn children. Only this newly
                # created process group belongs to the timed-out preparation step.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                log.write(f"[localservices] {what} timed out\n".encode())
                budget = self.ops.get(p.id, {}).get("budget", STARTUP_TIMEOUT)
                return False, f"启动超时（总预算 {budget:g}s）：{what}未完成"
            except OSError as e:
                return False, f"{what}无法执行：{e}"
            log.write(output)
        if proc.returncode != 0:
            out = "\n".join(redact(output.decode("utf-8", "replace")).strip().splitlines()[-12:])
            return False, f"{what}失败（exit {proc.returncode}）\n{out}"
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

    def _ready(self, p: Project, c: Component, cached: bool = True,
               timeout: float | None = None) -> tuple[bool, str]:
        ok, detail = (True, "")
        if c.ready:
            key = f"{p.id}.{c.id}"
            hit = self._probe_cache.get(key)
            if cached and hit and c.ready.interval and time.time() - hit < c.ready.interval:
                ok = True
            else:
                # status path: short timeout so one slow dev server can't stall the shared snapshot;
                # start path (cached=False) uses the manifest timeout to let first compiles finish
                probe_timeout = min(c.ready.timeout, 4.0 if cached else c.ready.timeout)
                if timeout is not None:
                    probe_timeout = min(probe_timeout, timeout)
                ok, detail = procs.probe(c.ready, probe_timeout)
                if ok:
                    self._probe_cache[key] = time.time()
                else:
                    self._probe_cache.pop(key, None)
        if ok:
            extra = self.adapters[p.id].component_health(c)
            if extra is not None:
                ok, detail = extra
        return ok, detail

    def _compose_state(self, p: Project, c: Component) -> dict:
        """compose.component_state 的短缓存版（状态刷新周期内复用，同探针缓存思路）。"""
        key = f"{p.id}.{c.id}"
        now = time.monotonic()
        hit = self._compose_state_cache.get(key)
        if hit and now - hit[0] < COMPOSE_STATE_TTL:
            return hit[1]
        try:
            st = compose.component_state(p, c)
        except Exception as e:  # noqa: BLE001 — docker 不可达时呈现 unknown，不炸整个快照
            st = {"state": "unknown", "health": None, "container": None, "error": str(e)}
        self._compose_state_cache[key] = (now, st)
        return st

    def _compose_component_status(self, p: Project, c: Component) -> dict:
        """compose 组件的状态映射（V2.7）：running+health / 端口探针 / Running 即就绪。"""
        key = f"{p.id}.{c.id}"
        st = self._compose_state(p, c)
        running = st.get("state") == "running"
        health = st.get("health")
        cs = {"id": c.id, "name": c.name or c.id, "kind": c.kind, "run": "compose",
              "pid": None, "running": running, "loaded": False,  # F-4：compose 无 launchd job，loaded 恒 false
              "started_at": self._compose_started.get(key), "required": c.required, "port": c.port,
              "last_exit": None, "failures": 0, "container": st.get("container"),
              "health": health or "none", "probe_used": key in self._compose_probe_used,
              "log": None, "code": {"state": "na", "label": "容器管理", "detail": ""}}
        note = ""
        if running and key not in self._compose_started:
            note = "容器已在运行（up 幂等，无需接管）"
        if st.get("state") == "unknown":
            cs.update(state="unknown", label="未知", detail=f"容器状态读取失败：{st.get('error', '')}")
            return cs
        if not running:
            detail = "" if st.get("state") == "missing" else f"容器状态 {st.get('state')}"
            cs.update(state="stopped", label="未运行", detail=detail)
            return cs
        if health is not None:
            if health == "healthy":
                cs.update(state="ready", label="就绪", detail=note)
            elif health == "unhealthy":
                cs.update(state="unhealthy", label="未就绪", detail="容器健康检查失败" + (f"；{note}" if note else ""))
            else:
                cs.update(state="starting", label="启动中", detail=f"健康检查 {health}" + (f"；{note}" if note else ""))
            return cs
        if c.ready:
            ok, detail = self._ready(p, c)
            if ok:
                cs.update(state="ready", label="就绪", detail=note)
                return cs
            started = self._compose_started.get(key, 0.0)
            if time.time() - started < c.start_timeout:
                cs.update(state="starting", label="启动中", detail=detail or "等待端口就绪")
            else:
                cs.update(state="unhealthy", label="未就绪", detail=detail or note)
            return cs
        cs.update(state="ready", label="就绪", detail=note or "容器运行中（无健康检查）")
        return cs

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
    def _startup_budget(self, p: Project | None) -> float:
        """启动总预算：STARTUP_TIMEOUT 为基线，组件声明的 start_timeout 超出默认 30s 的
        部分叠加进总预算（F-2：冷构建 >30s 时声明的超时应生效；叠加而非取 max，
        保持 STARTUP_TIMEOUT 可被测试/环境下调的既有语义）。"""
        if p is None:
            return STARTUP_TIMEOUT
        timeouts = [c.start_timeout for c in p.managed_components()]
        extra = max(0.0, max(timeouts, default=0.0) - 30.0)
        return STARTUP_TIMEOUT + min(270.0, extra)

    def _begin(self, pid: str, kind: str, budget: float | None = None) -> bool:
        with self.lock:
            if pid in self.ops:
                return False
            self.ops[pid] = {"kind": kind, "since": time.time()}
            if kind == "starting":
                self.ops[pid]["budget"] = budget if budget is not None else STARTUP_TIMEOUT
                self.ops[pid]["deadline"] = time.monotonic() + self.ops[pid]["budget"]
            return True

    def _startup_remaining(self, p: Project) -> float:
        op = self.ops.get(p.id, {})
        deadline = op.get("deadline")
        budget = op.get("budget", STARTUP_TIMEOUT)
        remaining = deadline - time.monotonic() if deadline is not None else budget
        if remaining <= 0:
            raise TimeoutError(f"启动超时（总预算 {budget:g}s）")
        return remaining

    def _end(self, pid: str) -> None:
        with self.lock:
            self.ops.pop(pid, None)

    def _prepare_dependencies(self, p: Project) -> None:
        if not any(d.required for d in p.dependencies):
            return

        def progress(detail):
            with self.lock:
                self.ops[p.id].update(kind="preparing", detail=detail)
                self.ops[p.id].pop("deadline", None)

        progress("检查必要依赖")
        self.infrastructure.ensure(p.dependencies, progress)
        with self.lock:
            self.ops[p.id].update(kind="starting", detail="",
                                  budget=self._startup_budget(p),
                                  deadline=time.monotonic() + self._startup_budget(p))

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
        if snap["state"] == "conflict":
            # A previous failure remains visible until retry. Recheck live ownership
            # rather than rejecting a retry based on that historical error.
            conflicts = self.conflicts(p)
            if conflicts:
                reason = "；".join(conflicts)
                self.store.set_intent(pid, "stopped", by="conflict")
                self._set_error(pid, "", "无法启动：" + reason)
                return {"ok": False, "accepted": False, "error": reason}
        if snap["state"] in ("available", "external"):
            if open_after:
                self.open(pid)
            return {"ok": True, "accepted": False,
                    "message": "已在运行" if snap["state"] == "available" else "已有外部实例在运行，已直接打开；可点「接管」改由本工具管理"}
        if not self._begin(pid, "starting", budget=self._startup_budget(p)):
            return {"ok": True, "accepted": False, "message": "启动中，请稍候"}
        self.store.set_intent(pid, "running")
        threading.Thread(target=self._do_start, args=(p, open_after), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "启动中"}

    def _do_start(self, p: Project, open_after: bool, only: list[str] | None = None, skip_build: bool = False,
                  by_user: bool = True) -> None:
        started_seq: list[tuple[str, Component]] = []  # ("local"|"compose", c) in start order
        previous: list[Component] = []
        success, conflicted = False, False
        try:
            self.errors.pop(p.id, None)
            ps = procs.snapshot()
            mine = self.our_pids(ps)
            previous = [c for c in p.managed_components() if launchd.info(self.label(p, c)).running]
            conf = self.conflicts(p, ps)
            if conf:
                conflicted = True
                self._set_error(p.id, "", "无法启动：" + "；".join(conf))
                if by_user:
                    self.store.set_intent(p.id, "stopped", by="conflict")
                # automatic start (autostart / re-login): keep intent, retry after the guard period
                return
            if not skip_build:
                self._prepare_dependencies(p)
            for c in self._order(p):
                if only and c.id not in only:
                    continue
                if c.run == "compose":
                    # V2.7: compose 组件不走 launchd/runner，readiness=容器状态
                    self._startup_remaining(p)
                    try:
                        res = compose.ensure_started(p, c)
                    except compose.ComposeError as e:
                        self._set_error(p.id, c.id, f"无法启动：compose 服务 {c.compose_service} 启动失败：{e}")
                        return
                    if res.get("action") != "already-running":
                        # 只有本次真正带起的容器才参与失败回滚；先前已在运行的容器不动
                        self._compose_started[f"{p.id}.{c.id}"] = time.time()
                        started_seq.append(("compose", c))
                    if not self._wait_compose_ready(p, c):
                        return
                    continue
                self._startup_remaining(p)
                ji = launchd.info(self.label(p, c))
                if ji.running and ji.pid in mine:
                    ok, _ = self._ready(p, c, timeout=self._startup_remaining(p))
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
                self._startup_remaining(p)
                started_seq.append(("local", c))
                ok, err = self._bootstrap(p, c)
                if not ok:
                    self._set_error(p.id, c.id, f"launchd 拒绝启动：{err}")
                    return
                if not self._wait_ready(p, c):
                    return
            for g in p.generators():
                self._run_generator(p, g, reason="启动")
            self._startup_remaining(p)
            success = True
            if open_after:
                self.open(p.id)
        except DependencyError as e:
            self._set_error(p.id, "", "必要依赖未就绪：" + str(e))
        except TimeoutError as e:
            self._set_error(p.id, "", str(e))
        except Exception as e:  # noqa: BLE001
            self._set_error(p.id, "", f"内部错误：{e}\n{traceback.format_exc(limit=3)}")
        finally:
            try:
                if not success and not conflicted:
                    if not previous:
                        self.store.set_intent(p.id, "stopped", by="start-failed")
                    if started_seq:
                        with self.lock:
                            self.ops[p.id]["kind"] = "stopping"
                        for kind, c in reversed(started_seq):
                            try:
                                if kind == "compose":
                                    # 只回滚本次启动带起的容器；先前已在运行的容器不动
                                    ok, err = (True, "")
                                    try:
                                        compose.stop_component(p, c)
                                    except compose.ComposeError as e:
                                        ok, err = False, str(e)
                                    self._compose_started.pop(f"{p.id}.{c.id}", None)
                                else:
                                    ok, err = self._bootout_wait(p, c)
                            except Exception as e:  # still release the operation if launchctl fails
                                ok, err = False, str(e)
                            if not ok:
                                original = self.errors.get(p.id, {}).get("message", "启动失败")
                                self._set_error(p.id, c.id, f"{original}；清理失败：{err}")
            finally:
                self._end(p.id)

    def _wait_compose_ready(self, p: Project, c: Component) -> bool:
        """compose 组件就绪：容器 Running，且（有 healthcheck 用 health；否则声明端口探针；都没有=Running 即就绪）。"""
        key = f"{p.id}.{c.id}"
        end = time.monotonic() + min(c.start_timeout, self._startup_remaining(p))
        detail = ""
        while time.monotonic() < end:
            try:
                st = compose.component_state(p, c)
            except compose.ComposeError as e:
                self._set_error(p.id, c.id, f"无法启动：compose 服务 {c.compose_service} 状态读取失败：{e}")
                return False
            if st["state"] == "missing":
                self._set_error(p.id, c.id, f"无法启动：compose 服务 {c.compose_service} 的容器不存在")
                return False
            if st["state"] != "running":
                time.sleep(min(0.2, max(0.0, end - time.monotonic())))
                continue
            health = st.get("health")
            if health is not None:
                if health == "healthy":
                    self._compose_probe_used.discard(key)
                    return True
                if health == "unhealthy":
                    self._set_error(p.id, c.id, f"无法启动：容器健康检查失败（unhealthy）")
                    return False
                detail = f"健康检查 {health}"
            elif c.ready:
                remaining = max(0.05, min(end - time.monotonic(), self._startup_remaining(p)))
                ok, detail = procs.probe(c.ready, min(c.ready.timeout, remaining))
                if ok:
                    self._compose_probe_used.add(key)
                    return True
            else:
                self._compose_probe_used.discard(key)
                return True  # Running 即就绪
            time.sleep(min(0.2, max(0.0, end - time.monotonic())))
        self._set_error(p.id, c.id, f"启动就绪超时（总预算 {STARTUP_TIMEOUT:g}s）：{detail}")
        return False

    def _wait_ready(self, p: Project, c: Component) -> bool:
        end = time.monotonic() + min(c.start_timeout, self._startup_remaining(p))
        label = self.label(p, c)
        detail = ""
        while time.monotonic() < end:
            rec = runner.read_record(p.id, c.id)
            if rec.get("gave_up"):
                self._set_error(p.id, c.id, f"{rec.get('error')}\n{tail(log_path(p.id, c.id), 15)}")
                return False
            ji = launchd.info(label)
            if not ji.loaded:
                self._set_error(p.id, c.id, "进程未在 launchd 中运行")
                return False
            if not ji.running and ji.last_exit not in (None, "(never exited)"):
                self._set_error(p.id, c.id, f"进程在就绪前退出（exit {ji.last_exit}）\n{tail(log_path(p.id, c.id), 15)}")
                return False
            if not ji.running:
                time.sleep(min(0.1, max(0, end - time.monotonic())))
                continue
            if c.port:
                listener = procs.listeners().get(c.port)
                if listener is not None and listener not in procs.descendants(ji.pid, procs.snapshot()):
                    self._set_error(p.id, c.id, f"端口 {c.port} 被其他进程占用（pid {listener}）")
                    return False
            remaining = min(end - time.monotonic(), self._startup_remaining(p))
            if remaining <= 0:
                break
            future = self._pool.submit(self._ready, p, c, cached=False, timeout=remaining)
            try:
                ok, detail = future.result(timeout=remaining)
            except TimeoutError:
                future.cancel()
                break
            if ok and time.monotonic() < end:
                return True
            time.sleep(min(0.1, max(0, end - time.monotonic())))
        self._set_error(p.id, c.id, f"启动就绪超时（总预算 {STARTUP_TIMEOUT:g}s）：{detail}\n{tail(log_path(p.id, c.id), 15)}")
        return False

    def stop(self, pid: str) -> dict:
        p = self.projects.get(pid)
        if not p or p.observe_only:
            return {"ok": False, "error": "不可停止"}
        if self.store.proj(pid).pop("takeover_pending", None):
            self.store.save()
        if not any(launchd.info(self.label(p, c)).loaded for c in p.managed_components()) and self.external_instances(p):
            self.store.set_intent(pid, "stopped")
            return {"ok": True, "accepted": False, "message": "运行中的是外部实例，本工具不停止它；已取消待接管"}
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
            self.errors.pop(p.id, None)
            for c in reversed(self._order(p)):
                if c.run == "compose":
                    try:
                        compose.stop_component(p, c)
                    except compose.ComposeError as e:
                        self._set_error(p.id, c.id, f"停止失败：{e}")
                        break
                    self._compose_started.pop(f"{p.id}.{c.id}", None)
                    continue
                ok, err = self._bootout_wait(p, c)
                if not ok:
                    self._set_error(p.id, c.id, f"停止失败：{err}")
                    break
        finally:
            self._end(p.id)

    def restart(self, pid: str, components: list[str] | None = None, reason: str = "user") -> dict:
        p = self.projects.get(pid)
        if not p or p.observe_only:
            return {"ok": False, "error": "不可重启"}
        if components is not None:
            known = {c.id for c in p.managed_components()}
            if (not isinstance(components, list) or not components
                    or any(not isinstance(cid, str) or cid not in known for cid in components)
                    or len(set(components)) != len(components)):
                return {"ok": False, "error": "重启组件必须为非空且不重复的已登记组件列表"}
        if self.external_instances(p):
            return {"ok": False, "error": "存在外部实例，请先接管，再重启指定组件"}
        busy = self.adapters[pid].busy()
        if busy:
            return {"ok": False, "error": f"有活动任务，已延后：{busy}"}
        if not self._begin(pid, "starting"):
            return {"ok": True, "accepted": False, "message": "操作进行中，请稍候"}
        self.store.set_intent(pid, "running")
        threading.Thread(target=self._do_restart, args=(p, components, True), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "重启中"}

    def group_action(self, group: str, action: str) -> dict:
        """Operate a declared workspace using existing per-project lifecycle operations."""
        members = sorted((p for p in self.projects.values() if p.group == group),
                         key=lambda p: (p.group_order, p.id))
        if not members or action not in ("start", "stop", "restart"):
            return {"ok": False, "error": "未知工作区或操作"}
        with self.lock:
            if any(p.id in self.ops for p in members):
                return {"ok": False, "error": "工作区成员操作进行中，请稍候"}
            for p in members:
                if p.observe_only or self.external_instances(p):
                    return {"ok": False, "error": f"{p.name} 包含外部实例／仅观察，请先处理其归属"}
                busy = self.adapters[p.id].busy()
                if busy:
                    return {"ok": False, "error": f"{p.name} 有活动任务，暂不操作：{busy}"}
            for p in members:
                self._begin(p.id, "stopping" if action == "stop" else "starting")
        threading.Thread(target=self._do_group_action, args=(members, action), daemon=True).start()
        return {"ok": True, "accepted": True, "message": "工作区操作中，按组件依赖顺序执行"}

    def _do_group_action(self, members: list[Project], action: str) -> None:
        order = list(reversed(members)) if action == "stop" else members
        try:
            for p in order:
                busy = self.adapters[p.id].busy()
                if busy:
                    self._set_error(p.id, "", f"工作区操作已停止：{busy}")
                    break
                # Each project keeps its own existing startup budget; queue time is excluded.
                if action != "stop":
                    self.ops[p.id]["deadline"] = time.monotonic() + STARTUP_TIMEOUT
                self.store.set_intent(p.id, "stopped" if action == "stop" else "running")
                if action == "restart":
                    self._do_restart(p, None, True)
                elif action == "start":
                    self._do_start(p, False)
                else:
                    self._do_stop(p)
                if p.id in self.errors:
                    break
        except Exception as err:
            self._set_error(p.id, "", f"工作区操作失败：{err}")
        finally:
            for p in members:
                self._end(p.id)

    def _do_restart(self, p: Project, components: list[str] | None, by_user: bool = False) -> None:
        try:
            self._prepare_dependencies(p)
            comps = [c for c in self._order(p) if not components or c.id in components]
            # Compile first so a broken/slow build never takes down the running instance.
            for c in comps:
                if c.run == "compose":
                    continue  # compose 组件无本地安装/编译步骤
                ok, err = self._maybe_install(p, c)
                if ok:
                    ok, err = self._build(p, c)
                if not ok:
                    self._set_error(p.id, c.id, err)
                    self._end(p.id)
                    return
            self._startup_remaining(p)
            for c in reversed(comps):
                if c.run == "compose":
                    try:
                        compose.stop_component(p, c)
                    except compose.ComposeError as e:
                        raise RuntimeError(f"{c.name or c.id} 停止失败：{e}")
                    self._compose_started.pop(f"{p.id}.{c.id}", None)
                    continue
                ok, err = self._bootout_wait(p, c)
                if not ok:
                    raise RuntimeError(f"{c.name or c.id} 停止失败：{err}")
            self._do_start(p, False, only=[c.id for c in comps], skip_build=True, by_user=by_user)
        except Exception as e:  # a preparation timeout must not leave a stuck operation
            self._set_error(p.id, "", f"重启失败：{e}")
            self._end(p.id)

    def open(self, pid: str) -> dict:
        p = self.projects.get(pid)
        if not p:
            return {"ok": False, "error": "未知项目"}
        url = self.adapters[pid].open_url()
        if not p.observe_only and not any(launchd.info(self.label(p, c)).running for c in p.managed_components()):
            adopt, ports = self._adoptable(p, self.external_instances(p, fresh=False))
            if adopt:
                url = self._external_url(p, ports)
        # V3.5：默认本地 IP 直连。仅当设置开启「使用域名」且配置了域名形式时，
        # 入口层健康才经域名打开；域名开启但入口故障 → 回退直连并说明缺口。
        entry_note = None
        st = self.domain_settings()
        if st.get("use_domains"):
            pub = self._public_open_url(p, st)
            if pub:
                url = pub
            elif self._form_endpoints(p, st):
                # 只有「该服务在当前域名形式下确有域名」才提示入口缺口；
                # custom 形式下无显式域名的服务本来就应直连，不算故障
                entry_note = "域名入口未就绪（系统件未安装或代理未运行），已打开直连地址；详见工作台域名诊断"
        if not url:
            return {"ok": False, "error": "没有可打开的地址"}
        subprocess.Popen(["/usr/bin/open", url])
        return {"ok": True, **({"note": entry_note} if entry_note else {})}

    @staticmethod
    def _form_endpoints(p: Project, settings: dict) -> list[Endpoint]:
        """V3.5：domain_form=custom → 只算显式登记入口；suffix（缺省）→ 全部。"""
        eps = p.endpoints
        if settings.get("domain_form") == "custom":
            eps = [e for e in eps if not e.derived]
        return eps

    def _public_open_url(self, p: Project, settings: dict | None = None) -> str | None:
        """入口层健康且服务处于可打开状态时返回无端口公开地址。

        V3.5：domain_form=custom 只跳转显式登记的入口（派生入口不算）。"""
        eps = self._form_endpoints(p, settings or self.domain_settings())
        if not eps:
            return None
        from . import locove
        try:
            st = locove.entry_status()
        except Exception:  # noqa: BLE001 — 域名层故障不阻塞直连打开
            return None
        if not st.get("entry", {}).get("ok"):
            return None
        snap = next((s for s in self.snapshot().get("projects", [])
                     if s.get("id") == p.id), None)
        if snap is None or snap.get("state") not in ("available", "partial", "external"):
            return None
        return f"http://{eps[0].host}{eps[0].open_path}"

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
            if c.run == "compose":
                # V2.7: compose 组件日志来自 docker logs（只读），同样过脱敏
                try:
                    out[c.id] = redact(compose.logs(p, c, lines) or "")
                except compose.ComposeError as e:
                    out[c.id] = f"[localservices] {e}"
                continue
            out[c.id] = tail(log_path(p.id, c.id), lines)
        return {"ok": True, "logs": out}

    # ---------- release (V2.1 / V2.7) ----------
    def release_service(self, sid: str) -> dict:
        """移除登记后释放本工具为该服务持有的运行期对象：bootout 已加载标签、删除
        agents/<prefix>.<sid>.*.plist、删除 run/<sid>.*.json、删除 state.json 该 sid 条目。

        幂等；只按 sid 前缀匹配，绝不触碰其他服务的文件，也绝不停容器（移除语义里
        共享/外部容器一律不动；受管 compose 容器由 ctl.stop 在移除流程更早阶段处置）。
        """
        released: list[str] = []
        errors: list[str] = []
        prefix = paths.label_prefix()
        try:
            for label in sorted(launchd.loaded_labels()):
                if label.startswith(f"{prefix}.{sid}."):
                    ok, err = launchd.bootout(label)
                    if ok:
                        released.append(label)
                    else:
                        errors.append(f"{label}: {err or 'bootout 失败'}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"launchd bootout: {e}")
        try:
            removed, errs = launchd.remove_plists(sid)
            released += [str(p) for p in removed]
            errors += errs
        except Exception as e:  # noqa: BLE001
            errors.append(f"删除 plist: {e}")
        try:
            for record in sorted(paths.run_dir().glob(f"{sid}.*.json")):
                try:
                    record.unlink()
                    released.append(str(record))
                except FileNotFoundError:
                    pass
                except OSError as e:
                    errors.append(f"{record}: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"删除 run 记录: {e}")
        with self.store.lock:
            try:
                if self.store.data.get("projects", {}).pop(sid, None) is not None:
                    self.store.save()
                    released.append(f"state:{sid}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"删除 state 条目: {e}")
        return {"ok": not errors, "released": released, "errors": errors}

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
            if self.store.proj(p.id).get("takeover_pending"):
                if not self.adapters[p.id].busy():
                    self.takeover(p.id)
                continue
            intent = self.store.intent(p)
            err = self.errors.get(p.id)
            if intent == "running" and err and time.time() - err["at"] < 120:
                continue  # a start just failed (build/install/launchd); don't hot-loop
            if intent == "running":
                for c in p.managed_components():
                    if c.run == "compose":
                        st = self._compose_state(p, c)
                        if st.get("state") != "running":
                            if self._begin(p.id, "starting"):
                                threading.Thread(target=self._do_start, args=(p, False),
                                                 kwargs={"by_user": False}, daemon=True).start()
                            break
                        continue
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
                    if c.run == "compose":
                        # 只停本会话由我们带起的容器；外部已在运行的容器按外部实例语义保留
                        if f"{p.id}.{c.id}" in self._compose_started:
                            try:
                                compose.stop_component(p, c)
                            except compose.ComposeError as e:
                                self._set_error(p.id, c.id, f"停止失败：{e}")
                            self._compose_started.pop(f"{p.id}.{c.id}", None)
                        continue
                    if launchd.info(self.label(p, c)).loaded:
                        self._bootout_wait(p, c)
        # 源码更新调度：与启停/生成器同属调度层，按最新快照事实驱动（R7/C4）。
        # 快照落后一个观测周期（≤3s）是调度语义的一部分，不是缺陷。
        self._schedule_updates()

    # ---------- status ----------
    def project_status(self, p: Project, ps: dict | None = None, listen: dict | None = None) -> dict:
        ad = self.adapters[p.id]
        base = {"id": p.id, "name": p.name, "source": str(p.source), "observe_only": p.observe_only,
                "description": p.description, "group": p.group,
                "group_name": p.group_name, "group_order": p.group_order,
                "endpoints": [{"id": e.id, "host": e.host, "component": e.component,
                               "open_path": e.open_path, "derived": e.derived,
                               "public_url": f"http://{e.host}{e.open_path}"}
                              for e in p.endpoints]}
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
            if c.run == "compose":
                cs = self._compose_component_status(p, c)
                comps.append(cs)
                comp_map[c.id] = cs
                continue
            ji = launchd.info(self.label(p, c))
            rec = runner.read_record(p.id, c.id)
            started = rec.get("started_at") if ji.running else None
            cs = {"id": c.id, "name": c.name or c.id, "kind": c.kind, "pid": ji.pid, "running": ji.running,
                  "loaded": ji.loaded, "started_at": started, "required": c.required,
                  "port": c.port,  # V3.5：直连地址/诊断直接可用，不依赖路由层收敛节奏
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
                         "managed": d.managed, "note": d.note, "required": d.required,
                         "recoverable": bool(d.required and d.container)})

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
        elif op and op["kind"] == "preparing":
            state = "preparing"
        elif op:
            state = "starting"
        elif intent != "running":
            state = "failed" if err and self.store.proj(p.id).get("intent_by") == "start-failed" else "stopped"
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
        external, ext_ports, ext_reason = [], {}, ""
        if state in ("stopped", "failed", "conflict") and not any_running and not op:
            ext = external = self.external_instances(p, ps, fresh=False)
            if ext:
                adopt, ext_ports = self._adoptable(p, ext)
                desc = "；".join(f"{e['name']}" + (f"（项目 {e['managed_project']}）" if e.get("managed_project") else "")
                                 + f" pid {e['pid']}" + (f"，端口 {'/'.join(map(str, e['ports']))}" if e["ports"] else "")
                                 + f"（{e['cmd']}）" for e in ext)
                for cs in managed:
                    mine = [e for e in ext if e["component"] == cs["id"]]
                    if mine:
                        cs.update(label="外部实例运行中", detail="；".join(
                            f"pid {e['pid']}" + (f" 端口 {'/'.join(map(str, e['ports']))}" if e["ports"] else "") for e in mine))
                if adopt:
                    state = "external"
                    pending = self.store.proj(p.id).get("takeover_pending")
                    ext_reason = (f"由其他程序启动，正在运行：{desc}。可直接打开；"
                                  + ("有活动任务，空闲后自动接管" if pending else "点「接管」改由本工具管理（会停止该实例）"))
                    if err and err.get("message", "").startswith("无法启动"):
                        err = None
                else:
                    state = "conflict"
                    ext_reason = ("检测到非本工具启动的实例，" + ("但未通过就绪检查" if all(e["same_project"] for e in ext)
                                                       else "且不是本项目的进程，本工具不会停止它") + "：" + desc)
        problems = []
        for cs in managed:
            if intent == "running" and cs["state"] not in ("ready",):
                problems.append(f"{cs['name']}：{cs['label']}" + (f"（{cs['detail']}）" if cs.get("detail") else ""))
        for d in dep_bad:
            aff = "、".join(d["affects"]) or p.name
            hint = "（启动项目时可恢复原容器）" if d["recoverable"] else (
                "（外部管理，本工具不启动）" if not d["managed"] else "")
            problems.append(f"依赖 {d['name']} 不可用，影响 {aff}" + hint)
        for g in gen_bad:
            problems.append(f"{g['name']}：{g['label']}")
        if op and op.get("detail"):
            reason = op["detail"]
        elif err:
            reason = err["message"]
        elif ext_reason:
            reason = ext_reason
        elif problems and state in ("partial", "failed"):
            reason = "；".join(problems)

        code_states = [cs["code"] for cs in managed
                       if cs["running"] and cs["code"].get("state") != "na"]  # compose 组件不参与代码态聚合
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
            "can_open": bool(ad.open_url()) and state in ("available", "partial", "external"),
            "can_takeover": bool(external) and all(e["same_project"] for e in external),
            "external_ports": ext_ports,
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
                    "components": [], "code": {"state": "unknown", "label": "未知"}, "source": str(p.source),
                    "group": p.group, "group_name": p.group_name, "group_order": p.group_order}

    def _refresh(self) -> dict:
        """纯观测：采集运行事实并投影为共享快照。不调度更新、不写运行意图（R7/C4）——
        更新/生成器/重试统一由 reconcile 周期依据本快照驱动（_schedule_updates）。"""
        self.reload()
        ps = procs.snapshot()
        projects = list(self.projects.values())
        out = list(self._pool.map(lambda p: self._status_safe(p, ps), projects))
        system = []
        if self.fsevents.get("ok") is False:
            system.append(self.fsevents["detail"])
        snap = {"projects": out, "generated_at": time.time(), "manifest_error": self.manifest_error,
                "system_warnings": system,
                "attention": bool(system) or any(s.get("state") in ("failed", "conflict", "partial") for s in out)}
        self._snapshot = snap
        return snap

    def _schedule_updates(self) -> None:
        """调度层（reconcile 周期）：按最新快照驱动源码更新调度。读取快照里的组件
        running/code 事实；观测（refresh）不再承担调度副作用，两层只经快照传递数据。"""
        snap_projects = {s.get("id"): s for s in (self._snapshot or {}).get("projects", [])}
        for p in list(self.projects.values()):
            if p.observe_only:
                continue
            st = snap_projects.get(p.id)
            if not st or st.get("intent") != "running":
                continue
            try:
                self._tick_updates(p, {c["id"]: c for c in st.get("components", [])
                                        if c.get("kind") != "generator"})
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def snapshot(self) -> dict:
        return self._snapshot
