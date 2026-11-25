"""Bound video bandwidth using delivery feedback from decoded frames."""

import math

from remotedesk.network.video import DEFAULT_MAX_BITRATE

_MIN_BITRATE = 1_000_000
_BASELINE_WINDOW = 30.0
_MAX_TRUSTED_BASELINE = 0.180
_MIN_SLOW_DELAY = 0.180
_MIN_HEALTHY_DELAY = 0.120
_SLOW_QUEUE_MARGIN = 0.110
_HEALTHY_QUEUE_MARGIN = 0.050
_WINDOW_EXPAND_HEALTHY_FRAMES = 8


class VideoRateControl:
    """Caller serializes feedback and reads with the stream's request lock.

    Delivery time includes transfer, network round trip and client decoding.
    Track a windowed minimum baseline so higher-RTT relays do not falsely
    throttle, while excess queuing delay still reduces the bitrate ceiling.
    Three slow frames reduce the ceiling, with a two-second cooldown to avoid
    repeated keyframes. Ten seconds of healthy feedback permit gradual recovery.
    """

    def __init__(self):
        self.bitrate = DEFAULT_MAX_BITRATE
        self._slow_frames = 0
        self._healthy_frames = 0
        self._healthy_since = None
        self._changed_at = float('-inf')
        self._window_started = None
        self._current_min = None
        self._previous_min = None
        self._congested = False

    @property
    def baseline_delay(self):
        if self._current_min is None:
            return None
        if self._previous_min is None:
            return self._current_min
        return min(self._current_min, self._previous_min)

    def frame_window(self, base_limit, max_limit, frame_rate):
        """Scale 60 fps flight credits with healthy RTT without bloating congested queues."""
        baseline = self.baseline_delay
        if baseline is None or self._congested or self._healthy_frames < _WINDOW_EXPAND_HEALTHY_FRAMES:
            return base_limit
        needed = math.ceil(baseline * frame_rate) + 1
        return max(base_limit, min(max_limit, needed))

    def _update_baseline(self, delay, now):
        if self._window_started is None:
            self._window_started = now
            self._current_min = delay
            return
        if now - self._window_started >= _BASELINE_WINDOW:
            self._previous_min = self._current_min
            self._current_min = delay
            self._window_started = now
        elif delay < self._current_min:
            self._current_min = delay

    def acknowledge(self, delay, now):
        self._update_baseline(delay, now)
        baseline = min(self.baseline_delay, _MAX_TRUSTED_BASELINE)
        slow_threshold = max(_MIN_SLOW_DELAY, baseline + _SLOW_QUEUE_MARGIN)
        healthy_threshold = max(_MIN_HEALTHY_DELAY, baseline + _HEALTHY_QUEUE_MARGIN)

        if delay > slow_threshold:
            self._healthy_since = None
            self._healthy_frames = 0
            self._congested = True
            self._slow_frames += 1
            if self._slow_frames >= 3 and now - self._changed_at >= 2:
                self.bitrate = max(_MIN_BITRATE, self.bitrate * 7 // 10)
                self._changed_at = now
                self._slow_frames = 0
            return

        self._slow_frames = 0
        if delay > healthy_threshold:
            self._healthy_since = None
            self._healthy_frames = 0
            return
        self._congested = False
        self._healthy_frames += 1
        if self._healthy_since is None:
            self._healthy_since = now
        if now - self._healthy_since >= 10:
            self.bitrate = min(DEFAULT_MAX_BITRATE, self.bitrate + 500_000)
            self._changed_at = now
            self._healthy_since = now
