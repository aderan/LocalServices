"""Real subprocess and registry coverage for verified registration."""

import plistlib
import socket
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from localservices import launchd, manifest, procs, register_api
from localservices.controller import Controller
from test_registry_v2 import PY, V2Base


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TrialRegistrationTest(V2Base):
    def verified_payload(self, port, listen_port=None):
        body = self.payload(port=port)
        body["verify_start"] = True
        body["service"]["open"] = f"http://127.0.0.1:{port}/"
        body["service"]["components"][0]["cmd"] += ["--bind", "127.0.0.1"]
        if listen_port is not None:
            body["service"]["components"][0]["cmd"][3] = str(listen_port)
        return body

    def test_registration_review_distinguishes_reservation_and_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listened = listener.getsockname()[1]
            reserved = free_port()
            self.assertTrue(self.reg.register_service(self.payload("owner", port=reserved))["ok"])
            body = self.verified_payload(reserved)
            body.update(revision=1, dry_run=True, review_ports=True)
            body["service"]["components"].append({
                "id": "other", "cmd": [PY, "-m", "http.server", str(listened)], "port": listened,
            })
            code, result = register_api.handle_register(body, None)
            self.assertEqual(code, 400, result)
            conflicts = {c["component"]: c for c in result["port_conflicts"]}
            self.assertEqual(conflicts["web"]["reserved_by"], "owner.web")
            self.assertIsNone(conflicts["web"]["listener_pid"])
            self.assertIsNone(conflicts["other"]["reserved_by"])
            self.assertGreater(conflicts["other"]["listener_pid"], 0)
            self.assertEqual(len({reserved, listened, *(c["suggested_port"] for c in conflicts.values())}), 4)
            self.assertEqual(self.reg.project("p1")["services"], ["owner"])

    def test_healthy_candidate_is_registered_and_trial_process_is_stopped(self):
        port = free_port()
        code, result = register_api.handle_register(self.verified_payload(port), None)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["trial"]["state"], "passed")
        self.assertEqual(self.reg.project("p1")["services"], ["demo"])
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)

    def test_registration_rejects_locove_control_port_even_without_a_listener(self):
        port = free_port()
        with patch.dict("os.environ", {"LOCALSERVICES_PORT": str(port)}):
            code, result = register_api.handle_register(self.payload(port=port), None)
        self.assertEqual(code, 400, result)
        self.assertEqual(result["port_conflicts"][0]["reserved_by"], "Locove")
        self.assertNotIn("demo", manifest.load_all())

    def test_registration_port_choice_verifies_and_preserves_candidate(self):
        old, new = free_port(), free_port()
        self.assertTrue(self.reg.register_service(self.payload("owner", port=old))["ok"])
        script = self.src / "app" / "preview.py"
        script.write_text(
            "import argparse, http.server\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--serve', action='store_true', required=True)\n"
            f"p.add_argument('--port', type=int, default={old})\n"
            "http.server.ThreadingHTTPServer(('127.0.0.1', p.parse_args().port), "
            "http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        body = self.verified_payload(old)
        body.update(revision=1, port_changes=[{"component": "web", "port": new}])
        body["service"]["open"] += "?tab=preview#top"
        advanced = {"cwd": "app", "required": False, "start_timeout": 8.0, "hot": True,
                    "auto_update": False, "external_cmd": "preview.py --serve"}
        body["service"]["components"][0].update(
            cmd=[PY, "preview.py", "--serve"], **advanced,
            ready={"http": f"http://127.0.0.1:{old}/?check={old}",
                   "tcp": f"127.0.0.1:{old}", "timeout": 1.5})
        code, result = register_api.handle_register(body, None)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["trial"]["checks"], [{"component": "web", "port": new}])
        _, saved = register_api.handle_service_definition("demo")
        definition = saved["definition"]
        component = definition["components"][0]
        self.assertEqual({key: component[key] for key in advanced}, advanced)
        self.assertEqual(component["cmd"], [PY, "preview.py", "--serve", "--port", str(new)])
        self.assertEqual(component["ready"]["http"], f"http://127.0.0.1:{new}/?check={old}")
        self.assertEqual(component["ready"]["tcp"], f"127.0.0.1:{new}")
        self.assertEqual(definition["open"], f"http://127.0.0.1:{new}/?tab=preview#top")
        self.assertEqual(manifest.load_all()["owner"].components[0].port, old)

    def test_open_tcp_port_does_not_override_a_failing_http_probe(self):
        port = free_port()
        body = self.verified_payload(port)
        body["service"]["components"][0]["ready"] = {
            "tcp": f"127.0.0.1:{port}", "http": f"http://127.0.0.1:{port}/missing",
        }
        with patch("localservices.trial.START_TIMEOUT", 2):
            code, result = register_api.handle_register(body, None)
        self.assertEqual(code, 400, result)
        self.assertIn("HTTP 404", result["trial"]["error"])
        self.assertNotIn("demo", manifest.load_all())

    def test_wrong_listener_port_leaves_candidate_unregistered(self):
        port, wrong_port = free_port(), free_port()
        with patch("localservices.trial.START_TIMEOUT", 2):
            code, result = register_api.handle_register(self.verified_payload(port, wrong_port), None)
        self.assertEqual(code, 400, result)
        self.assertEqual(result["trial"]["state"], "failed")
        self.assertEqual(result["trial"]["port_issue"]["actual_ports"], [wrong_port])
        self.assertNotIn("demo", manifest.load_all())
        self.assertIsNone(self.reg.project("p1"))

    def test_candidate_repair_rejects_a_listening_replacement(self):
        old = free_port()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            occupied = listener.getsockname()[1]
            body = self.verified_payload(old)
            body["port_changes"] = [{"component": "web", "port": occupied}]
            code, result = register_api.handle_register(body, None)
            self.assertEqual(code, 400, result)
            self.assertEqual(result["port_conflicts"][0]["port"], occupied)
            self.assertIsNotNone(result["port_conflicts"][0]["listener_pid"])
            self.assertIsNone(self.reg.project("p1"))

    def test_candidate_repair_with_an_unsupported_command_does_not_save(self):
        old, new = free_port(), free_port()
        body = self.verified_payload(old)
        body["service"]["components"][0]["cmd"] = ["/usr/bin/false"]
        body["port_changes"] = [{"component": "web", "port": new}]
        code, result = register_api.handle_register(body, None)
        self.assertEqual(code, 400, result)
        self.assertEqual(result["trial"]["state"], "failed")
        self.assertIsNone(self.reg.project("p1"))

    def managed_controller(self):
        labels = patch.dict("os.environ", {"LOCALSERVICES_LABEL_PREFIX": f"com.mybrain.{self.root.name}"})
        labels.start()
        self.addCleanup(labels.stop)
        ctl = Controller()
        self.addCleanup(ctl._pool.shutdown)
        self.addCleanup(launchd.bootout, ctl.label(ctl.projects["demo"], ctl.projects["demo"].component("web")))
        return ctl

    def test_one_click_verifies_launchd_and_keeps_existing_listener(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            old = occupied.getsockname()[1]
            body = self.verified_payload(old)
            body["service"]["dependencies"] = [{"name": "existing", "probe": {"tcp": f"127.0.0.1:{old}", "timeout": .3}}]
            body["service"]["evidence"] = {"summary": "fixture registration"}
            self.assertTrue(self.reg.register_service(body)["ok"])
            ctl = self.managed_controller()
            code, result = register_api.handle_service_repair("demo", {"mode": "auto"}, ctl)
            self.assertEqual(code, 200, result)
            service = manifest.load_all()["demo"]
            self.assertEqual(service.dependencies[0].probe.timeout, .3)
            self.assertEqual(service.registered["evidence_summary"], "fixture registration")
            self.assertNotEqual(service.component("web").port, old)
            self.assertEqual(ctl.project_status(ctl.projects["demo"])["state"], "available")
            self.assertTrue(procs.tcp_ok(f"127.0.0.1:{old}"))
            self.assertEqual(self.reg.project("p1")["services"], ["demo"])

    def test_one_click_does_not_commit_when_the_program_ignores_the_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            old = occupied.getsockname()[1]
            body = self.verified_payload(old)
            body["service"]["components"][0]["cmd"] = [
                PY, "-c", f"import http.server; http.server.HTTPServer(('127.0.0.1', {old}), "
                          "http.server.SimpleHTTPRequestHandler).serve_forever()", "--port", str(old),
            ]
            self.assertTrue(self.reg.register_service(body)["ok"])
            ctl = self.managed_controller()
            before = (manifest.load_all()["demo"].file.read_bytes(), self.reg.project("p1")["revision"])
            with patch("localservices.controller.STARTUP_TIMEOUT", 3):
                code, result = register_api.handle_service_repair("demo", {"mode": "auto"}, ctl)
            self.assertEqual(code, 400, result)
            self.assertEqual(result["trial"]["state"], "failed")
            self.assertIn(str(self.src), result["prompt"])
            service = manifest.load_all()["demo"]
            self.assertEqual((service.file.read_bytes(), self.reg.project("p1")["revision"]), before)
            self.assertFalse(launchd.info(ctl.label(service, service.component("web"))).loaded)
            self.assertTrue(procs.tcp_ok(f"127.0.0.1:{old}"))

    def test_replacement_port_taken_during_launch_keeps_original_registration(self):
        with ExitStack() as sockets:
            for _ in range(10):
                current = sockets.enter_context(socket.socket())
                competitor = sockets.enter_context(socket.socket())
                current.bind(("127.0.0.1", 0))
                old = current.getsockname()[1]
                try:
                    competitor.bind(("127.0.0.1", old + 1))
                except (OSError, OverflowError):
                    continue
                break
            else:
                self.fail("Could not reserve adjacent fixture ports")
            current.listen()
            self.assertTrue(self.reg.register_service(self.verified_payload(old))["ok"])
            ctl = self.managed_controller()
            boot = launchd.bootstrap

            def take_candidate(label):
                plist = plistlib.loads(launchd.plist_path(label).read_bytes())
                definition = manifest.load_file(Path(plist["ProgramArguments"][-1]))
                self.assertEqual(definition.component("web").port, competitor.getsockname()[1])
                competitor.listen()
                return boot(label)

            with patch.object(launchd, "bootstrap", side_effect=take_candidate):
                code, result = register_api.handle_service_repair("demo", {"mode": "auto"}, ctl)
            self.assertEqual(code, 400, result)
            self.assertEqual(manifest.load_all()["demo"].component("web").port, old)
            self.assertTrue(procs.tcp_ok(f"127.0.0.1:{competitor.getsockname()[1]}"))

    def test_concurrent_registration_claim_aborts_uncommitted_repair(self):
        with socket.socket() as current:
            current.bind(("127.0.0.1", 0))
            current.listen()
            old = current.getsockname()[1]
            self.assertTrue(self.reg.register_service(self.verified_payload(old))["ok"])
            ctl = self.managed_controller()
            boot = launchd.bootstrap
            candidate = []

            def register_first(label):
                plist = plistlib.loads(launchd.plist_path(label).read_bytes())
                staged = manifest.load_file(Path(plist["ProgramArguments"][-1]))
                candidate.append(staged.component("web").port)
                rival = self.payload("rival", port=candidate[0], project_id="other")
                self.assertTrue(self.reg.register_service(rival)["ok"])
                return boot(label)

            with patch.object(launchd, "bootstrap", side_effect=register_first):
                code, result = register_api.handle_service_repair("demo", {"mode": "auto"}, ctl)
            self.assertEqual(code, 400, result)
            projects = manifest.load_all()
            self.assertEqual(projects["demo"].component("web").port, old)
            self.assertEqual(projects["rival"].component("web").port, candidate[0])
            self.assertFalse(procs.tcp_ok(f"127.0.0.1:{candidate[0]}"))

    def test_declared_port_environment_variable_is_repaired(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            old = occupied.getsockname()[1]
            body = self.verified_payload(old)
            command = [PY, "-c", "import os,http.server; "
                       "http.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), "
                       "http.server.SimpleHTTPRequestHandler).serve_forever()"]
            body["service"]["components"][0].update(cmd=command, env={"PORT": str(old), "OTHER": "value"})
            self.assertTrue(self.reg.register_service(body)["ok"])
            ctl = self.managed_controller()
            code, result = register_api.handle_service_repair("demo", {"mode": "auto"}, ctl)
            self.assertEqual(code, 200, result)
            component = manifest.load_all()["demo"].component("web")
            self.assertEqual(component.cmd, command)
            self.assertEqual(component.env, {"PORT": str(result["port"]), "OTHER": "value"})

    def test_unsupported_startup_returns_a_prompt_without_starting(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            old = occupied.getsockname()[1]
            body = self.verified_payload(old)
            body["service"]["components"][0]["cmd"] = ["/bin/sh", "-c", "exit 1"]
            self.assertTrue(self.reg.register_service(body)["ok"])
            ctl = self.managed_controller()
            code, result = register_api.handle_service_repair("demo", {"mode": "auto"}, ctl)
            self.assertEqual(code, 409, result)
            self.assertIn(str(self.src), result["prompt"])
            self.assertIn(f"127.0.0.1:{old}", result["prompt"])
            self.assertIn("建议改用端口", result["prompt"])
            code, response = register_api.handle_service_repair("demo", {"mode": "prompt"}, ctl)
            self.assertEqual((code, response["prompt"]), (200, result["prompt"]))

    def test_repair_switches_port_and_keeps_service_identity(self):
        old, new = free_port(), free_port()
        self.assertTrue(self.reg.register_service(self.verified_payload(old))["ok"])
        ctl = type("ControllerFixture", (), {
            "projects": manifest.load_all(),
            "project_status": lambda self, project: {"state": "failed", "intent": "stopped"},
        })()
        command = [PY, "-m", "http.server", str(new), "--bind", "127.0.0.1"]
        code, result = register_api.handle_service_repair(
            "demo", {"revision": 1, "component": {"id": "web", "port": new, "cmd": command}}, ctl)
        self.assertEqual(code, 200, result)
        service = manifest.load_all()["demo"]
        self.assertEqual((service.components[0].port, service.open), (new, f"http://127.0.0.1:{new}/"))
        self.assertEqual(self.reg.project("p1")["services"], ["demo"])
