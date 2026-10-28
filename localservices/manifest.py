"""Service manifests: one TOML file per project in services/. Adding a file adds a project."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

DEFAULT_PATH = ":".join(
    [
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
    # generator only
    debounce: float = 2.0


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
    components: list[Component] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
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
    d["source"] = Path(os.path.expanduser(d["source"]))
    p = Project(**d, components=comps, dependencies=deps, file=path)
    seen = set()
    for c in p.components:
        if c.id in seen:
            raise ValueError(f"{path}: duplicate component {c.id}")
        seen.add(c.id)
        if c.kind in ("server", "worker", "generator") and not c.cmd:
            raise ValueError(f"{path}: component {c.id} has no cmd")
    return p


def load_all(directory: Path | None = None) -> dict[str, Project]:
    directory = directory or paths.services_dir()
    out: dict[str, Project] = {}
    for f in sorted(directory.glob("*.toml")):
        p = load_file(f)
        if p.id in out:
            raise ValueError(f"duplicate project id {p.id}")
        out[p.id] = p
    ports: dict[int, str] = {}
    for p in out.values():
        for c in p.managed_components():
            if not c.port:
                continue
            owner = f"{p.id}.{c.id}"
            if c.port in ports:
                raise ValueError(f"duplicate listen port {c.port}: {ports[c.port]} and {owner}")
            ports[c.port] = owner
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
