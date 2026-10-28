"""Avoid service operations during an active capture; polling never starts work."""
import json

from . import Adapter as Base
from .. import procs


class Adapter(Base):
    def busy(self):
        base = self.project.options.get("api", "http://127.0.0.1:8787")
        code, body, err = procs.http_get(base + "/api/capture-tasks", 2, max_bytes=8 * 1024 * 1024)
        if code != 200 or err:
            # A stopped API cannot have an in-process capture; a live but unreadable
            # API requires investigation before terminating it.
            probe = self.project.component("api").ready
            if probe and procs.probe(probe, 2)[0]:
                return "无法读取采集任务状态"
            return None
        try:
            tasks = json.loads(body)["tasks"]
            if not isinstance(tasks, list):
                return "采集任务状态不可读"
            active = [t for t in tasks if t.get("status") == "running"]
        except (ValueError, KeyError, TypeError, AttributeError):
            return "采集任务状态不可读"
        return f"{len(active)} 个采集任务运行中" if active else None
