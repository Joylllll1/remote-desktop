"""Recovery and UI-failure tests without a real desktop or Tk display."""

import io
import queue
import threading
import time
from contextlib import redirect_stdout
from unittest import TestCase, main
from unittest.mock import Mock, patch

from PIL import Image

from remotedesk.ui import viewer


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (24, 16)).save(output, "JPEG")
    return output.getvalue()


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("Timed out waiting for viewer state")


class Session:
    def __init__(self):
        self.closed = threading.Event()
        self.received = []

    def exchange(self, events):
        self.received.append(events)
        return jpeg()

    def close(self):
        self.closed.set()


class FakeRoot:
    def __init__(self):
        self.callbacks = []
        self.mainloop = lambda: None

    def after(self, delay, callback):
        self.callbacks.append(callback)
        return str(len(self.callbacks))

    def after_idle(self, callback):
        return self.after(0, callback)

    def after_cancel(self, job):
        pass

    def destroy(self):
        pass


def fake_view(worker):
    window = viewer._Viewer.__new__(viewer._Viewer)
    window.root = FakeRoot()
    window.canvas = Mock()
    window._indicator = Mock()
    window._disconnect = Mock()
    window.status = Mock()
    window._worker, window._input, window._frames = worker, worker.inputs, worker.frames
    window._closed = window._ended = False
    window._poll_job = window._resize_job = window._focus_job = None
    window._keys_down, window._buttons_down, window._key_release_jobs = {}, set(), {}
    window._frame = window._image_rect = window._rendered_frame = None
    window._image_item, window._message_item, window._dot = 1, 2, 3
    window.title = "家中电脑"
    return window


class ViewerRecoveryTests(TestCase):
    def setUp(self):
        self.output = io.StringIO()
        context = redirect_stdout(self.output)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        delays = patch.object(viewer, "_RETRY_DELAYS", (0.01, 0.02))
        delays.start()
        self.addCleanup(delays.stop)
        self.workers = []
        self.addCleanup(self.cleanup_workers)

    def cleanup_workers(self):
        for worker in self.workers:
            worker.cancel()
            if worker.ident is not None:
                worker.join(2)

    def worker(self, factory):
        worker = viewer._RecoveryWorker(factory, "家中电脑", viewer._InputBuffer(), Image)
        self.workers.append(worker)
        return worker

    def test_drop_recovers_without_replaying_input_and_requires_new_shown_frame(self):
        blocked, drop = threading.Event(), threading.Event()

        class Dropping(Session):
            def exchange(self, events):
                self.received.append(events)
                if len(self.received) == 1:
                    return jpeg()
                blocked.set()
                drop.wait(2)
                raise ConnectionResetError("hotspot reset")

        first, second = Dropping(), Session()
        sessions = iter((first, second))
        worker = self.worker(lambda: next(sessions))
        worker.start()
        old_epoch, frame = worker.frames.get(timeout=2)
        frame.close()
        self.assertFalse(worker.inputs.ready)
        self.assertTrue(worker.frame_shown(old_epoch))
        self.assertTrue(blocked.wait(2))
        worker.inputs.append({"type": "key", "key": "stale", "down": True})
        drop.set()
        wait_until(first.closed.is_set)
        worker.inputs.append({"type": "key", "key": "outage", "down": True})
        epoch, frame = worker.frames.get(timeout=2)
        frame.close()
        self.assertNotEqual(epoch, old_epoch)
        self.assertFalse(worker.inputs.ready)
        self.assertFalse(worker.frame_shown(old_epoch))
        self.assertEqual(second.received[0], [])
        self.assertTrue(worker.frame_shown(epoch))
        worker.inputs.append({"type": "key", "key": "c", "down": True})
        wait_until(lambda: len(second.received) >= 2)
        self.assertEqual(second.received[1], [{"type": "key", "key": "c", "down": True}])
        self.assertIn("连接中断", self.output.getvalue())

    def test_cancel_interrupts_backoff_without_retry_or_terminal_error(self):
        factory = Mock(side_effect=TimeoutError("hotspot unavailable"))
        with patch.object(viewer, "_RETRY_DELAYS", (60,)):
            worker = self.worker(factory)
            worker.start()
            wait_until(lambda: "秒后重试" in self.output.getvalue())
            started = time.monotonic()
            worker.cancel()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(factory.call_count, 1)
        self.assertIsNone(worker.terminal_error)

    def test_cancel_during_initial_open_closes_late_session_without_exchange(self):
        entered, finish = threading.Event(), threading.Event()
        session = Session()

        def open_session():
            entered.set()
            finish.wait(2)
            return session

        worker = self.worker(open_session)
        worker.start()
        self.assertTrue(entered.wait(2))
        worker.cancel()
        finish.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(session.closed.is_set())
        self.assertEqual(session.received, [])

    def test_auth_refusal_is_terminal_and_is_raised_even_before_ui_polls_it(self):
        refusal = ValueError("尚未授权当前设备")
        factory = Mock(side_effect=refusal)
        worker = self.worker(factory)
        window = fake_view(worker)

        def mainloop():
            worker.join(1)
            window.close()  # Close before the queued terminal update reaches Tk.

        window.root.mainloop = mainloop
        with self.assertRaisesRegex(ValueError, "尚未授权"):
            window.run()
        self.assertEqual(factory.call_count, 1)
        self.assertIs(worker.terminal_error, refusal)
        self.assertIn("连接已终止：尚未授权当前设备", self.output.getvalue())
        self.assertNotIn("已关闭", self.output.getvalue())

    def test_render_failure_is_terminal_and_never_claims_first_frame_shown(self):
        session = Session()
        worker = self.worker(lambda: session)
        window = fake_view(worker)
        window._render = Mock(side_effect=RuntimeError("Tk PhotoImage failed"))

        def mainloop():
            wait_until(lambda: not worker.frames.empty())
            window._poll()
            window.close()

        window.root.mainloop = mainloop
        with self.assertRaisesRegex(RuntimeError, "Tk PhotoImage failed"):
            window.run()
        self.assertTrue(session.closed.is_set())
        self.assertFalse(worker.inputs.ready)
        self.assertIn("连接已终止：无法显示远程桌面", self.output.getvalue())
        self.assertNotIn("已显示 家中电脑", self.output.getvalue())

    def test_invalid_image_is_terminal_protocol_error(self):
        session = Session()
        session.exchange = lambda events: b"not a JPEG"
        factory = Mock(return_value=session)
        worker = self.worker(factory)
        worker.start()
        worker.join(1)
        self.assertIsInstance(worker.terminal_error, ValueError)
        self.assertTrue(session.closed.is_set())
        self.assertEqual(factory.call_count, 1)

    def test_completed_picture_survives_drop_without_enabling_stale_input(self):
        session = Session()
        calls = []

        def exchange(events):
            calls.append(events)
            if len(calls) == 1:
                return jpeg()
            raise EOFError("transfer finished just before drop")

        session.exchange = exchange
        with patch.object(viewer, "_RETRY_DELAYS", (60,)):
            worker = self.worker(lambda: session)
            worker.start()
            wait_until(lambda: "秒后重试" in self.output.getvalue())
            self.assertFalse(worker.frames.empty())
            window = fake_view(worker)
            window._render = Mock()
            window._update_status = Mock()
            window._poll_updates()
            self.assertIsNotNone(window._frame)
            self.assertFalse(worker.inputs.ready)
            window._update_status.assert_not_called()
            self.assertIn("秒后重试", window.status.set.call_args.args[0])
            self.assertIn("已显示 家中电脑 的桌面画面。", self.output.getvalue())
            window._frame.close()

    def test_status_callback_failure_also_stops_transport(self):
        session = Session()
        worker = self.worker(lambda: session)
        window = fake_view(worker)
        window._render = Mock()
        window._update_status = Mock(side_effect=RuntimeError("Tk status failed"))

        def mainloop():
            wait_until(lambda: not worker.frames.empty())
            window._poll()
            window.close()

        window.root.mainloop = mainloop
        with self.assertRaisesRegex(RuntimeError, "Tk status failed"):
            window.run()
        self.assertTrue(session.closed.is_set())
        self.assertFalse(worker.inputs.ready)

    def test_programmatic_mainloop_exit_prints_intentional_close_summary(self):
        worker = self.worker(Session)
        window = fake_view(worker)
        window.run()
        self.assertIn("已关闭 家中电脑 的查看器（共显示 0 帧，用时 0 秒）。", self.output.getvalue())

    def test_successful_render_checkpoints_are_infrequent_and_close_counts_all_frames(self):
        with patch.object(viewer.time, "monotonic", return_value=100) as clock:
            worker = self.worker(Session)
            worker.inputs.begin(1)
            self.assertTrue(worker.frame_shown(1))
            clock.return_value = 129
            worker.frame_shown(1)
            self.assertNotIn("持续显示", self.output.getvalue())
            clock.return_value = 130
            worker.frame_shown(1)
            self.assertIn("已持续显示 3 帧（本次连接 30 秒）。", self.output.getvalue())
            clock.return_value = 140
            worker.inputs.begin(2)
            worker.frame_shown(2)
            self.assertEqual(worker.close_summary(), "已关闭 家中电脑 的查看器（共显示 4 帧，用时 40 秒）。")

    def test_lifecycle_logging_survives_absent_closed_or_broken_parent_pipe(self):
        for stream in (None, io.StringIO(), Mock(write=Mock(side_effect=BrokenPipeError))):
            if isinstance(stream, io.StringIO):
                stream.close()
            with self.subTest(stream=type(stream).__name__), patch.object(viewer.sys, "stdout", stream):
                viewer._lifecycle("正常状态")


if __name__ == "__main__":
    main()
