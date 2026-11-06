"""Locove V3 生命周期贯穿：注册（payload/diff/覆盖/定义 API）→ 快照/打开 → 移除/恢复预检。

覆盖：
- service.endpoints[] 注册落盘、dry-run diff 包含 endpoints、重复域名提交前失败（含 dry-run）。
- 覆盖层 endpoints[<id>].host 生效（GUI「你的覆盖优先生效」同一机制）。
- 定义 API 返回 endpoints（编辑对话框零变化回写的基础）。
- 快照 projects[].endpoints + open() 公开地址优先；入口不健康回退直连并给出缺口说明。
- 移除→他人抢占域名→恢复：域名冲突预检 409，零写入。
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

from localservices import manifest, overrides, register_api  # noqa: E402
from localservices.controller import Controller  # noqa: E402
from localservices.registry import Registry, RegistryError  # noqa: E402

PY = sys.executable


class _SimpleCtl:
    ops: dict = {}
    projects: dict = {}

    def reload(self):
        pass

    def label(self, p, c):
        return f"label.{p.id}.{c.id}"

    def stop(self, sid):
        return {"ok": True, "accepted": False, "message": "未运行"}

    def release_service(self, sid):
        return {"ok": True, "released": [], "errors": []}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-locove-life-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        self.src = self.root / "src"
        for d in (self.home, self.services, self.user_services, self.src):
            d.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-locove-life",
        })
        env.start()
        self.addCleanup(env.stop)
        self.reg = Registry()

    def payload(self, sid="demo", port=48601, project="p1", endpoints=None, revision=None):
        svc = {"id": sid, "name": sid.title(), "source": str(self.src),
               "components": [{"id": "web", "kind": "server",
                               "cmd": [PY, "-c", f"print('{sid}')"], "cwd": ".",
                               "port": port}]}
        if endpoints is not None:
            svc["endpoints"] = endpoints
        body = {"schemaVersion": 1, "project": {"id": project, "name": project.upper()},
                "service": svc}
        if revision is not None:
            body["revision"] = revision
        return body

    def register(self, sid="demo", port=48601, project="p1", endpoints=None, revision=None):
        if revision is None:  # 缺省取当前项目 revision（0=仅当不存在时创建）
            proj = self.reg.project(project)
            revision = proj["revision"] if proj else 0
        res = self.reg.register_service(
            self.payload(sid=sid, port=port, project=project, endpoints=endpoints,
                         revision=revision))
        self.assertTrue(res["ok"], res)
        return res


class RegistrationFlowTest(Env):
    def test_endpoints_persisted_to_toml(self):
        self.register(endpoints=[{"host": "demo.locove", "component": "web", "open_path": "/zh"}])
        text = (self.user_services / "demo.toml").read_text(encoding="utf-8")
        self.assertIn("[[endpoints]]", text)
        self.assertIn('host = "demo.locove"', text)
        p = manifest.load_all()["demo"]
        self.assertEqual(p.endpoints[0].open_path, "/zh")

    def test_derived_endpoint_when_payload_has_none(self):
        self.register()
        p = manifest.load_all()["demo"]
        self.assertEqual(p.endpoints[0].host, "demo.locove")  # 派生，不写 TOML
        text = (self.user_services / "demo.toml").read_text(encoding="utf-8")
        self.assertNotIn("[[endpoints]]", text)

    def test_duplicate_host_rejected_before_commit(self):
        self.register(sid="alpha", port=48701)
        res = self.reg.register_service(self.payload(
            sid="beta", port=48702,
            endpoints=[{"host": "alpha.locove", "component": "web"}],
            revision=self.reg.project("p1")["revision"]))
        self.assertFalse(res["ok"], res)
        self.assertIn("域名已被占用", res["error"])
        self.assertIn("alpha", res["error"])
        self.assertNotIn("beta", manifest.load_all())

    def test_duplicate_host_rejected_in_dry_run_too(self):
        self.register(sid="alpha", port=48711)
        res = self.reg.register_service(self.payload(
            sid="beta", port=48712,
            endpoints=[{"host": "alpha.locove", "component": "web"}],
            revision=self.reg.project("p1")["revision"]), dry_run=True)
        self.assertFalse(res["ok"], res)
        self.assertIn("域名已被占用", res["error"])

    def test_same_service_keeps_its_own_host_on_update(self):
        self.register(sid="svc", port=48721,
                      endpoints=[{"host": "svc.locove", "component": "web"}])
        proj = self.reg.project("p1")
        res = self.reg.register_service(
            self.payload(sid="svc", port=48721,
                         endpoints=[{"host": "svc.locove", "component": "web", "open_path": "/x"}],
                         revision=proj["revision"]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "updated")

    def test_dry_run_diff_contains_endpoint_change(self):
        self.register(sid="svc", port=48731)
        proj = self.reg.project("p1")
        res = self.reg._register(
            self.payload(sid="svc", port=48731,
                         endpoints=[{"host": "renamed.locove", "component": "web"}],
                         revision=proj["revision"]), dry_run=True)
        self.assertTrue(res["ok"], res)
        diff_blob = json.dumps(res.get("diff"), ensure_ascii=False)
        self.assertIn("endpoints", diff_blob)
        self.assertIn("renamed.locove", diff_blob)

    def test_definition_api_includes_endpoints(self):
        self.register(sid="svc", port=48741)
        code, res = register_api.handle_service_definition("svc")
        self.assertEqual(code, 200)
        eps = res["definition"].get("endpoints")
        self.assertEqual(eps[0]["host"], "svc.locove")
        self.assertEqual(eps[0]["component"], "web")


class OverrideEndpointTest(Env):
    def test_override_endpoint_host_applies_over_payload(self):
        self.register(sid="svc", port=48751,
                      endpoints=[{"host": "svc.locove", "component": "web"}])
        # 人工覆盖：域名改指自定义前缀（GUI 覆盖层同一文件）
        (self.home / "overrides.json").write_text(json.dumps({
            "schemaVersion": 1,
            "services": {"svc": {"fields": {"endpoints[default].host": "custom.locove"},
                                 "updated_at": "2026-11-04T00:00:00"}}}), encoding="utf-8")
        proj = self.reg.project("p1")
        res = self.reg.register_service(
            self.payload(sid="svc", port=48751,
                         endpoints=[{"host": "svc.locove", "component": "web"}],
                         revision=proj["revision"]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(manifest.load_all()["svc"].endpoints[0].host, "custom.locove")


class SnapshotAndOpenTest(Env):
    def _controller_with_service(self, host="snap.locove"):
        (self.services / "snap.toml").write_text(f"""
id = "snap"
name = "Snap"
source = "{self.src}"
open = "http://localhost:{48801}/zh"
[[components]]
id = "web"
cmd = ["sleep", "1"]
port = {48801}
[[endpoints]]
host = "{host}"
component = "web"
open_path = "/zh"
""", encoding="utf-8")
        ctl = Controller()
        ctl._snapshot = {"projects": [{"id": "snap", "state": "available"}]}
        return ctl

    def test_snapshot_projects_expose_endpoints(self):
        ctl = self._controller_with_service()
        snap = ctl.refresh_snapshot()
        proj = next(p for p in snap["projects"] if p["id"] == "snap")
        self.assertEqual(proj["endpoints"][0]["host"], "snap.locove")
        self.assertEqual(proj["endpoints"][0]["public_url"], "http://snap.locove:5670/zh")

    def _open_with_patches(self, entry_ok: bool, use_domains: bool = True) -> tuple[dict, object]:
        ctl = self._controller_with_service()
        for p_ in self._patch_open_path():
            p_.start()
            self.addCleanup(p_.stop)
        sub_mgr = patch("localservices.controller.subprocess")
        sub_mock = sub_mgr.start()
        self.addCleanup(sub_mgr.stop)
        status = {"state": "available"} if entry_ok else {"state": "failed"}
        with patch("localservices.locove.module_status", return_value=status), \
             patch.object(ctl, "domain_settings",
                          return_value={"use_domains": use_domains, "domain_form": "suffix",
                                        "domain_port": 5670}):
            res = ctl.open("snap")
        return res, sub_mock

    def _patch_open_path(self):
        from types import SimpleNamespace
        ji = SimpleNamespace(running=False, loaded=False, pid=None, last_exit=None)
        return [patch("localservices.controller.launchd.info", return_value=ji),
                patch("localservices.controller.procs.snapshot", return_value={}),
                patch("localservices.controller.procs.listeners", return_value={}),
                patch("localservices.controller.procs.probe", return_value=(False, "")),
                patch("localservices.controller.procs.all_cwds", return_value={})]

    def test_open_prefers_public_url_when_entry_healthy(self):
        res, sub_patch = self._open_with_patches(entry_ok=True)
        self.assertTrue(res["ok"])
        self.assertEqual(sub_patch.Popen.call_args[0][0][1], "http://snap.locove:5670/zh")
        self.assertNotIn("note", res)

    def test_open_falls_back_to_direct_with_note_when_entry_down(self):
        res, sub_patch = self._open_with_patches(entry_ok=False)
        self.assertTrue(res["ok"])
        self.assertIn("note", res)
        self.assertEqual(sub_patch.Popen.call_args[0][0][1], "http://localhost:48801/zh")

    def test_open_defaults_to_direct_when_domains_not_enabled(self):
        """V3.5：默认（未开启「使用域名」）即使入口健康也走本地直连地址。"""
        res, sub_patch = self._open_with_patches(entry_ok=True, use_domains=False)
        self.assertTrue(res["ok"])
        self.assertNotIn("note", res)
        self.assertEqual(sub_patch.Popen.call_args[0][0][1], "http://localhost:48801/zh")


class RestoreDomainPrecheckTest(Env):
    def test_restore_rejects_when_host_taken(self):
        self.register(sid="victim", port=48811,
                      endpoints=[{"host": "victim.locove", "component": "web"}])
        self.reg.remove_service("victim", "keep-external", _SimpleCtl())
        # 他人抢占域名
        self.register(sid="squatter", port=48812,
                      endpoints=[{"host": "victim.locove", "component": "web"}])
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_service("victim", {}, _SimpleCtl())
        self.assertIn("域名已被占用", str(cm.exception))
        self.assertIn("squatter", str(cm.exception))
        # 零写入：victim 未恢复登记
        self.assertNotIn("victim", manifest.load_all())

    def test_restore_succeeds_when_host_free(self):
        self.register(sid="victim", port=48821)
        self.reg.remove_service("victim", "keep-external", _SimpleCtl())
        res = self.reg.restore_service("victim", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual(manifest.load_all()["victim"].endpoints[0].host, "victim.locove")


if __name__ == "__main__":
    unittest.main()
