"""Real subprocess and registry coverage for verified registration."""

import socket
from unittest.mock import patch

from localservices import manifest, register_api
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

    def test_healthy_candidate_is_registered_and_trial_process_is_stopped(self):
        port = free_port()
        code, result = register_api.handle_register(self.verified_payload(port), None)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["trial"]["state"], "passed")
        self.assertEqual(self.reg.project("p1")["services"], ["demo"])
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)

    def test_wrong_listener_port_leaves_candidate_unregistered(self):
        port, wrong_port = free_port(), free_port()
        with patch("localservices.trial.START_TIMEOUT", 2):
            code, result = register_api.handle_register(self.verified_payload(port, wrong_port), None)
        self.assertEqual(code, 400, result)
        self.assertEqual(result["trial"]["state"], "failed")
        self.assertNotIn("demo", manifest.load_all())
        self.assertIsNone(self.reg.project("p1"))

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
