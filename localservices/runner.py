"""launchd entry point for one component: bounded crash backoff, then exec the real command.

launchd restarts us after a non-zero exit (KeepAlive.SuccessfulExit=false). Each launch we
look at how long the previous run lived; short runs count as consecutive failures. After
MAX_FAILURES we exit 0 so launchd stops restarting, and record why for the daemon.
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import manifest, paths

MAX_FAILURES = int(os.environ.get("LOCALSERVICES_MAX_FAILURES", "5"))
HEALTHY_AFTER = float(os.environ.get("LOCALSERVICES_HEALTHY_AFTER", "30"))  # longer run resets the counter
MAX_DELAY = 60.0
BACKOFF_SCALE = float(os.environ.get("LOCALSERVICES_BACKOFF_SCALE", "1"))


def record_path(project: str, component: str):
    return paths.run_dir() / f"{project}.{component}.json"


def read_record(project: str, component: str) -> dict:
    try:
        return json.loads(record_path(project, component).read_text())
    except (OSError, ValueError):
        return {}


def write_record(project: str, component: str, rec: dict) -> None:
    p = record_path(project, component)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def reset_record(project: str, component: str) -> None:
    rec = read_record(project, component)
    rec.update({"failures": 0, "gave_up": False, "fresh": True})
    write_record(project, component, rec)


def main(argv: list[str]) -> int:
    project_id, comp_id = argv[0], argv[1]
    now = time.time()
    rec = read_record(project_id, comp_id)
    failures = int(rec.get("failures", 0))
    if not rec.get("fresh"):
        last = rec.get("started_at")
        if last and now - float(last) < HEALTHY_AFTER:
            failures += 1
        else:
            failures = 0
    rec["fresh"] = False

    try:
        project = manifest.load_all()[project_id]
        comp = project.component(comp_id)
    except Exception as e:  # manifest broken: give up immediately, visible to daemon
        rec.update({"gave_up": True, "error": f"manifest: {e}", "failures": failures})
        write_record(project_id, comp_id, rec)
        print(f"[localservices] manifest error: {e}", flush=True)
        return 0

    if failures >= MAX_FAILURES:
        rec.update({"gave_up": True, "failures": failures, "gave_up_at": now,
                    "error": f"连续 {failures} 次启动后很快退出，已停止自动恢复"})
        write_record(project_id, comp_id, rec)
        print(f"[localservices] {project_id}/{comp_id}: {failures} quick failures, giving up", flush=True)
        return 0

    if failures:
        delay = min(2.0 ** failures, MAX_DELAY) * BACKOFF_SCALE
        rec.update({"failures": failures, "backoff": delay, "gave_up": False, "phase": "backoff",
                    "pid": os.getpid(), "backoff_until": now + delay})
        write_record(project_id, comp_id, rec)
        print(f"[localservices] {project_id}/{comp_id}: failure #{failures}, backoff {delay:.0f}s", flush=True)
        time.sleep(delay)

    env = manifest.component_env(project, comp)
    cwd = project.comp_cwd(comp)
    comp.cmd = [a.replace("{build}", str(paths.build_dir())) for a in comp.cmd]
    rec.update({
        "failures": failures, "gave_up": False, "error": None, "backoff": 0, "phase": "exec",
        "started_at": time.time(), "pid": os.getpid(), "cmd": comp.cmd, "cwd": str(cwd),
    })
    write_record(project_id, comp_id, rec)
    print(f"[localservices] {time.strftime('%Y-%m-%d %H:%M:%S')} start {project_id}/{comp_id}: "
          f"{' '.join(comp.cmd)} (cwd {cwd})", flush=True)
    os.chdir(cwd)
    try:
        os.execvpe(comp.cmd[0], comp.cmd, env)
    except OSError as e:
        print(f"[localservices] exec failed: {e}", flush=True)
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
