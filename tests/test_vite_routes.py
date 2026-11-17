"""Locove adapts Vite's upstream Host without changing the public route."""

import json
from pathlib import Path
import tempfile
import unittest

from localservices import locove
from localservices.manifest import Component, Endpoint, Project


class ViteRoutesTest(unittest.TestCase):
    def proxy_site(self, command, source=Path(".")):
        project = Project("demo", "Demo", source,
                          components=[Component("web", cmd=command, port=5173)],
                          endpoints=[Endpoint(host="demo.locove", component="web")])
        snapshot = {"projects": [{"id": "demo", "components": [{"id": "web", "state": "ready"}]}]}
        return locove.render_caddyfile(locove.desired_routes({"demo": project}, snapshot))

    def test_vite_binary_uses_upstream_host(self):
        caddyfile = self.proxy_site(["node_modules/.bin/vite", "--host", "127.0.0.1"])
        self.assertIn("http://demo.locove {", caddyfile)
        self.assertIn("header_up Host {upstream_hostport}", caddyfile)

    def test_vite_package_script_uses_upstream_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            (source / "package.json").write_text(json.dumps({"scripts": {"dev": "vite --strictPort"}}))
            caddyfile = self.proxy_site(["npm", "run", "dev"], source)
        self.assertIn("header_up Host {upstream_hostport}", caddyfile)

    def test_other_app_keeps_public_host(self):
        caddyfile = self.proxy_site(["node_modules/.bin/next", "dev"])
        self.assertNotIn("header_up Host", caddyfile)
