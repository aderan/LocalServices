"""resources v2（V2.6）：ps -a（含已停止容器）、新字段、consumers、container_state。

与 legacy tests/test_resources.py 互补：那里覆盖运行中容器的解析与 docker 不可达，
这里只测 v2 新行为，全部 mock resources._run / dockerctx，不跑真实 docker。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import dockerctx, manifest, resources  # noqa: E402


def ns(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


PS_RUNNING = {"Names": "app-pg", "Image": "postgres:16", "State": "running"}
PS_EXITED = {"Names": "demo-web-1", "Image": "alpine:3.20", "State": "exited"}

INSPECT_PG = {
    "Id": "abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
    "Name": "/app-pg",
    "Config": {"Image": "postgres:16", "Labels": {
        "com.docker.compose.project": "app", "com.docker.compose.service": "pg"}},
    "State": {"Status": "running", "Health": {"Status": "healthy"}},
    "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5434"}]}},
    "Mounts": [{"Type": "volume", "Name": "app_pg_data"},
               {"Type": "bind", "Source": "/tmp/data", "Destination": "/var/lib/postgresql/data"}],
}
INSPECT_WEB = {
    "Id": "fff000fff000fff000fff000fff000fff000fff000fff000fff000fff000ffff",
    "Name": "/demo-web-1",
    "Config": {"Image": "alpine:3.20", "Labels": {
        "com.docker.compose.project": "demo", "com.docker.compose.service": "web"}},
    "State": {"Status": "exited", "ExitCode": 0},
    "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18214"}]}},
    "Mounts": [],
}


class ResourcesV2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-res-v2-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "home"),
            "LOCALSERVICES_SERVICES": str(self.root / "services"),
            "LOCALSERVICES_USER_SERVICES": str(self.root / "user-services"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.calls: list[list[str]] = []
        self.ps_rows = [PS_RUNNING, PS_EXITED]
        self.inspect_rows = [INSPECT_PG, INSPECT_WEB]
        self.run_patcher = patch.object(resources, "_run", side_effect=self._fake_run)
        self.run_patcher.start()
        self.addCleanup(self.run_patcher.stop)

    def _fake_run(self, cmd, timeout=resources.TIMEOUT):
        self.calls.append(list(cmd))
        if cmd[1] == "ps":
            return ns(stdout="".join(json.dumps(r) + "\n" for r in self.ps_rows))
        if cmd[1] == "inspect":
            return ns(stdout="".join(json.dumps(r) + "\n" for r in self.inspect_rows))
        if cmd[1] == "container":
            return ns(stdout="exited\n")
        return ns(rc=1, stderr="unexpected")


class InventoryV2Test(ResourcesV2Base):
    def test_stopped_colima_replaces_placeholder_for_missing_context(self):
        dep = manifest.Dependency("Postgres", manifest.Probe(tcp="127.0.0.1:5432"), container="app-pg")
        project = manifest.Project("api", "API", Path("."), dependencies=[dep])
        placeholder = {"Name": "colima", "DockerEndpoint": "", "Error": "context not found"}
        profile = {"name": "default", "runtime": "docker", "status": "Stopped"}
        with patch.object(dockerctx, "name", return_value="colima"), \
                patch.object(dockerctx, "run", return_value=ns(stdout=json.dumps(placeholder))), \
                patch.object(dockerctx, "list_colima_profiles", return_value=[profile]):
            result = resources.environments([project], [])
        self.assertEqual(len(result["environments"]), 1)
        environment = result["environments"][0]
        self.assertEqual(environment["profile"], "default")
        self.assertEqual(environment["state"], "stopped")
        self.assertEqual(environment["contexts"], ["colima"])
        self.assertEqual([s["id"] for s in environment["services"]], ["api"])

    def test_environment_inventory_keeps_offline_bindings_and_groups_aliases(self):
        endpoint = dockerctx.colima_endpoint("sandbox")
        online = dockerctx.colima_endpoint("default")
        dep = manifest.Dependency("Postgres", manifest.Probe(tcp="127.0.0.1:5432"), container="app-pg")
        project = manifest.Project("api", "API", Path("."), dependencies=[dep],
                                   docker={"context": "colima-sandbox", "endpoint": endpoint})
        contexts = [{"Name": name, "DockerEndpoint": online} for name in ("colima", "alias")]

        def docker(args, **kwargs):
            if args[0] == "context":
                return ns(stdout="\n".join(map(json.dumps, contexts)))
            return ns(stdout="28.4.0")

        profiles = [{"name": "sandbox", "runtime": "docker", "status": "Stopped"},
                    {"name": "default", "runtime": "docker", "status": "Running"}]
        with patch.object(dockerctx, "run", side_effect=docker), \
                patch.object(dockerctx, "list_colima_profiles", return_value=profiles), \
                patch.object(dockerctx, "inspect_context", return_value=online):
            result = resources.environments([project], [{"id": "api", "state": "stopped", "label": "已停止"}])
        by_profile = {row["profile"]: row for row in result["environments"]}
        self.assertEqual(by_profile["default"]["contexts"], ["colima", "alias"])
        self.assertEqual(by_profile["default"]["state"], "ready")
        self.assertEqual(by_profile["sandbox"]["state"], "stopped")
        self.assertEqual(by_profile["sandbox"]["services"], [{
            "id": "api", "name": "API", "context": "colima-sandbox", "state": "stopped", "label": "已停止",
            "binding_changed": False,
        }])

    def test_uses_ps_all_and_reports_pinned_context(self):
        inv = resources.inventory()
        self.assertTrue(inv["ok"], inv)
        ps_cmds = [c for c in self.calls if c[:2] == ["docker", "ps"]]
        self.assertEqual(len(ps_cmds), 1)
        self.assertIn("-a", ps_cmds[0])
        self.assertEqual(inv["docker_context"], dockerctx.name())
        self.assertEqual({r["context"] for r in inv["resources"]}, {dockerctx.name()})

    def test_stopped_container_visible_with_new_fields(self):
        inv = resources.inventory()
        self.assertTrue(inv["ok"])
        by_name = {r["name"]: r for r in inv["resources"]}
        self.assertIn("demo-web-1", by_name, "已停止容器必须出现在盘点里")
        web = by_name["demo-web-1"]
        self.assertEqual(web["state"], "exited")
        self.assertEqual(web["container_id"], INSPECT_WEB["Id"])
        self.assertEqual(web["bind_mounts"], [])
        self.assertEqual(web["ports"], {"80/tcp": [{"host_ip": "0.0.0.0", "host_port": 18214}]})
        self.assertEqual(web["compose_project"], "demo")
        self.assertEqual(web["compose_service"], "web")
        pg = by_name["app-pg"]
        self.assertEqual(pg["state"], "running")
        self.assertEqual(pg["health"], "healthy")
        self.assertEqual(pg["container_id"], INSPECT_PG["Id"])
        self.assertEqual(pg["bind_mounts"], [{"source": "/tmp/data", "target": "/var/lib/postgresql/data"}])
        self.assertEqual(pg["volumes"], ["app_pg_data"])  # 旧字段保留

    def test_consumers_from_dependency_and_compose_refs(self):
        (Path(self.root / "services")).mkdir(parents=True, exist_ok=True)
        (self.root / "services" / "app.toml").write_text(
            f'id="app"\nname="App"\nsource="{self.root}"\n'
            '[[dependencies]]\nname="Postgres"\nprobe={tcp="127.0.0.1:5434"}\n'
            'container="app-pg"\nvolume="app_pg_data"\n'
            '[[components]]\nid="web"\nrun="compose"\ncompose_service="web"\ncompose_project="demo"\n'
        )
        inv = resources.inventory()
        self.assertTrue(inv["ok"], inv)
        self.assertEqual(inv["consumers"].get("container:app-pg"),
                         [{"service": "app", "via": "dependency", "running": True}])
        self.assertEqual(inv["consumers"].get("container:demo-web-1"),
                         [{"service": "app", "via": "compose", "running": False}])

    def test_consumers_survive_broken_manifests(self):
        (Path(self.root / "services")).mkdir(parents=True, exist_ok=True)
        (self.root / "services" / "broken.toml").write_text("not [ valid toml")
        inv = resources.inventory()
        self.assertTrue(inv["ok"], inv)
        self.assertEqual(inv["consumers"], {})

    def test_container_state_helper_ps_a_semantics(self):
        self.assertEqual(resources.container_state("app-pg"), "exited")  # fake 返回固定值
        with patch.object(resources, "_run", return_value=ns(rc=1, stderr="no such object")):
            self.assertIsNone(resources.container_state("ghost"))
        with patch.object(resources, "_run", side_effect=OSError("docker gone")):
            self.assertIsNone(resources.container_state("app-pg"))

    def test_default_run_seam_delegates_to_dockerctx(self):
        # 未打桩 _run 时：剥离 "docker" 前缀交给 dockerctx.run（显式 --context 的唯一出口）
        seen = []
        self.run_patcher.stop()
        try:
            with patch.object(dockerctx, "run", side_effect=lambda args, **kw: seen.append(list(args)) or ns()):
                result = resources._run(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=3)
        finally:
            self.run_patcher.start()
        self.assertEqual(seen, [["ps", "-a", "--format", "{{json .}}"]])
        self.assertEqual(result.returncode, 0)

    def test_inventory_unreachable_shape_unchanged(self):
        with patch.object(resources, "_run", return_value=ns(rc=1, stderr="daemon down")):
            inv = resources.inventory()
        self.assertEqual(inv, {"ok": False, "error": "daemon down"})
        with patch.object(resources, "_run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            inv = resources.inventory()
        self.assertFalse(inv["ok"])
        self.assertIn("超时", inv["error"])


if __name__ == "__main__":
    unittest.main()
