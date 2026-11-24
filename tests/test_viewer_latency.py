"""Input wakeups and continuous-frame draining without Tk or OS input."""

import queue
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from remotedesk.ui import viewer


class ObservedInputs(viewer._InputBuffer):
    def __init__(self, wait_timeout=None):
        super().__init__()
        self.wait_timeout = wait_timeout
        self.waits = queue.Queue()

    def wait(self, epoch, timeout):
        self.waits.put(timeout)
        # A long test-only timeout makes notifications observable independently
        # of the heartbeat timer, including input appended just before waiting.
        super().wait(epoch, timeout if self.wait_timeout is None else self.wait_timeout)


class InputSession:
    def __init__(self):
        self.sent = queue.Queue()

    def send_input(self, events):
        self.sent.put((time.monotonic(), list(events)))


class RecordedCancellation(threading.Event):
    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return super().wait(timeout)


class BurstSession:
    def __init__(self, continuous):
        self.supports_continuous_frames = continuous
        self.images = []
        self.drained = threading.Event()
        self.closed = threading.Event()

    def exchange_image(self, events):
        if len(self.images) == 4:
            self.drained.set()
            self.closed.wait(2)
            raise EOFError("burst session closed")
        image = Image.new("RGB", (8, 8), (len(self.images), 0, 0))
        self.images.append(image)
        return image

    def close(self):
        self.closed.set()


class ViewerLatencyTests(unittest.TestCase):
    def start_sender(self, inputs):
        worker = viewer._RecoveryWorker(None, "latency test", inputs, Image)
        inputs.begin(1)
        inputs.enable(1)
        session = InputSession()
        stopped, errors = threading.Event(), queue.Queue()
        sender = threading.Thread(
            target=worker._send_inputs, args=(session, 1, stopped, errors), daemon=True)

        def cleanup():
            stopped.set()
            worker.cancel()
            sender.join(1)

        self.addCleanup(cleanup)
        sender.start()
        self.assertEqual(session.sent.get(timeout=1)[1], [])
        return worker, session, sender, errors

    def test_append_release_and_pause_wake_sender_without_waiting_for_heartbeat(self):
        inputs = ObservedInputs(wait_timeout=5)
        worker, session, sender, errors = self.start_sender(inputs)
        key = {"type": "key", "key": "a", "down": True}
        for event in (key, {"type": "release"}):
            inputs.waits.get(timeout=1)
            inputs.append(event)
            self.assertEqual(session.sent.get(timeout=.5)[1], [event])

        inputs.waits.get(timeout=1)
        self.assertTrue(inputs.pause(1))
        self.assertEqual(session.sent.get(timeout=.5)[1], [{"type": "release"}])
        self.assertFalse(inputs.ready)
        inputs.waits.get(timeout=1)
        worker.cancel()
        sender.join(.5)
        self.assertFalse(sender.is_alive())
        self.assertTrue(errors.empty())
        self.assertTrue(session.sent.empty())

    def test_input_queued_between_send_and_wait_is_not_lost(self):
        before_wait, allow_wait = threading.Event(), threading.Event()

        class GatedInputs(viewer._InputBuffer):
            def wait(self, epoch, timeout):
                before_wait.set()
                allow_wait.wait(1)
                super().wait(epoch, 5)

        inputs = GatedInputs()
        worker, session, sender, errors = self.start_sender(inputs)
        self.addCleanup(allow_wait.set)
        self.assertTrue(before_wait.wait(1))
        event = {"type": "key", "key": "b", "down": True}
        inputs.append(event)  # Notification happens before Condition.wait starts.
        allow_wait.set()
        self.assertEqual(session.sent.get(timeout=.5)[1], [event])
        worker.cancel()
        sender.join(.5)
        self.assertFalse(sender.is_alive())
        self.assertTrue(errors.empty())

    def test_idle_sender_preserves_heartbeat_and_bounded_watchdog_wait(self):
        inputs = ObservedInputs()
        worker, session, sender, errors = self.start_sender(inputs)
        heartbeat_at, events = session.sent.get(timeout=.5)
        next_at, next_events = session.sent.get(timeout=.5)
        self.assertEqual((events, next_events), ([], []))
        self.assertGreaterEqual(next_at - heartbeat_at, .03)
        for _ in range(2):
            timeout = inputs.waits.get(timeout=1)
            self.assertGreaterEqual(timeout, 0)
            self.assertLessEqual(timeout, .05)
        worker.cancel()
        sender.join(.5)
        self.assertFalse(sender.is_alive())
        self.assertTrue(errors.empty())

    def test_new_epoch_wakes_old_sender_without_stealing_new_input(self):
        inputs = ObservedInputs(wait_timeout=5)
        _, session, sender, errors = self.start_sender(inputs)
        inputs.waits.get(timeout=1)
        inputs.begin(2)
        inputs.enable(2)
        event = {"type": "key", "key": "c", "down": True}
        inputs.append(event)
        sender.join(.5)
        self.assertFalse(sender.is_alive())
        self.assertEqual(inputs.take(2), [event])
        self.assertTrue(session.sent.empty())
        self.assertTrue(errors.empty())

    def test_v4_burst_drains_without_legacy_frame_delay(self):
        for continuous in (True, False):
            with self.subTest(continuous=continuous):
                session = BurstSession(continuous)
                worker = viewer._RecoveryWorker(
                    session, "burst test", viewer._InputBuffer(), Image)
                cancellation = RecordedCancellation()
                worker._cancelled = cancellation
                with patch.object(viewer, "_lifecycle"):
                    worker.start()
                    try:
                        self.assertTrue(session.drained.wait(2))
                        if continuous:
                            self.assertEqual(cancellation.waits, [])
                        else:
                            self.assertEqual(len(cancellation.waits), 4)
                            self.assertTrue(all(0 < delay <= .1 for delay in cancellation.waits))
                        self.assertEqual(worker.frames.qsize(), 1)
                        _, latest = worker.frames.get_nowait()
                        try:
                            self.assertIs(latest, session.images[-1])
                            self.assertEqual(latest.getpixel((0, 0)), (3, 0, 0))
                        finally:
                            latest.close()
                        for replaced in session.images[:-1]:
                            with self.assertRaises(ValueError):
                                replaced.getpixel((0, 0))
                    finally:
                        worker.cancel()
                        worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(worker.terminal_error)


if __name__ == "__main__":
    unittest.main()
