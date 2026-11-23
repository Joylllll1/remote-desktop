"""Touchpad conversion and host scroll dispatch without real OS input."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from remotedesk.host.desktop import Desktop
from remotedesk.ui import viewer


def event(dx=0, dy=0, signed=True, **values):
    packed = ((dx & 0xffff) << 16) | (dy & 0xffff)
    if signed and packed >= 0x80000000:
        packed -= 0x100000000
    return SimpleNamespace(x=50, y=50, delta=packed, state=0, **values)


class ScrollTests(unittest.TestCase):
    def setUp(self):
        self.view = viewer._Viewer.__new__(viewer._Viewer)
        self.view._closed = self.view._ended = False
        self.view._image_rect = (0, 0, 100, 100)
        self.view._input = viewer._InputBuffer()
        self.view._wheel_remainders = {"x": 0, "y": 0}
        self.view._touchpad_remainders = {"x": 0, "y": 0}
        self.view._key_release_jobs = {}
        self.view._keys_down = {}
        self.view._buttons_down = set()
        self.view.canvas = Mock()

    def test_small_vertical_gestures_accumulate_and_keep_direction(self):
        for _ in range(7):
            self.view._touchpad_scroll(event(dy=-5))
        self.assertEqual(self.view._input.take(), [])
        self.assertEqual(self.view._touchpad_scroll(event(dy=-5)), 'break')
        sent = self.view._input.take()
        self.assertEqual([(e['dx'], e['dy']) for e in sent], [(0, -1)])
        self.assertAlmostEqual(sent[0]['x'], 50 / 99)
        self.view._touchpad_scroll(event(dy=80))
        self.assertEqual(self.view._input.take()[0]['dy'], 2)

    def test_diagonal_signed_and_unsigned_deltas(self):
        for signed in (True, False):
            self.view._touchpad_scroll(event(dx=-80, dy=40, signed=signed))
            sent = self.view._input.take()
            self.assertEqual([(e['dx'], e['dy']) for e in sent], [(2, 1)])

    def test_shift_does_not_reinterpret_precise_two_axis_data(self):
        gesture = event(dx=40, dy=-40)
        gesture.state = 1
        self.view._touchpad_scroll(gesture)
        self.assertEqual([(e['dx'], e['dy']) for e in self.view._input.take()], [(-1, -1)])

    def test_outside_image_or_paused_does_not_queue_gesture(self):
        self.view._touchpad_scroll(event(dy=35))
        outside = event(dy=5)
        outside.x = 101
        self.view._touchpad_scroll(outside)
        self.view._touchpad_scroll(event(dy=5))
        self.assertEqual(self.view._input.take(), [])
        self.view._input.disable()
        self.view._touchpad_scroll(event(dy=80))
        self.assertEqual(self.view._input.take(), [])
        self.assertEqual(self.view._touchpad_remainders, {'x': 0, 'y': 0})

    def test_focus_loss_clears_partial_gesture(self):
        self.view._touchpad_scroll(event(dy=35))
        self.view._clear_local_input()
        self.view._touchpad_scroll(event(dy=5))
        self.assertEqual(self.view._input.take(), [])

    def test_large_delta_stays_inside_host_limits(self):
        self.view._touchpad_scroll(event(dx=32767, dy=-32768))
        sent = self.view._input.take()[0]
        self.assertEqual((sent['dx'], sent['dy']), (-100, -100))

    def test_mouse_wheel_still_works_on_mac_and_windows(self):
        for platform, delta in (('darwin', -1), ('win32', -120)):
            with patch.object(viewer.sys, 'platform', platform):
                self.view._mouse_wheel(SimpleNamespace(x=50, y=50, state=0, delta=delta))
            self.assertEqual(self.view._input.take()[0]['dy'], -1)

    def test_touchpad_event_is_accepted_and_dispatched_by_host(self):
        self.view._touchpad_scroll(event(dx=40, dy=-80))
        desktop = Desktop()
        desktop._monitor = {'left': 0, 'top': 0, 'width': 1920, 'height': 1080}
        desktop._mouse = Mock()
        with patch.object(desktop, '_require_open'):
            desktop.apply(self.view._input.take())
        desktop._mouse.scroll.assert_called_once_with(-1, -2)
        self.assertEqual(desktop._mouse.position, (969, 545))


if __name__ == '__main__':
    unittest.main()
