import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from localservices import agent_api, resources
from localservices.agents import detect


class AgentDiagnosisTests(unittest.TestCase):
    def test_service_failure_is_redacted_and_queued(self):
        ctl = Mock()
        ctl.snapshot.return_value = {"projects": [
            {"id": "shicha", "name": "ShiCha", "state": "conflict",
             "reason": "port 5176 in use", "error": {"component": "admin"}},
        ]}
        ctl.logs.return_value = {"logs": {"admin": "password=secret-value"}}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"LOCALSERVICES_HOME": tmp}), \
                patch.object(agent_api.diagnose, "run", return_value={"summary": "Port busy", "steps": []}) as run:
            code, body = agent_api.handle_diagnose({"service": "shicha"}, ctl)
            self.assertEqual(code, 202, body)
            for _ in range(100):
                _, job = agent_api.handle_job_get(body["job_id"])
                if job.get("state") == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(job["state"], "done", job)
            self.assertIn("port 5176", str(run.call_args))
            self.assertNotIn("secret-value", str(run.call_args))
            self.assertEqual(agent_api.handle_diagnoses_get()[1]["jobs"][0]["id"], job["id"])

    def test_rejects_healthy_service(self):
        ctl = Mock()
        ctl.snapshot.return_value = {"projects": [{"id": "shicha", "state": "available"}]}
        self.assertEqual(agent_api.handle_diagnose({"service": "shicha"}, ctl)[0], 409)

    def test_service_and_environment_share_a_diagnosis_job(self):
        endpoint = "unix:///private/tmp/shicha-docker.sock"
        ctl = Mock(projects={})
        ctl.snapshot.return_value = {"projects": [
            {"id": "shicha", "state": "failed", "reason": "Docker not ready",
             "error": {"docker_target": {"endpoint": endpoint}}},
        ]}
        env = {"endpoint": endpoint, "state": "stopped"}
        release = threading.Event()

        def diagnose(*_):
            release.wait(3)
            return {"summary": "Docker is stopped", "steps": []}

        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"LOCALSERVICES_HOME": tmp}), \
                patch.object(resources, "environments", return_value={"environments": [env]}), \
                patch.object(agent_api.diagnose, "run", side_effect=diagnose):
            try:
                code, service_job = agent_api.handle_diagnose({"service": "shicha"}, ctl)
                self.assertEqual(code, 202, service_job)
                code, env_job = agent_api.handle_diagnose({"endpoint": endpoint}, ctl)
                self.assertEqual(code, 202, env_job)
                self.assertEqual(service_job["job_id"], env_job["job_id"])
                ctl.logs.assert_not_called()
            finally:
                release.set()
            for _ in range(100):
                _, job = agent_api.handle_job_get(service_job["job_id"])
                if job.get("state") == "done":
                    break
                time.sleep(0.02)
            self.assertEqual(job["state"], "done", job)

    def test_macos_proxy_is_only_added_to_child_environment(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://explicit:9999"}, clear=True), \
                patch.object(detect, "_system_proxies", return_value={
                    "http": "http://localhost:7897", "https": "http://localhost:7897",
                }):
            env = detect.agent_environment()
            self.assertEqual(env["http_proxy"], "http://localhost:7897")
            self.assertEqual(env["HTTPS_PROXY"], "http://explicit:9999")
            self.assertNotIn("https_proxy", env)

    def test_explicit_agent_proxy_works_without_platform_proxy(self):
        with patch.dict("os.environ", {"LOCALSERVICES_AGENT_PROXY": "http://127.0.0.1:7897"}, clear=True), \
                patch.object(detect, "_system_proxies", return_value={}):
            env = detect.agent_environment()
            self.assertEqual(env["https_proxy"], "http://127.0.0.1:7897")
            self.assertNotIn("LOCALSERVICES_AGENT_PROXY", env)
