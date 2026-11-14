"""Runtime locations. Everything can be redirected with LOCALSERVICES_HOME (used by tests)."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
    env = os.environ.get("LOCALSERVICES_HOME")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "LocalServices"


def logs_dir() -> Path:
    env = os.environ.get("LOCALSERVICES_HOME")
    if env:
        return Path(env) / "logs"
    return Path.home() / "Library" / "Logs" / "LocalServices"


def services_dir() -> Path:
    env = os.environ.get("LOCALSERVICES_SERVICES")
    return Path(env) if env else home() / "builtin-services"


def user_services_dir() -> Path:
    """Registered (GUI/CLI/Skill) service definitions; same TOML schema as services_dir()."""
    env = os.environ.get("LOCALSERVICES_USER_SERVICES")
    return Path(env) if env else home() / "services"


def service_search_path() -> list[Path]:
    dirs = [services_dir(), user_services_dir()]
    return [d for d in dirs if d.is_dir()]


def drafts_dir() -> Path:
    """Incomplete registrations (JSON drafts); never loaded by the manifest engine."""
    return home() / "drafts"


def registry_file() -> Path:
    """Workspace registry: Projects, Directories, service membership, agent choice."""
    return home() / "registry.json"


def agent_connections_file() -> Path:
    return home() / "agent-connections.json"


def jobs_dir() -> Path:
    return home() / "jobs"


def label_prefix() -> str:
    return os.environ.get("LOCALSERVICES_LABEL_PREFIX", "com.mybrain.ls")


def agents_dir() -> Path:
    return home() / "agents"


def run_dir() -> Path:
    return home() / "run"


def build_dir() -> Path:
    return home() / "build"


def state_file() -> Path:
    return home() / "state.json"


def removed_file() -> Path:
    """用户配置层移除标记（tombstone）：V2.0。"""
    return home() / "removed.json"


def removed_dir() -> Path:
    """移除归档目录（removed/<sid>-<ts>.json）。"""
    return home() / "removed"


def overrides_file() -> Path:
    """人工覆盖层（V2.3）。"""
    return home() / "overrides.json"


def token_file() -> Path:
    return home() / "token"


def daemon_port() -> int:
    return int(os.environ.get("LOCALSERVICES_PORT", "47800"))


def ensure() -> None:
    for d in (home(), logs_dir(), agents_dir(), run_dir(), build_dir(),
              user_services_dir(), drafts_dir(), jobs_dir(), removed_dir()):
        d.mkdir(parents=True, exist_ok=True)
