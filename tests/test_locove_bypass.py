"""Exercise the proxy bypass operation with no real system writes."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from localservices import locove, locove_optin


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "locove-proxy-bypass.sh"
ACTIVE_ID = "11111111-2222-3333-4444-555555555555"
EXISTING = ["localhost", "*.local", "10.0.0.0/8"]


class SystemProxyScriptTest(unittest.TestCase):
    def run_script(self, existing, fail_write=False, switch_network=False):
        with tempfile.TemporaryDirectory(prefix="locove-bypass-") as directory:
            root = Path(directory)
            command = root / "system-command"
            command.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path.cwd()
arguments = sys.argv[1:]
if not arguments:
    key = sys.stdin.read().strip().split()[-1]
    if key == 'State:/Network/Global/IPv4':
        if os.environ['SWITCH_NETWORK'] == '1' and (root / 'read').exists():
            print('PrimaryService : 99999999-2222-3333-4444-555555555555')
        else:
            print('PrimaryService : ' + os.environ['ACTIVE_ID'])
    elif key == 'Setup:/Network/Service/' + os.environ['ACTIVE_ID']:
        print('UserDefinedName : Active Wi-Fi')
    else:
        sys.exit(1)
elif arguments[0] == '-getproxybypassdomains':
    if arguments[1:] != ['Active Wi-Fi']:
        sys.exit(1)
    (root / 'read').touch()
    print(os.environ['EXISTING'])
elif arguments[0] == '-setproxybypassdomains':
    with (root / 'writes').open('a') as writes:
        writes.write(json.dumps(arguments[1:]) + '\\n')
    sys.exit(int(os.environ['FAIL_WRITE']))
else:
    sys.exit(1)
""")
            command.chmod(0o755)
            for name in ("scutil", "networksetup"):
                (root / name).symlink_to(command)
            # Leave the privileged script's system command paths fixed in production.
            script = root / "bypass.sh"
            script.write_text(SCRIPT.read_text().replace("/usr/sbin/scutil", str(command))
                              .replace("/usr/sbin/networksetup", str(command)))
            (root / "matched.local").touch()
            result = subprocess.run(
                ["/bin/bash", str(script), "locove"], cwd=root, capture_output=True, text=True,
                env=dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}",
                         ACTIVE_ID=ACTIVE_ID, EXISTING="\n".join(existing),
                         FAIL_WRITE=str(int(fail_write)), SWITCH_NETWORK=str(int(switch_network))),
                timeout=5,
            )
            writes = root / "writes"
            return result, [json.loads(line) for line in writes.read_text().splitlines()] if writes.exists() else []

    def test_adds_only_to_active_service_preserving_literal_rules(self):
        result, writes = self.run_script(EXISTING)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(writes, [["Active Wi-Fi", *EXISTING, "*.locove"]])

    def test_failed_write_does_not_replace_existing_rules(self):
        result, writes = self.run_script(EXISTING, fail_write=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(writes, [["Active Wi-Fi", *EXISTING, "*.locove"]])

    def test_does_not_write_after_network_change(self):
        result, writes = self.run_script(EXISTING, switch_network=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(writes, [])

    def test_already_installed_does_not_write(self):
        result, writes = self.run_script([*EXISTING, "*.locove"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(writes, [])

    def test_empty_system_list_adds_the_rule(self):
        result, writes = self.run_script(["There aren't any bypass domains set on Active Wi-Fi."])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(writes, [["Active Wi-Fi", "*.locove"]])


class ProxyBypassOperationTest(unittest.TestCase):
    MISSING = """<dictionary> {
  ExceptionsList : <array> {
    0 : localhost
    1 : shicha.locove
  }
  HTTPEnable : 1
}
"""
    CONFIGURED = MISSING.replace("    1 : shicha.locove", "    1 : shicha.locove\n    2 : *.locove")

    def test_authorizes_and_checks_effective_configuration(self):
        with patch.object(locove, "_scutil_proxy", side_effect=[self.MISSING, self.MISSING, self.CONFIGURED]), \
                patch.object(locove_optin, "admin_run", return_value={"ok": True}) as authorize, \
                patch.object(locove_optin.time, "sleep"):
            result = locove_optin.bypass()
        self.assertTrue(result["ok"], result)
        authorize.assert_called_once()

    def test_existing_rule_needs_no_authorization(self):
        with patch.object(locove, "_scutil_proxy", return_value=self.CONFIGURED), \
                patch.object(locove_optin, "admin_run") as authorize:
            result = locove_optin.bypass()
        self.assertTrue(result["ok"], result)
        authorize.assert_not_called()

    def test_unapplied_change_is_reported_as_failure(self):
        with patch.object(locove, "_scutil_proxy", return_value=self.MISSING), \
                patch.object(locove_optin, "admin_run", return_value={"ok": True}), \
                patch.object(locove_optin.time, "sleep"):
            result = locove_optin.bypass()
        self.assertEqual(result["reason"], "verification-failed")

    def test_automatic_proxy_does_not_request_authorization(self):
        automatic = self.MISSING.replace("  HTTPEnable : 1", "  ProxyAutoConfigEnable : 1")
        with patch.object(locove, "_scutil_proxy", return_value=automatic), \
                patch.object(locove_optin, "admin_run") as authorize:
            result = locove_optin.bypass()
        self.assertEqual(result["reason"], "proxy-configuration")
        authorize.assert_not_called()
