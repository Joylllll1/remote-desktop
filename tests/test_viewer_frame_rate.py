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


if __name__ == '__main__':
    unittest.main()
