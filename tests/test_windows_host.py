"""Windows orchestration regressions using mocked OS boundaries, no real input.

These run on any OS; passing does not establish live Windows compatibility.
"""
import io
import sys
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from remotedesk.host.desktop import Desktop


class WindowsHostRegressionTests(unittest.TestCase):
    def test_unchanged_replies_do_not_raise_quality_and_force_full_frames(self):
        desktop = Desktop()
        desktop.reset_stream(minimum_width=1280)
        with patch("remotedesk.host.desktop.time.monotonic") as clock:
            clock.side_effect = [i / 10 for i in range(100)]
            for _ in range(100):
                self.assertEqual(desktop._frame_profile(), (1280, 55))
                desktop.frame_transmitted(False)

    def test_stream_quality_floor_survives_slow_requests_and_reconnect(self):
        desktop = Desktop()
        desktop.reset_stream(minimum_width=1280)
        with patch("remotedesk.host.desktop.time.monotonic", side_effect=[0, 2, 5, 9]):
            for _ in range(3):
                self.assertEqual(desktop._frame_profile(), (1280, 55))
            desktop.reset_stream(minimum_width=1280)
            self.assertEqual(desktop._frame_profile(), (1280, 55))

    def test_quality_restarts_small_and_adapts_to_the_request_interval(self):
        desktop = Desktop()
        with patch("remotedesk.host.desktop.time.monotonic") as clock:
            clock.side_effect = [0, *[i / 5 for i in range(1, 17)], 6, 9, 10]
            first = desktop._frame_profile()
            for _ in range(16):
                fast = desktop._frame_profile()
            self.assertEqual(fast, (1920, 70))
            self.assertLess(desktop._frame_profile()[0], fast[0])
            self.assertEqual(desktop._frame_profile(), first)
            desktop.reset_stream()
            self.assertEqual(desktop._frame_profile(), first)
        self.assertLess(first[0], fast[0])

    def test_dpi_capture_initializes_before_input_and_resize_keeps_physical_coordinates(self):
        # Windows input and capture must share DPI awareness. Initializing an
        # input controller before MSS can lock in incompatible coordinates.
        order = []
        geometry = {'left': -200, 'top': 40, 'width': 200, 'height': 100}
        capture = Mock()
        capture.primary_monitor = geometry
        capture.grab.return_value = types.SimpleNamespace(
            size=(200, 100), bgra=bytes([30, 60, 90, 255]) * 20000)
        mouse = Mock()
        keyboard = Mock()

        def open_capture():
            order.append('capture/DPI')
            return capture

        def open_mouse():
            order.append('mouse')
            self.assertIn('capture/DPI', order)
            return mouse

        def open_keyboard():
            order.append('keyboard')
            self.assertIn('capture/DPI', order)
            return keyboard

        left, right, middle = object(), object(), object()
        mss = types.ModuleType('mss')
        mss.MSS = open_capture
        pynput = types.ModuleType('pynput')
        pynput.mouse = types.SimpleNamespace(
            Controller=open_mouse,
            Button=types.SimpleNamespace(left=left, right=right, middle=middle))
        pynput.keyboard = types.SimpleNamespace(
            Controller=open_keyboard, Key=types.SimpleNamespace(__members__={}))

        with patch.object(sys, 'platform', 'win32'), patch.dict(
            sys.modules, {'mss': mss, 'pynput': pynput}
        ):
            with patch.object(Desktop, 'MAX_WIDTH', 100), Desktop() as desktop:
                jpeg = desktop.frame()
                with Image.open(io.BytesIO(jpeg)) as image:
                    image.load()
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.size, (100, 50))
                desktop.apply([
                    {'type': 'button', 'button': 'left', 'down': True, 'x': 1, 'y': 1},
                    {'type': 'key', 'key': 'q', 'down': True},
                ])
                self.assertEqual(mouse.position, (-1, 139))
                mouse.press.assert_called_once_with(left)
                keyboard.press.assert_called_once_with('q')
            mouse.release.assert_called_once_with(left)
            keyboard.release.assert_called_once_with('q')
            capture.close.assert_called_once()
        self.assertEqual(order, ['capture/DPI', 'mouse', 'keyboard'])


if __name__ == '__main__':
    unittest.main()
