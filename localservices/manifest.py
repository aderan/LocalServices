"""Service manifests: one TOML file per project in services/. Adding a file adds a project."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

DEFAULT_PATH = ":".join(
    [
        str(Path.home() / ".pyenv" / "shims"),  # generic per-user python env, first so python3 resolves there
        str(Path.home() / ".goenv" / "shims"),  # generic per-user go env (N04 F-3: homebrew go may be older than go.mod requires)
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        str(Path.home() / ".local/bin"),
        str(Path.home() / "go/bin"),
        "/usr/local/go/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
)


@dataclass
class Probe:
    http: str | None = None  # GET, 2xx/3xx = ok
    tcp: str | None = None  # host:port
    json_ok: str | None = None  # optional dotted key that must be truthy in JSON body
    timeout: float = 2.0
    interval: float = 0.0  # cache successful results this long (expensive dev-server pages)

    @classmethod
    def parse(cls, d: dict | None) -> "Probe | None":
        if not d:
            return None
        return cls(**d)


@dataclass
class Component:
    id: str
    kind: str = "server"  # server | worker | generator
    name: str = ""
    cmd: list[str] = field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    env_files: list[str] = field(default_factory=list)
    port: int | None = None
    ready: Probe | None = None
    required: bool = True
    start_timeout: float = 30.0
    after: list[str] = field(default_factory=list)
    # code update
    hot: bool = False  # dev server applies source edits itself
    watch: list[str] = field(default_factory=list)
    restart_on: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)  # run before (re)start; failure keeps old instance
    install: list[str] = field(default_factory=list)
    install_when: list[str] = field(default_factory=list)  # files whose change requires install
    install_marker: str | None = None  # e.g. node_modules: present + no recorded hash = adopt as installed
    auto_update: bool = True
    # ownership: recognise instances we did not start
    external_cmd: str | None = None  # regex on process command line
    external_cwd: str | None = None  # relative to source; defaults to cwd
    external_cwd_exact: bool = False  # distinguish apps that share a monorepo source
    # V2.7 run adapter: local process (launchd+runner) vs docker compose service
    run: str = "local"  # local | compose
    compose_service: str | None = None  # required when run=compose
    compose_project: str | None = None  # default: name of the cwd directory
    compose_files: list[str] = field(default_factory=list)  # default: auto-detect in cwd
    provision: str = "existing"  # existing = start only; up = compose up -d --no-deps (needs explicit user confirmation at registration)
    # generator only
    debounce: float = 2.0


@dataclass
class Endpoint:
    """公开访问入口：host 是用户在浏览器看到的稳定无端口域名（Locove），
    component 决定真实上游组件（其监听端口），open_path 只是打开页面的落点。
    整个应用路径/脚本/资源都经同一 host 代理，不按 path 拆分。"""

    id: str = "web"
    host: str = ""
    component: str = ""
    open_path: str = "/"


def domain_suffix() -> str:
    """`.locove` 是本机命名约定（单机私有，非公网 TLD）；测试可用环境变量重定向。"""
    return os.environ.get("LOCALSERVICES_DOMAIN_SUFFIX", "locove")


def _norm_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if not host:
        raise ValueError("endpoint host 不能为空")
    if "." not in host:  # 单段 label：补全局后缀
        host = f"{host}.{domain_suffix()}"
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError(f"endpoint host 非法：{host!r}")
    return host


def _norm_open_path(path: str | None) -> str:
    if not path:
        return "/"
    return path if path.startswith("/") else f"/{path}"


@dataclass
class Dependency:
    name: str
    probe: Probe
    affects: list[str] = field(default_factory=list)
    managed: bool = False
    note: str = ""
    required: bool = False  # must be ready before compiling or starting the application
    container: str | None = None  # permission to start this existing Colima container, never create/stop it
    volume: str | None = None  # required existing named volume; prevents attaching the wrong database


@dataclass
class Project:
    id: str
    name: str
    source: Path
    open: str | None = None
    autostart: bool = False
    adapter: str | None = None
    observe_only: bool = False
    description: str = ""
    group: str | None = None
    group_name: str | None = None
    group_order: int = 0
    # workspace layer (registry): which user Project this service belongs to, and how it
    # was registered. Old manifests have neither and get a project derived at migration.
    project: str | None = None
    registered: dict = field(default_factory=dict)
    components: list[Component] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    file: Path | None = None
    options: dict = field(default_factory=dict)

    def component(self, cid: str) -> Component:
        for c in self.components:
            if c.id == cid:
                return c
        raise KeyError(f"{self.id}: no component {cid}")

    def managed_components(self) -> list[Component]:
        return [c for c in self.components if c.kind in ("server", "worker")]

    def generators(self) -> list[Component]:
        return [c for c in self.components if c.kind == "generator"]

    def comp_cwd(self, c: Component) -> Path:
        return (self.source / c.cwd).resolve()

    def public_urls(self) -> list[str]:
        """无端口公开地址（http://<host><open_path>）；供打开入口与 UI 主地址使用。"""
        return [f"http://{e.host}{_norm_open_path(e.open_path)}" for e in self.endpoints]

    def component_by_id(self, cid: str) -> Component | None:
        return next((c for c in self.components if c.id == cid), None)


def endpoint_rows(d: dict) -> list[dict]:
    """TOML 层 dict（id/name/source/open/components[/endpoints]）→ 归一化 endpoint 行。

    显式 endpoints 规范化；未提供时按稳定规则派生——与加载端同一逻辑，
    注册比较（_content_equal）、域名冲突预检共用，保证「预览 = 实际」两端一致。"""
    p = Project(id=str(d.get("id") or ""), name=str(d.get("name") or d.get("id") or ""),
                source=Path(os.path.expanduser(str(d.get("source") or "."))),
                open=d.get("open"),
                components=[_component(dict(c)) for c in d.get("components", []) or []])
    try:
        eps = _derive_endpoints(p, d.get("endpoints") or [])
    except ValueError:
        return [{"__invalid__": True}]
    return [{"id": e.id, "host": e.host, "component": e.component, "open_path": e.open_path}
            for e in eps]


def validate_component_graph(p: "Project") -> None:
    """组件依赖图校验：未知 after 引用与环在加载/注册阶段明确失败，并报告链路。

    引用可以指向任何组件（含 generator）；环按全组件图判定。"""
    ids = {c.id for c in p.components}
    for c in p.components:
        unknown = [a for a in c.after if a not in ids]
        if unknown:
            raise ValueError(f"{p.id}: 组件 {c.id} 的 after 引用未知组件 {', '.join(unknown)}")
    # 环检测：DFS 留痕，报出完整链路
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {c.id: WHITE for c in p.components}
    stack: list[str] = []

    def visit(cid: str) -> None:
        color[cid] = GRAY
        stack.append(cid)
        comp = next(c for c in p.components if c.id == cid)
        for nxt in comp.after:
            if color.get(nxt, BLACK) == GRAY:
                cycle = stack[stack.index(nxt):] + [nxt]
                raise ValueError(f"{p.id}: 组件依赖环 {' → '.join(cycle)}")
            if color.get(nxt) == WHITE:
                visit(nxt)
        stack.pop()
        color[cid] = BLACK

    for c in p.components:
        if color[c.id] == WHITE:
            visit(c.id)


def _derive_endpoints(p: "Project", raw: list[dict]) -> list[Endpoint]:
    """显式 endpoints 规范化；旧清单没有 endpoints 时按稳定规则派生缺省入口：
    host = <service id>.<suffix>（锚定稳定 id，不随显示名变化）；
    component = open URL 端口对应的组件，否则第一个有端口的 server 组件；
    open_path = open URL 的路径，否则 /。没有任何可浏览端口 → 无入口。"""
    if raw:
        out, seen = [], set()
        for i, d in enumerate(raw):
            if not isinstance(d, dict):
                raise ValueError(f"{p.id}: endpoints[{i}] 必须是表")
            host = _norm_host(str(d.get("host", "")))
            comp = str(d.get("component", ""))
            if not comp:
                raise ValueError(f"{p.id}: endpoint {host} 缺少 component")
            if p.component_by_id(comp) is None:
                raise ValueError(f"{p.id}: endpoint {host} 引用未知组件 {comp}")
            if host in seen:
                raise ValueError(f"{p.id}: endpoint host 重复：{host}")
            seen.add(host)
            label = host.split(".")[0]
            out.append(Endpoint(id=str(d.get("id") or f"ep-{i + 1}-{label}"), host=host,
                                component=comp, open_path=_norm_open_path(d.get("open_path"))))
        return out
    open_port, open_path = None, "/"
    if p.open:
        m = re.fullmatch(r"https?://[^/:]+:(\d+)(/.*)?", p.open)
        if m:
            open_port, open_path = int(m.group(1)), _norm_open_path(m.group(2))
    target = None
    for c in p.components:
        if c.kind == "server" and c.port:
            if open_port is not None and c.port == open_port:
                target = c
                break
            if target is None:
                target = c
    if target is None:
        return []
    return [Endpoint(id="default", host=f"{p.id}.{domain_suffix()}", component=target.id,
                     open_path=open_path)]


def _component(d: dict) -> Component:
    d = dict(d)
    d["ready"] = Probe.parse(d.get("ready"))
    return Component(**d)


def load_file(path: Path) -> Project:
    with open(path, "rb") as f:
        d = tomllib.load(f)
    comps = [_component(c) for c in d.pop("components", [])]
    deps = []
    for dep in d.pop("dependencies", []):
        dep = dict(dep)
        dep["probe"] = Probe(**dep["probe"])
        deps.append(Dependency(**dep))
    raw_endpoints = d.pop("endpoints", [])
    d["source"] = Path(os.path.expanduser(d["source"]))
    p = Project(**d, components=comps, dependencies=deps, file=path)
    seen = set()
    for c in p.components:
        if c.id in seen:
            raise ValueError(f"{path}: duplicate component {c.id}")
        seen.add(c.id)
        if c.run == "compose":
            if not c.compose_service:
                raise ValueError(f"{path}: component {c.id} run=compose 需要 compose_service")
            if c.provision not in ("existing", "up"):
                raise ValueError(f"{path}: component {c.id} provision 必须是 existing/up")
            continue  # compose 组件无本地 cmd
        if c.kind in ("server", "worker", "generator") and not c.cmd:
            raise ValueError(f"{path}: component {c.id} has no cmd")
    validate_component_graph(p)
    p.endpoints = _derive_endpoints(p, raw_endpoints)
    return p


def load_all(directory: Path | None = None) -> dict[str, Project]:
    """Load built-in services plus user-registered ones. An explicit `directory` loads only
    that directory (used by tests); otherwise the search path is services_dir() +
    user_services_dir(). Duplicate ids and duplicate listen ports are rejected across all
    loaded files so there is exactly one registration authority. Services listed in the
    user-level tombstone file (V2.0) are skipped — removing a builtin never edits the
    repo-distributed template."""
    from . import tombstones
    dirs = [Path(directory)] if directory else paths.service_search_path()
    skipped = tombstones.services()
    builtin_dir = paths.services_dir().resolve()
    out: dict[str, Project] = {}
    for d in dirs:
        for f in sorted(d.glob("*.toml")):
            p = load_file(f)
            if p.id in out:
                raise ValueError(f"duplicate project id {p.id}: {out[p.id].file} and {f}")
            # V2.0 修订：tombstone 只遮蔽内置目录的模板；用户层重新注册的同 id 文件始终加载
            if directory is None and p.id in skipped and f.parent.resolve() == builtin_dir:
                continue
            out[p.id] = p
    ports: dict[int, str] = {}
    hosts: dict[str, str] = {}
    for p in out.values():
        for c in p.managed_components():
            if not c.port:
                continue
            owner = f"{p.id}.{c.id}"
            if c.port in ports:
                raise ValueError(f"duplicate listen port {c.port}: {ports[c.port]} and {owner}")
            ports[c.port] = owner
        for e in p.endpoints:
            owner = f"{p.id}.{e.id}"
            if e.host in hosts:
                raise ValueError(f"duplicate endpoint host {e.host}: {hosts[e.host]} and {owner}")
            hosts[e.host] = owner
    return out


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        env[k] = v
    return env


def component_env(p: Project, c: Component) -> dict[str, str]:
    env = {
        "PATH": DEFAULT_PATH,
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "USER": os.environ.get("USER", ""),
        "LOCALSERVICES": "1",
    }
    for ef in c.env_files:
        env.update(read_env_file((p.source / ef)))
    for k, v in c.env.items():
        env[k] = os.path.expanduser(v.replace("{source}", str(p.source)))
    return env
