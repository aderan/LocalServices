"""resources.inventory 测试：全部 mock 模块内 runner（_run），不跑真实 docker。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import resources  # noqa: E402


def fake_run(ps_rows=(), inspect_rows=(), context="colima\n", rc=0, err=""):
    """按子命令返回预置输出，模拟 docker context show / ps / inspect。"""
    def _run(cmd, timeout=10.0):
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "context":
            return SimpleNamespace(returncode=rc, stdout=context, stderr=err)
        if sub == "ps":
            return SimpleNamespace(returncode=rc, stdout="".join(json.dumps(r) + "\n" for r in ps_rows), stderr=err)
        return SimpleNamespace(returncode=rc, stdout="".join(json.dumps(r) + "\n" for r in inspect_rows), stderr=err)
    return _run


PS_POSTGRES = {"Names": "climbo-postgres", "Image": "postgres:16", "State": "running"}
PS_TOOLS = {"Names": "tool-box", "Image": "busybox:1", "State": "running"}

INSPECT_POSTGRES = {
    "Name": "/climbo-postgres",
    "Config": {"Image": "postgres:16", "Labels": {
        "com.docker.compose.project": "climbo-server",
        "com.docker.compose.service": "postgres"}},
    "State": {"Status": "running", "Health": {"Status": "healthy"}},
    "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5434"}]}},
    "Mounts": [{"Type": "volume", "Name": "climbo-server_postgres_data"},
               {"Type": "bind", "Source": "/tmp/x", "Destination": "/data"}],
}
INSPECT_TOOLS = {
    "Name": "/tool-box",
    "Config": {"Image": "busybox:1", "Labels": {}},
    "State": {"Status": "running"},
    "NetworkSettings": {"Ports": {}},
    "Mounts": [],
}


class InventoryTest(unittest.TestCase):
    def test_parses_compose_labels_ports_volumes_health(self):
        with patch.object(resources, "_run", fake_run([PS_POSTGRES, PS_TOOLS],
                                                      [INSPECT_POSTGRES, INSPECT_TOOLS])):
            inv = resources.inventory()
        self.assertTrue(inv["ok"], inv)
        self.assertEqual(inv["docker_context"], "colima")
        pg, tool = inv["resources"]
        self.assertEqual(pg["id"], "container:climbo-postgres")
        self.assertEqual(pg["kind"], "docker-container")
        self.assertEqual(pg["name"], "climbo-postgres")
        self.assertEqual(pg["image"], "postgres:16")
        self.assertEqual(pg["state"], "running")
        self.assertEqual(pg["health"], "healthy")
        self.assertEqual(pg["compose_project"], "climbo-server")
        self.assertEqual(pg["compose_service"], "postgres")
        self.assertEqual(pg["ports"], {"5432/tcp": [{"host_ip": "0.0.0.0", "host_port": 5434}]})
        self.assertEqual(pg["volumes"], ["climbo-server_postgres_data"])  # 只收 named volume
        self.assertEqual(tool["health"], "none")
        self.assertIsNone(tool["compose_project"])

    def test_unhealthy_and_no_containers(self):
        unhealthy = dict(INSPECT_POSTGRES)
        unhealthy["State"] = {"Status": "running", "Health": {"Status": "unhealthy"}}
        with patch.object(resources, "_run", fake_run([PS_POSTGRES], [unhealthy])):
            inv = resources.inventory()
        self.assertEqual(inv["resources"][0]["health"], "unhealthy")
        with patch.object(resources, "_run", fake_run(ps_rows=[], inspect_rows=[])):
            inv = resources.inventory()
        self.assertEqual(inv["resources"], [])

    def test_docker_unreachable_cases(self):
        with patch.object(resources, "_run", side_effect=FileNotFoundError("docker")):
            inv = resources.inventory()
        self.assertFalse(inv["ok"])
        self.assertIn("docker", inv["error"])

        with patch.object(resources, "_run", side_effect=subprocess.TimeoutExpired("docker", 10)):
            inv = resources.inventory()
        self.assertFalse(inv["ok"])
        self.assertIn("超时", inv["error"])

        # 守护进程关闭：docker 返回非零
        with patch.object(resources, "_run", fake_run(rc=1, err="Cannot connect to the Docker daemon")):
            inv = resources.inventory()
        self.assertFalse(inv["ok"])
        self.assertIn("Cannot connect", inv["error"])


if __name__ == "__main__":
    unittest.main()
