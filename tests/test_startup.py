"""Isolated startup registration checks; never alter the user's login items."""

import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from remotedesk.login import startup


def desktop_value(value):
    """Read the desktop-entry string layer independently of Exec tokenization."""
    escapes = {"s": " ", "n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    return re.sub(r"\\(.)", lambda match: escapes[match[1]], value)


def desktop_argv(value):
    decoded = desktop_value(value)
    # All generated arguments are quoted, so this grammar also detects stray
    # shell syntax, injected extra arguments and prematurely closed quotes.
    arguments = []
    position = 0
    while position < len(decoded):
        match = re.match(r'"((?:\\["`$\\]|[^"\\])*)"(?: |$)', decoded[position:])
        if match is None:
            raise AssertionError("Invalid desktop Exec quoting: " + repr(decoded[position:]))
        argument = re.sub(r'\\(["`$\\])', r'\1', match[1])
        if re.search(r"(?<!%)%(?!%)", argument):
            raise AssertionError("Unescaped desktop field code")
        arguments.append(argument.replace("%%", "%"))
        position += match.end()
    return arguments


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.home = self.root / "home 用户"
        self.home.mkdir()
        self.project = self.root / '项目 with space & $(`quote`) %f = ;'
        self.project.mkdir()
        (self.project / "remote_desktop.py").write_text("# not executed\n", encoding="utf-8")
        self.python = self.root / 'venv 用户 & $() %f' / "bin" / "python"
        self.startup_directory = self.root / "Windows 用户 Startup & $()"
        self.ps_calls = []
        self.fail_shortcut = False
        self.patches = [
            patch.object(Path, "home", return_value=self.home),
            patch.object(startup, "LAUNCHER", self.project / "remote_desktop.py"),
            patch.object(sys, "executable", str(self.python)),
            patch.dict(os.environ, {}, clear=True),
            patch.object(startup.subprocess, "run", side_effect=self.fake_powershell),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def fake_powershell(self, command, **kwargs):
        self.assertEqual(command[1:5], ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
        script = command[5]
        self.ps_calls.append((script, kwargs))
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertNotIn(str(self.project), script)
        self.assertNotIn(str(self.python), script)
        data = json.loads(kwargs["input"])
        if script == startup._PS_PREFIX + startup._PS_STARTUP_DIRECTORY:
            return subprocess.CompletedProcess(command, 0, json.dumps(str(self.startup_directory)), "")
        self.assertEqual(script, startup._PS_PREFIX + startup._PS_CREATE_SHORTCUT)
        if self.fail_shortcut:
            return subprocess.CompletedProcess(command, 1, "", "shortcut denied")
        # Only the isolated temporary .lnk path supplied by install is touched.
        target = Path(data["shortcut"])
        self.assertTrue(target.is_relative_to(self.root))
        target.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def command(self):
        return [str(self.python), str(self.project / "remote_desktop.py"), "gui", "--login"]

    def test_macos_roundtrip_literal_arguments_and_visible_login_only_job(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertFalse(startup.enabled())
            startup.install()
            self.assertTrue(startup.enabled())
            path = self.home / "Library" / "LaunchAgents" / (startup._LABEL + ".plist")
            data = plistlib.loads(path.read_bytes())
            self.assertEqual(data["ProgramArguments"], self.command())
            self.assertEqual(data["WorkingDirectory"], str(self.project))
            self.assertEqual(data["LimitLoadToSessionType"], "Aqua")
            self.assertTrue(data["RunAtLoad"])
            self.assertNotIn("KeepAlive", data)
            self.assertEqual(self.ps_calls, [])
            startup.install()
            self.assertEqual(list(path.parent.iterdir()), [path])
            startup.uninstall()
            startup.uninstall()
            self.assertFalse(startup.enabled())

    def test_linux_roundtrip_and_two_stage_escaping(self):
        configuration = self.root / "custom config 中文"
        with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": str(configuration)}):
            self.assertFalse(startup.enabled())
            startup.install()
            path = configuration / "autostart" / startup._LINUX_NAME
            text = path.read_text(encoding="utf-8")
            values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            self.assertEqual(desktop_argv(values["Exec"]), self.command())
            self.assertEqual(desktop_value(values["Path"]), str(self.project))
            self.assertEqual(values["Terminal"], "false")
            self.assertEqual(values["Hidden"], "false")
            self.assertTrue(startup.enabled())
            self.assertEqual(self.ps_calls, [])
            startup.uninstall()
            self.assertFalse(startup.enabled())

    def test_frozen_macos_login_keeps_existing_identity_without_source_launcher(self):
        executable = self.project / "远程桌面.app" / "Contents" / "MacOS" / "RemoteDesktop"
        with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(executable)), \
                patch.object(startup, "LAUNCHER", self.root / "_MEI_missing" / "remote_desktop.py"):
            startup.install()
            path = self.home / "Library" / "LaunchAgents" / "local.remote-desktop.gui.plist"
            data = plistlib.loads(path.read_bytes())
            self.assertEqual(data["Label"], "local.remote-desktop.gui")
            self.assertEqual(data["ProgramArguments"], [str(executable), "gui", "--login"])
            self.assertEqual(data["WorkingDirectory"], str(executable.parent))
            self.assertTrue(startup.enabled())
            startup.uninstall()
            self.assertFalse(path.exists())

    def test_windows_pythonw_login_remains_a_visible_gui_launcher(self):
        windowed = self.python.with_name("pythonw.exe")
        with patch.object(sys, "platform", "win32"), patch.object(sys, "executable", str(windowed)):
            startup.install()
            data = json.loads(startup._entry_path().read_text(encoding="utf-8"))
            self.assertEqual(data["executable"], str(windowed))
            self.assertEqual(data["arguments"], subprocess.list2cmdline([str(startup.LAUNCHER), "gui", "--login"]))
            self.assertEqual(data["directory"], str(self.project))

    def test_linux_executable_containing_equals_uses_env_without_a_shell(self):
        with patch.object(sys, "platform", "linux"), patch.object(sys, "executable", str(self.python) + "=new"):
            startup.install()
            text = startup._entry_path().read_text(encoding="utf-8")
            command = next(line[5:] for line in text.splitlines() if line.startswith("Exec="))
            self.assertEqual(desktop_argv(command), ["/usr/bin/env", "--", str(self.python) + "=new", *self.command()[1:]])

    def test_linux_relative_xdg_config_home_uses_home_default(self):
        with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path"}):
            startup.install()
            self.assertTrue((self.home / ".config" / "autostart" / startup._LINUX_NAME).is_file())

    def test_desktop_arguments_preserve_newlines_tabs_and_percent_field_codes(self):
        command = ["/python", 'line\nbreak\tcarriage\rquote" dollar$ tick` slash\\ %F %% %U ; & |']
        encoded = " ".join(map(startup._desktop_argument, command))
        self.assertNotIn("\n", encoded)
        self.assertNotIn("\t", encoded)
        self.assertNotIn("\r", encoded)
        self.assertEqual(desktop_argv(encoded), command)

    def test_windows_roundtrip_shortcut_arguments_are_json_data(self):
        windows_command = [r"C:\Python 用户 & $x;\python.exe", r"C:\项目 space & $();%f\remote_desktop.py", "gui", "--login"]
        with patch.object(sys, "platform", "win32"), patch.object(startup, "_command", return_value=windows_command):
            self.assertFalse(startup.enabled())
            self.assertFalse(self.startup_directory.exists())
            startup.install()
            self.assertTrue(startup.enabled())
            path = self.startup_directory / startup._WINDOWS_NAME
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["executable"], windows_command[0])
            self.assertEqual(data["arguments"], '"C:\\项目 space & $();%f\\remote_desktop.py" gui --login')
            self.assertEqual(data["directory"], r"C:\项目 space & $();%f")
            self.assertIn("$link.WindowStyle = 1", self.ps_calls[-2][0])
            self.assertEqual(list(self.startup_directory.iterdir()), [path])
            startup.install()
            startup.uninstall()
            startup.uninstall()
            self.assertFalse(startup.enabled())

    def test_windows_argument_quoting_handles_quotes_and_trailing_backslashes(self):
        windows_command = [r"C:\Python\python.exe", 'C:\\odd "quote" & $()\\', "gui", "--login"]
        with patch.object(sys, "platform", "win32"), patch.object(startup, "_command", return_value=windows_command):
            startup.install()
            data = json.loads(startup._entry_path().read_text(encoding="utf-8"))
            self.assertEqual(data["arguments"], '"C:\\odd \\"quote\\" & $()\\\\" gui --login')
            self.assertNotIn(windows_command[1], self.ps_calls[-2][0])

    def test_windows_shortcut_failure_keeps_old_registration_and_cleans_staging(self):
        with patch.object(sys, "platform", "win32"):
            startup.install()
            path = startup._entry_path()
            before = path.read_bytes()
            self.fail_shortcut = True
            with self.assertRaisesRegex(RuntimeError, "shortcut denied"):
                startup.install()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_atomic_write_failure_preserves_old_entry_and_cleans_temporary_file(self):
        with patch.object(sys, "platform", "darwin"):
            startup.install()
            path = startup._entry_path()
            original = path.read_bytes()
            with patch.object(startup.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    startup.install()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_venv_interpreter_symlink_is_preserved(self):
        self.python.parent.mkdir(parents=True)
        target = self.root / "base-python"
        target.write_bytes(b"not executed")
        try:
            self.python.symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        self.assertEqual(startup._command()[0], str(self.python))

    def test_enabled_and_uninstall_do_not_create_directories(self):
        for platform in ("darwin", "linux", "win32"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                path = startup._entry_path()
                self.assertFalse(path.parent.exists())
                self.assertFalse(startup.enabled())
                startup.uninstall()
                self.assertFalse(path.parent.exists())

    def test_powershell_errors_are_actionable(self):
        for error in (OSError("not found"), subprocess.TimeoutExpired("powershell", 20)):
            with self.subTest(error=error), patch.object(startup.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "Windows PowerShell"):
                    startup._powershell(startup._PS_STARTUP_DIRECTORY)


if __name__ == "__main__":
    unittest.main()
