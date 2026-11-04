"""Locove V3 契约行为测试（M1：品牌、Endpoint 数据模型、意图唯一写入者、依赖图校验）。

覆盖：
- 组件依赖图：未知 after 引用 / 环在加载与注册时明确失败，并报告链路。
- 运行意图唯一写入者：恢复流程经统一入口写意图，后续任何 Store 保存不覆盖恢复结果；
  registry 侧有 ctl 时绝不直接改写 state.json。
- Endpoint 数据模型：显式 endpoints 解析与规范化、旧清单缺省派生（稳定 <id>.locove）、
  跨服务 host 冲突在加载时拒绝。
- 品牌：Dashboard 主入口呈现 Locove／洛湾。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import manifest, paths  # noqa: E402
from localservices.controller import Controller  # noqa: E402
from localservices.registry import Registry, RegistryError  # noqa: E402


TOML_BODY = """
id = "{sid}"
name = "{sid}"
source = "{src}"
open = "http://localhost:{port}/zh"
[[components]]
id = "web"
cmd = ["python3", "-m", "http.server", "{port}"]
port = {port}
"""


class IsolatedEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-locove-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "home"),
            "LOCALSERVICES_SERVICES": str(self.root / "services"),
            "LOCALSERVICES_USER_SERVICES": str(self.root / "user-services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-locove-test",
        })
        env.start()
        self.addCleanup(env.stop)
        paths.ensure()
        (self.root / "services").mkdir(parents=True, exist_ok=True)
        (self.root / "user-services").mkdir(parents=True, exist_ok=True)
        (self.root / "src").mkdir(parents=True, exist_ok=True)

    def write_service(self, directory: Path, sid: str, body: str, port: int = 8100) -> Path:
        f = directory / f"{sid}.toml"
        f.write_text(body.format(sid=sid, src=self.root / "src", port=port), encoding="utf-8")
        return f


# ---------- 依赖图 ----------

class DependencyGraphTest(IsolatedEnv):
    def test_unknown_after_rejected_at_load(self):
        f = self.write_service(self.root / "services", "bad-ref", TOML_BODY + """
[[components]]
id = "worker"
cmd = ["sleep", "100"]
after = ["nope"]
""")
        with self.assertRaises(ValueError) as cm:
            manifest.load_file(f)
        self.assertIn("nope", str(cm.exception))

    def test_cycle_rejected_at_load_with_chain(self):
        f = self.write_service(self.root / "services", "cyclic", TOML_BODY + """
[[components]]
id = "a2"
cmd = ["sleep", "100"]
after = ["a3"]
[[components]]
id = "a3"
cmd = ["sleep", "100"]
after = ["a2"]
""")
        with self.assertRaises(ValueError) as cm:
            manifest.load_file(f)
        msg = str(cm.exception)
        self.assertIn("依赖环", msg)
        self.assertIn("a2", msg)
        self.assertIn("a3", msg)

    def test_valid_after_still_loads(self):
        f = self.write_service(self.root / "services", "ordered", TOML_BODY + """
[[components]]
id = "worker"
cmd = ["sleep", "100"]
after = ["web"]
""")
        p = manifest.load_file(f)
        self.assertEqual([c.id for c in p.components], ["web", "worker"])

    def test_registration_rejects_unknown_after(self):
        payload = {
            "schemaVersion": 1, "revision": 0,
            "project": {"id": "p1", "name": "P1"},
            "service": {
                "id": "badref", "name": "BadRef", "source": str(self.root / "src"),
                "components": [
                    {"id": "web", "cmd": ["sleep", "1"], "port": 8123},
                    {"id": "w", "cmd": ["sleep", "1"], "after": ["ghost"]},
                ],
            },
        }
        with self.assertRaises(RegistryError) as cm:
            Registry()._register(payload)
        self.assertIn("ghost", str(cm.exception))

    def test_registration_rejects_cycle(self):
        payload = {
            "schemaVersion": 1, "revision": 0,
            "project": {"id": "p1", "name": "P1"},
            "service": {
                "id": "cycle", "name": "Cycle", "source": str(self.root / "src"),
                "components": [
                    {"id": "a", "cmd": ["sleep", "1"], "after": ["b"]},
                    {"id": "b", "cmd": ["sleep", "1"], "after": ["a"]},
                ],
            },
        }
        with self.assertRaises(RegistryError) as cm:
            Registry()._register(payload)
        self.assertIn("依赖环", str(cm.exception))


# ---------- 意图唯一写入者 ----------

class _RecordingCtl:
    """restore 用的最小 ctl 桩：记录统一入口调用。"""

    ops: dict = {}
    projects: dict = {}

    def reload(self):
        pass

    def stop(self, sid):
        return {"ok": True, "accepted": False, "message": "未运行"}

    def release_service(self, sid):
        return {"ok": True, "released": [], "errors": []}

    def set_intent_direct(self, sid, intent, by="restore"):
        self.calls.append((sid, intent, by))
        return True

    def __init__(self):
        self.calls = []


class IntentSingleWriterTest(IsolatedEnv):
    def test_restore_intent_survives_subsequent_store_saves(self):
        ctl = Controller()
        ctl.store.set_intent("svc-a", "running", by="user")
        # 恢复写意图：必须与 Store 内存一致，而不是只改文件
        ctl.set_intent_direct("svc-restored", "stopped", by="restore")
        # 之后任何其他服务的启停触发整份 state.json 重写
        ctl.store.set_intent("svc-b", "running", by="user")
        data = json.loads(paths.state_file().read_text(encoding="utf-8"))
        self.assertEqual(data["projects"]["svc-restored"]["intent"], "stopped")
        self.assertEqual(data["projects"]["svc-a"]["intent"], "running")
        self.assertEqual(data["projects"]["svc-b"]["intent"], "running")
        self.assertEqual(ctl.store.data["projects"]["svc-restored"]["intent"], "stopped")

    def test_registry_restore_uses_unified_writer(self):
        ctl = _RecordingCtl()
        Registry._apply_restore_intent(ctl, "svc-x", "stopped")
        self.assertEqual(ctl.calls, [("svc-x", "stopped", "restore")])

    def test_registry_restore_falls_back_without_ctl_writer(self):
        # 旧测试桩/无统一入口时：仍显式写文件（保持既有行为，不静默失败）
        paths.state_file().write_text(json.dumps({"projects": {}}), encoding="utf-8")
        Registry._apply_restore_intent(None, "svc-y", "running")
        data = json.loads(paths.state_file().read_text(encoding="utf-8"))
        self.assertEqual(data["projects"]["svc-y"]["intent"], "running")


# ---------- Endpoint 数据模型 ----------

class EndpointModelTest(IsolatedEnv):
    def test_explicit_endpoints_parsed_and_normalized(self):
        f = self.write_service(self.root / "services", "explicit", TOML_BODY + """
[[endpoints]]
host = "api.explicit.locove"
component = "web"
open_path = "/api"

[[endpoints]]
id = "alt"
host = "alt"          # 无后缀 label：规范化为 alt.<suffix>
component = "web"
""")
        p = manifest.load_file(f)
        self.assertEqual([e.host for e in p.endpoints], ["api.explicit.locove", "alt.locove"])
        self.assertEqual(p.endpoints[0].open_path, "/api")
        self.assertEqual(p.endpoints[1].open_path, "/")

    def test_legacy_manifest_derives_stable_default_endpoint(self):
        f = self.write_service(self.root / "services", "buildbase", TOML_BODY, port=3000)
        p = manifest.load_file(f)
        self.assertEqual(len(p.endpoints), 1)
        e = p.endpoints[0]
        self.assertEqual(e.host, "buildbase.locove")
        self.assertEqual(e.component, "web")
        self.assertEqual(e.open_path, "/zh")

    def test_legacy_without_open_uses_first_ported_server(self):
        body = """
id = "{sid}"
name = "{sid}"
source = "{src}"
[[components]]
id = "api"
cmd = ["sleep", "1"]
port = 8231
[[components]]
id = "web"
cmd = ["sleep", "1"]
port = 8232
"""
        f = self.write_service(self.root / "services", "noopen", body)
        p = manifest.load_file(f)
        e = p.endpoints[0]
        self.assertEqual((e.host, e.component, e.open_path), ("noopen.locove", "api", "/"))

    def test_no_server_port_means_no_endpoint(self):
        body = """
id = "{sid}"
name = "{sid}"
source = "{src}"
[[components]]
id = "worker"
kind = "worker"
cmd = ["sleep", "1"]
"""
        f = self.write_service(self.root / "services", "workonly", body)
        p = manifest.load_file(f)
        self.assertEqual(p.endpoints, [])

    def test_unknown_endpoint_component_rejected(self):
        f = self.write_service(self.root / "services", "badcomp", TOML_BODY + """
[[endpoints]]
host = "badcomp.locove"
component = "ghost"
""")
        with self.assertRaises(ValueError) as cm:
            manifest.load_file(f)
        self.assertIn("ghost", str(cm.exception))

    def test_duplicate_hosts_across_services_rejected(self):
        self.write_service(self.root / "services", "svc1", TOML_BODY, port=8301)
        # 显式抢占 svc1 的派生域名
        self.write_service(self.root / "user-services", "svc2", TOML_BODY + """
[[endpoints]]
host = "svc1.locove"
component = "web"
""", port=8302)
        with self.assertRaises(ValueError) as cm:
            manifest.load_all()
        self.assertIn("svc1.locove", str(cm.exception))

    def test_rename_keeps_domain_registration_stable(self):
        # 显示名称变化不影响派生域名（域名锚定服务 id，不锚定 name）
        f = self.write_service(self.root / "services", "stable", TOML_BODY, port=8311)
        p1 = manifest.load_file(f)
        text = f.read_text(encoding="utf-8").replace('name = "stable"', 'name = "新名称"')
        f.write_text(text, encoding="utf-8")
        p2 = manifest.load_file(f)
        self.assertEqual(p1.endpoints[0].host, p2.endpoints[0].host)

    def test_public_url_helper(self):
        f = self.write_service(self.root / "services", "pub", TOML_BODY, port=8321)
        p = manifest.load_file(f)
        self.assertEqual(p.public_urls(), ["http://pub.locove/zh"])


# ---------- 品牌 ----------

class BrandingTest(unittest.TestCase):
    def test_dashboard_presents_locove(self):
        html = (REPO / "localservices" / "web" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("Locove／洛湾", html)
        self.assertNotIn("MyBrain Services", html)


if __name__ == "__main__":
    unittest.main()
