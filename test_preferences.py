"""Persist an explicit device choice independently of the displayed hostname."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_preferences import disable_login, enable_login, login_peer


class LoginPreferenceTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Path(folder) / "app" / "login-peer.json"
        self.enterContext(patch("desktop_preferences.settings_file", return_value=self.settings))
        self.startup = self.enterContext(patch("desktop_preferences.desktop_startup"))
        self.startup.enabled.return_value = False

    def test_enable_replace_and_disable(self):
        enable_login("node-1", "出门的 Mac")
        self.startup.enabled.return_value = True
        self.assertEqual(login_peer(), ("node-1", "出门的 Mac"))
        enable_login("node-2", "出门的 Mac")
        self.assertEqual(login_peer(), ("node-2", "出门的 Mac"))
        disable_login()
        self.assertIsNone(login_peer())
        self.startup.uninstall.assert_called_once()

    def test_failed_install_cannot_resurrect_stale_authorization(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{"id":"old","name":"Mac"}', encoding="utf-8")
        self.startup.install.side_effect = OSError("registration failed")
        with self.assertRaises(OSError):
            enable_login("new", "Mac")
        self.startup.enabled.return_value = True
        self.assertIsNone(login_peer())

    def test_failed_update_keeps_previously_enabled_identity(self):
        enable_login("first", "Mac")
        self.startup.enabled.return_value = True
        self.startup.install.side_effect = OSError("registration failed")
        with self.assertRaises(OSError):
            enable_login("new", "Mac")
        self.assertEqual(login_peer(), ("first", "Mac"))

    def test_failed_removal_revokes_permission(self):
        enable_login("first", "Mac")
        self.startup.enabled.return_value = True
        self.startup.uninstall.side_effect = OSError("entry locked")
        with self.assertRaises(OSError):
            disable_login()
        self.assertIsNone(login_peer())
