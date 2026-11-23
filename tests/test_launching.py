"""Application routing checks without opening a desktop or spawning a host."""

import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import remotedesk
from remotedesk.ui import panel


class ApplicationCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.script = self.root / "项目 with spaces" / "remote_desktop.py"
        self.script.parent.mkdir()
        self.script.write_text("# not executed\n", encoding="utf-8")
        self.python = self.root / "runtime" / "python"
        self.python.parent.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(sys, "frozen", False, create=True).start()
        patch.object(sys, "executable", str(self.python)).start()

    def test_source_and_embedded_runtime_keep_existing_cli_arguments(self):
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform):
                command = remotedesk.application_command("host-id", "stable-ID", worker=True, launcher=self.script)
                self.assertEqual(command, [str(self.python), str(self.script), "host-id", "stable-ID"])
                self.assertEqual(remotedesk.command_directory(command), str(self.script.parent))

    def test_frozen_app_uses_executable_and_not_an_extracted_source_file(self):
        executable = self.root / "远程桌面.app" / "Contents" / "MacOS" / "RemoteDesktop"
        missing_script = self.root / "_MEI_unpacked" / "remote_desktop.py"
        with patch.object(sys, "frozen", True), patch.object(sys, "executable", str(executable)):
            command = remotedesk.application_command("connect-id", "stable-ID", worker=True, launcher=missing_script)
            self.assertEqual(command, [str(executable), "connect-id", "stable-ID"])
            self.assertEqual(remotedesk.command_directory(command), str(executable.parent))
            self.assertFalse(missing_script.exists())

    def test_windows_pythonw_workers_use_sibling_console_python(self):
        console = self.python.with_name("python.exe")
        console.write_bytes(b"not executed")
        windowed = self.python.with_name("pythonw.exe")
        with patch.object(sys, "platform", "win32"), patch.object(sys, "executable", str(windowed)):
            worker = remotedesk.application_command("host-id", "stable-ID", worker=True, launcher=self.script)
            login = remotedesk.application_command("gui", "--login", launcher=self.script)
            self.assertEqual(worker[0], str(console))
            self.assertEqual(login[0], str(windowed))
            self.assertEqual(worker[1:], [str(self.script), "host-id", "stable-ID"])

    def test_missing_console_python_reports_installation_problem(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(sys, "executable", str(self.python.with_name("pythonw.exe"))):
            with self.assertRaisesRegex(RuntimeError, "python.exe"):
                remotedesk.application_command("host-id", "stable-ID", worker=True, launcher=self.script)

    def test_missing_source_launcher_is_not_silently_replaced_by_the_module(self):
        with self.assertRaisesRegex(RuntimeError, "remote_desktop.py"):
            remotedesk.application_command("gui", launcher=self.root / "missing.py")

    def test_worker_stdio_changes_only_explicit_captured_workers(self):
        streams = [Mock(spec=["reconfigure"]) for _ in range(3)]
        with patch.object(sys, "stdin", streams[0]), patch.object(sys, "stdout", streams[1]), \
                patch.object(sys, "stderr", streams[2]), patch.dict(os.environ, {}, clear=True):
            remotedesk.configure_worker_stdio()
            for stream in streams:
                stream.reconfigure.assert_not_called()
            os.environ["REMOTEDESK_WORKER_STDIO"] = "1"
            remotedesk.configure_worker_stdio()
            for stream in streams:
                stream.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", io.StringIO()), \
                patch.dict(os.environ, {"REMOTEDESK_WORKER_STDIO": "1"}):
            remotedesk.configure_worker_stdio()


class PanelCommandTests(unittest.TestCase):
    """Exercise real panel launch sites with fake UI and subprocess handles."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.script = self.root / "remote_desktop.py"
        self.script.write_text("# not executed\n", encoding="utf-8")
        self.peer = {"ID": "stable-ID", "HostName": "设备", "Online": True}
        self.panel = panel._Panel.__new__(panel._Panel)
        self.panel.script = self.script
        self.panel._closing = False
        self.panel._closed = False
        self.panel._viewers = {}
        self.panel._messages = queue.Queue()
        self.panel._host_state = "stopped"
        self.panel._host_process = None
        self.panel._host_tail = []
        self.panel._auto_intent = True
        self.panel._auto_target = (self.peer["ID"], self.peer["HostName"])
        self.panel._selected = lambda: self.peer
        self.panel._cancel_retry = Mock()
        self.panel._update_controls = Mock()
        self.panel._log = Mock()
        self.panel.host_text = Mock()
        self.panel.selection_text = Mock()
        self.process = Mock()
        self.process.poll.return_value = 0
        self.process.wait.return_value = 0
        self.process.stdout = io.StringIO()
        self.addCleanup(patch.stopall)
        patch.object(sys, "frozen", False, create=True).start()
        self.spawn = patch.object(panel.subprocess, "Popen", return_value=self.process).start()
        patch.object(panel.threading, "Thread").start()
        patch.object(panel.tempfile, "TemporaryFile", side_effect=lambda **kwargs: io.BytesIO()).start()

    def test_frozen_panel_routes_all_existing_actions_without_source_file(self):
        executable = self.root / "RemoteDesktop.app" / "Contents" / "MacOS" / "RemoteDesktop"
        self.panel.script = self.root / "missing-extraction" / "remote_desktop.py"
        with patch.object(sys, "frozen", True), patch.object(sys, "executable", str(executable)):
            self.panel.connect()
            arguments, options = self.spawn.call_args
            self.assertEqual(arguments[0], [str(executable), "connect-id", "stable-ID"])
            self.assertEqual(options["cwd"], str(executable.parent))
            self.assertEqual(options["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            for automatic, action in ((False, "host-id"), (True, "host-login-id")):
                with self.subTest(action=action), patch.dict(os.environ, {}, clear=True):
                    self.panel._start_host(self.peer, automatic=automatic)
                    arguments, options = self.spawn.call_args
                    self.assertEqual(arguments[0], [str(executable), action, "stable-ID"])
                    self.assertEqual(options["cwd"], str(executable.parent))
                    self.assertEqual(options["stdin"], panel.subprocess.PIPE)
                    self.assertNotIn("PYINSTALLER_RESET_ENVIRONMENT", options["env"])
                    self.assertEqual(options["env"]["REMOTEDESK_WORKER_STDIO"], "1")
                    self.assertFalse(options["shell"])

    def test_windows_panel_uses_console_sibling_for_viewer_and_host(self):
        windowed = self.root / "runtime" / "pythonw.exe"
        windowed.parent.mkdir()
        console = windowed.with_name("python.exe")
        console.write_bytes(b"not executed")
        with patch.object(sys, "platform", "win32"), patch.object(sys, "executable", str(windowed)):
            self.panel.connect()
            self.assertEqual(self.spawn.call_args.args[0], [str(console), str(self.script), "connect-id", "stable-ID"])
            self.panel._start_host(self.peer)
            self.assertEqual(self.spawn.call_args.args[0], [str(console), str(self.script), "host-id", "stable-ID"])
            self.assertEqual(self.spawn.call_args.kwargs["cwd"], str(self.script.parent))


class WorkerEntryPointTests(unittest.TestCase):
    def test_captured_entry_point_outputs_utf8_even_with_ascii_environment(self):
        result = subprocess.run(
            [sys.executable, str(remotedesk.LAUNCHER), "--help"],
            env=dict(os.environ, PYTHONIOENCODING="ascii", REMOTEDESK_WORKER_STDIO="1"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("打开设备和共享管理窗口", result.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
