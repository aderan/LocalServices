"""Startup contracts through manifest loading and the Controller's public operations.

OS process inspection/launchd are replaced at their boundary; all state is temporary.
"""

import os
import socket
from pathlib import Path
import tempfile
import threading
import time
import sys
import unittest
from unittest.mock import patch

from localservices import launchd, manifest, procs
from localservices.controller import Controller, log_path


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-startup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.defs = self.root / "services"
        self.defs.mkdir()
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "runtime"),
            "LOCALSERVICES_SERVICES": str(self.defs),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-startup-test",
        })
        env.start()
        self.addCleanup(env.stop)

    def project(self, name, port):
        source = self.root / name
        source.mkdir(exist_ok=True)
        (self.defs / f"{name}.toml").write_text(
            f'id="{name}"\nname="{name}"\nsource="{source}"\n'
            '[[components]]\nid="web"\ncmd=["true"]\n'
            f'port={port}\nready={{http="http://127.0.0.1:{port}/"}}\n'
        )

    def controller(self):
        ctl = Controller()
        self.addCleanup(ctl._pool.shutdown, wait=True)
        return ctl

    def simulated_service(self, probe):
        self.project("fixture", 49001)
        ctl = self.controller()
        state = {"loaded": False, "running": True, "bootouts": 0}

        def job_info(label):
            return launchd.JobInfo(loaded=state["loaded"], running=state["loaded"] and state["running"],
                                   pid=42 if state["loaded"] and state["running"] else None,
                                   last_exit=None if state["running"] else "0")

        def bootstrap(label):
            state["loaded"] = True
            return True, ""

        def bootout(label):
            state["loaded"] = False
            state["bootouts"] += 1
            return True, ""

        boundaries = [
            patch.object(launchd, "info", side_effect=job_info),
            patch.object(launchd, "bootstrap", side_effect=bootstrap),
            patch.object(launchd, "bootout", side_effect=bootout),
            patch.object(procs, "snapshot", side_effect=lambda: {
                42: procs.Proc(42, 1, 0, "fixture")
            } if state["loaded"] and state["running"] else {}),
            patch.object(procs, "tcp_listeners", side_effect=lambda: {49001: [procs.TCPListener(42, "127.0.0.1")]} if state["loaded"] else {}),
            patch.object(procs, "all_cwds", return_value={42: str(self.root / "fixture")}),
            patch.object(procs, "probe", side_effect=probe),
        ]
        for boundary in boundaries:
            boundary.start()
            self.addCleanup(boundary.stop)
        return ctl, state

    def wait_finished(self, ctl, timeout=2):
        end = time.monotonic() + timeout
        while ctl.ops and time.monotonic() < end:
            time.sleep(0.01)
        return not ctl.ops

    def monorepo_instance(self, *, nested, port):
        self.project("atlas", 49001)
        ctl = self.controller()
        project = ctl.projects["atlas"]
        component = project.component("web")
        component.external_cmd = "vite"
        component.external_cwd_exact = True
        cwd = project.source / "apps/explore" if nested else project.source
        process = {42: procs.Proc(42, 1, 0, f"node vite --port {port}")}
        with patch.object(launchd, "info", return_value=launchd.JobInfo()), \
                patch.object(procs, "tcp_listeners", return_value={port: [procs.TCPListener(42, "127.0.0.1")]}), \
                patch.object(procs, "all_cwds", return_value={42: str(cwd.resolve())}):
            return ctl, project, ctl.external_instances(project, process)

    def test_exact_cwd_does_not_adopt_a_monorepo_subapp(self):
        _, _, instances = self.monorepo_instance(nested=True, port=49002)
        self.assertEqual(instances, [])

    def test_exact_cwd_recognizes_the_root_app_on_another_port(self):
        _, _, instances = self.monorepo_instance(nested=False, port=49002)
        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0]["same_project"])
        self.assertEqual(instances[0]["ports"], [49002])

    def test_exact_cwd_rejects_takeover_of_a_subapp_holding_the_root_port(self):
        ctl, project, instances = self.monorepo_instance(nested=True, port=49001)
        self.assertEqual(len(instances), 1)
        self.assertFalse(instances[0]["same_project"])
        self.assertFalse(ctl._adoptable(project, instances)[0])

    def test_duplicate_ports_rejected_before_start(self):
        self.project("buildbase", 3000)
        self.project("climbo", 3000)
        with self.assertRaisesRegex(ValueError, r"3000.*buildbase.*climbo"):
            manifest.load_all(self.defs)

    def test_another_managed_project_is_a_port_conflict(self):
        self.project("buildbase", 3000)
        self.project("climbo", 3001)
        ctl = self.controller()
        process = {42: procs.Proc(42, 1, 0, "node vite --port 3000")}

        def job_info(label):
            return launchd.JobInfo(loaded=True, running=True, pid=42) if "climbo" in label else launchd.JobInfo()

        with patch.object(launchd, "info", side_effect=job_info), \
                patch.object(procs, "snapshot", return_value=process), \
                patch.object(procs, "tcp_listeners", return_value={3000: [procs.TCPListener(42, "127.0.0.1")]}), \
                patch.object(procs, "all_cwds", return_value={42: str(self.root / "climbo")}):
            response = ctl.start("buildbase")
            self.assertFalse(response["accepted"])
            status = ctl.project_status(ctl.projects["buildbase"])
            self.assertEqual(status["state"], "conflict")
            self.assertIn("climbo", status["reason"])

    def test_startup_budget_bounds_slow_probe_cleans_up_and_allows_retry(self):
        release = threading.Event()
        timeouts = []

        def slow_probe(probe, timeout=None):
            if release.is_set():
                return True, ""
            timeouts.append(timeout)
            time.sleep(min(timeout or probe.timeout, 0.4))
            return False, "not ready"

        ctl, state = self.simulated_service(slow_probe)
        with patch("localservices.controller.STARTUP_TIMEOUT", 0.2, create=True):
            try:
                self.assertTrue(ctl.start("fixture")["accepted"])
                self.assertTrue(self.wait_finished(ctl), "startup did not finish within its budget")
                self.assertFalse(state["loaded"], "timed-out process must be unloaded")
                status = ctl.project_status(ctl.projects["fixture"])
                self.assertEqual(status["state"], "failed")
                self.assertEqual(status["intent"], "stopped")
                self.assertIn("超时", status["reason"])
                self.assertLessEqual(max(timeouts), 0.2)
                self.assertTrue(ctl.stop("fixture")["ok"])
                self.assertTrue(self.wait_finished(ctl))
                release.set()
                self.assertTrue(ctl.start("fixture")["accepted"])
                self.assertTrue(self.wait_finished(ctl))
                self.assertEqual(ctl.project_status(ctl.projects["fixture"])["state"], "available")
            finally:
                release.set()
                self.wait_finished(ctl, 2)

    def test_process_exiting_before_ready_fails_without_waiting_for_timeout(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (False, "not ready"))
        state["running"] = False
        started = time.monotonic()
        ctl.start("fixture")
        self.assertTrue(self.wait_finished(ctl))
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(state["loaded"])
        self.assertIn("就绪前退出", ctl.project_status(ctl.projects["fixture"])["reason"])

    def test_launchd_never_exited_is_not_an_early_exit(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (True, ""))
        state["running"] = False
        original = launchd.info

        def launching(label):
            result = original(label)
            if result.loaded and not result.running:
                result.last_exit = "(never exited)"
                state["running"] = True
            return result

        with patch.object(launchd, "info", side_effect=launching):
            ctl.start("fixture")
            self.assertTrue(self.wait_finished(ctl))
        self.assertEqual(ctl.project_status(ctl.projects["fixture"])["state"], "available")

    def test_preparation_uses_the_same_startup_budget(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (True, ""))
        ctl.projects["fixture"].component("web").build = [
            sys.executable, "-c", "import time; time.sleep(10)",
        ]
        with patch("localservices.controller.STARTUP_TIMEOUT", 0.2):
            ctl.start("fixture")
            self.assertTrue(self.wait_finished(ctl))
        self.assertFalse(state["loaded"])
        self.assertEqual(ctl.project_status(ctl.projects["fixture"])["state"], "failed")
        self.assertIn("编译", ctl.project_status(ctl.projects["fixture"])["reason"])

    def test_listener_owned_by_someone_else_cannot_satisfy_readiness(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (True, ""))
        # A process acquires the port after the initial preflight but before our probe.
        with patch.object(procs, "tcp_listeners", side_effect=lambda: {49001: [procs.TCPListener(99, "127.0.0.1")]} if state["loaded"] else {}):
            ctl.start("fixture")
            self.assertTrue(self.wait_finished(ctl))
        status = ctl.project_status(ctl.projects["fixture"])
        self.assertEqual(status["state"], "failed")
        self.assertIn("pid 99", status["error"]["message"])
        self.assertIn("可以重试", status["reason"])
        self.assertIsNone(status["port_repair"])

    def test_failed_bind_is_recorded_from_this_start_attempt(self):
        ctl, state = self.simulated_service(lambda *args: (True, ""))
        state["running"] = False
        original = launchd.bootstrap

        def bootstrap(label):
            with log_path("fixture", "web").open("a") as log:
                log.write("listen EADDRINUSE: address already in use 127.0.0.1:49001\n")
            return original(label)

        with patch.object(launchd, "bootstrap", side_effect=bootstrap):
            self.assertTrue(ctl.start("fixture")["accepted"])
            self.assertTrue(self.wait_finished(ctl))
        status = ctl.project_status(ctl.projects["fixture"])
        self.assertEqual([issue["kind"] for issue in status["port_issues"]], ["bind_failed"])
        self.assertTrue(ctl.start("fixture")["accepted"])
        self.assertTrue(self.wait_finished(ctl))
        self.assertEqual(ctl.project_status(ctl.projects["fixture"])["port_issues"], [])

    def test_ipv6_only_listener_does_not_block_a_distinct_ipv4_endpoint(self):
        with socket.socket(socket.AF_INET6) as listener:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(("::1", 0))
            listener.listen()
            self.project("fixture", listener.getsockname()[1])
            ctl = self.controller()
            with patch.object(launchd, "info", return_value=launchd.JobInfo()):
                status = ctl.project_status(ctl.projects["fixture"])
            self.assertEqual(status["state"], "stopped", status["reason"])

    def test_running_process_without_probe_must_listen_on_its_declared_port(self):
        ctl, state = self.simulated_service(lambda *args: (True, ""))
        project = ctl.projects["fixture"]
        project.component("web").ready = None
        with patch.object(procs, "tcp_listeners", side_effect=lambda: {49002: [procs.TCPListener(42, "127.0.0.1")]} if state["loaded"] else {}):
            with patch("localservices.controller.STARTUP_TIMEOUT", 0.2):
                self.assertTrue(ctl.start("fixture")["accepted"])
                self.assertTrue(self.wait_finished(ctl))
                status = ctl.project_status(project)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"]["port_issue"]["kind"], "wrong_port")
        self.assertEqual(status["error"]["port_issue"]["actual_ports"], [49002])

    def test_released_port_clears_conflict_without_another_start(self):
        ctl, _ = self.simulated_service(lambda *args: (True, ""))
        with patch.object(procs, "tcp_listeners", return_value={49001: [procs.TCPListener(99, "127.0.0.1")]}):
            self.assertFalse(ctl.start("fixture")["accepted"])
        self.assertEqual(ctl.project_status(ctl.projects["fixture"])["state"], "stopped")

    def test_start_can_retry_after_a_port_conflict_is_removed(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (True, ""))
        with patch.object(procs, "tcp_listeners", return_value={49001: [procs.TCPListener(99, "127.0.0.1")]}):
            self.assertFalse(ctl.start("fixture")["accepted"])
        self.assertTrue(ctl.start("fixture")["accepted"])
        self.assertTrue(self.wait_finished(ctl))
        self.assertEqual(ctl.project_status(ctl.projects["fixture"])["state"], "available")

    def test_restart_timeout_preserves_the_previous_running_instance(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (True, ""))
        ctl.start("fixture")
        self.assertTrue(self.wait_finished(ctl))
        ctl.projects["fixture"].component("web").build = [
            sys.executable, "-c", "import time; time.sleep(10)",
        ]
        with patch("localservices.controller.STARTUP_TIMEOUT", 0.2):
            self.assertTrue(ctl.restart("fixture")["accepted"])
            self.assertTrue(self.wait_finished(ctl))
        self.assertTrue(state["loaded"])
        self.assertEqual(state["bootouts"], 0)

    def test_restart_with_exhausted_budget_does_not_stop_old_instance(self):
        ctl, state = self.simulated_service(lambda *args, **kwargs: (True, ""))
        ctl.start("fixture")
        self.assertTrue(self.wait_finished(ctl))
        with patch("localservices.controller.STARTUP_TIMEOUT", 0):
            self.assertTrue(ctl.restart("fixture")["accepted"])
            self.assertTrue(self.wait_finished(ctl))
        self.assertTrue(state["loaded"])
        self.assertEqual(state["bootouts"], 0)

    def test_missing_required_dependency_blocks_api_before_bootstrap(self):
        def probe(target, timeout=None):
            return (False, "connection refused") if target.tcp else (True, "")

        ctl, state = self.simulated_service(probe)
        dependency = manifest.Dependency("Postgres", manifest.Probe(tcp="127.0.0.1:49002"))
        dependency.required = True
        ctl.projects["fixture"].dependencies.append(dependency)
        self.assertTrue(ctl.start("fixture")["accepted"])
        self.assertTrue(self.wait_finished(ctl))
        status = ctl.project_status(ctl.projects["fixture"])
        self.assertEqual(status["state"], "failed")
        self.assertIn("Postgres", status["reason"])
        self.assertFalse(state["loaded"])
        self.assertFalse((self.root / "runtime/logs/fixture.web.log").exists(),
                         "no compilation or launch may happen before required dependencies are ready")

    def test_docker_recovery_does_not_resume_a_service_stopped_by_user(self):
        ctl, _ = self.simulated_service(lambda *args: (True, ""))
        service = ctl.projects["fixture"]
        target = {"context": "fixture-docker", "endpoint": "unix:///tmp/fixture-docker.sock"}
        service.docker = dict(target)
        ctl.errors[service.id] = {"docker_target": target}
        ctl.store.set_intent(service.id, "stopped")
        self.assertEqual(ctl.resume_docker_services(target), [])

    def test_docker_failure_preserves_intent_and_recovery_resumes_service(self):
        from localservices import dockerctx
        from localservices.infrastructure import RuntimeUnavailable

        ctl, _ = self.simulated_service(lambda *args: (True, ""))
        service = ctl.projects["fixture"]
        target = {"context": "colima-fixture", "endpoint": dockerctx.colima_endpoint("fixture")}
        service.docker = dict(target)
        service.dependencies.append(manifest.Dependency("DB", manifest.Probe(tcp="127.0.0.1:49002"),
                                                        required=True, container="fixture-db", volume="data"))
        profiles = [{"name": "fixture", "runtime": "docker"}]
        with patch.object(ctl.infrastructure, "ensure", side_effect=RuntimeUnavailable("Docker stopped")), \
                patch.object(dockerctx, "inspect_context", return_value=target["endpoint"]), \
                patch.object(dockerctx, "list_colima_profiles", return_value=profiles):
            self.assertTrue(ctl.start(service.id)["accepted"])
            self.assertTrue(self.wait_finished(ctl))
        self.assertEqual(ctl.store.intent_read(service), "running")
        self.assertEqual(ctl.errors[service.id]["docker_profile"], "fixture")
        unrelated = {**target, "endpoint": "unix:///tmp/another.sock"}
        self.assertEqual(ctl.resume_docker_services(unrelated), [])
        with patch.object(ctl.infrastructure, "ensure"):
            self.assertEqual(ctl.resume_docker_services(target), [service.id])
            self.assertTrue(self.wait_finished(ctl))
        self.assertEqual(ctl.project_status(service)["state"], "available")

    def test_environment_preparation_is_visible_and_has_a_separate_budget(self):
        entered, release = threading.Event(), threading.Event()
        caller = threading.current_thread()

        def probe(target, timeout=None):
            if target.tcp and threading.current_thread() is not caller and not entered.is_set():
                entered.set()
                release.wait(2)
            return True, ""

        ctl, state = self.simulated_service(probe)
        ctl.projects["fixture"].dependencies.append(manifest.Dependency(
            "Postgres", manifest.Probe(tcp="127.0.0.1:49002"), required=True))
        with patch("localservices.controller.STARTUP_TIMEOUT", 0.1):
            try:
                self.assertTrue(ctl.start("fixture")["accepted"])
                self.assertTrue(entered.wait(1))
                time.sleep(0.15)  # environment preparation does not consume the application's budget
                status = ctl.project_status(ctl.projects["fixture"])
                self.assertEqual(status["state"], "preparing")
                self.assertIn("Postgres", status["reason"])
                self.assertFalse(state["loaded"])
                release.set()
                self.assertTrue(self.wait_finished(ctl))
                self.assertEqual(ctl.project_status(ctl.projects["fixture"])["state"], "available")
            finally:
                release.set()
                self.wait_finished(ctl)


if __name__ == "__main__":
    unittest.main()
