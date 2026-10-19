"""Acceptance helper: Next.js hot-update path using the real Buildbase manifest on an APFS clone.

Runs an isolated controller (own home / label prefix / port 3020) so a Buildbase instance that
other sessions are using is never touched. Prints one summary line; exit 0 = verified.
  python3 tests/verify_next_clone.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = Path.home() / "Sources/Products/Buildbase"
WORK = Path.home() / "Library/Application Support/LocalServices/verify"  # real path: FSEvents ignores /tmp symlink
CLONE = WORK / "buildbase-clone"
PORT = 3020
home = Path(tempfile.mkdtemp(prefix="lsverify-"))
(home / "services").mkdir()
os.environ.update({"LOCALSERVICES_HOME": str(home / "h"), "LOCALSERVICES_SERVICES": str(home / "services"),
                   "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsverify"})
sys.path.insert(0, str(REPO))
from localservices import launchd, procs  # noqa: E402
from localservices.controller import Controller  # noqa: E402


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    shutil.rmtree(CLONE, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-c", "-R", str(SRC), str(CLONE)], check=True)
    shutil.rmtree(CLONE / ".next", ignore_errors=True)
    m = (REPO / "services/buildbase.toml").read_text()
    m = m.replace('source = "~/Sources/Products/Buildbase"', f'source = "{CLONE}"').replace("3000", str(PORT))
    m = m.replace('id = "buildbase"', 'id = "bbverify"')
    (home / "services/bbverify.toml").write_text(m)
    ctl = Controller()
    p = ctl.projects["bbverify"]
    try:
        log("starting clone via controller")
        ctl.start("bbverify")
        end = time.time() + 480
        while time.time() < end and ("bbverify" in ctl.ops or ctl.project_status(p)["state"] != "available"):
            time.sleep(5)
        st = ctl.project_status(p)
        if st["state"] != "available":
            log(f"FAIL start: {st['state']} {st.get('reason')}")
            return 1
        pid = st["components"][0]["pid"]
        log(f"available pid={pid}")
        route = CLONE / "src/app/api/ls-probe/route.ts"
        route.parent.mkdir(parents=True, exist_ok=True)
        results = []
        for ver in ("v1", "v2"):
            route.write_text(f'export function GET() {{ return new Response("LS_PROBE next {ver}"); }}\n')
            ok = False
            for _ in range(60):
                code, body, _ = procs.http_get(f"http://127.0.0.1:{PORT}/api/ls-probe", 60)
                if code == 200 and f"LS_PROBE next {ver}".encode() in body:
                    ok = True
                    break
                time.sleep(2)
            pid_now = ctl.project_status(p)["components"][0]["pid"]
            results.append((ver, ok, pid_now == pid))
            log(f"{ver}: served={ok} same_pid={pid_now == pid}")
        code = ctl.project_status(p)["code"]
        log(f"code state: {code.get('label')}")
        good = all(ok and same for _, ok, same in results)
        print(f"VERIFY NEXT: {'PASS' if good else 'FAIL'} {results}", flush=True)
        return 0 if good else 1
    finally:
        for c in p.managed_components():
            ctl._bootout_wait(p, c)
        shutil.rmtree(CLONE, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
