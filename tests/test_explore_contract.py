"""Explore is independent of the Atlas shell, including process ownership."""
import unittest
from pathlib import Path

from localservices import manifest


class ExploreContractTest(unittest.TestCase):
    def setUp(self):
        self.projects = manifest.load_all(Path(__file__).resolve().parents[1] / "services")
        self.project = self.projects["explore"]

    def test_entry_opens_the_explore_frontend(self):
        self.assertEqual(self.project.open, "http://127.0.0.1:5178/explore/#/capture?mode=agent")
        self.assertEqual(self.project.component("web").cwd, "apps/explore")

    def test_both_components_have_distinct_fixed_ports_and_probes(self):
        api = self.project.component("api")
        web = self.project.component("web")
        self.assertEqual((api.port, web.port), (8787, 5178))
        self.assertEqual(api.ready.http, "http://127.0.0.1:8787/api/health")
        self.assertEqual(api.ready.json_ok, "ok")
        self.assertEqual(web.ready.http, "http://127.0.0.1:5178/explore/")
        self.assertEqual(web.after, ["api"])

    def test_backend_uses_existing_env_and_does_not_auto_restart(self):
        api = self.project.component("api")
        self.assertEqual(api.env_files, [".env", ".env.local"])
        self.assertFalse(api.auto_update)
        self.assertEqual(api.env["EXPLORE_HOST"], "127.0.0.1")
        self.assertNotIn("EXPLORE_DATA_DIR", api.env)

    def test_atlas_and_explore_require_their_own_working_directories(self):
        atlas = self.projects["ui-atlas"].component("web")
        self.assertTrue(atlas.external_cwd_exact)
        for component in self.project.managed_components():
            self.assertTrue(component.external_cwd_exact)
