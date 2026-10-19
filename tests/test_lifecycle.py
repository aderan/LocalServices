"""Lifecycle tests against real launchd with an isolated label prefix, home and synthetic services.

Run: python3 -m unittest discover -s tests -v   (from repo root; takes ~2 minutes)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="lstest-"))
os.environ.update({
    "LOCALSERVICES_HOME": str(TMP / "home"),
    "LOCALSERVICES_SERVICES": str(TMP / "services"),
    "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lstest",
    "LOCALSERVICES_THROTTLE": "1",
    "LOCALSERVICES_BACKOFF_SCALE": "0.25",
    "LOCALSERVICES_HEALTHY_AFTER": "4",
    "LOCALSERVICES_MAX_FAILURES": "3",
})
sys.path.insert(0, str(REPO))

from localservices import launchd, procs, runner  # noqa: E402
from localservices.controller import Controller  # noqa: E402

FIXTURE = REPO / "tests" / "fixture_server.py"
PY = sys.executable


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait(fn, timeout=30.0, interval=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return fn()


class Base(unittest.TestCase):
    project_id = "fx"
    extra = ""

    @classmethod
    def setUpClass(cls):
        cls.src = TMP / f"src-{cls.project_id}"
        cls.src.mkdir(parents=True, exist_ok=True)
        (cls.src / "VERSION").write_text("v1")
        (TMP / "services").mkdir(exist_ok=True)
        cls.port = free_port()
        cls.busy = cls.src / "BUSY"
        cls.crash = cls.src / "CRASH"
        cls.broken = cls.src / "BROKEN"
        (TMP / "services" / f"{cls.project_id}.toml").write_text(textwrap.dedent(f"""
            id = "{cls.project_id}"
            name = "Fixture {cls.project_id}"
            source = "{cls.src}"
            open = "http://127.0.0.1:{cls.port}/"
            options = {{ busy_http = "http://127.0.0.1:{cls.port}/busy" }}

            [[components]]
            id = "api"
            cmd = ["{PY}", "{FIXTURE}", "{cls.port}", "--crash-file", "{cls.crash}", "--busy-file", "{cls.busy}"]
            port = {cls.port}
            ready = {{ http = "http://127.0.0.1:{cls.port}/health", json_ok = "ok" }}
            start_timeout = 20
            watch = ["VERSION"]
            build = ["{PY}", "-c", "import os,sys; sys.exit(3 if os.path.exists('BROKEN') else 0)"]
            external_cmd = "fixture_server"
            {cls.extra}
        """))
        cls.ctl = Controller()

    @classmethod
    def tearDownClass(cls):
        for p in cls.ctl.projects.values():
            for c in p.managed_components():
                launchd.bootout(cls.ctl.label(p, c))

    def status(self):
        self.ctl.reload()
        return self.ctl.project_status(self.ctl.projects[self.project_id])

    def wait_state(self, *states, timeout=40):
        return wait(lambda: self.status()["state"] in states and self.project_id not in self.ctl.ops, timeout)

    def version(self):
        code, body, _ = procs.http_get(f"http://127.0.0.1:{self.port}/health")
        return json.loads(body)["version"] if code == 200 else None


class LifecycleTest(Base):
    def test_1_start_idempotent_stop(self):
        r = self.ctl.start("fx")
        self.assertTrue(r["accepted"])
        r2 = self.ctl.start("fx")  # immediate double click
        self.assertFalse(r2["accepted"])
        self.assertTrue(self.wait_state("available"), self.status())
        pid = launchd.info(self.ctl.label(self.ctl.projects["fx"], self.ctl.projects["fx"].components[0])).pid
        r3 = self.ctl.start("fx")
        self.assertFalse(r3["accepted"])
        self.assertEqual(r3["message"], "已在运行")
        listeners = procs.listeners()
        self.assertEqual(listeners.get(self.port) in procs.descendants(pid, procs.snapshot()), True)

        self.ctl.stop("fx")
        self.assertTrue(self.wait_state("stopped"))
        # reconcile must not bring an intentionally stopped project back
        for _ in range(3):
            self.ctl.reconcile()
            time.sleep(1)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertFalse(procs.tcp_ok(f"127.0.0.1:{self.port}"))

    def test_2_crash_recovery_and_bounded_backoff(self):
        self.ctl.start("fx")
        self.assertTrue(self.wait_state("available"))
        comp = self.ctl.projects["fx"].components[0]
        label = self.ctl.label(self.ctl.projects["fx"], comp)
        pid1 = launchd.info(label).pid
        time.sleep(4.5)  # past HEALTHY_AFTER: a crash now is not a "quick" failure
        os.kill(pid1, signal.SIGKILL)
        self.assertTrue(wait(lambda: (launchd.info(label).pid or pid1) != pid1 and self.version(), 30))
        self.assertEqual(self.status()["state"], "available")

        # now make every start crash: bounded retries, then give up and report failure
        self.crash.write_text("1")
        os.kill(launchd.info(label).pid, signal.SIGKILL)
        rec = wait(lambda: runner.read_record("fx", "api").get("gave_up") and runner.read_record("fx", "api"), 60)
        self.assertTrue(rec, runner.read_record("fx", "api"))
        st = self.status()
        self.assertEqual(st["state"], "failed")
        self.assertIn("停止自动恢复", st["reason"])
        runs = launchd.info(label).runs
        time.sleep(4)
        self.assertEqual(launchd.info(label).runs, runs, "launchd kept restarting after give-up")
        # user fixes the problem and retries
        self.crash.unlink()
        self.ctl.start("fx")
        self.assertTrue(self.wait_state("available"))

    def test_3_conflict_with_external_instance(self):
        self.ctl.stop("fx")
        self.assertTrue(self.wait_state("stopped"))
        ext = subprocess.Popen([PY, str(FIXTURE), str(self.port)], cwd=self.src,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait(lambda: procs.tcp_ok(f"127.0.0.1:{self.port}"), 10))
            self.ctl.start("fx")
            self.assertTrue(self.wait_state("conflict"), self.status())
            st = self.status()
            self.assertIn(str(ext.pid), st["reason"])
            self.assertIsNone(ext.poll(), "external process must not be killed")
            self.assertEqual(st["intent"], "stopped")
        finally:
            ext.terminate()
            ext.wait()

    def test_4_code_update_busy_deferral_and_build_failure(self):
        self.ctl.start("fx")
        self.assertTrue(self.wait_state("available"))
        self.assertEqual(self.version(), "v1")
        # active synthetic job: change is detected but deferred
        self.busy.write_text("1")
        time.sleep(1.1)
        (self.src / "VERSION").write_text("v2")
        time.sleep(4)
        self.ctl.refresh_snapshot()
        st = self.status()
        self.assertEqual(st["code"]["state"], "pending")
        self.assertIn("已延后", st["components"][0]["code"]["label"])
        self.assertEqual(self.version(), "v1")
        # job finished: update applied at the next safe point
        self.busy.unlink()
        self.assertTrue(wait(lambda: (self.ctl.refresh_snapshot() and self.version() == "v2"), 40))
        self.assertTrue(wait(lambda: self.status()["code"]["state"] == "synced", 20), self.status()["code"])
        # broken build: old instance keeps serving, status says update failed
        self.broken.write_text("1")
        time.sleep(1.1)
        (self.src / "VERSION").write_text("v3")
        self.assertTrue(wait(lambda: (self.ctl.refresh_snapshot() and self.status()["code"]["state"] == "failed"), 40),
                        self.status()["code"])
        self.assertEqual(self.version(), "v2")
        self.assertIn("编译失败", self.status()["code"]["detail"])
        # fix and edit again: recovers
        self.broken.unlink()
        time.sleep(1.1)
        (self.src / "VERSION").write_text("v4")
        self.assertTrue(wait(lambda: (self.ctl.refresh_snapshot() and self.version() == "v4"), 40))

    def test_5_controller_restart_rediscovers(self):
        self.ctl.start("fx")
        self.assertTrue(self.wait_state("available"))
        comp = self.ctl.projects["fx"].components[0]
        pid = launchd.info(self.ctl.label(self.ctl.projects["fx"], comp)).pid
        ctl2 = Controller()  # simulates daemon restart / re-login reconcile
        st = ctl2.project_status(ctl2.projects["fx"])
        self.assertEqual(st["state"], "available")
        self.assertEqual(st["components"][0]["pid"], pid)
        ctl2.reconcile()
        time.sleep(1)
        self.assertEqual(launchd.info(self.ctl.label(self.ctl.projects["fx"], comp)).pid, pid, "no duplicate start")

    def test_6_relogin_restores_intent(self):
        # simulate logout: jobs disappear from launchd but saved intent remains
        self.ctl.start("fx")
        self.assertTrue(self.wait_state("available"))
        comp = self.ctl.projects["fx"].components[0]
        launchd.bootout(self.ctl.label(self.ctl.projects["fx"], comp))
        ctl2 = Controller()
        ctl2.reconcile()
        self.assertTrue(wait(lambda: ctl2.project_status(ctl2.projects["fx"])["state"] == "available" and "fx" not in ctl2.ops, 30))
        # intentionally stopped project stays stopped after "re-login"
        ctl2.stop("fx")
        self.assertTrue(wait(lambda: ctl2.project_status(ctl2.projects["fx"])["state"] == "stopped" and "fx" not in ctl2.ops, 30))
        ctl3 = Controller()
        ctl3.reconcile()
        time.sleep(2)
        self.assertEqual(ctl3.project_status(ctl3.projects["fx"])["state"], "stopped")

    def test_7_log_redaction(self):
        self.ctl.start("fx")
        self.assertTrue(self.wait_state("available"))
        logs = self.ctl.logs("fx", None)["logs"]["api"]
        self.assertIn("fixture: listening", logs)
        self.assertNotIn("hunter2hunter2", logs)
        from localservices.controller import redact
        self.assertNotIn("9736a2", redact("Workbench: http://127.0.0.1:1/#t=9736a27340bd2295a89f35cb76db19f5"))
        self.assertNotIn("s3cret", redact("postgres://u:s3cret@host/db"))


class DependencyTest(Base):
    project_id = "fxdep"
    extra = ""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        dead = free_port()
        with open(TMP / "services" / "fxdep.toml", "a") as f:
            f.write(textwrap.dedent(f"""
                [[dependencies]]
                name = "FakeModel"
                probe = {{ tcp = "127.0.0.1:{dead}" }}
                affects = ["api"]
            """))
        cls.ctl.reload()

    def test_dependency_missing_is_partial(self):
        self.ctl.start("fxdep")
        self.assertTrue(self.wait_state("partial", "available"))
        st = self.status()
        self.assertEqual(st["state"], "partial")
        self.assertIn("FakeModel", st["reason"])
        self.ctl.stop("fxdep")
        self.wait_state("stopped")


class HttpAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = dict(os.environ, LOCALSERVICES_PORT=str(cls.port))
        cls.proc = subprocess.Popen([PY, "-m", "localservices.daemon"], cwd=REPO, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait(lambda: procs.tcp_ok(f"127.0.0.1:{cls.port}"), 15)
        cls.token = (TMP / "home" / "token").read_text().strip()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def req(self, method, path, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   data=b"{}" if method == "POST" else None, headers=headers or {})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_auth(self):
        tok = {"X-LocalServices-Token": self.token}
        self.assertEqual(self.req("GET", "/api/ping"), 200)
        self.assertEqual(self.req("GET", "/api/status"), 401)
        self.assertEqual(self.req("GET", "/api/status", tok), 200)
        self.assertEqual(self.req("POST", "/api/projects/fx/stop", {"X-LocalServices-Token": "wrong"}), 401)
        self.assertEqual(self.req("POST", "/api/projects/fx/stop", {**tok, "Origin": "http://evil.example"}), 403)
        self.assertEqual(self.req("GET", "/api/status", {**tok, "Host": "evil.example:80"}), 403)
        self.assertEqual(self.req("GET", "/", {"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self.req("POST", "/api/projects/fx/rm-rf"), 404 if False else 401)


def tearDownModule():
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "com.mybrain.lstest." in line:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{line.split()[-1]}"])
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
