"""Raw capture colors, aligned rows, buffer lifetime and real codec integration."""

import unittest
from unittest.mock import Mock

from mss.screenshot import ScreenShot
from PIL import Image, ImageChops, ImageStat

from remotedesk.host.desktop import Desktop
from remotedesk.network.video import VideoDecoder, VideoEncoder


class VideoCaptureTests(unittest.TestCase):
    def test_colors_row_alignment_and_ownership(self):
        for width in (11, 64, 321):
            with self.subTest(width=width):
                with Image.new('RGB', (width, 27)) as source:
                    source.paste((210, 50, 20), (0, 0, width, 9))
                    source.paste((20, 190, 50), (0, 9, width, 18))
                    source.paste((30, 50, 220), (0, 18, width, 27))
                    shot = ScreenShot.from_size(bytearray(source.tobytes('raw', 'BGRX')), width, 27)
                    desktop = Desktop()
                    desktop._capture = Mock()
                    desktop._capture.grab.return_value = shot
                    frame = desktop.capture_video_frame(1920)
                    self.assertEqual(frame.format.name, 'yuv444p')
                    # A subsequent grab may reuse its native buffer.
                    shot.raw[:] = bytes(len(shot.raw))
                    with frame.to_image() as image, ImageChops.difference(source, image) as diff:
                        self.assertLessEqual(max(high for _, high in diff.getextrema()), 3)

    def test_scaled_frames_and_bitrate_resets_decode_immediately(self):
        desktop = Desktop()
        desktop._capture = Mock()
        desktop._capture.grab.return_value = ScreenShot.from_size(
            bytearray([20, 50, 210, 0]) * 1921 * 1081, 1921, 1081)
        encoder, decoder = VideoEncoder(frame_rate=60), VideoDecoder()
        try:
            for width, bitrate in ((1920, 8_000_000), (1280, 2_000_000), (1920, 8_000_000)):
                encoder.set_bitrate(bitrate)
                frame = desktop.capture_video_frame(width)
                self.assertEqual(frame.width, width)
                with decoder.decode(encoder.encode_frame(frame)) as image:
                    self.assertEqual(image.size, (frame.width, frame.height))
                    for actual, expected in zip(ImageStat.Stat(image).mean, (210, 50, 20)):
                        self.assertLess(abs(actual - expected), 4)
        finally:
            encoder.close()
            decoder.close()

    def test_invalid_width_does_not_capture(self):
        desktop = Desktop()
        desktop._capture = Mock()
        for width in (None, True, 0, 3841, 1920.0):
            with self.assertRaises(ValueError):
                desktop.capture_video_frame(width)
        desktop._capture.grab.assert_not_called()
