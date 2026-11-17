"""Locove applies loopback compatibility to explicitly selected entries."""

from pathlib import Path
import unittest

from localservices import locove
from localservices.manifest import Component, Endpoint, Project


class LoopbackDashboardRoutesTest(unittest.TestCase):
    def proxy_for(self, service, command, port=5678, owner=None):
        component = Component("web", cmd=command, port=8000)
        host = f"{service}.locove"
        project = Project(service, service, Path("."),
                          components=[component], endpoints=[Endpoint(host=host, component="web")])
        snapshot = {"projects": [{"id": service, "components": [{"id": "web", "state": "ready"}]}]}
        settings = {"loopback_endpoints": {host: owner}} if owner else {}
        routes = locove.desired_routes({service: project}, snapshot, settings)
        return locove.render_caddyfile(routes, port=port)

    def test_any_configured_application_routes_through_loopback_checks(self):
        site = self.proxy_for("demo", ["make", "run"],
                              owner={"service": "demo", "component": "web"})
        self.assertIn('header Origin "http://demo.locove:5678"', site)
        self.assertIn("header_up Host {upstream_hostport}", site)
        self.assertIn('respond @write_without_origin "Origin required" 403', site)

    def test_configured_entry_uses_same_origin_on_default_http_port(self):
        site = self.proxy_for("demo", ["python3", "server.py"], port=80,
                              owner={"service": "demo", "component": "web"})
        self.assertIn('header Origin "http://demo.locove"', site)
        self.assertIn("request_header @public_origin Origin http://127.0.0.1:8000", site)

    def test_old_hardcoded_entry_defaults_to_forwarded_headers(self):
        site = self.proxy_for("englishlearn-admin", ["make", "sources-manager"])
        self.assertNotIn("header_up Host", site)
        self.assertNotIn("request_header", site)

    def test_override_does_not_apply_to_reassigned_endpoint(self):
        site = self.proxy_for("demo", ["make", "run"],
                              owner={"service": "demo", "component": "previous"})
        self.assertNotIn("header_up Host", site)
        self.assertNotIn("request_header", site)
