"""Scoped Windows timer precision for 60 fps capture and presentation."""

import ctypes
import sys

from remotedesk.network.video import validate_frame_rate


class FrameTimer:
    """Own one 1 ms timer request while the caller runs at 60 fps.

    Use and close on the owning stream/UI thread. Timer support is optional;
    unavailable APIs or a rejected request leave normal scheduling in place.
    """

    def __init__(self):
        self._rate = 30
        self._end_period = None

    def set_rate(self, frame_rate):
        frame_rate = validate_frame_rate(frame_rate)
        if self._rate == frame_rate:
            return
        self.close()
        self._rate = frame_rate
        if frame_rate != 60 or sys.platform != 'win32':
            return
        try:
            winmm = ctypes.WinDLL('winmm', use_last_error=True)
            begin, end = winmm.timeBeginPeriod, winmm.timeEndPeriod
            for function in (begin, end):
                function.argtypes = [ctypes.c_uint]
                function.restype = ctypes.c_uint
            if begin(1) == 0:
                self._end_period = end
        except (AttributeError, OSError):
            pass

    def close(self):
        end, self._end_period = self._end_period, None
        self._rate = 30
        if end is not None:
            try:
                end(1)
            except (AttributeError, OSError):
                pass
