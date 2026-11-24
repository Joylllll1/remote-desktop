"""Real software H.264 checks using synthetic pixels; no user screen capture."""

import io
import random
import struct
import unittest
from fractions import Fraction
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from remotedesk.network import video
from remotedesk.network.video import VideoDecoder, VideoEncoder


def packet(data, size=(320, 180), *, keyframe=True, version=1, flags=None):
    if flags is None:
        flags = int(keyframe)
    return b'H' + struct.pack('!BBHHI', version, flags, *size, len(data)) + data


def access_unit(nals):
    return b''.join(b'\x00\x00\x00\x01' + nal for nal in nals)


def encoded_nals(payload):
    return list(video._nals(payload[11:]))


def rgb_pixels(image):
    values = iter(image.tobytes())
    return zip(values, values, values)


def synthetic_sps(width, height, *, refs=1, crop_to=None):
    """A valid minimal High 4:4:4 SPS, including deliberately unsafe dimensions."""
    fields = []

    def integer(value, count):
        fields.append(f'{value:0{count}b}')

    def ue(value):
        encoded = f'{value + 1:b}'
        fields.append('0' * (len(encoded) - 1) + encoded)

    integer(244, 8)
    integer(0, 8)
    integer(50, 8)
    ue(0)
    ue(3)
    integer(0, 1)
    ue(0)
    ue(0)
    integer(0, 2)
    ue(0)
    ue(2)
    ue(refs)
    integer(0, 1)
    ue((width + 15) // 16 - 1)
    ue((height + 15) // 16 - 1)
    integer(1, 1)
    integer(1, 1)
    visible = crop_to or (width, height)
    right = (width + 15) // 16 * 16 - visible[0]
    bottom = (height + 15) // 16 * 16 - visible[1]
    integer(bool(right or bottom), 1)
    if right or bottom:
        ue(0)
        ue(right)
        ue(0)
        ue(bottom)
    integer(0, 1)
    fields.append('1')
    bits = ''.join(fields)
    bits += '0' * (-len(bits) % 8)
    raw = int(bits, 2).to_bytes(len(bits) // 8, 'big')
    escaped = bytearray()
    zeros = 0
    for value in raw:
        if zeros == 2 and value <= 3:
            escaped.append(3)
            zeros = 0
        escaped.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return b'\x67' + bytes(escaped)


class VideoBoundsTests(unittest.TestCase):
    def test_envelope_bounds_are_checked_without_loading_native_decoder(self):
        invalid = [
            None, b'', bytearray(b'H'), b'U', b'Jnot-a-video', b'H',
            packet(b'x', (0, 1)), packet(b'x', (video.MAX_WIDTH + 1, 1)),
            packet(b'x', (3840, 3840)), packet(b'x', version=2),
            packet(b'x', flags=2), packet(b'x')[:-1],
            packet(b'x') + b'trailing', b'H' + b'x' * video.MAX_FRAME,
        ]
        decoder = VideoDecoder()
        self.addCleanup(decoder.close)
        with patch.object(video, '_av', side_effect=AssertionError('native decoder must not load')):
            for value in invalid:
                with self.subTest(value=value[:16] if isinstance(value, bytes) else type(value)):
                    with self.assertRaises(ValueError):
                        decoder.decode(value)

    def test_malicious_sps_is_rejected_before_native_frame_allocation(self):
        # A matching small outer envelope cannot hide a huge coded surface or
        # a large reference-picture allocation in its SPS.
        invalid = [
            synthetic_sps(4096, 16),
            synthetic_sps(16, 4096),
            synthetic_sps(3840, 3840),
            synthetic_sps(3840, 2160, crop_to=(16, 16)),
            synthetic_sps(16, 16, refs=16),
            b'\x67\xf4\x00',
            b'\x67' + b'x' * 4096,
            b'\x67\xf4\x00\x00\x03\xff',
            b'\x67\xf4\x00\x00\x00\x03\x01',
        ]
        with patch.object(video, '_new_decoder', side_effect=AssertionError('unsafe native allocation')):
            for sps in invalid:
                with self.subTest(sps=sps[:20].hex()), self.assertRaises(ValueError):
                    VideoDecoder().decode(packet(access_unit([sps]), (16, 16)))

    def test_malformed_and_unbounded_nal_lists_are_rejected_before_native_decode(self):
        invalid = [
            b'not-annex-b', b'\x00\x00\x01', b'\x00\x00\x01\x87',
            access_unit([b'\x6fdata']), access_unit([b'\x09\xf0'] * 129),
            access_unit([b'\x06metadata']), access_unit([b'\x65\x80']),
            access_unit([synthetic_sps(16, 16), synthetic_sps(16, 16)]),
        ]
        with patch.object(video, '_new_decoder', side_effect=AssertionError('malformed native input')):
            for data in invalid:
                with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                    VideoDecoder().decode(packet(data, (16, 16)))

    def test_encoder_rejects_bad_image_and_bitrate_without_native_allocation(self):
        with Image.new('RGBA', (4, 4)) as rgba, Image.new('RGB', (3841, 1)) as wide:
            with patch.object(video, '_new_encoder', side_effect=AssertionError('native encoder must not load')):
                for image in (None, rgba, wide):
                    with self.subTest(image=type(image)), self.assertRaises(ValueError):
                        VideoEncoder().encode(image)
        for rate in (True, None, 0, 255_999, 32_000_001, 8_000_000.0):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                VideoEncoder(max_bitrate=rate)
        for rate in (True, False, None, 0, 29, 31, 59, 61, 60.0, '60'):
            with self.subTest(frame_rate=rate), self.assertRaises(ValueError):
                VideoEncoder(frame_rate=rate)
            with self.subTest(frame_rate=rate), self.assertRaises(ValueError):
                VideoEncoder().set_frame_rate(rate)


@unittest.skipUnless(video.encoder_available() and video.decoder_available(), 'PyAV H.264 wheel not installed')
class VideoCodecTests(unittest.TestCase):
    def setUp(self):
        self.encoder, self.decoder = VideoEncoder(), VideoDecoder()
        self.addCleanup(self.encoder.close)
        self.addCleanup(self.decoder.close)

    def encode_decode(self, image):
        payload = self.encoder.encode(image)
        result = self.decoder.decode(payload)
        self.addCleanup(result.close)
        return payload, result

    def test_each_input_immediately_produces_one_owned_rgb_frame_with_odd_dimensions(self):
        for index in range(10):
            with Image.new('RGB', (321, 181), (20 + index, 80, 170)) as image:
                payload, result = self.encode_decode(image)
                self.assertEqual(payload[:1], b'H')
                version, flags, width, height, length = struct.unpack_from('!BBHHI', payload, 1)
                self.assertEqual((version, width, height, length), (1, 321, 181, len(payload) - 11))
                self.assertEqual(bool(flags & 1), index == 0)
                self.assertEqual(result.mode, 'RGB')
                self.assertEqual(result.size, image.size)
                self.assertLess(max(abs(a - b) for a, b in zip(result.getpixel((100, 100)), image.getpixel((100, 100)))), 5)
                # A returned PIL image owns its pixels and cannot alter the
                # decoder's native reference picture for the next P frame.
                result.putpixel((100, 100), (255, 0, 255))
                result.close()

    def test_small_colored_text_and_single_pixel_strokes_keep_color_fidelity(self):
        with Image.new('RGB', (640, 360), '#f9fafb') as image:
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=13)
            for index, color in enumerate(('#d62228', '#1354d4', '#16803b')):
                draw.text((20, 20 + index * 22), 'Small 13px text: abcdef 0123456789', fill=color, font=font)
                draw.line((420 + index * 8, 20, 420 + index * 8, 110), fill=color, width=1)
            for frame_rate in (30, 60):
                with self.subTest(frame_rate=frame_rate):
                    self.encoder.set_frame_rate(frame_rate)
                    _, result = self.encode_decode(image)
                    foreground = [
                        abs(actual - expected)
                        for original, decoded in zip(rgb_pixels(image), rgb_pixels(result))
                        if original != (249, 250, 251)
                        for expected, actual in zip(original, decoded)
                    ]
                    self.assertLess(sum(foreground) / len(foreground), 9)
                    for index in range(3):
                        expected = image.getpixel((420 + index * 8, 70))
                        actual = result.getpixel((420 + index * 8, 70))
                        self.assertLess(max(abs(a - b) for a, b in zip(expected, actual)), 18)

    def test_scrolling_interframes_are_significantly_smaller_than_full_jpegs(self):
        with Image.new('RGB', (960, 1000), '#f5f7fb') as canvas:
            draw = ImageDraw.Draw(canvas)
            font = ImageFont.load_default(size=14)
            draw.rectangle((0, 0, 160, 999), fill='#24374d')
            for row in range(50):
                y = row * 20
                draw.rectangle((180, y + 2, 210 + row % 5 * 30, y + 14), fill=('#467db0' if row % 2 else '#7c9c55'))
                draw.text((370, y + 1), f'row {row:03d}: application window with text 0123456789', fill='#263b50', font=font)
                draw.line((175, y + 19, 945, y + 19), fill='#d6dce4')
            video_bytes = jpeg_bytes = 0
            for index in range(24):
                with canvas.crop((0, index * 8, 960, index * 8 + 540)) as image:
                    payload, result = self.encode_decode(image)
                    if index:
                        self.assertFalse(payload[2] & 1)
                        video_bytes += len(payload)
                        output = io.BytesIO()
                        image.save(output, format='JPEG', quality=75, subsampling=0)
                        jpeg_bytes += len(output.getvalue())
                    self.assertEqual(result.size, image.size)
            self.assertLess(video_bytes, jpeg_bytes * 0.4)

    def test_resolution_close_and_bitrate_changes_send_self_contained_idrs(self):
        for size in ((320, 180), (127, 73), (1920, 1080), (1, 1)):
            with Image.new('RGB', size, '#3475a2') as image:
                payload, result = self.encode_decode(image)
                self.assertTrue(payload[2] & 1)
                self.assertEqual(result.size, size)
                with VideoDecoder().decode(payload) as restarted:
                    self.assertEqual(restarted.size, size)
        with Image.new('RGB', (1, 1), '#3475a2') as image:
            self.encoder.set_bitrate(4_000_000)
            self.assertTrue(self.encoder.encode(image)[2] & 1)
            self.assertFalse(self.encoder.encode(image)[2] & 1)
            self.encoder.set_bitrate(4_000_000)
            self.assertFalse(self.encoder.encode(image)[2] & 1)
            self.encoder.close()
            self.encoder.close()
            payload = self.encoder.encode(image)
            self.assertTrue(payload[2] & 1)
            self.decoder.close()
            self.decoder.close()
            with self.decoder.decode(payload) as restarted:
                self.assertEqual(restarted.size, image.size)

    def test_frame_rate_switch_resets_timing_and_delivers_idr_then_immediate_p_frame(self):
        with Image.new('RGB', (321, 181), '#3475a2') as image:
            self.encode_decode(image)
            for frame_rate in (60, 30, 60):
                self.encoder.set_frame_rate(frame_rate)
                keyframe = self.encoder.encode(image)
                self.assertTrue(keyframe[2] & 1)
                self.assertEqual(self.encoder._codec.time_base, Fraction(1, frame_rate))
                self.assertEqual(self.encoder._codec.framerate, Fraction(frame_rate, 1))
                self.assertEqual(self.encoder._codec.gop_size, frame_rate * 2)
                # An existing decoder and one joining at the changed rate both
                # consume the new SPS/PPS/IDR followed by its P frame immediately.
                fresh = VideoDecoder()
                try:
                    for decoder in (self.decoder, fresh):
                        with decoder.decode(keyframe) as result:
                            self.assertEqual(result.size, image.size)
                    self.encoder.set_frame_rate(frame_rate)
                    for invalid in (True, 60.0, 24):
                        with self.assertRaises(ValueError):
                            self.encoder.set_frame_rate(invalid)
                    interframe = self.encoder.encode(image)
                    self.assertFalse(interframe[2] & 1)
                    for decoder in (self.decoder, fresh):
                        with decoder.decode(interframe) as result:
                            self.assertEqual(result.size, image.size)
                finally:
                    fresh.close()

    def test_decoder_requires_new_keyframe_after_close_and_rejects_dimension_lies(self):
        with Image.new('RGB', (321, 181), '#236d9c') as image:
            keyframe, _ = self.encode_decode(image)
            interframe = self.encoder.encode(image)
            self.decoder.close()
            with self.assertRaises(ValueError):
                self.decoder.decode(interframe)
            with patch.object(video, '_new_decoder', side_effect=AssertionError('SPS mismatch must be checked first')):
                for size in ((320, 181), (321, 180)):
                    with self.subTest(size=size), self.assertRaises(ValueError):
                        self.decoder.decode(packet(keyframe[11:], size))
            with self.decoder.decode(keyframe) as recovered:
                self.assertEqual(recovered.size, image.size)

    def test_duplicate_pictures_missing_parameters_and_b_slices_are_rejected_before_native_decode(self):
        with Image.new('RGB', (320, 180), '#236d9c') as image:
            payload = self.encoder.encode(image)
        nals = encoded_nals(payload)
        sps = next(n for n in nals if n[0] & 31 == 7)
        pps = next(n for n in nals if n[0] & 31 == 8)
        slices = [n for n in nals if n[0] & 31 == 5]
        invalid = [
            [sps, *slices], [pps, sps, *slices], [sps, pps],
            [sps, pps, *slices, slices[0]],
            [sps, pps, b'\x65\xa0'],  # first_mb=0, B slice type=1
            [sps, pps, *slices, b'\x6funsupported extension'],
        ]
        with patch.object(video, '_new_decoder', side_effect=AssertionError('invalid picture structure')):
            for nals in invalid:
                with self.subTest(nals=[n[0] & 31 for n in nals]), self.assertRaises(ValueError):
                    self.decoder.decode(packet(access_unit(nals)))

    def test_invalid_outer_packet_does_not_consume_reference_picture(self):
        with Image.new('RGB', (320, 180), '#236d9c') as image:
            self.encode_decode(image)
            payload = self.encoder.encode(image)
            with self.assertRaises(ValueError):
                self.decoder.decode(payload + b'extra')
            with self.decoder.decode(payload) as result:
                self.assertEqual(result.size, image.size)

    def test_native_decode_error_discards_reference_chain(self):
        with Image.new('RGB', (320, 180), '#236d9c') as image:
            self.encode_decode(image)
            payload = self.encoder.encode(image)
            class FailingDecoder:
                def decode(self, packet):
                    raise ValueError('broken native access unit')
            self.decoder._codec = FailingDecoder()
            with self.assertRaises(ValueError):
                self.decoder.decode(payload)
            self.assertIsNone(self.decoder._codec)
            self.assertIsNone(self.decoder._sequence)
            with self.assertRaises(ValueError):
                self.decoder.decode(payload)

    def test_real_truncated_access_unit_is_rejected(self):
        rng = random.Random(12)
        for index in range(2):
            with Image.frombytes('RGB', (640, 360), rng.randbytes(640 * 360 * 3)) as image:
                payload = self.encoder.encode(image)
                if index == 0:
                    with self.decoder.decode(payload):
                        pass
        truncated = access_unit(nal[:5] for nal in encoded_nals(payload))
        with self.assertRaises(ValueError):
            self.decoder.decode(packet(truncated, (640, 360), keyframe=False))

    def test_periodic_idrs_and_high_entropy_frames_remain_bounded(self):
        rng = random.Random(19)
        for frame_rate in (30, 60):
            self.encoder.set_frame_rate(frame_rate)
            self.encoder.close()
            total = 0
            frame_count = frame_rate * 2 + 5
            for index in range(frame_count):
                with Image.frombytes('RGB', (320, 180), rng.randbytes(320 * 180 * 3)) as image:
                    payload, result = self.encode_decode(image)
                    self.assertLessEqual(len(payload), video.MAX_FRAME)
                    self.assertEqual(bool(payload[2] & 1), index in (0, frame_rate * 2))
                    self.assertEqual(result.size, image.size)
                    total += len(payload)
            # VBV permits a short initial burst, then caps aggregate encoded data.
            self.assertLess(total, 8_000_000 / 8 * (frame_count / frame_rate + 0.2))


if __name__ == '__main__':
    unittest.main()
