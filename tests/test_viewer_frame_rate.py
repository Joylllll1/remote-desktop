"""Frame-rate selection and display scheduling independent of Tk or OS input."""
import unittest
from unittest.mock import Mock, patch
from remotedesk.ui import viewer
from tests.test_viewer_recovery import fake_view
from PIL import Image
from remotedesk.network.stream import _next_capture_deadline


class FrameRateViewerTests(unittest.TestCase):
    def test_capture_pacing_corrects_small_overshoot_but_never_catches_up_a_stall(self):
        interval = 1 / 60
        self.assertAlmostEqual(_next_capture_deadline(10, 10.003, interval), 10 + interval)
        self.assertAlmostEqual(_next_capture_deadline(10, 11, interval), 11 + interval)
        self.assertAlmostEqual(_next_capture_deadline(0, 10, interval), 10 + interval)

    def worker(self):
        return viewer._RecoveryWorker(None, 'fps test', viewer._InputBuffer(), Image)

    def test_preference_validated_and_applied_only_to_supported_host(self):
        worker = self.worker()
        self.assertEqual(worker.frame_rate, 60)
        for value in (True, 0, 59, 60.0, '60'):
            with self.assertRaises(ValueError):
                worker.set_frame_rate(value)
        worker.set_frame_rate(30)
        for supported in (False, True):
            session = Mock(supports_frame_rate_control=supported)
            worker._apply_resolution(session)
            session.set_resolution.assert_called_once_with(1920)
            if supported:
                session.set_frame_rate.assert_called_once_with(30)
            else:
                session.set_frame_rate.assert_not_called()

    def test_legacy_host_disables_choice_and_reconnect_restores_preference(self):
        worker = self.worker()
        worker.set_frame_rate(30)
        window = fake_view(worker)
        window._set_frame_rate_capability(False)
        window._frame_rate_combo.configure.assert_called_with(state='disabled')
        window._frame_rate_choice.set.assert_called_with('由共享端决定')
        window._set_frame_rate_capability(True)
        window._frame_rate_combo.configure.assert_called_with(state='readonly')
        window._frame_rate_choice.set.assert_called_with('标准 · 30 帧')

    def test_settings_changed_during_send_converge_to_latest_selection(self):
        # Simulate UI callbacks running while a session setter waits for its
        # writer lock. The cache must describe the values actually sent.
        for during_connect in (False, True):
            with self.subTest(during_connect=during_connect):
                worker = self.worker()
                session = Mock(
                    supports_resolution_control=True,
                    supports_frame_rate_control=True,
                    supports_continuous_frames=True,
                    supports_async_input=False,
                    media_format='H.264 · 4:4:4',
                )
                sent_rates = []
                frame_count = 0

                def set_resolution(width):
                    if width == 1280:
                        worker.set_frame_rate(60)

                def set_frame_rate(fps):
                    sent_rates.append(fps)
                    if worker.resolution == 1280:
                        worker.set_frame_rate(30)

                def receive(events):
                    nonlocal frame_count
                    frame_count += 1
                    if frame_count == 1:
                        worker.set_resolution(1280)
                        worker.set_frame_rate(30)
                    if frame_count == 4:
                        worker.cancel()
                    return Image.new('RGB', (8, 8))

                if during_connect:
                    worker.set_resolution(1280)
                    worker.set_frame_rate(30)
                session.set_resolution.side_effect = set_resolution
                session.set_frame_rate.side_effect = set_frame_rate
                session.exchange_image.side_effect = receive
                worker._factory = lambda: session
                with patch.object(viewer, '_lifecycle'):
                    worker.run()
                self.assertIsNone(worker.terminal_error)
                self.assertEqual(frame_count, 4)
                self.assertEqual(worker.frame_rate, 30)
                self.assertEqual(sent_rates[-1], 30)

    def test_display_period_accounts_for_render_cost(self):
        for render_seconds, expected_delay in ((.003, 5), (.012, 1)):
            with self.subTest(render_seconds=render_seconds):
                window = fake_view(self.worker())
                window.root = Mock()
                window._poll_updates = Mock()
                window._check_stale = Mock()
                with patch.object(viewer.time, 'monotonic', side_effect=(0, render_seconds)):
                    window._poll()
                window.root.after.assert_called_once_with(expected_delay, window._poll)

    def test_render_skips_same_size_resize_and_reuses_photo_image_in_place(self):
        window = fake_view(self.worker())
        window.Image = Image
        window.ImageTk = Mock()
        photo = Mock()
        window.ImageTk.PhotoImage.return_value = photo
        window._photo = None
        window._rendered_size = None
        window.canvas.winfo_width.return_value = 320
        window.canvas.winfo_height.return_value = 180
        first = Image.new('RGB', (320, 180), '#112233')
        second = Image.new('RGB', (320, 180), '#445566')
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        with patch.object(Image.Image, 'resize', side_effect=AssertionError('same-size resize unnecessary')):
            window._frame = first
            window._render()
            window.ImageTk.PhotoImage.assert_called_once_with(first, master=window.root)
            photo.paste.assert_not_called()
            window._frame = second
            window._render()
            window.ImageTk.PhotoImage.assert_called_once()
            photo.paste.assert_called_once_with(second)


if __name__ == '__main__':
    unittest.main()
