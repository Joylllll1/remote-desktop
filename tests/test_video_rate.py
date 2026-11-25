"""Deterministic delivery feedback, hysteresis, and bandwidth limits."""

import unittest

from remotedesk.network.rate import VideoRateControl


class VideoRateTests(unittest.TestCase):
    def test_slow_burst_reduces_ceiling_once_and_has_a_floor(self):
        rate = VideoRateControl()
        rate.acknowledge(.4, 1)
        rate.acknowledge(.4, 1.1)
        self.assertEqual(rate.bitrate, 8_000_000)
        rate.acknowledge(.4, 1.2)
        self.assertEqual(rate.bitrate, 5_600_000)
        for _ in range(20):
            rate.acknowledge(.4, 1.3)
        self.assertEqual(rate.bitrate, 5_600_000)
        for now in range(4, 200):
            rate.acknowledge(.4, now)
        self.assertEqual(rate.bitrate, 1_000_000)

    def test_recovery_requires_sustained_healthy_feedback(self):
        rate = VideoRateControl()
        for _ in range(3):
            rate.acknowledge(.4, 0)
        rate.acknowledge(.1, 1)
        rate.acknowledge(.1, 10)
        self.assertEqual(rate.bitrate, 5_600_000)
        rate.acknowledge(.2, 10.5)
        rate.acknowledge(.1, 11)
        rate.acknowledge(.1, 20)
        self.assertEqual(rate.bitrate, 5_600_000)
        rate.acknowledge(.1, 21)
        self.assertEqual(rate.bitrate, 6_100_000)
        for now in range(22, 200):
            rate.acknowledge(.1, now)
        self.assertEqual(rate.bitrate, 8_000_000)

    def test_high_rtt_uncongested_relay_keeps_bitrate_and_expands_frame_window(self):
        rate = VideoRateControl()
        # Simulate 160 ms RTT + decode (~170 ms) without queuing congestion.
        for index in range(12):
            rate.acknowledge(.170, index * .05)
        self.assertEqual(rate.bitrate, 8_000_000)
        self.assertEqual(rate.frame_window(4, 8, 60), 8)
        # Excess queuing delay above baseline (> 0.170 + 0.110 = 0.280) still throttles
        # and immediately shrinks the flight window back to base_limit.
        for index in range(3):
            rate.acknowledge(.350, 2 + index * .1)
        self.assertEqual(rate.bitrate, 5_600_000)
        self.assertEqual(rate.frame_window(4, 8, 60), 4)
        # Sustained healthy feedback at 170 ms can recover bitrate on a high-RTT relay.
        for now in range(5, 20):
            rate.acknowledge(.170, now)
        self.assertGreater(rate.bitrate, 5_600_000)
        self.assertEqual(rate.frame_window(4, 8, 60), 8)
