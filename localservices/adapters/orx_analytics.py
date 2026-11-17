"""orx-analytics: observe only. Reads the engine's read-only `health` op over its unix socket.

Never sends refresh.request (would hold a demand lease), never runs `watch start/stop`,
never signals the engine or legacy watcher.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import Adapter as Base


class Adapter(Base):
    def _home(self) -> Path:
        return Path(os.path.expanduser(os.environ.get("ORX_ANALYTICS_HOME") or self.project.options.get("data_dir", "~/.orx-analytics")))

    def _app(self) -> str:
        return self.project.options.get("app", "/Applications/ORX Analytics (Internal Preview).app")

    _cache: tuple[float, dict] = (0.0, {})
    _probing = False

    def observe(self) -> dict:
        """Engine health can take several seconds; probe in the background, serve the last result."""
        t, res = self._cache
        if time.time() - t >= 15 and not self._probing:
            self._probing = True

            def run():
                try:
                    self._cache = (time.time(), self._observe())
                finally:
                    self._probing = False
            threading.Thread(target=run, daemon=True).start()
        return res or {"state": "unknown", "label": "未知", "detail": "首次探测中"}

    def _health(self) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(8)  # engine answers health in ~0.5–3s while it is importing snapshots
        try:
            s.connect(str(self._home() / "engine.sock"))
            s.sendall(b'{"op":"health","protocol_version":1}\n')
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 4 * 1024 * 1024:
                d = s.recv(65536)
                if not d:
                    break
                buf += d
        finally:
            s.close()
        return json.loads(buf)

    def _lock_pid(self) -> int | None:
        try:
            return int(json.loads((self._home() / "engine.lock").read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)  # signal 0: existence check only, nothing is delivered
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def _app_running(self) -> bool:
        r = subprocess.run(["pgrep", "-f", "ORXAnalyticsApp"], capture_output=True)
        return r.returncode == 0

    def _observe(self) -> dict:
        actions = [{"id": "open-app", "label": "打开 App"}, {"id": "open-log", "label": "引擎日志"}]
        app = "App 运行中" if self._app_running() else "App 未运行"
        lock = self._lock_pid()
        try:
            h = self._health()
        except FileNotFoundError:
            return {"state": "stopped", "label": "引擎未运行", "detail": f"无 engine.sock；{app}", "actions": actions}
        except TimeoutError:
            if lock and self._alive(lock):
                return {"state": "available", "label": "观察中（引擎繁忙）",
                        "detail": f"引擎 pid {lock} 存活，health 查询超时（正在导入快照）；{app} · 外部管理",
                        "actions": actions}
            return {"state": "failed", "label": "引擎无响应", "detail": f"health 超时且 engine.lock 进程不存活；{app}",
                    "actions": actions}
        except (OSError, ValueError) as e:
            return {"state": "failed", "label": "引擎无响应", "detail": f"{e}；{app}", "actions": actions}
        if not h.get("ok", True) and h.get("error"):
            return {"state": "failed", "label": "引擎报错", "detail": str(h["error"]), "actions": actions}
        eng = h.get("engine", {})
        tier = eng.get("tier", {})
        demand = (eng.get("clients") or {}).get("demand") or {}
        holders = ", ".join(f"{k}×{len(v)}" for k, v in demand.items() if v) or "无"
        detail = (f"引擎 pid {eng.get('pid')} · 档位 {tier.get('current')}（{tier.get('interval_sec')}s）"
                  f" · 活动 run {tier.get('active_runs')} · 需求租约 {holders} · {app} · 外部管理")
        return {"state": "available", "label": "观察中", "detail": detail, "actions": actions,
                "components": [{"id": "engine", "name": "协调引擎", "kind": "external", "state": "ready",
                                "label": "运行中", "detail": detail, "pid": eng.get("pid"),
                                "started_at": None}]}

    def log_paths(self) -> dict[str, Path]:
        return {"engine": self._home() / "logs" / "engine-launch.log"}

    def action(self, name: str) -> tuple[bool, str]:
        if name == "open-app":
            result = subprocess.run(["/usr/bin/open", self._app()], capture_output=True, text=True, timeout=10)
            if result.returncode:
                return False, result.stderr.strip() or "无法打开 App"
            return True, "已请求打开 App，等待引擎健康检查"
        if name == "open-log":
            log = self.log_paths()["engine"]
            if not log.exists():
                return False, "日志不存在"
            subprocess.Popen(["/usr/bin/open", "-a", "Console", str(log)])
            return True, "已打开日志"
        return super().action(name)
