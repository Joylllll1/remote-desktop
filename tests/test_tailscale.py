import os
import subprocess
import unittest
from unittest.mock import patch

from remotedesk.network import tailscale


class TailscaleCliTests(unittest.TestCase):
    @patch.object(tailscale, "executable", return_value="/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    @patch.object(tailscale.subprocess, "run")
    def test_finder_environment_uses_cli_for_status_and_identity(self, run, executable):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, '{"BackendState":"Running","Peer":{}}'),
            subprocess.CompletedProcess([], 0, '{"Node":{"StableID":"test-peer"}}'),
        ]
        with patch.object(tailscale.sys, "platform", "darwin"), patch.dict(os.environ, {"HOME": "/example"}, clear=True):
            self.assertEqual(tailscale.network_status()["BackendState"], "Running")
            self.assertEqual(tailscale.identify(("127.0.0.1", 5000)), "test-peer")
            self.assertNotIn("TAILSCALE_BE_CLI", os.environ)
        self.assertEqual([call.args[0][1:] for call in run.call_args_list],
                         [["status", "--json"], ["whois", "--json", "127.0.0.1:5000"]])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], {"HOME": "/example", "TAILSCALE_BE_CLI": "1"})

    def test_invalid_output_is_actionable_for_status_and_denies_identity(self):
        for output in ("", "The Tailscale GUI failed to start", "[]", "{}"):
            with self.subTest(output=output), patch.object(
                    tailscale, "_run_tailscale", return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaisesRegex(ValueError, "确认已连接后点击"):
                    tailscale.network_status()
                self.assertIsNone(tailscale.identify(("127.0.0.1", 5000)))
