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


class AsyncSession(Session):
    supports_async_input = True

    def __init__(self):
        super().__init__()
        self.blocked = threading.Event()
        self.sent = []

    def exchange(self, events):
        self.received.append(events)
        if len(self.received) == 1:
            return jpeg()
        self.blocked.set()
        self.closed.wait(2)
        raise EOFError("frame connection closed")

    def send_input(self, events):
        self.sent.append((time.monotonic(), list(events)))


class ImageSession(AsyncSession):
    supports_resolution_control = True

    def __init__(self):
        super().__init__()
        self.width = None
        self.applied = []
        self.requests = []
        self.entered = threading.Event()
        self.allow_first = threading.Event()
        self.allow_first.set()
        self.images = []

    def set_resolution(self, width):
        self.width = width
        self.applied.append((threading.get_ident(), width))

    def exchange_image(self, events):
        self.received.append(events)
        self.requests.append(self.width)
        self.entered.set()
        if len(self.requests) == 1:
            self.allow_first.wait(2)
            image = Image.new("RGB", (24, 16))
            self.images.append(image)
            return image
        self.blocked.set()
        self.closed.wait(2)
        raise EOFError("image connection closed")

    def close(self):
        super().close()
        self.allow_first.set()


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
    window._resolution_combo = Mock()
    window._resolution_hint = Mock()
    window._resolution_choice = Mock()
    window._resolution_choice.get.return_value = "清晰 · 1920"
    window.status = Mock()
    window._worker, window._input, window._frames = worker, worker.inputs, worker.frames
    window._closed = window._ended = False
    window._poll_job = window._resize_job = window._focus_job = None
    window._keys_down, window._buttons_down, window._key_release_jobs = {}, set(), {}
    window._frame = window._image_rect = window._rendered_frame = None
    window._last_rendered_at, window._stale = None, False
    window._image_item, window._message_item, window._dot = 1, 2, 3
    window._message_background = 4
    window.canvas.bbox.return_value = (0, 0, 180, 50)
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

    def test_stale_picture_pauses_input_keeps_epoch_and_recovers_on_new_render(self):
        worker = self.worker(Session)
        window = fake_view(worker)
        picture = Image.new("RGB", (24, 16))
        self.addCleanup(picture.close)
        window._frame = window._rendered_frame = picture
        window._render = Mock()
        worker.inputs.begin(7)
        worker.frame_shown(7)
        worker.inputs.append({"type": "key", "key": "a", "down": True})
        window._keys_down[12] = "a"
        window._last_rendered_at = 100
        with patch.object(viewer.time, "monotonic", return_value=101.99) as clock:
            window._poll()
            self.assertTrue(worker.inputs.ready)
            clock.return_value = 102
            window._poll()
            self.assertFalse(worker.inputs.ready)
            self.assertTrue(worker.inputs.matches(7))
            self.assertEqual(worker.inputs.take(), [{"type": "release"}])
            self.assertFalse(window._keys_down)
            self.assertIs(window._frame, picture)
            self.assertEqual(window.status.set.call_args.args[0], "画面延迟 · 已 2 秒未更新")
            clock.return_value = 105
            window._safe_status()
            self.assertEqual(window.status.set.call_args.args[0], "画面延迟 · 已 5 秒未更新")
            worker.inputs.append({"type": "key", "key": "b", "down": True})
            self.assertEqual(worker.inputs.take(), [])
            worker.frames.put((7, picture))
            window._poll()
            self.assertFalse(window._stale)
            self.assertTrue(worker.inputs.ready)
            self.assertEqual(window._last_rendered_at, 105)
            self.assertIn("已连接", window.status.set.call_args.args[0])
            worker.inputs.append({"type": "key", "key": "c", "down": True})
            self.assertEqual(worker.inputs.take(), [{"type": "key", "key": "c", "down": True}])

    def test_stale_epoch_cannot_reenable_controls_after_connection_changes(self):
        worker = self.worker(Session)
        window = fake_view(worker)
        picture = Image.new("RGB", (24, 16))
        self.addCleanup(picture.close)
        window._frame = window._rendered_frame = picture
        window._render = Mock()
        window._last_rendered_at = 100
        worker.inputs.begin(7)
        worker.frame_shown(7)
        with patch.object(viewer.time, "monotonic", return_value=103):
            window._poll()
            worker.inputs.begin(8)
            worker.frames.put((7, picture))
            window._poll()
            self.assertFalse(worker.inputs.ready)
            self.assertTrue(worker.inputs.matches(8))
            self.assertFalse(worker.inputs.enable(7))

    def test_async_sender_delivers_controls_release_and_heartbeats_during_blocked_frame(self):
        session = AsyncSession()
        worker = self.worker(lambda: session)
        worker.start()
        epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        worker.frame_shown(epoch)
        self.assertTrue(session.blocked.wait(1))
        sender = worker._sender
        worker.inputs.append({"type": "key", "key": "a", "down": True})
        wait_until(lambda: any(batch == [{"type": "key", "key": "a", "down": True}] for _, batch in session.sent))
        worker.inputs.append({"type": "key", "key": "a", "down": False})
        wait_until(lambda: any(batch == [{"type": "key", "key": "a", "down": False}] for _, batch in session.sent))
        worker.inputs.pause()
        wait_until(lambda: any(batch == [{"type": "release"}] for _, batch in session.sent))
        wait_until(lambda: sum(not batch for _, batch in session.sent) >= 3)
        self.assertTrue(all(events == [] for events in session.received))
        self.assertTrue(all(len(batch) <= 128 for _, batch in session.sent))
        self.assertLess(max(b[0] - a[0] for a, b in zip(session.sent, session.sent[1:])), 0.25)
        worker.cancel()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(sender.is_alive())
        self.assertIsNone(worker._sender)
        self.assertTrue(session.closed.is_set())

    def test_async_sender_failure_drives_reconnect_after_old_sender_has_exited(self):
        trigger = threading.Event()

        class FailingSender(AsyncSession):
            def send_input(self, events):
                super().send_input(events)
                if trigger.is_set():
                    raise ConnectionResetError("input channel lost")

        first, second = FailingSender(), AsyncSession()
        opened, old_sender = [], []

        def factory():
            if opened:
                self.assertFalse(old_sender[0].is_alive())
                self.assertIsNone(worker._sender)
            session = (first, second)[len(opened)]
            opened.append(session)
            return session

        worker = self.worker(factory)
        worker.start()
        epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        worker.frame_shown(epoch)
        self.assertTrue(first.blocked.wait(1))
        old_sender.append(worker._sender)
        worker.inputs.append({"type": "key", "key": "stale", "down": True})
        trigger.set()
        next_epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        self.assertNotEqual(epoch, next_epoch)
        self.assertTrue(first.closed.is_set())
        self.assertFalse(worker.inputs.ready)
        self.assertFalse(worker.inputs.enable(epoch))
        self.assertTrue(all(batch == [] for _, batch in second.sent))
        worker.frame_shown(next_epoch)
        count = len(first.sent)
        worker.inputs.append({"type": "key", "key": "c", "down": True})
        wait_until(lambda: any(batch == [{"type": "key", "key": "c", "down": True}] for _, batch in second.sent))
        self.assertEqual(len(first.sent), count)
        self.assertFalse(any(event.get("key") == "stale" for _, batch in second.sent for event in batch))
        self.assertIn("秒后重试：input channel lost", self.output.getvalue())

    def test_async_sender_bounds_batches_while_receiver_is_blocked(self):
        session = AsyncSession()
        worker = self.worker(lambda: session)
        worker.start()
        epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        worker.frame_shown(epoch)
        self.assertTrue(session.blocked.wait(1))
        expected = [{"type": "key", "key": "a", "down": bool(index % 2)} for index in range(300)]
        for event in expected:
            worker.inputs.append(event)
        wait_until(lambda: sum(len(batch) for _, batch in session.sent) >= 300)
        batches = [batch for _, batch in session.sent if batch]
        self.assertEqual([len(batch) for batch in batches], [128, 128, 44])
        self.assertEqual([event for batch in batches for event in batch], expected)

    def test_sender_watchdog_releases_without_tk_callbacks_and_resumes_only_after_shown(self):
        session = AsyncSession()
        worker = self.worker(lambda: session)
        worker.start()
        epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        worker.frame_shown(epoch)
        self.assertTrue(session.blocked.wait(1))
        worker.inputs.append({"type": "key", "key": "a", "down": True})
        wait_until(lambda: any(batch == [{"type": "key", "key": "a", "down": True}] for _, batch in session.sent))
        # Advance only the successful-render age, without faking thread clocks.
        with worker._lock:
            worker._input_shown_at = time.monotonic() - 3
        wait_until(lambda: any(batch == [{"type": "release"}] for _, batch in session.sent))
        self.assertFalse(worker.inputs.ready)
        self.assertTrue(worker.inputs.matches(epoch))
        worker.inputs.append({"type": "key", "key": "b", "down": True})
        self.assertEqual(worker.inputs.take(epoch), [])
        self.assertTrue(worker.frame_shown(epoch))
        worker.inputs.append({"type": "key", "key": "c", "down": True})
        wait_until(lambda: any(batch == [{"type": "key", "key": "c", "down": True}] for _, batch in session.sent))
        self.assertFalse(any(event.get("key") == "b" for _, batch in session.sent for event in batch))

    def test_repeated_pause_preserves_queued_release_and_old_epoch_does_not_refresh_watchdog(self):
        worker = self.worker(Session)
        worker.inputs.begin(2)
        worker.frame_shown(2)
        with worker._lock:
            shown_at = worker._input_shown_at
        self.assertFalse(worker.frame_shown(1))
        with worker._lock:
            self.assertEqual(worker._input_shown_at, shown_at)
            self.assertEqual(worker._input_shown_epoch, 2)
            worker._input_shown_at = time.monotonic() - 3
        self.assertFalse(worker._watch_input_freshness(1))
        self.assertTrue(worker._watch_input_freshness(2))
        self.assertFalse(worker.inputs.pause(2))  # Tk's later pause cannot erase release.
        self.assertFalse(worker._watch_input_freshness(2))
        self.assertEqual(worker.inputs.take(2), [{"type": "release"}])

    def test_async_sender_protocol_failure_is_terminal_without_hidden_thread(self):
        session = AsyncSession()
        session.send_input = Mock(side_effect=ValueError("input protocol rejected"))
        factory = Mock(return_value=session)
        worker = self.worker(factory)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(worker.terminal_error, ValueError)
        self.assertEqual(str(worker.terminal_error), "input protocol rejected")
        self.assertEqual(factory.call_count, 1)
        self.assertIsNone(worker._sender)

    def test_epoch_specific_take_cannot_steal_new_session_input(self):
        inputs = viewer._InputBuffer()
        inputs.begin(2)
        inputs.enable(2)
        event = {"type": "key", "key": "c", "down": True}
        inputs.append(event)
        self.assertEqual(inputs.take(1), [])
        self.assertEqual(inputs.take(2), [event])

    def test_legacy_transport_keeps_input_exchange_and_logs_once_per_connection(self):
        session = Session()
        worker = self.worker(lambda: session)
        worker.start()
        epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        worker.frame_shown(epoch)
        worker.inputs.append({"type": "key", "key": "a", "down": True})
        wait_until(lambda: any(batch for batch in session.received))
        self.assertIsNone(worker._sender)
        self.assertEqual(self.output.getvalue().count("对方共享端使用旧版传输"), 1)

    def test_selected_resolution_is_applied_on_connect_and_direct_image_is_not_redecoded(self):
        session = ImageSession()
        session.exchange = Mock(side_effect=AssertionError("JPEG exchange must not be used"))
        worker = self.worker(lambda: session)
        worker.set_resolution(1280)
        with patch.object(Image, "open", side_effect=AssertionError("No JPEG redecode needed")):
            worker.start()
            _, picture = worker.frames.get(timeout=2)
            self.assertIs(picture, session.images[0])
            self.assertEqual(session.requests[0], 1280)
            self.assertTrue(all(width == 1280 for _, width in session.applied))
            worker.cancel()
            worker.join(1)
        self.assertEqual(picture.getpixel((0, 0)), (0, 0, 0))  # UI owns popped image.
        picture.close()
        session.exchange.assert_not_called()

    def test_resolution_switch_during_receive_applies_only_to_next_request(self):
        session = ImageSession()
        session.allow_first.clear()
        worker = self.worker(lambda: session)
        worker.start()
        self.assertTrue(session.entered.wait(1))
        self.assertEqual(session.requests, [1920])
        before = list(session.applied)
        worker.set_resolution(1280)
        self.assertEqual(session.applied, before)
        session.allow_first.set()
        _, picture = worker.frames.get(timeout=2)
        picture.close()
        self.assertTrue(session.blocked.wait(1))
        self.assertEqual(session.requests, [1920, 1280])
        self.assertTrue(all(identity == worker.ident for identity, _ in session.applied))

    def test_resolution_choice_survives_reconnect(self):
        first, second = ImageSession(), ImageSession()
        first.exchange_image = Mock(side_effect=ConnectionResetError("reopen profile"))
        sessions = iter((first, second))
        worker = self.worker(lambda: next(sessions))
        worker.set_resolution(1280)
        worker.start()
        epoch, picture = worker.frames.get(timeout=2)
        picture.close()
        self.assertEqual(epoch, 2)
        self.assertTrue(first.closed.is_set())
        self.assertTrue(all(width == 1280 for _, width in first.applied + second.applied))
        self.assertEqual(second.requests[0], 1280)

    def test_old_protocol_can_return_images_without_unsupported_resolution_calls(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                session = ImageSession()
                session.supports_resolution_control = False
                session.supports_async_input = asynchronous
                session.set_resolution = Mock(side_effect=AssertionError("Unsupported setting"))
                worker = self.worker(lambda: session)
                worker.set_resolution(1280)
                worker.start()
                _, picture = worker.frames.get(timeout=2)
                picture.close()
                worker.cancel()
                worker.join(1)
                session.set_resolution.assert_not_called()
                self.assertIsNone(worker.terminal_error)

    def test_resolution_capability_updates_controls_without_overwriting_connection_status(self):
        worker = self.worker(Session)
        window = fake_view(worker)
        for supported, state, hint in ((False, "disabled", "需升级共享端才能选择画质"),
                                        (True, "readonly", "")):
            window.status.reset_mock()
            worker.states.put(("capabilities", supported))
            window._poll_updates()
            window._resolution_combo.configure.assert_called_with(state=state)
            window._resolution_hint.set.assert_called_with(hint)
            window.status.set.assert_not_called()
        window._resolution_choice.get.return_value = "省流量 · 1280"
        window._resolution_changed()
        self.assertEqual(worker.resolution, 1280)
        for invalid in (True, 640, 1920.0):
            with self.assertRaises(ValueError):
                worker.set_resolution(invalid)
        self.assertEqual(worker.resolution, 1280)

    def test_cancel_closes_decoded_image_returned_after_cancellation(self):
        session = ImageSession()
        picture = Image.new("RGB", (24, 16))
        worker = self.worker(lambda: session)

        def receive(events):
            worker.cancel()
            return picture

        session.exchange_image = receive
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        with self.assertRaises(ValueError):
            picture.getpixel((0, 0))

    def test_ui_closes_replaced_and_final_decoded_images(self):
        worker = self.worker(Session)
        window = fake_view(worker)
        previous, current = Image.new("RGB", (24, 16)), Image.new("RGB", (24, 16))
        window._frame = window._rendered_frame = previous
        window._render = lambda: setattr(window, "_rendered_frame", window._frame)
        worker.inputs.begin(1)
        worker.frames.put((1, current))
        window._poll_updates()
        with self.assertRaises(ValueError):
            previous.getpixel((0, 0))
        self.assertEqual(current.getpixel((0, 0)), (0, 0, 0))
        window.close()
        with self.assertRaises(ValueError):
            current.getpixel((0, 0))


if __name__ == "__main__":
    main()
