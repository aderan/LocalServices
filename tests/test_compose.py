"""compose.py（V2.7 运行适配）单元测试 + Controller 的 compose 分派集成测试。

所有 docker/docker-compose 子进程接缝全部 mock（compose._run / compose._compose_run /
dockerctx），绝不跑真实 docker；不绑定任何端口。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from localservices import compose, dockerctx, launchd, procs  # noqa: E402
from localservices.compose import ComposeError  # noqa: E402
from localservices.controller import Controller  # noqa: E402
from localservices.manifest import Component, Project  # noqa: E402

ORIG_COMPOSE_RUN = compose._compose_run  # 守卫测试要用未打桩的原实现


def ns(rc=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class ComposeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-compose-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = (self.root / "demo").resolve()  # comp_cwd 会 resolve，断言用同一路径
        self.cwd.mkdir()
        (self.cwd / "docker-compose.yml").write_text("services: {web: {image: alpine:3.20}}\n")
        (self.cwd / "docker-compose.override.yml").write_text("services: {web: {}}\n")
        self.project = Project(id="demo", name="Demo", source=self.root,
                               components=[Component(id="web", run="compose", compose_service="web",
                                                     cwd="demo")])
        self.component = self.project.components[0]
        self.docker_calls: list[list[str]] = []
        self.compose_calls: list[list[str]] = []
        # 容器表：label 过滤与 name 过滤共用的 ps -a 行
        self.ps_rows: list[dict] = []
        for seam in (
            patch.object(compose, "_run", side_effect=self._docker),
            patch.object(compose, "_compose_run", side_effect=self._docker_compose),
            patch.object(dockerctx, "is_local", return_value=True),
            patch.object(dockerctx, "endpoint", return_value="unix:///Users/x/.colima/docker.sock"),
        ):
            seam.start()
            self.addCleanup(seam.stop)

    # ---- fakes ----
    def _docker(self, args, timeout=compose.TIMEOUT):
        self.docker_calls.append(list(args))
        if args[0] == "ps":
            if "--filter" in args:
                flt = args[args.index("--filter") + 1]
                if flt.startswith("label="):
                    proj = flt.split("=", 1)[1].split(",", 1)[0].removeprefix("com.docker.compose.project=")
                    rows = [r for r in self.ps_rows
                            if (r.get("Labels") or {}).get("com.docker.compose.project") == proj]
                else:  # name= 过滤（回退路径）
                    want = flt.split("=", 1)[1]
                    rows = [r for r in self.ps_rows if r.get("Names") == want]
                if "{{.Names}}" in args:  # 纯名字格式（回退路径用）
                    return ns(stdout="".join(r["Names"] + "\n" for r in rows))
                return ns(stdout="".join(json.dumps(r) + "\n" for r in rows))
            return ns(stdout="")
        if args[0] == "container" and args[1] == "inspect":
            name = args[-1]
            row = next((r for r in self.ps_rows if r.get("Names") == name), None)
            if row is None:
                return ns(rc=1, stderr="no such container")
            return ns(stdout=json.dumps(row.get("_state", {"Running": row.get("State") == "running"})))
        if args[0] == "start":
            for r in self.ps_rows:
                if r.get("Names") == args[1]:
                    r["State"] = "running"
                    r["_state"] = {"Running": True}
            return ns()
        if args[0] == "logs":
            return ns(stdout="log-line-1\nlog-line-2\n")
        return ns(rc=1, stderr=f"unexpected {args}")

    def _docker_compose(self, args, timeout=compose.COMPOSE_TIMEOUT):
        self.compose_calls.append(list(args))
        if "up" in args:
            svc = args[-1]
            proj = args[args.index("-p") + 1]
            self.ps_rows.append({"Names": f"{proj}-{svc}-1", "State": "running",
                                 "Labels": {"com.docker.compose.project": proj,
                                            "com.docker.compose.service": svc},
                                 "_state": {"Running": True}})
        elif "stop" in args:
            svc = args[-1]
            for r in self.ps_rows:
                if (r.get("Labels") or {}).get("com.docker.compose.service") == svc:
                    r["State"] = "exited"
                    r["_state"] = {"Running": False}
        return ns()

    def add_container(self, name, service="web", project="demo", state="running", health=None):
        row = {"Names": name, "State": state,
               "Labels": {"com.docker.compose.project": project,
                          "com.docker.compose.service": service}}
        if health is not None:
            row["_state"] = {"Running": state == "running", "Health": {"Status": health}}
        self.ps_rows.append(row)
        return row


class EnsureStartedTest(ComposeBase):
    def test_existing_stopped_container_is_started_via_docker_start(self):
        self.add_container("demo-web-1", state="exited")
        result = compose.ensure_started(self.project, self.component)
        self.assertEqual(result["action"], "started")
        self.assertEqual(result["container"], "demo-web-1")
        self.assertIn(["start", "demo-web-1"], self.docker_calls)
        self.assertEqual(self.compose_calls, [])  # 已有容器：绝不 compose up

    def test_already_running_container_is_noop(self):
        self.add_container("demo-web-1", state="running")
        result = compose.ensure_started(self.project, self.component)
        self.assertEqual(result["action"], "already-running")
        self.assertNotIn(["start", "demo-web-1"], self.docker_calls)
        self.assertEqual(self.compose_calls, [])

    def test_missing_container_with_provision_existing_refuses(self):
        with self.assertRaisesRegex(ComposeError, "容器不存在且未授权创建（provision=existing）"):
            compose.ensure_started(self.project, self.component)
        self.assertEqual(self.compose_calls, [])

    def test_missing_container_with_provision_up_runs_up_no_deps(self):
        self.component.provision = "up"
        self.component.compose_files = ["docker-compose.yml", "docker-compose.override.yml"]
        result = compose.ensure_started(self.project, self.component)
        self.assertEqual(result["action"], "up")
        # argv 精确断言：-p <proj> -f … up -d --no-deps <svc>，绝无 down/rm
        self.assertEqual(self.compose_calls, [[
            "-p", "demo",
            "-f", str(self.cwd / "docker-compose.yml"),
            "-f", str(self.cwd / "docker-compose.override.yml"),
            "up", "-d", "--no-deps", "web",
        ]])
        self.assertEqual(result["container"], "demo-web-1")

    def test_remote_engine_actions_refuse(self):
        with patch.object(dockerctx, "is_local", return_value=False), \
                patch.object(dockerctx, "endpoint", return_value="tcp://far:2375"), \
                patch.object(dockerctx, "name", return_value="remote-ctx"):
            for fn in (compose.ensure_started, compose.stop_component):
                with self.assertRaisesRegex(ComposeError, "远程 engine 不自动操作"):
                    fn(self.project, self.component)
        self.assertEqual(self.docker_calls, [])
        self.assertEqual(self.compose_calls, [])


class StopComponentTest(ComposeBase):
    def test_running_container_stopped_via_compose_stop_never_down(self):
        self.add_container("demo-web-1", state="running")
        result = compose.stop_component(self.project, self.component)
        self.assertEqual(result["action"], "stopped")
        self.assertEqual(self.compose_calls, [[
            "-p", "demo",
            "-f", str(self.cwd / "docker-compose.yml"),
            "-f", str(self.cwd / "docker-compose.override.yml"),  # override 在基础文件之后
            "stop", "web",
        ]])
        for forbidden in ("down", "rm", "kill", "-v", "--volumes"):
            self.assertNotIn(forbidden, self.compose_calls[0])

    def test_missing_container_is_noop(self):
        result = compose.stop_component(self.project, self.component)
        self.assertEqual(result, {"ok": True, "action": "missing", "container": None})
        self.assertEqual(self.compose_calls, [])

    def test_already_stopped_container_needs_nothing(self):
        self.add_container("demo-web-1", state="exited")
        result = compose.stop_component(self.project, self.component)
        self.assertEqual(result["action"], "already-stopped")
        self.assertEqual(self.compose_calls, [])

    def test_adapter_guard_refuses_destructive_verbs(self):
        for verb in (["-p", "demo", "down"], ["-p", "demo", "rm", "-f"]):
            with self.assertRaisesRegex(ComposeError, "绝不执行"):
                ORIG_COMPOSE_RUN(verb)


class ResolutionTest(ComposeBase):
    def test_container_name_via_labels_then_fallback(self):
        self.add_container("custom-db-1", service="db", project="demo")
        self.component.compose_service = "db"
        self.assertEqual(compose.container_name(self.project, self.component), "custom-db-1")
        # 无标签匹配时回退 <project>-<service>-1 字面名
        self.component.compose_service = "web"
        self.add_container("demo-web-1", service="other", project="other")
        self.assertEqual(compose.container_name(self.project, self.component), "demo-web-1")
        # 完全没有 → None
        self.ps_rows.clear()
        self.assertIsNone(compose.container_name(self.project, self.component))

    def test_compose_project_defaults_to_cwd_dirname(self):
        self.assertEqual(compose.project_name(self.project, self.component), "demo")
        self.component.compose_project = "explicit"
        self.assertEqual(compose.project_name(self.project, self.component), "explicit")

    def test_compose_files_autodetect_and_missing(self):
        files = compose.compose_files(self.project, self.component)
        self.assertEqual(files, [self.cwd / "docker-compose.yml", self.cwd / "docker-compose.override.yml"])
        empty = Project(id="e", name="E", source=self.root / "e", components=[])
        (self.root / "e").mkdir()
        with self.assertRaisesRegex(ComposeError, "找不到 compose 文件"):
            compose.compose_files(empty, Component(id="w", run="compose", compose_service="w"))
        self.component.compose_files = ["nope.yml"]
        with self.assertRaisesRegex(ComposeError, "compose 文件不存在"):
            compose.compose_files(self.project, self.component)


class StateAndLogsTest(ComposeBase):
    def test_component_state_mapping(self):
        self.assertIsNone(compose.component_state(self.project, self.component)["container"])
        self.assertEqual(compose.component_state(self.project, self.component)["state"], "missing")
        self.add_container("demo-web-1", state="exited")
        st = compose.component_state(self.project, self.component)
        self.assertEqual((st["state"], st["health"], st["container"]), ("exited", None, "demo-web-1"))
        self.ps_rows.clear()
        self.add_container("demo-web-1", state="running", health="starting")
        st = compose.component_state(self.project, self.component)
        self.assertEqual((st["state"], st["health"]), ("running", "starting"))

    def test_logs_uses_docker_logs_tail(self):
        self.add_container("demo-web-1")
        out = compose.logs(self.project, self.component, lines=50)
        self.assertIn("log-line-1", out)
        self.assertIn(["logs", "--tail", "50", "demo-web-1"], self.docker_calls)
        self.ps_rows.clear()
        with self.assertRaisesRegex(ComposeError, "容器不存在"):
            compose.logs(self.project, self.component)


# ---------------- Controller 集成：local + compose 混排 ----------------

class ControllerComposeTest(unittest.TestCase):
    """start/stop 顺序、失败回滚（停 compose 组件）、状态映射、logs 路由（compose 函数全 mock）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-ctl-compose-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.defs = self.root / "services"
        self.defs.mkdir()
        self.src = self.root / "src"
        self.src.mkdir()
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "runtime"),
            "LOCALSERVICES_SERVICES": str(self.defs),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-compose-test",
        })
        env.start()
        self.addCleanup(env.stop)
        (self.defs / "mixed.toml").write_text(
            f'id="mixed"\nname="Mixed"\nsource="{self.src}"\n'
            '[[components]]\nid="db"\nrun="compose"\ncompose_service="db"\n'
            'compose_project="mixeddb"\n'
            '[[components]]\nid="web"\ncmd=["true"]\nafter=["db"]\nport=49001\n'
            'ready={http="http://127.0.0.1:49001/"}\n'
        )
        self.ctl = Controller()
        self.addCleanup(self.ctl._pool.shutdown, wait=True)
        self.p = self.ctl.projects["mixed"]
        self.order: list[str] = []
        self.compose_state = {"state": "running", "health": "healthy", "container": "mixeddb-db-1"}
        self.state = {"loaded": False, "running": False}

        def job_info(label):
            return launchd.JobInfo(loaded=self.state["loaded"],
                                   running=self.state["loaded"] and self.state["running"],
                                   pid=42 if self.state["loaded"] and self.state["running"] else None,
                                   last_exit=None if self.state["running"] else "0")

        def bootstrap(label):
            self.order.append("bootstrap:web")
            self.state["loaded"] = True
            self.state["running"] = True
            return self.bootstrap_ok, ""

        def bootout(label):
            self.order.append("bootout:web")
            self.state["loaded"] = False
            self.state["running"] = False
            return True, ""

        def ensure_started(p, c):
            self.order.append(f"compose-up:{c.id}")
            return {"ok": True, "action": "started", "container": "mixeddb-db-1"}

        def stop_component(p, c):
            self.order.append(f"compose-stop:{c.id}")
            return {"ok": True, "action": "stopped", "container": "mixeddb-db-1"}

        def component_state(p, c):
            return dict(self.compose_state)

        def compose_logs(p, c, lines=200):
            return "container-log-line"

        self.bootstrap_ok = True
        boundaries = [
            patch.object(launchd, "info", side_effect=job_info),
            patch.object(launchd, "bootstrap", side_effect=bootstrap),
            patch.object(launchd, "bootout", side_effect=bootout),
            patch.object(compose, "ensure_started", side_effect=ensure_started),
            patch.object(compose, "stop_component", side_effect=stop_component),
            patch.object(compose, "component_state", side_effect=component_state),
            patch.object(compose, "logs", side_effect=compose_logs),
            patch.object(procs, "snapshot", return_value={}),
            patch.object(procs, "listeners", return_value={}),
            patch.object(procs, "all_cwds", return_value={}),
            patch.object(procs, "probe", return_value=(True, "")),
        ]
        for boundary in boundaries:
            boundary.start()
            self.addCleanup(boundary.stop)

    def wait_finished(self, timeout=3):
        end = time.monotonic() + timeout
        while self.ctl.ops and time.monotonic() < end:
            time.sleep(0.01)
        return not self.ctl.ops

    def test_start_orders_compose_before_dependent_local_and_reports_ready(self):
        self.assertTrue(self.ctl.start("mixed")["accepted"])
        self.assertTrue(self.wait_finished(), self.ctl.errors)
        self.assertEqual(self.order[:2], ["compose-up:db", "bootstrap:web"])
        st = self.ctl.project_status(self.p)
        self.assertEqual(st["state"], "available", st)
        db = next(c for c in st["components"] if c["id"] == "db")
        self.assertEqual(db["run"], "compose")
        self.assertEqual(db["state"], "ready")
        self.assertEqual(db["container"], "mixeddb-db-1")
        self.assertFalse(db["probe_used"])  # healthcheck 已覆盖就绪，无需端口探针
        self.assertIsNone(db["pid"])  # compose 组件从无 launchd 身份

    def test_external_running_container_is_ready_with_note(self):
        st = self.ctl.project_status(self.p)  # 从未 start：容器“已在运行”
        db = next(c for c in st["components"] if c["id"] == "db")
        self.assertEqual(db["state"], "ready")
        self.assertIn("容器已在运行", db["detail"])
        self.assertIn("无需接管", db["detail"])

    def test_compose_readiness_falls_back_to_probe_without_health(self):
        self.compose_state.update(health=None)
        self.ctl.start("mixed")
        self.assertTrue(self.wait_finished())
        st = self.ctl.project_status(self.p)
        db = next(c for c in st["components"] if c["id"] == "db")
        self.assertEqual(db["state"], "ready")
        # _wait_compose_ready 走 procs.probe（web 组件 db 无 ready → Running 即就绪）；
        # 状态层有 ready 探针则复用 _ready。此组件未声明 ready → running 即就绪。
        self.assertEqual(db["label"], "就绪")

    def test_failure_rollback_stops_compose_component_in_reverse(self):
        self.bootstrap_ok = False  # web 的 launchd bootstrap 失败 → 回滚
        self.assertTrue(self.ctl.start("mixed")["accepted"])
        self.assertTrue(self.wait_finished())
        self.assertEqual(self.order, ["compose-up:db", "bootstrap:web", "bootout:web", "compose-stop:db"])
        self.assertIn("mixed", self.ctl.errors)
        self.assertNotIn("mixed", self.ctl.ops)
        self.assertNotIn("mixed.db", self.ctl._compose_started)  # 回滚后不再视为本会话带起

    def test_stop_handles_both_kinds(self):
        self.ctl.start("mixed")
        self.assertTrue(self.wait_finished())
        self.assertTrue(self.ctl.stop("mixed")["accepted"])
        self.assertTrue(self.wait_finished())
        self.assertIn("compose-stop:db", self.order)
        self.assertIn("bootout:web", self.order)
        self.assertNotIn("mixed.db", self.ctl._compose_started)  # stop 后清掉本会话带起记录
        # 下一刷新周期看到容器已退出（模拟 compose stop 生效）
        self.compose_state.update(state="exited", health=None)
        self.ctl._compose_state_cache.clear()
        st = self.ctl.project_status(self.p)
        db = next(c for c in st["components"] if c["id"] == "db")
        self.assertEqual(db["state"], "stopped")

    def test_logs_routes_compose_components_to_docker_logs(self):
        out = self.ctl.logs("mixed", None)["logs"]
        self.assertIn("container-log-line", out["db"])
        self.assertEqual(out["web"], "")  # 本地组件走文件 tail（无日志文件 → 空串）
        only = self.ctl.logs("mixed", "db")["logs"]
        self.assertEqual(list(only), ["db"])  # 指定组件时只返回该组件

    def test_compose_container_already_running_is_not_rolled_back(self):
        # 容器已运行（ensure 返回 already-running）→ 失败回滚不得停它
        with patch.object(compose, "ensure_started",
                          return_value={"ok": True, "action": "already-running",
                                        "container": "mixeddb-db-1"}):
            self.bootstrap_ok = False
            self.ctl.start("mixed")
            self.assertTrue(self.wait_finished())
        self.assertNotIn("compose-stop:db", self.order)
        self.assertNotIn("mixed.db", self.ctl._compose_started)


if __name__ == "__main__":
    unittest.main()
