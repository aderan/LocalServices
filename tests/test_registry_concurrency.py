"""Registry 并发测试：两个子进程（spawn，各自 Registry 实例）经 Barrier 同步后同时注册。

验证 flock + 锁内重读合并：不同服务并发 → 两条都成功、revision 无重复无丢失、文件无损坏；
同 id 并发 → 恰好一条 created，另一条 unchanged，不产生两份文件。
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PY = sys.executable


def _worker(q, barrier, env, payload):
    try:
        os.environ.update(env)
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from localservices.registry import Registry
        barrier.wait(timeout=60)
        res = Registry().register_service(payload)
        q.put({"ok": bool(res.get("ok")), "action": res.get("action"), "error": res.get("error")})
    except Exception as e:  # noqa: BLE001
        q.put({"ok": False, "action": None, "error": f"{type(e).__name__}: {e}"})


class ConcurrencyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lsconc-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.services = self.root / "services"
        self.user_services = self.root / "user-services"
        self.src = self.root / "src"
        for d in (self.home, self.services, self.user_services, self.src):
            d.mkdir(parents=True)
        self.env = {
            "LOCALSERVICES_HOME": str(self.home),
            "LOCALSERVICES_SERVICES": str(self.services),
            "LOCALSERVICES_USER_SERVICES": str(self.user_services),
        }
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.ctx = mp.get_context("spawn")

    def payload(self, pid, sid, port):
        return {
            "schemaVersion": 1, "revision": 0,
            "project": {"id": pid, "name": pid.title()},
            "service": {
                "id": sid, "name": sid, "source": str(self.src),
                "components": [{"id": "web", "cmd": [PY, "-m", "http.server", str(port)],
                                "port": port, "ready": {"http": f"http://127.0.0.1:{port}/"}}],
            },
        }

    def run_two(self, payloads):
        q = self.ctx.Queue()
        barrier = self.ctx.Barrier(2)
        procs = [self.ctx.Process(target=_worker, args=(q, barrier, self.env, p)) for p in payloads]
        for p in procs:
            p.start()
        results = [q.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(timeout=120)
            self.assertEqual(p.exitcode, 0)
        return results


class DifferentServicesTest(ConcurrencyBase):
    def test_both_succeed_no_lost_revision(self):
        results = self.run_two([self.payload("proc-a", "svc-a", 47901),
                                self.payload("proc-b", "svc-b", 47902)])
        for r in results:
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["action"], "created")
        data = json.loads((self.home / "registry.json").read_text())
        self.assertEqual(data["revision"], 2)  # 两次递增，无丢失、无重复
        self.assertEqual(data["schemaVersion"], 1)
        by_id = {p["id"]: p for p in data["projects"]}
        self.assertEqual(set(by_id), {"proc-a", "proc-b"})
        self.assertEqual(by_id["proc-a"]["services"], ["svc-a"])
        self.assertEqual(by_id["proc-b"]["services"], ["svc-b"])
        self.assertEqual(sorted(f.name for f in self.user_services.glob("*.toml")),
                         ["svc-a.toml", "svc-b.toml"])
        from localservices import manifest
        self.assertEqual(set(manifest.load_all()), {"svc-a", "svc-b"})  # 两份 TOML 完好可加载


class SameIdTest(ConcurrencyBase):
    def test_exactly_one_created(self):
        payload = self.payload("same", "svc-x", 47905)
        results = self.run_two([payload, dict(payload)])
        actions = sorted(r["action"] or "" for r in results)
        self.assertEqual(actions, ["created", "unchanged"], results)  # 恰好一条 created
        self.assertEqual([f.name for f in self.user_services.glob("*.toml")], ["svc-x.toml"])
        data = json.loads((self.home / "registry.json").read_text())
        self.assertEqual(data["revision"], 1)
        self.assertEqual(data["projects"][0]["services"], ["svc-x"])


if __name__ == "__main__":
    unittest.main()
