"""Windows timer ownership using mocked WinMM; no global timer changes."""

import ctypes
import unittest
from unittest.mock import Mock, call, patch

from remotedesk.network import timing


class FrameTimerTests(unittest.TestCase):
    def windows(self, begin_result=0):
        winmm = Mock()
        winmm.timeBeginPeriod.return_value = begin_result
        winmm.timeEndPeriod.return_value = 0
        self.enterContext(patch.object(timing.sys, 'platform', 'win32'))
        loader = self.enterContext(patch.object(timing.ctypes, 'WinDLL', return_value=winmm, create=True))
        return winmm, loader

    def test_60fps_requests_one_millisecond_and_30fps_releases_once(self):
        winmm, loader = self.windows()
        timer = timing.FrameTimer()
        self.addCleanup(timer.close)
        timer.set_rate(30)
        loader.assert_not_called()
        timer.set_rate(60)
        timer.set_rate(60)
        loader.assert_called_once_with('winmm', use_last_error=True)
        winmm.timeBeginPeriod.assert_called_once_with(1)
        winmm.timeEndPeriod.assert_not_called()
        for function in (winmm.timeBeginPeriod, winmm.timeEndPeriod):
            self.assertEqual(function.argtypes, [ctypes.c_uint])
            self.assertIs(function.restype, ctypes.c_uint)
        timer.set_rate(30)
        timer.set_rate(30)
        timer.close()
        timer.close()
        winmm.timeEndPeriod.assert_called_once_with(1)

    def test_switching_and_reusing_after_close_balance_every_successful_request(self):
        winmm, _ = self.windows()
        timer = timing.FrameTimer()
        self.addCleanup(timer.close)
        for frame_rate in (60, 30, 60):
            timer.set_rate(frame_rate)
        timer.close()
        timer.set_rate(60)
        timer.close()
        self.assertEqual(winmm.mock_calls, [call.timeBeginPeriod(1), call.timeEndPeriod(1)] * 3)

    def test_rejected_request_does_not_release_an_unowned_timer(self):
        winmm, _ = self.windows(begin_result=1)
        timer = timing.FrameTimer()
        timer.set_rate(60)
        timer.set_rate(60)
        timer.set_rate(30)
        timer.close()
        winmm.timeBeginPeriod.assert_called_once_with(1)
        winmm.timeEndPeriod.assert_not_called()
        winmm.timeBeginPeriod.return_value = 0
        timer.set_rate(60)
        timer.close()
        self.assertEqual(winmm.timeBeginPeriod.call_count, 2)
        winmm.timeEndPeriod.assert_called_once_with(1)

    def test_unavailable_library_or_function_keeps_normal_scheduling(self):
        for failure in (OSError('winmm unavailable'), AttributeError('API unavailable')):
            with self.subTest(failure=failure):
                winmm, loader = self.windows()
                loader.side_effect = failure
                timer = timing.FrameTimer()
                timer.set_rate(60)
                timer.set_rate(30)
                timer.close()
                winmm.timeEndPeriod.assert_not_called()

    def test_failed_api_calls_do_not_break_stream_shutdown(self):
        winmm, _ = self.windows()
        winmm.timeBeginPeriod.side_effect = OSError('request unavailable')
        timer = timing.FrameTimer()
        timer.set_rate(60)
        timer.close()
        winmm.timeEndPeriod.assert_not_called()
        winmm.timeBeginPeriod.side_effect = None
        timer.set_rate(60)
        winmm.timeEndPeriod.side_effect = OSError('release unavailable')
        timer.close()
        timer.close()
        winmm.timeEndPeriod.assert_called_once_with(1)

    def test_non_windows_is_noop_and_bad_rates_do_not_change_ownership(self):
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform), patch.object(timing.sys, 'platform', platform), \
                    patch.object(timing.ctypes, 'WinDLL', create=True) as loader:
                timer = timing.FrameTimer()
                timer.set_rate(60)
                timer.set_rate(30)
                timer.close()
                loader.assert_not_called()
        winmm, _ = self.windows()
        timer = timing.FrameTimer()
        self.addCleanup(timer.close)
        timer.set_rate(60)
        for invalid in (True, False, None, 60.0, 24, '60'):
            with self.subTest(frame_rate=invalid), self.assertRaises(ValueError):
                timer.set_rate(invalid)
        winmm.timeEndPeriod.assert_not_called()
        timer.close()
        winmm.timeEndPeriod.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
