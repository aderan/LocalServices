"""Guard the integration with ShiCha's shared development entry point."""
import unittest
from pathlib import Path

from localservices import manifest


class ShiChaContractTest(unittest.TestCase):
    def test_api_uses_shared_build_and_run_entry(self):
        p = manifest.load_all(Path(__file__).resolve().parent / "fixtures" / "service-manifests")['shicha']
        c = p.component('api')
        self.assertIn('../../scripts/dev_api.py', c.cmd)
        self.assertIn('../../scripts/dev_api.py', c.build)
        self.assertIn('run', c.cmd)
        self.assertIn('build', c.build)

    def test_readiness_requires_receiver(self):
        p = manifest.load_all(Path(__file__).resolve().parent / "fixtures" / "service-manifests")['shicha']
        self.assertEqual(p.component('api').ready.json_ok, 'receiverEnabled')

    def test_frontend_proxy_is_explicit(self):
        p = manifest.load_all(Path(__file__).resolve().parent / "fixtures" / "service-manifests")['shicha']
        self.assertEqual(p.component('admin').env['VITE_PROXY_TARGET'], 'http://127.0.0.1:8000')
