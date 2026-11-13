"""验收重开后的 8 项缺口修复行为测试。

对应复核发现：
1. 注册两阶段回滚（registry 落盘失败恢复 TOML 原字节）
2. 停止确认后移除（组件仍在运行 → 拒绝删除登记）
3. 损坏 registry.json 先备份再当空（原记录保留在 .corrupt-* 备份）
5. dry_run 重新识别差异（不落盘、字段级 old/new）
7. 项目 autostart/favorite 经 upsert 设置
8. export → register 严格 payload 往返（依赖/构建/安装/标记全保留）
GUI 侧（4/6/7 入口与批量 revision）为静态锚点断言。
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
TMP = Path(tempfile.mkdtemp(prefix="lsfix-"))


class FixEnv(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(TMP / "home"),
            "LOCALSERVICES_SERVICES": str(TMP / "services"),
            "LOCALSERVICES_USER_SERVICES": str(TMP / "home" / "services"),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsfix",
        })
        env.start()
        self.addCleanup(env.stop)
        (TMP / "home").mkdir(parents=True, exist_ok=True)
        (TMP / "home" / "services").mkdir(parents=True, exist_ok=True)
        (TMP / "services").mkdir(parents=True, exist_ok=True)
        (TMP / "src").mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(REPO))

    def payload(self, sid="fx1", revision=None, svc_over=None, **over):
        svc = {"id": sid, "name": "Fx", "source": str(TMP / "src"),
               "components": [{"id": "api", "kind": "server", "cmd": ["/bin/sleep", "60"],
                               "cwd": ".", "port": 48221,
                               "ready": {"http": "http://127.0.0.1:48221/health"}}]}
        svc.update(svc_over or over.pop("svc", {}))
        body = {"schemaVersion": 1, "project": {"id": "p1", "name": "P1"}, "service": svc}
        if revision is not None:
            body["revision"] = revision
        return body


class RegistryReliabilityTest(FixEnv):
    def test_1_registry_save_failure_rolls_back_toml(self):
        from localservices.registry import Registry
        r = Registry()
        self.assertTrue(r.register_service(self.payload())["ok"])
        target = TMP / "home" / "services" / "fx1.toml"
        before = target.read_bytes()
        # 更新场景：registry 落盘失败 → 异常抛出（daemon 层转 500）且 TOML 恢复原字节
        with patch.object(Registry, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                r.register_service(self.payload(svc_over={"name": "Fx2"}, revision=1))
        self.assertEqual(target.read_bytes(), before, "更新失败必须恢复原字节")
        # 新建场景（独立身份才会走到写盘）：失败 → 文件不出现
        with patch.object(Registry, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                r.register_service(self.payload(sid="fx2", revision=1, svc_over={"components": [{
                    "id": "api", "kind": "server", "cmd": ["/bin/sleep", "62"], "cwd": ".",
                    "port": 48223, "ready": {"http": "http://127.0.0.1:48223/health"}}]}))
        self.assertFalse((TMP / "home" / "services" / "fx2.toml").exists(), "新建失败不得留下半成品")

    def test_2_remove_refused_while_component_running(self):
        from localservices.registry import Registry

        class FakeJI:
            running, loaded, pid, last_exit = True, True, 123, None

        class FakeComp:
            id = "api"

        class FakeProj:
            id = "fx1"
            components = [FakeComp()]
            managed_components = lambda self: self.components  # noqa: E731

        class FakeCtl:
            ops = {}
            projects = {"fx1": FakeProj()}
            def reload(self): pass
            def label(self, p, c): return "com.mybrain.lsfix.fx1.api"
            def stop(self, sid): return {"ok": True, "accepted": False, "message": "已停止"}

        from localservices import registry as R
        r = Registry()
        self.assertTrue(r.register_service(self.payload())["ok"])
        target = TMP / "home" / "services" / "fx1.toml"
        before = target.read_bytes()
        from localservices import launchd as launchd_mod
        from localservices.registry import RegistryError
        with patch.object(launchd_mod, "info", return_value=FakeJI()):
            with self.assertRaises(RegistryError) as cm:
                r.remove_service("fx1", "stop", FakeCtl())
        self.assertIn("仍在运行", str(cm.exception))
        self.assertTrue(target.exists() and target.read_bytes() == before, "拒绝移除时登记不得删除")

    def test_3_corrupt_registry_backed_up_not_overwritten(self):
        from localservices import paths
        from localservices.registry import Registry
        paths.registry_file().write_text("{corrupt!!")
        r = Registry()
        self.assertEqual(r.data()["projects"], [])
        baks = list(paths.home().glob("registry.json.corrupt-*"))
        self.assertTrue(baks, "损坏文件必须先备份")
        self.assertIn("corrupt", baks[0].read_text())
        # 备份不随后续写入消失
        r.ensure_project("px", "PX", None, None)
        self.assertTrue(baks[0].exists())

    def test_5_dry_run_diff_no_writes(self):
        from localservices.registry import Registry
        r = Registry()
        self.assertTrue(r.register_service(self.payload())["ok"])
        target = TMP / "home" / "services" / "fx1.toml"
        before = target.read_bytes()
        reg_before = paths_reg()
        changed = self.payload(svc_over={"name": "Fx 改名"}, revision=1)
        changed["service"]["components"][0]["auto_update"] = False
        res = r.register_service(changed, dry_run=True)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("action"), "diff")
        fields = {d["field"] for d in res["diff"]}
        self.assertIn("name", fields)
        self.assertIn("components[api].auto_update", fields)
        self.assertEqual(target.read_bytes(), before, "dry_run 不得落盘")
        self.assertEqual(paths_reg(), reg_before, "dry_run 不得改 registry")

    def test_7_project_autostart_favorite_upsert(self):
        from localservices.registry import Registry
        r = Registry()
        proj, _ = r.ensure_project("pa", "PA", None, None)
        self.assertFalse(proj.get("autostart"))
        proj, _ = r.ensure_project("pa", "PA", None, None, autostart=True, favorite=True)
        self.assertTrue(proj["autostart"] and proj["favorite"])
        data = r.data()
        stored = next(p for p in data["projects"] if p["id"] == "pa")
        self.assertTrue(stored["autostart"] and stored["favorite"])

    def test_8_export_import_roundtrip(self):
        from localservices import manifest
        from localservices.registry import Registry
        # 用 registry 注册一个带依赖/构建/安装/顺序/更新策略的服务，再从清单导出
        r = Registry()
        payload = self.payload(sid="rt1", revision=1,
                               svc_over={"components": [{
                                   "id": "api", "kind": "server", "cmd": ["/bin/sleep", "61"],
                                   "cwd": ".", "port": 48222,
                                   "ready": {"http": "http://127.0.0.1:48222/health"}}]})
        payload["service"]["components"][0].update({
            "build": ["make", "build"], "install": ["make", "deps"], "install_marker": ".deps",
            "after": [], "auto_update": False, "watch": ["src"], "external_cmd": "rt-server"})
        payload["service"]["dependencies"] = [
            {"name": "Postgres", "probe": {"tcp": "127.0.0.1:5432"}, "affects": ["api"],
             "required": True, "container": "pg", "volume": "pgdata"}]
        self.assertTrue(r.register_service(payload)["ok"])
        loaded = manifest.load_all()
        self.assertIn("rt1", loaded)
        # cli.export 的净荷构造逻辑与 localservices.cli 相同；这里直接调用其内部实现太重，
        # 用 subprocess 调真实 CLI（读真实清单路径由环境变量指向 TMP）
        import subprocess
        out = subprocess.run(
            [sys.executable, "-m", "localservices.cli", "export", "rt1"],
            capture_output=True, text=True, cwd=REPO,
            env={**os.environ})
        self.assertEqual(out.returncode, 0, out.stderr)
        exported = json.loads(out.stdout)
        self.assertEqual(exported["schemaVersion"], 1)
        self.assertIn("service", exported)
        # 换一台"新机器"（重置 home/services），导入导出的 payload
        (TMP / "home2").mkdir(exist_ok=True)
        (TMP / "home2" / "services").mkdir(exist_ok=True)
        with patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(TMP / "home2"),
            "LOCALSERVICES_USER_SERVICES": str(TMP / "home2" / "services"),
        }):
            r2 = Registry()
            res = r2.register_service(exported)
            self.assertTrue(res.get("ok"), res)
            loaded2 = manifest.load_all()
            p = loaded2["rt1"]
            c = p.components[0]
            self.assertEqual(c.build[:2], ["make", "build"])
            self.assertEqual(c.install, ["make", "deps"])
            self.assertEqual(c.install_marker, ".deps")
            self.assertFalse(c.auto_update)
            self.assertEqual(c.watch, ["src"])
            self.assertEqual(p.dependencies[0].container, "pg")
            self.assertEqual(p.dependencies[0].volume, "pgdata")


def paths_reg() -> bytes:
    from localservices import paths
    try:
        return paths.registry_file().read_bytes()
    except OSError:
        return b""


class GuiAnchorTest(unittest.TestCase):
    """GUI 侧修复的静态锚点（4 全字段转发 / 5 差异展示 / 6 批量 revision / 7 项目管理入口）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO / "localservices" / "web" / "dashboard.html").read_text(encoding="utf-8")

    def test_4_full_field_forwarding(self):
        self.assertIn("COMP_FWD_KEYS", self.html)
        for key in ("build", "install", "install_when", "install_marker", "after",
                    "auto_update", "watch", "external_cmd", "env_files", "shell_script"):
            self.assertIn(f'"{key}"', self.html)
        self.assertIn("c.dependencies || []", self.html) or self.assertIn(
            "(c.dependencies || []).map", self.html)
        # 依赖随注册保留的可见提示
        self.assertIn("依赖", self.html)

    def test_5_diff_preview(self):
        self.assertIn("dry_run: true", self.html)
        self.assertIn("wizPrefetchDiff", self.html)
        self.assertIn("与已登记服务", self.html)
        self.assertIn("你的修改优先生效", self.html)

    def test_6_batch_revision_handling(self):
        self.assertIn("wizRegisterOnce", self.html)
        self.assertIn("w.target = {...w.target, id: res.project_id, revision: res.revision}", self.html)

    def test_7_project_management_entries(self):
        for anchor in ("wizPresetFor", "添加服务…", "重新识别", "toggleAutostart",
                       "自动启动", "wizAnalyzePresetDirs", "联合识别"):
            self.assertIn(anchor, self.html)


if __name__ == "__main__":
    unittest.main()
