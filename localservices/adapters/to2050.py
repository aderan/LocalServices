"""To2050 research workbench: API + real CoreExecutor worker (job_adapter) + web.

Worker readiness = fresh worker_lease heartbeat in artifacts/index.sqlite (read-only).
Busy = any v2 job queued/running.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .. import procs
from . import Adapter as Base

LEASE_SECONDS = 30


class Adapter(Base):
    def _api(self) -> str:
        return self.project.options.get("api", "http://127.0.0.1:8002")

    def component_health(self, comp):
        if comp.id != "worker":
            return None
        db = self.project.source / self.project.options.get("artifact_root", "artifacts") / "index.sqlite"
        if not db.exists():
            return False, "任务库尚未创建（worker 未报到）"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute("SELECT worker_id, heartbeat_at FROM worker_lease WHERE id=1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            return False, f"读取 worker 租约失败：{e}"
        if not row:
            return False, "worker 尚未持有租约"
        try:
            hb = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
        except ValueError:
            return False, f"租约心跳无法解析：{row[1]}"
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        if age > LEASE_SECONDS:
            return False, f"worker 心跳已过期 {int(age)}s"
        return True, ""

    def busy(self) -> str | None:
        code, body, err = procs.http_get(self._api() + "/api/v2/jobs?limit=100", 5)
        if code == 0:
            return None
        if code != 200:
            return f"无法确认作业状态（{err}），暂缓更新"
        try:
            data = json.loads(body)
            jobs = data.get("jobs", data.get("items", [])) if isinstance(data, dict) else data
        except ValueError:
            return "作业状态无法解析，暂缓更新"
        active = [j for j in jobs if isinstance(j, dict) and j.get("status") in ("queued", "running")]
        return f"{len(active)} 个研究作业排队/运行中" if active else None
