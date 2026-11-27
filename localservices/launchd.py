"""launchd wiring: one per-user LaunchAgent per managed component.

Plists live in our own agents dir (not ~/Library/LaunchAgents), so launchd never loads a
component on its own at login; the daemon re-bootstraps components whose saved intent is
"running". launchd still owns keep-alive and process-group teardown.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths


def domain() -> str:
    return f"gui/{os.getuid()}"


def label_for(project: str, component: str) -> str:
    return f"{paths.label_prefix()}.{project}.{component}"


def plist_path(label: str) -> Path:
    return paths.agents_dir() / f"{label}.plist"


def write_plist(label: str, project: str, component: str, log_path: Path, env: dict[str, str],
                staged: Path | None = None) -> Path:
    data = {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "localservices.runner", project, component],
        "WorkingDirectory": str(paths.REPO_ROOT),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": int(os.environ.get("LOCALSERVICES_THROTTLE", "5")),
        "ExitTimeOut": 20,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if staged is not None:
        data["ProgramArguments"].append(str(staged))
    p = plist_path(label)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(data, f)
    os.replace(tmp, p)
    return p


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def bootstrap(label: str) -> tuple[bool, str]:
    r = _run("bootstrap", domain(), str(plist_path(label)))
    if r.returncode == 0:
        return True, ""
    # 37 / "already loaded" style errors mean the job exists; treat as success-ish.
    msg = (r.stderr or r.stdout).strip()
    if info(label).loaded:
        return True, msg
    return False, msg or f"launchctl bootstrap exit {r.returncode}"


def bootout(label: str) -> tuple[bool, str]:
    r = _run("bootout", f"{domain()}/{label}")
    if r.returncode == 0 or not info(label).loaded:
        return True, ""
    return False, (r.stderr or r.stdout).strip() or f"launchctl bootout exit {r.returncode}"


def kickstart(label: str) -> tuple[bool, str]:
    r = _run("kickstart", "-k", f"{domain()}/{label}")
    return r.returncode == 0, (r.stderr or r.stdout).strip()


@dataclass
class JobInfo:
    loaded: bool = False
    running: bool = False
    pid: int | None = None
    last_exit: str | None = None
    runs: int = 0


_PID = re.compile(r"^\s*pid = (\d+)", re.M)
_STATE = re.compile(r"^\s*state = (\S+)", re.M)
_EXIT = re.compile(r"^\s*last exit code = (.+)$", re.M)
_RUNS = re.compile(r"^\s*runs = (\d+)", re.M)


def info(label: str) -> JobInfo:
    r = _run("print", f"{domain()}/{label}")
    if r.returncode != 0:
        return JobInfo()
    out = r.stdout
    ji = JobInfo(loaded=True)
    m = _STATE.search(out)
    ji.running = bool(m and m.group(1) == "running")
    m = _PID.search(out)
    if m and ji.running:
        ji.pid = int(m.group(1))
    m = _EXIT.search(out)
    if m:
        ji.last_exit = m.group(1).strip()
    m = _RUNS.search(out)
    if m:
        ji.runs = int(m.group(1))
    return ji


def loaded_labels() -> set[str]:
    r = _run("list")
    prefix = paths.label_prefix() + "."
    out: set[str] = set()
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith(prefix):
            out.add(parts[2])
    return out


def plists_for(sid_prefix: str) -> list[Path]:
    """本工具为某服务持有的全部 plist（agents/<prefix>.<sid>.*.plist），无论是否已加载。"""
    return sorted(paths.agents_dir().glob(f"{paths.label_prefix()}.{sid_prefix}.*.plist"))


def remove_plists(sid_prefix: str) -> tuple[list[Path], list[str]]:
    """删除上述 plist 文件；返回 (removed, errors)。只删文件，bootout 由调用方编排（V2.1/V2.7）。"""
    removed: list[Path] = []
    errors: list[str] = []
    for plist in plists_for(sid_prefix):
        try:
            plist.unlink()
            removed.append(plist)
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append(f"{plist}: {e}")
    return removed, errors
