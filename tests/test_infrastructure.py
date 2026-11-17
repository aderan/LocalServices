"""Container recovery uses the existing local runtime/container/volume only."""

import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

from localservices.infrastructure import DependencyError, Infrastructure
from localservices.manifest import Dependency, Probe


class InfrastructureTest(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.runtime = False
        self.running = False
        self.exists = True
        self.volume = "fixture_data"
        self.health = "healthy"
        self.endpoint = f"unix://{Path.home()}/.colima/default/docker.sock"
        self.profile = "default"
        self.reachable = True
        self.dependency = Dependency("Postgres", Probe(tcp="127.0.0.1:5434"),
                                     required=True, container="fixture-postgres", volume=self.volume)

    def command(self, command, **kwargs):
        self.commands.append(command)
        args = command[1:]
        executable = Path(command[0]).name
        code, output = 0, ""
        if executable == "colima":
            if args[0] == "list":
                output = json.dumps({"name": self.profile, "status": "Stopped", "runtime": "docker"})
            elif args[0] == "start":
                self.runtime = True
            else:
                self.fail(f"unexpected Colima operation: {args}")
        elif "context" in args:
            output = self.endpoint
        elif "info" in args:
            code = 0 if self.runtime else 1
        elif "inspect" in args:
            code = 0 if self.exists else 1
            output = json.dumps({
                "state": {"Running": self.running, "Health": {"Status": self.health}},
                "mounts": [{"Type": "volume", "Name": self.volume}],
                "ports": {"5432/tcp": [{"HostPort": "5434"}]},
            })
        elif "start" in args:
            self.running = True
        else:
            self.fail(f"unexpected Docker operation: {args}")
        return subprocess.CompletedProcess(command, code, stdout=output, stderr="fixture error" if code else "")

    def recover(self):
        self.progress = []
        with patch("localservices.infrastructure.subprocess.run", side_effect=self.command), \
                patch("localservices.infrastructure.shutil.which", side_effect=lambda name, **kwargs: f"/usr/local/bin/{name}"), \
                patch("localservices.infrastructure.procs.probe", side_effect=lambda *args, **kwargs: (self.running and self.reachable, "unavailable")):
            Infrastructure().ensure([self.dependency], self.progress.append)

    def test_recovers_runtime_then_existing_container_without_creating_resources(self):
        self.recover()
        self.assertTrue(self.running)
        self.assertTrue(any(Path(c[0]).name == "colima" and "start" in c for c in self.commands))
        self.assertTrue(any(Path(c[0]).name == "docker" and "start" in c for c in self.commands))
        self.assertFalse(any(word in c for c in self.commands for word in ("create", "rm", "pull", "stop")))

    def test_restores_the_profile_that_owns_the_bound_socket(self):
        self.profile = "locove-test"
        self.endpoint = f"unix://{Path.home()}/.colima/{self.profile}/docker.sock"
        self.recover()
        starts = [c[1:] for c in self.commands if Path(c[0]).name == "colima" and c[1] == "start"]
        self.assertEqual(starts, [["start", self.profile, "--activate=false"]])

    def test_bound_compose_does_not_start_colima_without_permission(self):
        binding = {"context": "colima", "endpoint": self.endpoint, "autostart": False}
        with patch("localservices.infrastructure.subprocess.run", side_effect=self.command), \
                patch("localservices.infrastructure.shutil.which", side_effect=lambda name, **kwargs: name):
            with self.assertRaisesRegex(DependencyError, "请先启动运行环境"):
                Infrastructure().ensure([], lambda _: None, docker_required=True, binding=binding)
        self.assertFalse(any(c[0] == "colima" and c[1] == "start" for c in self.commands))

    def test_healthy_existing_services_are_not_restarted(self):
        self.runtime = self.running = True
        self.recover()
        self.assertFalse(any("start" in c for c in self.commands))

    def test_missing_container_is_not_recreated(self):
        self.runtime = True
        self.exists = False
        with self.assertRaisesRegex(DependencyError, "不存在"):
            self.recover()
        self.assertFalse(any("start" in c for c in self.commands))

    def test_wrong_volume_is_never_started(self):
        self.runtime = True
        self.volume = "different_data"
        with self.assertRaisesRegex(DependencyError, "原数据卷"):
            self.recover()
        self.assertFalse(any("start" in c for c in self.commands))

    def test_wrong_port_mapping_is_rejected(self):
        self.runtime = True
        self.dependency.probe.tcp = "127.0.0.1:9999"
        with self.assertRaisesRegex(DependencyError, "预期端口 9999"):
            self.recover()

    def test_remote_docker_context_is_never_used(self):
        self.endpoint = "tcp://other-host:2375"
        with self.assertRaisesRegex(DependencyError, "本机默认环境"):
            self.recover()
        self.assertFalse(any("start" in c for c in self.commands))

    def test_missing_colima_profile_is_not_created(self):
        self.profile = "other-profile"
        with self.assertRaisesRegex(DependencyError, "未自动创建"):
            self.recover()
        self.assertFalse(any("start" in c for c in self.commands))

    def test_unhealthy_database_is_not_treated_as_ready(self):
        self.runtime = self.running = True
        self.health = "unhealthy"
        with self.assertRaisesRegex(DependencyError, "健康检查失败"):
            self.recover()

    def test_unreachable_forwarded_port_has_a_bounded_wait(self):
        self.runtime = self.running = True
        self.reachable = False
        with patch("localservices.infrastructure.ENVIRONMENT_TIMEOUT", 0.05):
            with self.assertRaisesRegex(DependencyError, "环境准备超时"):
                self.recover()

    def test_concurrent_projects_share_a_single_runtime_start(self):
        infrastructure = Infrastructure()
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_command = self.command

        def command(args, **kwargs):
            if Path(args[0]).name == "colima" and "start" in args:
                entered.set()
                release.wait(2)
            return original_command(args, **kwargs)

        def recover():
            try:
                infrastructure.ensure([self.dependency], lambda detail: waiting.set() if "等待共享" in detail else None)
            except Exception as exc:
                errors.append(exc)

        with patch("localservices.infrastructure.subprocess.run", side_effect=command), \
                patch("localservices.infrastructure.shutil.which", side_effect=lambda name, **kwargs: f"/usr/local/bin/{name}"), \
                patch("localservices.infrastructure.procs.probe", return_value=(True, "")):
            first = threading.Thread(target=recover)
            second = threading.Thread(target=recover)
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                waiting.clear()
                second.start()
                self.assertTrue(waiting.wait(1))
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(sum(Path(c[0]).name == "colima" and "start" in c for c in self.commands), 1)


if __name__ == "__main__":
    unittest.main()
