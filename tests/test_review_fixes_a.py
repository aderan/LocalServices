"""V2 复审修复（A 线）行为测试：R02/R03/R04/R10 + R06 后台半。

- R02 整项目恢复写盘前预检端口/重复 id/身份冲突 → 409 零写入（字节不变）
- R03 多服务人工覆盖整项目恢复：overrides.json 单次合并写入；_atomic_commit 拒绝重复目标
- R04 compose 只按 compose 标签解析容器；名字回退移除（未知归属绝不 start/stop）
- R10 恢复到另一 Project 时 TOML project 字段同事务重绑定（归属一致）
- R06 本次用户编辑（edited）优先于旧覆盖；覆盖路径支持任意嵌套 dotted path；dry_run 同合并
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PY = sys.executable


def ns(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class _SimpleCtl:
    """最小 fake 控制器：无 launchd 记录、stop/release 成功（绝不触真实 launchd/docker）。"""

    ops: dict = {}
    projects: dict = {}

    def reload(self):
        pass

    def label(self, p, c):
        return f"label.{p.id}.{c.id}"

    def stop(self, sid):
        return {"ok": True, "accepted": False, "message": "未运行"}

    def release_service(self, sid):
        return {"ok": True, "released": [f"{sid}.api"], "errors": []}


class ReviewFixBase(unittest.TestCase):
    """隔离环境（LOCALSERVICES_* 指向 tmp），端口预算 18300-18399。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsrvfix-a-")
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
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.lsrvfixa",
        })
        env.start()
        self.addCleanup(env.stop)
        from localservices.registry import Registry
        self.reg = Registry()

    # ---- helpers ----

    def payload(self, sid="demo", port=18301, project="p1", revision=None, name=None,
                comps_over=None, extra_svc=None):
        comp = {"id": "api", "kind": "server", "cmd": [PY, "-c", f"print('{sid}')"],
                "cwd": ".", "port": port}
        comp.update(comps_over or {})
        svc = {"id": sid, "name": name or sid.title(), "source": str(self.src),
               "components": [comp]}
        svc.update(extra_svc or {})
        body = {"schemaVersion": 1, "project": {"id": project, "name": project.upper()},
                "service": svc}
        if revision is not None:
            body["revision"] = revision
        return body

    def register(self, sid="demo", port=18301, project="p1", revision=None, name=None,
                 comps_over=None, extra_svc=None):
        from localservices.registry import Registry
        if revision is None:
            proj = self.reg.project(project)
            revision = proj["revision"] if proj else 0
        res = self.reg.register_service(self.payload(sid=sid, port=port, project=project,
                                                    revision=revision, name=name,
                                                    comps_over=comps_over, extra_svc=extra_svc))
        self.assertTrue(res["ok"], res)
        return res

    def remove_project(self, pid="p1", disposition="stop"):
        res = self.reg.remove_project(pid, disposition, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        return res

    def archive_path(self, sid) -> Path:
        cands = sorted((self.home / "removed").glob(f"{sid}-*.json"))
        self.assertTrue(cands, f"缺少 {sid} 的归档")
        return cands[-1]

    def snapshot_bytes(self) -> dict[str, bytes]:
        out = {}
        for name in ("registry.json", "removed.json", "overrides.json", "state.json"):
            p = self.home / name
            try:
                out[name] = p.read_bytes()
            except OSError:
                out[name] = b""
        return out


# ================================================================ R02

class R02ProjectRestorePreflightTest(ReviewFixBase):
    """整项目恢复缺少端口冲突预检（复核 R02）：写盘前一次性验证，冲突零写入。"""

    def test_restore_into_port_conflict_409_zero_writes(self):
        from localservices import tombstones
        from localservices.registry import RegistryError
        self.register(sid="svc-a", port=18301)
        self.register(sid="svc-b", port=18302, revision=1)
        self.remove_project()
        # 期间另一项目占用了 49001 同款场景：这里用 18301
        self.register(sid="web", port=18301, project="other")
        before = self.snapshot_bytes()
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("18301", str(cm.exception) + json.dumps(
            cm.exception.payload.get("details", []), ensure_ascii=False))
        # 冲突对象点名占用者（web.api = 占用服务的组件）与新成员（svc-a）
        blob = str(cm.exception) + json.dumps(cm.exception.payload, ensure_ascii=False)
        self.assertIn("已被 web.api 占用", blob)
        self.assertIn("svc-a", blob)
        self.assertEqual(self.snapshot_bytes(), before, "冲突时零写入：四个文件字节一致")
        # 恢复能力仍在：移除占位者后可成功恢复
        self.reg.remove_service("web", "keep-external", _SimpleCtl())
        res = self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)

    def test_restore_internal_member_conflict_409(self):
        from localservices.registry import RegistryError
        self.register(sid="svc-a", port=18303)
        self.register(sid="svc-b", port=18304, revision=1)
        self.remove_project()
        # 模拟移除期间归档漂移：两个成员声明同一端口
        ap = self.archive_path("svc-b")
        doc = json.loads(ap.read_text(encoding="utf-8"))
        doc["toml"] = doc["toml"].replace("18304", "18303")
        ap.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        before = self.snapshot_bytes()
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertEqual(cm.exception.status, 409)
        blob = str(cm.exception) + json.dumps(cm.exception.payload, ensure_ascii=False)
        self.assertIn("18303", blob)
        self.assertEqual(self.snapshot_bytes(), before, "内部冲突同样零写入")

    def test_restore_duplicate_identity_between_members_409(self):
        from localservices.registry import RegistryError
        self.register(sid="svc-a", port=18305)
        self.register(sid="svc-b", port=18306, revision=1)
        self.remove_project()
        # 归档漂移：svc-b 与 svc-a 完全同身份（同 source/cwd/cmd），仅端口不同
        ap = self.archive_path("svc-b")
        doc = json.loads(ap.read_text(encoding="utf-8"))
        toml_a = json.loads(self.archive_path("svc-a").read_text(encoding="utf-8"))["toml"]
        doc["toml"] = toml_a.replace('id = "svc-a"', 'id = "svc-b"')
        ap.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        with self.assertRaises(RegistryError) as cm:
            self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("身份", str(cm.exception) + json.dumps(
            cm.exception.payload, ensure_ascii=False))

    def test_clean_restore_still_works(self):
        from localservices import manifest, tombstones
        self.register(sid="svc-a", port=18307)
        self.register(sid="svc-b", port=18308, revision=1)
        self.remove_project()
        res = self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        loaded = manifest.load_all()
        self.assertIn("svc-a", loaded)
        self.assertIn("svc-b", loaded)
        self.assertEqual(sorted(self.reg.project("p1")["services"]), ["svc-a", "svc-b"])
        self.assertEqual(tombstones.projects(), set())


# ================================================================ R03

class R03MultiOverrideRestoreTest(ReviewFixBase):
    """多服务人工覆盖使整项目恢复失败（复核 R03）：单次合并写入。"""

    def test_two_edited_services_restore_overrides_once(self):
        from localservices import overrides
        self.register(sid="svc-a", port=18311, name="甲改名",
                      extra_svc={"edited": ["name"]})
        self.register(sid="svc-b", port=18312, revision=1, name="乙改名",
                      extra_svc={"edited": ["name"]})
        self.assertEqual(overrides.fields("svc-a"), {"name": "甲改名"})
        self.assertEqual(overrides.fields("svc-b"), {"name": "乙改名"})
        self.remove_project()
        self.assertEqual(overrides.fields("svc-a"), {}, "移除后活跃覆盖层清空")
        res = self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual(overrides.fields("svc-a"), {"name": "甲改名"})
        self.assertEqual(overrides.fields("svc-b"), {"name": "乙改名"})
        # overrides.json 是合法 JSON 且单次写入（文件存在且无残留 tmp）
        data = json.loads((self.home / "overrides.json").read_text(encoding="utf-8"))
        self.assertEqual(data["services"]["svc-a"]["fields"], {"name": "甲改名"})
        self.assertEqual(data["services"]["svc-b"]["fields"], {"name": "乙改名"})
        self.assertEqual(list((self.home).glob(".overrides.json.*.tmp")), [])

    def test_third_project_overrides_preserved(self):
        from localservices import overrides
        self.register(sid="svc-a", port=18313, name="甲改名",
                      extra_svc={"edited": ["name"]})
        self.register(sid="svc-b", port=18314, revision=1, name="乙改名",
                      extra_svc={"edited": ["name"]})
        # 第三个项目（不移除）的既有覆盖
        self.register(sid="svc-c", port=18315, project="other", name="丙覆盖",
                      extra_svc={"edited": ["name"]})
        self.remove_project("p1")
        res = self.reg.restore_project("p1", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual(overrides.fields("svc-c"), {"name": "丙覆盖"},
                         "其他项目的覆盖必须原样保留")

    def test_atomic_commit_rejects_duplicate_targets(self):
        from localservices.registry import _atomic_commit
        target = self.user_services / "dup.toml"
        with self.assertRaises(RuntimeError) as cm:
            _atomic_commit([(target, b"one"), (target, b"two")])
        self.assertIn("dup.toml", str(cm.exception))
        self.assertFalse(target.exists(), "拒绝发生在任何写入之前")
        self.assertEqual(list(self.user_services.glob(".dup.toml.*.tmp")), [],
                         "不得留下临时文件")


# ================================================================ R04

class R04ComposeLabelOnlyTest(unittest.TestCase):
    """compose 名字回退认领未知容器（复核 R04）：只按 compose 标签解析。"""

    def setUp(self):
        from localservices import compose, dockerctx
        from localservices.manifest import Component, Project
        self.compose = compose
        self.tmp = tempfile.TemporaryDirectory(prefix="lsrvfix-cmp-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cwd = (root / "demo").resolve()
        self.cwd.mkdir()
        (self.cwd / "docker-compose.yml").write_text(
            "services: {web: {image: alpine:3.20}}\n")
        self.project = Project(id="demo", name="Demo", source=root,
                               components=[Component(id="web", run="compose",
                                                     compose_service="web", cwd="demo")])
        self.component = self.project.components[0]
        self.ps_rows: list[dict] = []
        self.docker_calls: list[list[str]] = []
        self.compose_calls: list[list[str]] = []
        for seam in (
            patch.object(compose, "_run", side_effect=self._docker),
            patch.object(compose, "_compose_run", side_effect=self._docker_compose),
            patch.object(dockerctx, "is_local", return_value=True),
            patch.object(dockerctx, "endpoint",
                         return_value="unix:///Users/x/.colima/docker.sock"),
        ):
            seam.start()
            self.addCleanup(seam.stop)

    def _docker(self, args, timeout=None):
        self.docker_calls.append(list(args))
        if args[0] == "ps":
            flt = args[args.index("--filter") + 1]
            if flt.startswith("label="):
                proj = flt.removeprefix("label=com.docker.compose.project=")
                rows = [r for r in self.ps_rows
                        if (r.get("Labels") or {}).get("com.docker.compose.project") == proj
                        and (r.get("Labels") or {}).get("com.docker.compose.service")
                        == self.component.compose_service]
            else:  # 名字过滤（修复后绝不应出现）
                want = flt.removeprefix("name=")
                rows = [r for r in self.ps_rows if r.get("Names") == want]
            if "{{.Names}}" in args:
                return ns(stdout="".join(r["Names"] + "\n" for r in rows))
            return ns(stdout="".join(json.dumps(r) + "\n" for r in rows))
        if args[0] == "container" and args[1] == "inspect":
            name = args[-1]
            row = next((r for r in self.ps_rows if r.get("Names") == name), None)
            if row is None:
                return ns(rc=1, stderr="no such container")
            return ns(stdout=json.dumps(row.get("_state", {"Running": False})))
        if args[0] == "start":
            row = next((r for r in self.ps_rows if r.get("Names") == args[1]), None)
            if row is not None:
                row["_state"] = {"Running": True}
            return ns()
        if args[0] == "logs":
            return ns(stdout="log-line\n")
        return ns(rc=1, stderr=f"unexpected {args}")

    def _docker_compose(self, args, timeout=None):
        self.compose_calls.append(list(args))
        if "up" in args:
            proj = args[args.index("-p") + 1]
            svc = args[-1]
            self.ps_rows.append({"Names": f"{proj}-{svc}-1",
                                 "Labels": {"com.docker.compose.project": proj,
                                            "com.docker.compose.service": svc},
                                 "_state": {"Running": True}})
        return ns()

    def _add_unlabeled(self, name="demo-web-1"):
        """无 compose 标签的停止容器（他人创建，只是名字巧合）。"""
        self.ps_rows.append({"Names": name, "Labels": "", "_state": {"Running": False}})

    def test_unlabeled_same_name_container_not_claimed(self):
        self._add_unlabeled()
        self.assertIsNone(self.compose.container_name(self.project, self.component))
        # 不再按名字过滤兜底
        self.assertFalse(any("--filter" in c and c[c.index("--filter") + 1].startswith("name=")
                             for c in self.docker_calls), "名字回退必须移除")
        # provision=existing：报未授权创建，绝不 docker start
        from localservices.compose import ComposeError
        with self.assertRaisesRegex(ComposeError, "未授权创建"):
            self.compose.ensure_started(self.project, self.component)
        self.assertFalse(any(c[0] == "start" for c in self.docker_calls),
                         "未知归属容器绝不 start")
        self.assertEqual(self.compose_calls, [])

    def test_labeled_match_still_resolves(self):
        self.ps_rows.append({"Names": "custom-web-x",
                             "Labels": {"com.docker.compose.project": "demo",
                                        "com.docker.compose.service": "web"},
                             "_state": {"Running": False}})
        self.assertEqual(self.compose.container_name(self.project, self.component),
                         "custom-web-x")
        res = self.compose.ensure_started(self.project, self.component)
        self.assertEqual(res["action"], "started")
        self.assertIn(["start", "custom-web-x"], self.docker_calls)

    def test_missing_with_provision_up_creates_labeled(self):
        self.component.provision = "up"
        res = self.compose.ensure_started(self.project, self.component)
        self.assertEqual(res["action"], "up")
        self.assertTrue(any(c[-1] == "web" and "up" in c for c in self.compose_calls))
        # up 后按标签解析到新建容器
        self.assertEqual(self.compose.container_name(self.project, self.component),
                         "demo-web-1")

    def test_stop_with_no_resolved_container_is_noop(self):
        self._add_unlabeled()  # 名字巧合的未知容器在场
        res = self.compose.stop_component(self.project, self.component)
        self.assertEqual(res, {"ok": True, "action": "missing", "container": None})
        self.assertEqual(self.compose_calls, [], "绝不停止无法证明归属的容器")
        # 状态与日志同样只认标签解析结果
        st = self.compose.component_state(self.project, self.component)
        self.assertEqual(st["state"], "missing")
        from localservices.compose import ComposeError
        with self.assertRaisesRegex(ComposeError, "容器不存在"):
            self.compose.logs(self.project, self.component)


# ================================================================ R10

class R10RestoreRebindTest(ReviewFixBase):
    """恢复到另一 Project 的双重归属（复核 R10）：TOML project 字段同事务重写。"""

    def test_restore_to_other_project_rebinds_toml(self):
        from localservices import manifest
        self.register(sid="mv1", port=18321, project="p1")
        self.reg.ensure_project("q", "Q", None, None)
        self.reg.remove_service("mv1", "keep-external", _SimpleCtl())
        res = self.reg.restore_service("mv1", {"project_id": "q"}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        text = (self.user_services / "mv1.toml").read_text(encoding="utf-8")
        self.assertIn('project = "q"', text)
        self.assertNotIn('project = "p1"', text)
        self.assertEqual(manifest.load_all()["mv1"].project, "q", "查询层归属 = 目标项目")
        self.assertIn("mv1", self.reg.project("q")["services"])
        self.assertNotIn("mv1", self.reg.project("p1")["services"])

    def test_restore_to_original_project_byte_stable(self):
        self.register(sid="st1", port=18322, project="p1")
        self.reg.remove_service("st1", "keep-external", _SimpleCtl())
        arch_toml = json.loads(self.archive_path("st1").read_text(encoding="utf-8"))["toml"]
        res = self.reg.restore_service("st1", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual((self.user_services / "st1.toml").read_bytes(),
                         arch_toml.encode("utf-8"), "恢复原项目路径字节不变")

    def test_restore_to_removed_target_recreates_and_rebinds(self):
        """目标项目已随整项目移除：恢复单服务时以归档重建并重绑定 TOML。"""
        from localservices import manifest
        self.register(sid="mv2", port=18323, project="p1")
        self.remove_project()
        res = self.reg.restore_service("mv2", {}, _SimpleCtl())
        self.assertTrue(res["ok"], res)
        self.assertEqual(manifest.load_all()["mv2"].project, "p1")


# ================================================================ R06 backend

class R06OverridePriorityTest(ReviewFixBase):
    """旧覆盖压制本次编辑（复核 R06 后台半）：本次 edited > 旧覆盖 > 模型候选。"""

    def test_second_edit_wins_over_stale_override(self):
        from localservices import manifest, overrides
        self.register(sid="ed1", port=18331, name="A", extra_svc={"edited": ["name"]})
        self.assertEqual(overrides.fields("ed1"), {"name": "A"})
        res = self.register(sid="ed1", port=18331, revision=1, name="B",
                            extra_svc={"edited": ["name"]})
        self.assertEqual(res["action"], "updated", res)
        self.assertEqual(manifest.load_all()["ed1"].name, "B", "本次编辑值必须生效")
        self.assertEqual(overrides.fields("ed1"), {"name": "B"}, "覆盖层更新为最新编辑值")

    def test_dry_run_reflects_edited_bypass(self):
        self.register(sid="ed2", port=18332, name="A", extra_svc={"edited": ["name"]})
        d = self.reg.register_service(self.payload(sid="ed2", port=18332, revision=1,
                                                   name="B",
                                                   extra_svc={"edited": ["name"]}),
                                      dry_run=True)
        self.assertEqual(d["action"], "diff", d)
        fmap = {x["field"]: x for x in d["diff"]}
        self.assertIn("name", fmap)
        self.assertEqual(fmap["name"]["new"], "B")
        self.assertEqual(fmap["name"]["old"], "A")

    def test_accept_new_still_clears(self):
        from localservices import manifest, overrides
        self.register(sid="ed3", port=18333, name="A", extra_svc={"edited": ["name"]})
        res = self.register(sid="ed3", port=18333, revision=1, name="C",
                            extra_svc={"accept_new": ["*"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(manifest.load_all()["ed3"].name, "C")
        self.assertEqual(overrides.fields("ed3"), {}, "accept_new 语义不变：清覆盖")

    def test_nested_ready_http_override_recorded_and_applied(self):
        from localservices import manifest, overrides
        path = "components[api].ready.http"
        self.register(sid="ed4", port=18334,
                      comps_over={"ready": {"http": "http://127.0.0.1:18334/ready"}},
                      extra_svc={"edited": [path]})
        self.assertEqual(overrides.fields("ed4"), {path: "http://127.0.0.1:18334/ready"})
        # 重新识别回候选值 /health：覆盖保护生效
        res = self.register(sid="ed4", port=18334, revision=1,
                            comps_over={"ready": {"http": "http://127.0.0.1:18334/health"}})
        self.assertEqual(res["action"], "unchanged", res)
        probe = manifest.load_all()["ed4"].component("api").ready
        self.assertEqual(probe.http, "http://127.0.0.1:18334/ready")

    def test_nested_env_override_recorded_and_applied(self):
        from localservices import manifest, overrides
        path = "components[api].env.DB_HOST"
        self.register(sid="ed5", port=18335,
                      comps_over={"env": {"DB_HOST": "pg.local"}},
                      extra_svc={"edited": [path]})
        self.assertEqual(overrides.fields("ed5"), {path: "pg.local"})
        res = self.register(sid="ed5", port=18335, revision=1,
                            comps_over={"env": {"DB_HOST": "localhost"}})
        self.assertEqual(res["action"], "unchanged", res)
        self.assertEqual(manifest.load_all()["ed5"].component("api").env["DB_HOST"],
                         "pg.local")

    def test_nested_override_pure_functions(self):
        """纯函数形状：GUI 与后台共用同一合并逻辑。"""
        from localservices import overrides
        definition = {"name": "N", "components": [
            {"id": "api", "env": {"A": "1"}, "ready": {"http": "http://x/health"}}]}
        out = overrides.apply_fields(definition, {
            "components[api].ready.http": "http://x/ready",
            "components[api].env.A": "2",
        })
        self.assertEqual(out["components"][0]["ready"]["http"], "http://x/ready")
        self.assertEqual(out["components"][0]["env"]["A"], "2")
        self.assertEqual(definition["components"][0]["ready"]["http"],
                         "http://x/health", "apply 不得改入参（副本语义）")
        self.assertEqual(
            overrides._get_path(out, "components[api].ready.http"), "http://x/ready")


if __name__ == "__main__":
    unittest.main()
