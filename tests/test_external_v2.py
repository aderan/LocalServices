"""外部实例识别增强（V2.8）：LAUNCHER_RE 监督父链攀爬 + docker 宿主代理端口归属。

ps/listeners/cwds/launchd/dockerctx 全部 mock，不跑任何真实命令。
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

from localservices import compose, dockerctx, launchd, procs  # noqa: E402
from localservices.controller import Controller, LAUNCHER_RE  # noqa: E402


class ExternalV2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ls-ext-v2-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.defs = self.root / "services"
        self.defs.mkdir()
        self.src = self.root / "climb"
        (self.src / "app").mkdir(parents=True)
        env = patch.dict(os.environ, {
            "LOCALSERVICES_HOME": str(self.root / "runtime"),
            "LOCALSERVICES_SERVICES": str(self.defs),
            "LOCALSERVICES_LABEL_PREFIX": "com.mybrain.ls-ext-test",
        })
        env.start()
        self.addCleanup(env.stop)
        (self.defs / "climb.toml").write_text(
            f'id="climb"\nname="Climb"\nsource="{self.src}"\n'
            '[[components]]\nid="web"\ncmd=["true"]\nport=18250\n'
            'ready={http="http://127.0.0.1:18250/"}\n'
        )
        for boundary in (
            patch.object(launchd, "info", return_value=launchd.JobInfo()),
            patch.object(procs, "snapshot", return_value={}),
            patch.object(procs, "listeners", return_value={}),
            patch.object(procs, "all_cwds", return_value={}),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)
        self.ctl = Controller()
        self.addCleanup(self.ctl._pool.shutdown, wait=True)

    def use_ps(self, ps, listeners, cwds):
        stop1 = patch.object(procs, "snapshot", return_value=ps)
        stop2 = patch.object(procs, "listeners", return_value=listeners)
        stop3 = patch.object(procs, "all_cwds", return_value=cwds)
        for s in (stop1, stop2, stop3):
            s.start()
            self.addCleanup(s.stop)

    def docker_ps_publish(self, names_by_port):
        def fake_run(args, timeout=dockerctx.TIMEOUT, **kw):
            if args and args[0] == "ps" and "--filter" in args:
                flt = args[args.index("--filter") + 1]
                if flt.startswith("publish="):
                    port = int(flt.split("=", 1)[1])
                    names = names_by_port.get(port, [])
                    rows = [{"Names": n} for n in names]
                    return SimpleNamespace(returncode=0,
                                           stdout="".join(json.dumps(r) + "\n" for r in rows), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        p = patch.object(dockerctx, "run", side_effect=fake_run)
        p.start()
        self.addCleanup(p.stop)


class LauncherClimbTest(ExternalV2Base):
    def test_climbs_uvicorn_reloader_chain_to_make(self):
        src = str(self.src.resolve())
        ps = {
            200: procs.Proc(200, 210, 0, "/usr/local/bin/python3 -m uvicorn app.main:app --reload --port 18250"),
            210: procs.Proc(210, 220, 0, "python3.12 -m uvicorn app.main:app --reload --port 18250"),
            220: procs.Proc(220, 1, 0, "make dev"),
        }
        self.use_ps(ps, {18250: 200}, {p: src for p in ps})
        ext = self.ctl.external_instances(self.ctl.projects["climb"], ps)
        self.assertEqual(len(ext), 1, ext)
        self.assertEqual(ext[0]["pid"], 220)  # 攀爬到监督根 make
        self.assertTrue(ext[0]["same_project"])
        self.assertEqual(ext[0]["ports"], [18250])

    def test_climbs_go_wrapper_even_when_server_binary_is_unnamed(self):
        src = str(self.src.resolve())
        ps = {
            300: procs.Proc(300, 301, 0, "/private/tmp/go-build999/b001/exe/api --port 18250"),
            301: procs.Proc(301, 1, 0, "go run ./cmd/api"),
        }
        self.use_ps(ps, {18250: 300}, {p: src for p in ps})
        ext = self.ctl.external_instances(self.ctl.projects["climb"], ps)
        self.assertEqual([e["pid"] for e in ext], [301])

    def test_climb_stops_at_non_launcher_parent(self):
        src = str(self.src.resolve())
        ps = {
            200: procs.Proc(200, 210, 0, "node server.js --port 18250"),
            210: procs.Proc(210, 1, 0, "/Applications/IDE.app/Contents/MacOS/idea"),  # 不匹配 launcher
        }
        self.use_ps(ps, {18250: 200}, {p: src for p in ps})
        ext = self.ctl.external_instances(self.ctl.projects["climb"], ps)
        self.assertEqual([e["pid"] for e in ext], [200])

    def test_regex_matches_new_launcher_families(self):
        for cmd in ("uv run --directory /x uvicorn app:app", "air -c .air.toml",
                    "just dev", "task watch", "python -m http.server 18250",
                    "/opt/homebrew/bin/python3.13 -m uvicorn x", "make dev"):
            self.assertTrue(LAUNCHER_RE.search(cmd), cmd)
        for cmd in ("google-chrome https://x", "taskwarrior list", "makefile", "goland"):
            self.assertFalse(LAUNCHER_RE.search(cmd), cmd)


class DockerProxyAttributionTest(ExternalV2Base):
    def setUp(self):
        super().setUp()
        self.proxy_ps = {400: procs.Proc(400, 1, 0,
                                         "/usr/local/bin/docker-proxy -proto tcp -host-port 18250")}
        self.use_ps(self.proxy_ps, {18250: 400}, {})

    def test_unknown_container_is_named_not_a_process_conflict(self):
        self.docker_ps_publish({18250: ["unknown-web"]})
        p = self.ctl.projects["climb"]
        ext = self.ctl.external_instances(p)
        self.assertEqual(len(ext), 1, ext)
        e = ext[0]
        self.assertEqual(e["kind"], "docker-container")
        self.assertEqual(e["container"], "unknown-web")
        self.assertIsNone(e["managed_project"])
        self.assertEqual(e["tree"], [])  # 绝不把代理进程树当作可接管/可杀对象
        conflicts = self.ctl.conflicts(p)
        self.assertEqual(conflicts, ["web：端口 18250 由容器 unknown-web 发布占用（未登记）"])
        self.assertNotIn("非本工具进程", "".join(conflicts))

    def test_dependency_container_running_is_no_external_instance(self):
        from localservices.manifest import Dependency, Probe
        p = self.ctl.projects["climb"]
        p.dependencies.append(Dependency("Postgres", Probe(tcp="127.0.0.1:5434"),
                                         container="dep-pg", volume="pgdata", required=False))
        self.docker_ps_publish({18250: ["dep-pg"]})
        self.assertEqual(self.ctl.external_instances(p), [])
        self.assertEqual(self.ctl.conflicts(p), [])

    def test_own_compose_container_running_is_no_external_instance(self):
        # 组件改声明为 run=compose：自己的容器在运行 → 非外部实例、非冲突（up 幂等）
        p = self.ctl.projects["climb"]
        c = p.component("web")
        c.run, c.compose_service, c.compose_project = "compose", "web", "climb"
        with patch.object(compose, "container_name", return_value="climb-web-1"):
            self.docker_ps_publish({18250: ["climb-web-1"]})
            self.assertEqual(self.ctl.external_instances(p), [])
            self.assertEqual(self.ctl.conflicts(p), [])

    def test_other_registered_project_container_gets_managed_attribution(self):
        (self.defs / "other.toml").write_text(
            f'id="other"\nname="Other"\nsource="{self.root / "other-src"}"\n'
            '[[components]]\nid="svc"\nrun="compose"\ncompose_service="svc"\ncompose_project="othersvc"\n'
        )
        (self.root / "other-src").mkdir()
        self.ctl.reload()
        other = self.ctl.projects["other"]

        def container_name(p, c):
            return "othersvc-svc-1" if p.id == "other" else None

        with patch.object(compose, "container_name", side_effect=container_name):
            self.docker_ps_publish({18250: ["othersvc-svc-1"]})
            ext = self.ctl.external_instances(self.ctl.projects["climb"])
            self.assertEqual(len(ext), 1, ext)
            self.assertEqual(ext[0]["managed_project"], "other")
            conflicts = self.ctl.conflicts(self.ctl.projects["climb"])
            self.assertEqual(conflicts, ["web：端口被项目 other 的容器 othersvc-svc-1 占用"])

    def test_unresolvable_docker_keeps_process_behavior(self):
        # dockerctx 抛错/非零 → 回退普通进程冲突语义
        with patch.object(dockerctx, "run", side_effect=OSError("docker down")):
            p = self.ctl.projects["climb"]
            ext = self.ctl.external_instances(p)
            self.assertEqual(len(ext), 1)
            self.assertNotIn("kind", ext[0])  # 普通进程记录
            self.assertIn("非本工具进程", self.ctl.conflicts(p)[0])


if __name__ == "__main__":
    unittest.main()
