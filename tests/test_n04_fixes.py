"""N04 真实验证发现的缺陷修复的行为门禁（F-B1/F-2/F-4/F-5/F-6）。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from localservices import compose  # noqa: E402
from localservices.compose import _ps_labels  # noqa: E402


class PsLabelsTest(unittest.TestCase):
    """F-B1：docker ps --format {{json .}} 的 Labels 是逗号拼接字符串（真实引擎行为）。"""

    def test_string_labels_parsed(self):
        row = {"Labels": "com.docker.compose.project=demo,com.docker.compose.service=web"}
        labels = _ps_labels(row)
        self.assertEqual(labels["com.docker.compose.project"], "demo")
        self.assertEqual(labels["com.docker.compose.service"], "web")

    def test_dict_and_missing_labels(self):
        self.assertEqual(_ps_labels({"Labels": {"a": "b"}}), {"a": "b"})
        self.assertEqual(_ps_labels({}), {})
        self.assertEqual(_ps_labels({"Labels": ""}), {})

    def test_container_name_with_real_cli_label_shape(self):
        """模拟真实 docker ps 输出（Labels 字符串）走 container_name 全路径。"""
        import json as _json
        from types import SimpleNamespace

        line = _json.dumps({"Names": "demo-web-1",
                            "Labels": "com.docker.compose.project=demo,com.docker.compose.service=web"})
        rows = "\n".join([
            _json.dumps({"Names": "other-1", "Labels": ""}), line])

        def fake_run(args, **kw):
            if "--format" in args and "{{json .}}" in args:
                return SimpleNamespace(returncode=0, stdout=rows, stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        p = SimpleNamespace(id="x", source=Path("."))
        c = SimpleNamespace(compose_service="web", compose_project="demo", compose_files=[], cwd=".")
        with patch.object(compose, "_run", fake_run), \
             patch.object(compose, "project_name", lambda p, c: "demo"):
            self.assertEqual(compose.container_name(p, c), "demo-web-1")


class StartupBudgetTest(unittest.TestCase):
    """F-2：启动预算随组件 start_timeout 扩展，不再固定 30s。"""

    def test_budget_extends_with_start_timeout(self):
        from localservices.controller import Controller, STARTUP_TIMEOUT
        from localservices.manifest import Component, Project
        comps = [Component(id="api", cmd=["/usr/bin/true"], start_timeout=90),
                 Component(id="web", cmd=["/usr/bin/true"], start_timeout=45)]
        p = Project(id="demo", name="demo", source=Path("."), components=comps)
        self.assertEqual(Controller._startup_budget(None, None), STARTUP_TIMEOUT)
        self.assertEqual(Controller._startup_budget(None, p), STARTUP_TIMEOUT + 60.0)

    def test_budget_capped(self):
        from localservices.controller import Controller, STARTUP_TIMEOUT
        from localservices.manifest import Component, Project
        comps = [Component(id="api", cmd=["/usr/bin/true"], start_timeout=9999)]
        p = Project(id="demo", name="demo", source=Path("."), components=comps)
        self.assertEqual(Controller._startup_budget(None, p), STARTUP_TIMEOUT + 270.0)


class ResourceRefWarningTest(unittest.TestCase):
    """F-5：引用已停止容器合法，但必须给“已停止，启动时恢复”警告。"""

    def test_exited_container_warns(self):
        from localservices.registry import _check_resource_ref
        warnings: list[str] = []
        details: list[str] = []
        inv = {"ok": True, "resources": [{"name": "demo-db", "state": "exited"}]}
        with patch("localservices.registry.resources.inventory", return_value=inv):
            _check_resource_ref("DB", "container:demo-db", "demo-db", details, warnings)
        self.assertFalse(details)
        self.assertTrue(any("已停止" in w and "启动时将按需恢复" in w for w in warnings))

    def test_unknown_state_legacy_shape_no_warning(self):
        from localservices.registry import _check_resource_ref
        warnings: list[str] = []
        inv = {"ok": True, "resources": [{"name": "old-db"}]}  # 旧 V1 形状：无 state 字段
        with patch("localservices.registry.resources.inventory", return_value=inv):
            _check_resource_ref("DB", "container:old-db", "old-db", [], warnings)
        self.assertFalse(warnings)

    def test_running_container_no_warning(self):
        from localservices.registry import _check_resource_ref
        warnings: list[str] = []
        details: list[str] = []
        inv = {"ok": True, "resources": [{"name": "demo-db", "state": "running"}]}
        with patch("localservices.registry.resources.inventory", return_value=inv):
            _check_resource_ref("DB", "container:demo-db", "demo-db", details, warnings)
        self.assertFalse(warnings)
        self.assertFalse(details)


if __name__ == "__main__":
    unittest.main()
