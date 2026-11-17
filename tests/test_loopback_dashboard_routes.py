"""Locove adapts the known loopback-only dashboards at the local proxy."""

from pathlib import Path
import unittest

from localservices import locove
from localservices.manifest import Component, Endpoint, Project


class LoopbackDashboardRoutesTest(unittest.TestCase):
    def proxy_for(self, service, command, port=5678):
        component = Component("web", cmd=command, port=8000)
        host = f"{service}.locove"
        project = Project(service, service, Path("."),
                          components=[component], endpoints=[Endpoint(host=host, component="web")])
        snapshot = {"projects": [{"id": service, "components": [{"id": "web", "state": "ready"}]}]}
        return locove.render_caddyfile(locove.desired_routes({service: project}, snapshot), port=port)

    def test_sources_manager_routes_through_loopback_checks(self):
        site = self.proxy_for("englishlearn-admin", ["make", "sources-manager"])
        self.assertIn('header Origin "http://englishlearn-admin.locove:5678"', site)
        self.assertIn("header_up Host {upstream_hostport}", site)
        self.assertIn('respond @write_without_origin "Origin required" 403', site)

    def test_creator_dashboard_uses_same_origin_on_default_http_port(self):
        site = self.proxy_for("creatorstudio-dashboard", ["python3", "-m", "creator_studio", "dashboard"], port=80)
        self.assertIn('header Origin "http://creatorstudio-dashboard.locove"', site)
        self.assertIn("request_header @public_origin Origin http://127.0.0.1:8000", site)

    def test_other_make_target_preserves_the_upstream_headers(self):
        site = self.proxy_for("another-app", ["make", "sources-manager"])
        self.assertNotIn("header_up Host", site)
        self.assertNotIn("request_header", site)
