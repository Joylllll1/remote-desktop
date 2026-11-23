"""Bounded viewer lifecycle streaming without Tk or remote connections."""

import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from remotedesk.ui import panel


class PanelViewerLogTests(unittest.TestCase):
    def make_panel(self, process, identity="device-ID", name="家中电脑"):
        instance = panel._Panel.__new__(panel._Panel)
        instance._closed = False
        instance._closing = False
        instance._viewers = {identity: process}
        instance._viewer_status = {}
        instance._messages = queue.Queue()
        instance._log = Mock()
        instance._show_failure = Mock()
        instance._clear_notice = Mock()
        instance._update_controls = Mock()
        instance.selection_text = Mock()
        instance._mode = lambda: "connect"
        instance._selected = lambda: {"ID": identity, "HostName": name, "Online": True}
        return instance

    def logged(self, instance):
        return "\n".join(call.args[0] for call in instance._log.call_args_list)

    def test_flushed_worker_status_reaches_panel_before_worker_exits(self):
        worker = (
            "import sys\n"
            "print('[查看器] 正在连接 家中电脑…', flush=True)\n"
            "sys.stdin.readline()\n"
            "print('[查看器] 已关闭 家中电脑 的查看器（共显示 0 帧，用时 1 秒）。', flush=True)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", worker], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        instance = self.make_panel(process)
        output = panel._ViewerOutput()
        reader = threading.Thread(target=panel._watch_viewer,
                                  args=(process, output, instance._messages, "device-ID", "家中电脑"), daemon=True)
        reader.start()
        try:
            message = instance._messages.get(timeout=3)
            self.assertEqual(message[0], "viewer_output")
            instance._viewer_output(*message[1:])
            self.assertIsNone(process.poll())
            self.assertIn("正在连接 家中电脑", self.logged(instance))
            self.assertIn("正在连接", instance._viewer_status["device-ID"][1])
            process.stdin.write("close\n")
            process.stdin.flush()
            process.stdin.close()
            while True:
                message = instance._messages.get(timeout=3)
                if message[0] == "viewer_exit":
                    instance._viewer_exit(*message[1:])
                    break
                instance._viewer_output(*message[1:])
            self.assertIn("连接记录保留", self.logged(instance))
            self.assertIn("正在连接 家中电脑", self.logged(instance))
            self.assertNotIn("device-ID", instance._viewers)
            instance._show_failure.assert_not_called()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            if not process.stdin.closed:
                process.stdin.close()
            reader.join(timeout=3)
            self.assertFalse(reader.is_alive())
            self.assertTrue(process.stdout.closed)

    def test_burst_output_bounds_pending_lines_tail_and_queue_notifications(self):
        process = Mock()
        process.stdout = io.StringIO("".join("状态 %d\n" % index for index in range(10000)))
        process.wait.return_value = 0
        messages = queue.Queue()
        output = panel._ViewerOutput()
        panel._watch_viewer(process, output, messages, "stable-ID", "设备")
        self.assertEqual(messages.qsize(), 2)
        self.assertEqual(messages.get_nowait()[0], "viewer_output")
        self.assertEqual(messages.get_nowait()[0], "viewer_exit")
        lines, dropped = output.take()
        self.assertEqual(len(lines), panel._VIEWER_PENDING_LINES)
        self.assertEqual(dropped, 10000 - panel._VIEWER_PENDING_LINES)
        self.assertEqual(lines[-1], "状态 9999")
        self.assertEqual(output.last_detail(), "状态 9999")
        self.assertLessEqual(len(output._tail), 30)
        self.assertTrue(process.stdout.closed)

    def test_long_physical_line_is_truncated_without_unbounded_read(self):
        class BoundedStream(io.StringIO):
            def readline(self, size=-1):
                if size <= 0 or size > panel._VIEWER_LINE_LIMIT + 1:
                    raise AssertionError("unbounded pipe read")
                return super().readline(size)

        process = Mock()
        process.stdout = BoundedStream("x" * 20000 + "\n最后状态\n")
        process.wait.return_value = 0
        output = panel._ViewerOutput()
        panel._watch_viewer(process, output, queue.Queue(), "stable-ID", "设备")
        lines, dropped = output.take()
        self.assertEqual(len(lines), 2)
        self.assertIn("已截断", lines[0])
        self.assertLessEqual(len(lines[0]), panel._VIEWER_LINE_LIMIT)
        self.assertEqual(lines[1], "最后状态")
        self.assertEqual(dropped, 0)

    def test_notifications_resume_after_ui_drains_a_batch(self):
        output = panel._ViewerOutput()
        self.assertTrue(output.append("正在连接"))
        self.assertFalse(output.append("已连接"))
        self.assertEqual(output.take(), (["正在连接", "已连接"], 0))
        self.assertTrue(output.append("正在重新连接"))
        self.assertEqual(output.take(), (["正在重新连接"], 0))
        self.assertFalse(output.append(" \n"))

    def test_exit_zero_after_explicit_close_keeps_retry_history_and_reason(self):
        process = Mock()
        instance = self.make_panel(process)
        output = panel._ViewerOutput()
        output.append("[查看器] 暂时无法连接，将在 2 秒后重试：共享尚未开启")
        output.append("[查看器] 正在重新连接 家中电脑…")
        output.append("[查看器] 已关闭 家中电脑 的查看器（共显示 0 帧，用时 3 秒）。")
        instance._viewer_exit("device-ID", "家中电脑", process, 0, output)
        text = self.logged(instance)
        self.assertIn("共享尚未开启", text)
        self.assertIn("正在重新连接", text)
        self.assertIn("连接记录保留在详情中", text)
        self.assertIn("关闭前", instance._viewer_status["device-ID"][1])
        self.assertNotIn("device-ID", instance._viewers)
        instance._show_failure.assert_not_called()

    def test_terminal_error_remains_a_failure_after_viewer_window_closes(self):
        process = Mock()
        instance = self.make_panel(process)
        output = panel._ViewerOutput()
        output.append("[查看器] 已连接 家中电脑，正在接收画面。")
        output.append("[查看器] 连接已终止：被控端拒绝了此设备")
        output.append("[查看器] 已关闭 家中电脑 的查看器（共显示 0 帧，用时 2 秒）。")
        instance._viewer_exit("device-ID", "家中电脑", process, 1, output)
        instance._show_failure.assert_called_once()
        self.assertIn("被控端拒绝了此设备", instance._show_failure.call_args.args[0])
        self.assertIn("连接已终止", instance._viewer_status["device-ID"][1])
        self.assertIn("已连接", self.logged(instance))

    def test_nonzero_exit_without_output_reports_its_exit_code(self):
        process = Mock()
        instance = self.make_panel(process)
        instance._viewer_exit("device-ID", "家中电脑", process, 7, panel._ViewerOutput())
        self.assertIn("查看器退出码 7", instance._show_failure.call_args.args[0])

    def test_late_old_process_output_and_exit_cannot_replace_new_session_status(self):
        old, current, other = Mock(), Mock(), Mock()
        instance = self.make_panel(current)
        instance._viewers["other-ID"] = other
        instance._remember_viewer_status("device-ID", current, "已显示新连接的画面")
        output = panel._ViewerOutput()
        output.append("旧连接被拒绝")
        instance._viewer_output("device-ID", "同名设备", old, output)
        instance._viewer_exit("device-ID", "同名设备", old, 1, output)
        self.assertIs(instance._viewers["device-ID"], current)
        self.assertIs(instance._viewers["other-ID"], other)
        self.assertEqual(instance._viewer_status["device-ID"], (current, "已显示新连接的画面"))
        self.assertIn("同名设备（上次连接）", self.logged(instance))
        instance._show_failure.assert_not_called()
        instance._update_controls.assert_not_called()

    def test_finished_history_is_bounded_and_selected_status_survives_controls_refresh(self):
        process = Mock()
        instance = self.make_panel(process)
        for index in range(panel._VIEWER_HISTORY_LIMIT + 5):
            instance._remember_viewer_status(str(index), process, "旧记录")
        self.assertEqual(len(instance._viewer_status), panel._VIEWER_HISTORY_LIMIT)
        self.assertNotIn("0", instance._viewer_status)
        instance._viewers.clear()
        instance._remember_viewer_status("device-ID", process, "查看器已关闭；连接记录保留在详情中。关闭前：共享尚未开启")
        instance._host_process = None
        instance._host_state = "stopped"
        instance._refreshing = False
        instance._retry_job = None
        instance._auto_intent = False
        instance._preferences_loaded = False
        instance._preference_busy = False
        instance._saved_peer = None
        for name in ("refresh_button", "connect_button", "start_button", "stop_button", "enable_button", "disable_button"):
            setattr(instance, name, Mock())
        panel._Panel._update_controls(instance)
        self.assertIn("共享尚未开启", instance.selection_text.set.call_args.args[0])

    def test_connect_captures_utf8_pipe_and_retains_worker_launch_environment(self):
        process = Mock()
        process.poll.return_value = None
        instance = self.make_panel(process)
        instance._viewers.clear()
        with tempfile.TemporaryDirectory() as directory:
            instance.script = Path(directory) / "remote_desktop.py"
            instance.script.write_text("# never executed\n", encoding="utf-8")
            command = [sys.executable, str(instance.script), "connect-id", "device-ID"]
            with patch.object(panel, "application_command", return_value=command) as route, \
                    patch.object(panel.subprocess, "Popen", return_value=process) as spawn, \
                    patch.object(panel.threading, "Thread") as thread:
                instance.connect()
        route.assert_called_once_with("connect-id", "device-ID", worker=True, launcher=instance.script)
        arguments, options = spawn.call_args
        self.assertEqual(arguments[0], command)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertTrue(options["text"])
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "replace")
        self.assertEqual(options["env"]["REMOTEDESK_WORKER_STDIO"], "1")
        self.assertEqual(options["env"]["PYTHONUNBUFFERED"], "1")
        self.assertIs(thread.call_args.kwargs["target"], panel._watch_viewer)
        self.assertIs(instance._viewers["device-ID"], process)

    def test_failed_replacement_launch_does_not_revive_stale_process_errors(self):
        old = Mock()
        old.poll.return_value = 0
        instance = self.make_panel(old)
        instance.script = Path("unused.py")
        with patch.object(panel, "application_command", side_effect=RuntimeError("缺少配套解释器")):
            instance.connect()
        self.assertNotIn("device-ID", instance._viewers)
        failure = instance._show_failure.call_args.args[0]
        output = panel._ViewerOutput()
        output.append("旧会话的迟到错误")
        instance._viewer_exit("device-ID", "家中电脑", old, 1, output)
        self.assertEqual(instance._show_failure.call_args.args[0], failure)
        self.assertIn("缺少配套解释器", instance._viewer_status["device-ID"][1])

    def test_poll_delivers_stream_and_exit_events_to_retained_history(self):
        process = Mock()
        instance = self.make_panel(process)
        instance._retry_deadline = None
        instance.root = Mock()
        output = panel._ViewerOutput()
        output.append("[查看器] 已持续显示 150 帧（本次连接 30 秒）。")
        instance._messages.put(("viewer_output", "device-ID", "家中电脑", process, output))
        instance._messages.put(("viewer_exit", "device-ID", "家中电脑", process, 0, output))
        instance._poll()
        self.assertIn("已持续显示 150 帧", self.logged(instance))
        self.assertIn("连接记录保留", instance._viewer_status["device-ID"][1])
        self.assertNotIn("device-ID", instance._viewers)
        instance._show_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
