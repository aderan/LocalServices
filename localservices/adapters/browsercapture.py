"""BrowserCapture: fixed loopback port (Host/Origin checks stay intact), shared data root.

Open uses the service's own authorized workbench URL (#t=<admin secret>, a fragment the UI
moves to sessionStorage); the URL is handed to `open` and never logged or returned by the API.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import procs
from . import Adapter as Base

WORKER_STALE = 90.0


class Adapter(Base):
    def _data(self) -> Path:
        d = self.project.options.get("data_dir") or "~/Library/Application Support/BrowserCapture"
        return Path(os.path.expanduser(d))

    def _port(self) -> int:
        return int(self.project.options.get("port", 47615))

    def _admin(self) -> str | None:
        try:
            return (self._data() / "config" / "admin.secret").read_text().strip()
        except OSError:
            return None

    _cache: tuple[float, dict | None] = (0.0, None)

    def _overview(self) -> dict | None:
        t, ov = self._cache
        if ov is not None and time.time() - t < 10:
            return ov
        ov = self._fetch_overview()
        self._cache = (time.time(), ov)
        return ov

    def _fetch_overview(self) -> dict | None:
        tok = self._admin()
        if not tok:
            return None
        code, body, _ = procs.http_get(f"http://127.0.0.1:{self._port()}/v1/overview", 5,
                                       {"Authorization": f"Bearer {tok}"})
        if code != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None

    def component_health(self, comp):
        if comp.id != "worker":
            return None
        ov = self._overview()
        if ov is None:
            return False, "服务未就绪，无法确认 worker"
        seen = (ov.get("worker") or {}).get("lastSeenAt")
        if not seen:
            return False, "worker 尚未向服务报到"
        age = time.time() - seen / 1000
        if age > WORKER_STALE:
            return False, f"worker 已 {int(age)}s 未报到"
        return True, ""

    def busy(self) -> str | None:
        ov = self._overview()
        if not ov:
            return None
        n = (ov.get("counts") or {}).get("processing", 0)
        return f"{n} 个处理作业运行中" if n else None

    def open_url(self) -> str | None:
        tok = self._admin()
        if not tok:
            return f"http://127.0.0.1:{self._port()}/"
        return f"http://127.0.0.1:{self._port()}/#t={tok}"
