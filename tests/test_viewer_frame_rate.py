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

    def test_combobox_popdown_focus_keyerror_does_not_fail_display_or_motion(self):
        worker = self.worker()
        window = fake_view(worker)
        window._frame = Image.new('RGB', (320, 180))
        self.addCleanup(window._frame.close)
        window._image_rect = (0, 0, 320, 180)
        window._input.begin(1)
        window._input.enable(1)
        window.canvas.focus_get.side_effect = KeyError('popdown')
        window._safe_status()
        self.assertIsNone(worker.terminal_error)
        self.assertFalse(window._ended)
        self.assertIn('点击画面控制', window.status.set.call_args.args[0])
        self.assertEqual(window._mouse_move(Mock(x=10, y=10)), 'break')
        self.assertEqual(window._input.take(), [])

    def test_rendered_poll_defers_next_timer_until_after_idle_redraw(self):
        window = fake_view(self.worker())
        window.root = Mock()
        window.root.after_idle.return_value = 'idle-1'
        window.root.after.return_value = 'timer-1'
        window._poll_updates = Mock(return_value=True)
        window._check_stale = Mock()
        with patch.object(viewer.time, 'monotonic', side_effect=(10.0, 10.015)):
            window._poll()
            window.root.after.assert_not_called()
            window.root.after_idle.assert_called_once()
            self.assertEqual(window._poll_idle_job, 'idle-1')
            # Invoke the deferred idle callback after DisplayCanvas finishes.
            callback = window.root.after_idle.call_args.args[0]
            callback()
        self.assertIsNone(window._poll_idle_job)
        self.assertEqual(window._poll_job, 'timer-1')
        window.root.after.assert_called_once_with(1, window._poll)

    def test_menu_pause_cancels_pending_poll_and_resume_restarts_it(self):
        window = fake_view(self.worker())
        window.root = Mock()
        window.root.after.return_value = 'timer-2'
        window._poll_job = 'timer-1'
        window._poll_idle_job = 'idle-1'
        window._resize_job = 'resize-1'
        window._pause_for_menu()
        self.assertEqual(window._menu_depth, 1)
        self.assertIsNone(window._poll_job)
        self.assertIsNone(window._poll_idle_job)
        self.assertIsNone(window._resize_job)
        self.assertEqual(
            [call.args[0] for call in window.root.after_cancel.call_args_list],
            ['timer-1', 'idle-1', 'resize-1'],
        )
        # Polling is suppressed while a native popup menu is open.
        window._poll_updates = Mock()
        window._poll()
        window._poll_updates.assert_not_called()
        window.root.after.assert_not_called()
        # Resuming restores the poll timer once all menus close.
        window._resume_from_menu()
        self.assertEqual(window._menu_depth, 0)
        self.assertEqual(window._poll_job, 'timer-2')
        window.root.after.assert_called_once_with(viewer._DISPLAY_POLL_MS, window._poll)

    def test_bind_menu_pause_inserts_resume_tag_immediately_after_widget_class(self):
        window = fake_view(self.worker())
        window.root = Mock()
        window._menu_resume_tag = 'RemoteMenuResume-test'
        window._menu_resume_sequences = set()
        widget = Mock()
        widget.winfo_class.return_value = 'TCombobox'
        widget.bindtags.return_value = ('.bar.cb', 'TCombobox', '.', 'all')
        window._bind_menu_pause(widget, ('<ButtonPress-1>', '<Down>'))
        widget.bindtags.assert_called_with(('.bar.cb', 'TCombobox', 'RemoteMenuResume-test', '.', 'all'))
        self.assertEqual(
            [call.args[:2] for call in widget.bind.call_args_list],
            [('<ButtonPress-1>', window._pause_for_menu), ('<Down>', window._pause_for_menu)],
        )
        self.assertEqual(
            [call.args[:3] for call in window.root.bind_class.call_args_list],
            [
                ('RemoteMenuResume-test', '<ButtonPress-1>', window._resume_from_menu),
                ('RemoteMenuResume-test', '<Down>', window._resume_from_menu),
            ],
        )


if __name__ == '__main__':
    unittest.main()
