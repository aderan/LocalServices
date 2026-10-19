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
    return Path(env) if env else REPO_ROOT / "services"


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


def token_file() -> Path:
    return home() / "token"


def daemon_port() -> int:
    return int(os.environ.get("LOCALSERVICES_PORT", "47800"))


def ensure() -> None:
    for d in (home(), logs_dir(), agents_dir(), run_dir(), build_dir()):
        d.mkdir(parents=True, exist_ok=True)
