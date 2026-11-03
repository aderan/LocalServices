"""MediaScope: dashboard restart is deferred while persistent tasks or live recordings are active.

Probes only local SQLite-backed endpoints (/api/tasks, /api/live/overview); never /api/health
(it calls LLM / sidecars) or /api/live/stream (may hit Douyin).
"""

from __future__ import annotations

import json

from .. import procs
from . import Adapter as Base

ACTIVE = {"pending", "running", "waiting"}

# /api/tasks 无过滤参数，返回全量任务史（2026-11 实测 464 条 ≈ 500KB，只增不减），
# 默认 256KB 上限会截断成 200+空 body，被误报成“任务状态无法解析”并永久卡住更新
PROBE_MAX_BYTES = 8 * 1024 * 1024


class Adapter(Base):
    def _base(self) -> str:
        return self.project.open.rstrip("/")

    def busy(self) -> str | None:
        reasons = []
        code, body, err = procs.http_get(self._base() + "/api/tasks", 5, max_bytes=PROBE_MAX_BYTES)
        if code == 0:
            return None  # dashboard down: nothing of ours to interrupt
        if code != 200:
            return f"无法确认任务状态（/api/tasks {err}），暂缓更新"
        if not body:
            return f"任务状态响应异常（{err or '空响应'}），暂缓更新"
        try:
            data = json.loads(body)
            tasks = data.get("tasks", data) if isinstance(data, dict) else data
            active = [t for t in (tasks or []) if isinstance(t, dict) and t.get("status") in ACTIVE]
        except (ValueError, TypeError):
            return "任务状态无法解析，暂缓更新"
        if active:
            reasons.append(f"{len(active)} 个任务进行中")
        code, body, _ = procs.http_get(self._base() + "/api/live/overview", 5, max_bytes=PROBE_MAX_BYTES)
        if code == 200:
            try:
                ov = json.loads(body)
                rooms = ov.get("rooms", []) if isinstance(ov, dict) else []
                rec = [r for r in rooms if isinstance(r, dict)
                       and (r.get("phase") in ("starting", "recording", "finishing") or r.get("finishing"))]
                if rec:
                    reasons.append(f"{len(rec)} 路直播录制中")
            except ValueError:
                pass
        return "，".join(reasons) or None
