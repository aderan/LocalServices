"""Project adapters: small hooks for project-specific busy / health / open behaviour."""

from __future__ import annotations

import importlib
from pathlib import Path

from ..manifest import Component, Project


class Adapter:
    def __init__(self, project: Project):
        self.project = project

    def busy(self) -> str | None:
        """Reason string when a restart/update must be deferred (active recording/job).

        Generic form: options.busy_http returns JSON whose options.busy_key is truthy when busy.
        """
        url = self.project.options.get("busy_http")
        if not url:
            return None
        from .. import procs
        import json

        code, body, err = procs.http_get(url, 2.0)
        if code != 200:
            return None  # service down: nothing running that we could interrupt
        try:
            val = json.loads(body).get(self.project.options.get("busy_key", "busy"))
        except ValueError:
            return None
        return f"活动任务进行中（{val}）" if val else None

    def component_health(self, comp: Component) -> tuple[bool, str] | None:
        """Extra readiness beyond the manifest probe; None = no opinion."""
        return None

    def open_url(self) -> str | None:
        return self.project.open

    def observe(self) -> dict:
        """observe_only projects: {'state','label','detail','actions':[...]}."""
        return {"state": "unknown", "label": "未知", "detail": ""}

    def log_paths(self) -> dict[str, Path]:
        """Named logs owned by an externally managed service."""
        return {}

    def action(self, name: str) -> tuple[bool, str]:
        return False, f"不支持的操作 {name}"


def load(project: Project) -> Adapter:
    if not project.adapter:
        return Adapter(project)
    mod = importlib.import_module(f"{__name__}.{project.adapter}")
    return mod.Adapter(project)
