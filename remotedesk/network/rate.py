"""Bound video bandwidth using delivery feedback from decoded frames."""

from remotedesk.network.video import DEFAULT_MAX_BITRATE


class VideoRateControl:
    """Caller serializes feedback and reads with the stream's request lock.

    Delivery time includes transfer, network round trip and client decoding.
    Three slow frames reduce the ceiling, with a two-second cooldown to avoid
    repeated keyframes. Ten seconds of healthy feedback permit gradual recovery.
    """

    def __init__(self):
        self.bitrate = DEFAULT_MAX_BITRATE
        self._slow_frames = 0
        self._healthy_since = None
        self._changed_at = float('-inf')

    def acknowledge(self, delay, now):
        if delay > .250:
            self._healthy_since = None
            self._slow_frames += 1
            if self._slow_frames >= 3 and now - self._changed_at >= 2:
                self.bitrate = max(1_000_000, self.bitrate * 7 // 10)
                self._changed_at = now
                self._slow_frames = 0
            return

        self._slow_frames = 0
        if delay > .150:
            self._healthy_since = None
            return
        if self._healthy_since is None:
            self._healthy_since = now
        if now - self._healthy_since >= 10:
            self.bitrate = min(DEFAULT_MAX_BITRATE, self.bitrate + 500_000)
            self._changed_at = now
            self._healthy_since = now
