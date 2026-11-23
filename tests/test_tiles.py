"""Synthetic codec/capture checks: no user screens, controllers or network."""

import io
import random
import struct
import types
import unittest
import zlib
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, JpegImagePlugin
from remotedesk.host.desktop import Desktop
from remotedesk.network import tiles
from remotedesk.network.tiles import TileDecoder, TileEncoder


def png(image):
    output = io.BytesIO()
    image.save(output, format='PNG', compress_level=3)
    return output.getvalue()


def patch_packet(size, patches):
    return (b'T' + struct.pack('!HHH', *size, len(patches)) +
            b''.join(struct.pack('!HHHHI', x, y, w, h, len(data)) + data
                     for x, y, w, h, data in patches))


def rectangles(payload):
    width, height, count = struct.unpack_from('!HHH', payload, 1)
    position = 7
    result = []
    for _ in range(count):
        x, y, w, h, length = struct.unpack_from('!HHHHI', payload, position)
        position += 12
        result.append((x, y, w, h, payload[position:position + length]))
        position += length
    return (width, height), result


def chunk(kind, body):
    return struct.pack('!I', len(body)) + kind + body + struct.pack('!I', zlib.crc32(kind + body) & 0xffffffff)


class TileCodecTests(unittest.TestCase):
    def setUp(self):
        self.encoder, self.decoder = TileEncoder(), TileDecoder()
        self.addCleanup(self.encoder.close)
        self.addCleanup(self.decoder.close)
        self.image = Image.new('RGB', (256, 256), '#f3f5f8')
        self.addCleanup(self.image.close)

    def prime(self, image=None):
        payload = self.encoder.encode(image or self.image)
        result = self.decoder.decode(payload)
        self.addCleanup(result.close)
        return payload, result

    def assert_bad_keeps_base(self, payload):
        before = self.decoder.decode(b'U')
        try:
            with self.assertRaises(ValueError):
                self.decoder.decode(payload)
            after = self.decoder.decode(b'U')
            try:
                self.assertEqual(after.size, before.size)
                self.assertEqual(after.tobytes(), before.tobytes())
            finally:
                after.close()
        finally:
            before.close()

    def test_first_frame_is_quality_75_444_jpeg_and_owned_rgb(self):
        payload, decoded = self.prime()
        self.assertEqual(payload[:1], b'J')
        with Image.open(io.BytesIO(payload[1:])) as source:
            self.assertEqual(JpegImagePlugin.get_sampling(source), 0)
            expected = io.BytesIO()
            self.image.save(expected, format='JPEG', quality=75, subsampling=0)
            self.assertEqual(payload[1:], expected.getvalue())
        self.assertEqual(decoded.mode, 'RGB')
        self.assertEqual(decoded.size, self.image.size)
        decoded.putpixel((0, 0), (255, 0, 0))
        with self.decoder.decode(b'U') as next_frame:
            self.assertNotEqual(next_frame.getpixel((0, 0)), (255, 0, 0))

    def test_1080_text_and_cursor_updates_are_tiny_lossless_dirty_rectangles(self):
        with Image.new('RGB', (1920, 1080), '#edf1f5') as desktop:
            draw = ImageDraw.Draw(desktop)
            draw.rectangle((0, 0, 300, 1080), fill='#243447')
            for index in range(50):
                draw.text((330, 20 + index * 20), f'line {index:03d}: synthetic UI text and status', fill='#273d50')
            keyframe, _ = self.prime(desktop)
            with desktop.copy() as changed:
                changed_draw = ImageDraw.Draw(changed)
                changed_draw.text((650, 125), 'updated', fill='#c02719')
                changed_draw.rectangle((1105, 707, 1108, 723), fill='#121212')
                payload = self.encoder.encode(changed)
                self.assertEqual(payload[:1], b'T')
                self.assertLess(len(payload), len(keyframe) // 20)
                dimensions, patches = rectangles(payload)
                self.assertEqual(dimensions, (1920, 1080))
                with self.decoder.decode(payload) as result:
                    for x, y, w, h, _ in patches:
                        self.assertLessEqual(w, 128)
                        self.assertLessEqual(h, 128)
                        with result.crop((x, y, x + w, y + h)) as actual, changed.crop((x, y, x + w, y + h)) as expected:
                            self.assertEqual(actual.tobytes(), expected.tobytes())
                    self.assertEqual(result.getpixel((1105, 710)), (18, 18, 18))
                self.assertEqual(self.encoder.encode(changed), b'U')

    def test_tiny_update_avoids_full_jpeg_encode(self):
        self.prime()
        self.image.putpixel((17, 22), (250, 10, 20))
        with patch.object(self.encoder, '_jpeg', side_effect=AssertionError('full encode on tiny change')):
            payload = self.encoder.encode(self.image)
        self.assertEqual(payload[:1], b'T')
        _, entries = rectangles(payload)
        self.assertEqual(entries[0][:4], (17, 22, 1, 1))

    def test_history_is_raw_copy_and_caller_mutation_is_detected(self):
        self.prime()
        old = self.encoder._previous
        self.image.putpixel((44, 55), (15, 26, 37))
        payload = self.encoder.encode(self.image)
        self.assertEqual(payload[:1], b'T')
        self.assertEqual(self.encoder._previous.getpixel((44, 55)), (15, 26, 37))
        with self.assertRaises(ValueError):
            old.getpixel((0, 0))
        self.assertEqual(self.encoder.encode(self.image), b'U')

    def test_dense_changes_and_resolution_changes_use_keyframes(self):
        self.prime()
        with Image.new('RGB', (256, 256), '#23567c') as dense:
            self.assertEqual(self.encoder.encode(dense)[:1], b'J')
        with Image.new('RGB', (400, 200), '#23567c') as resized:
            payload = self.encoder.encode(resized)
            self.assertEqual(payload[:1], b'J')
            with self.decoder.decode(payload) as result:
                self.assertEqual(result.size, resized.size)

    def test_high_entropy_patch_falls_back_when_jpeg_is_smaller(self):
        self.prime()
        noise = Image.frombytes('RGB', (128, 128), random.Random(41).randbytes(128 * 128 * 3))
        self.addCleanup(noise.close)
        self.image.paste(noise, (0, 0))
        patch_only = patch_packet(self.image.size, [(0, 0, 128, 128, png(noise))])
        payload = self.encoder.encode(self.image)
        self.assertEqual(payload[:1], b'J')
        self.assertLess(len(payload), len(patch_only))

    def test_more_than_maximum_dirty_tiles_falls_back_to_keyframe(self):
        with Image.new('RGB', (2880, 2880), 'white') as image:
            self.encoder.encode(image)
            for y in range(0, 2880, 128):
                for x in range(0, 2880, 128):
                    image.putpixel((x, y), (0, 0, 0))
            self.assertEqual(self.encoder.encode(image)[:1], b'J')

    def test_no_base_rejects_unchanged_or_patch(self):
        for payload in (b'U', patch_packet((1, 1), [(0, 0, 1, 1, png(Image.new('RGB', (1, 1))))])):
            with self.subTest(payload=payload[:8]), self.assertRaises(ValueError):
                self.decoder.decode(payload)

    def test_owned_return_images_cannot_mutate_or_close_decoder_base(self):
        _, result = self.prime()
        original = self.decoder.decode(b'U')
        expected = original.tobytes()
        original.close()
        result.close()
        with self.decoder.decode(b'U') as unchanged:
            self.assertEqual(unchanged.tobytes(), expected)

    def test_close_releases_references_and_is_idempotent(self):
        self.prime()
        previous, base = self.encoder._previous, self.decoder._base
        self.encoder.close(); self.encoder.close()
        self.decoder.close(); self.decoder.close()
        self.assertIsNone(self.encoder._previous)
        self.assertIsNone(self.decoder._base)
        for image in (previous, base):
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))
        self.assertEqual(self.encoder.encode(self.image)[:1], b'J')
        with self.assertRaises(ValueError):
            self.decoder.decode(b'U')

    def test_encoder_rejects_non_rgb_or_oversized_images(self):
        with Image.new('RGBA', (10, 10)) as rgba, Image.new('RGB', (3841, 1)) as wide:
            for image in (None, rgba, wide):
                with self.subTest(image=image), self.assertRaises(ValueError):
                    self.encoder.encode(image)

    def test_rejects_packet_type_size_prefix_and_truncation_atomically(self):
        self.prime()
        invalid = [None, b'', bytearray(b'U'), b'X', b'Uextra', b'J', b'Jnotjpeg', b'T', b'T\x00', b'J' + b'x' * tiles.MAX_FRAME]
        for payload in invalid:
            with self.subTest(payload_type=type(payload), length=len(payload) if payload is not None else 0):
                self.assert_bad_keeps_base(payload)

    def test_patch_bounds_dimensions_counts_lengths_and_overlap(self):
        self.prime()
        with Image.new('RGB', (10, 10), 'red') as small:
            data = png(small)
        invalid = [
            b'T' + struct.pack('!HHH', 256, 256, 0),
            b'T' + struct.pack('!HHH', 256, 256, 513),
            b'T' + struct.pack('!HHH', 3840, 3840, 1),
            patch_packet((255, 256), [(0, 0, 10, 10, data)]),
            patch_packet((256, 256), [(250, 0, 10, 10, data)]),
            patch_packet((256, 256), [(0, 250, 10, 10, data)]),
            patch_packet((256, 256), [(0, 0, 0, 10, data)]),
            patch_packet((256, 256), [(0, 0, 129, 10, data)]),
            patch_packet((256, 256), [(0, 0, 10, 129, data)]),
            patch_packet((256, 256), [(0, 0, 10, 9, data)]),
            patch_packet((256, 256), [(0, 0, 10, 10, b'')]),
            patch_packet((256, 256), [(0, 0, 10, 10, data)])[:-1],
            patch_packet((256, 256), [(0, 0, 10, 10, data)]) + b'extra',
            patch_packet((256, 256), [(0, 0, 10, 10, data), (5, 5, 10, 10, data)]),
            b'T' + struct.pack('!HHH', 256, 256, 1) + struct.pack('!HHHHI', 0, 0, 10, 10, 0xffffffff),
        ]
        for payload in invalid:
            with self.subTest(header=payload[:20]):
                self.assert_bad_keeps_base(payload)

    def test_png_chunks_crc_format_and_header_are_checked_before_loading(self):
        self.prime()
        with Image.new('RGB', (4, 4), 'red') as small:
            valid = png(small)
            jpeg = io.BytesIO(); small.save(jpeg, format='JPEG')
        end_ihdr = 33
        invalid = [
            jpeg.getvalue(), valid[:-1], valid + b'extra',
            valid[:end_ihdr] + chunk(b'tEXt', b'name\x00value') + valid[end_ihdr:],
            valid[:end_ihdr] + chunk(b'iCCP', b'profile\x00\x00' + zlib.compress(b'x' * 10000)) + valid[end_ihdr:],
            valid[:end_ihdr] + valid[8:end_ihdr] + valid[end_ihdr:],
            valid[:-1] + bytes([valid[-1] ^ 1]),
            tiles._PNG + chunk(b'IHDR', struct.pack('!IIBBBBB', 50000, 50000, 8, 2, 0, 0, 0)) + valid[end_ihdr:],
        ]
        for data in invalid:
            with self.subTest(data=data[:40]):
                self.assert_bad_keeps_base(patch_packet((256, 256), [(0, 0, 4, 4, data)]))

    def test_malformed_late_patch_never_copies_or_changes_the_base(self):
        self.prime()
        with Image.new('RGB', (4, 4), 'red') as small:
            good = png(small)
        payload = patch_packet((256, 256), [(0, 0, 4, 4, good), (128, 128, 4, 4, b'bad')])
        before = self.decoder._base.tobytes()
        with patch.object(self.decoder._base, 'copy', side_effect=AssertionError('base copied before validation')):
            with self.assertRaises(ValueError):
                self.decoder.decode(payload)
        self.assertEqual(self.decoder._base.tobytes(), before)

    def test_jpeg_dimensions_are_checked_before_pixel_load_and_base_survives(self):
        payload, _ = self.prime()
        data = bytearray(payload[1:])
        marker = data.index(b'\xff\xc0')
        struct.pack_into('!H', data, marker + 7, 3841)
        with patch.object(JpegImagePlugin.JpegImageFile, 'load', side_effect=AssertionError('oversized JPEG loaded')) as load:
            with self.assertRaises(ValueError):
                self.decoder.decode(b'J' + data)
            load.assert_not_called()
        with self.decoder.decode(b'U') as base:
            self.assertEqual(base.size, (256, 256))


class CaptureImageTests(unittest.TestCase):
    def make_desktop(self, size, monitor=None):
        source = Image.new('RGB', size, '#46789a')
        self.addCleanup(source.close)
        shot = types.SimpleNamespace(size=size, bgra=source.tobytes('raw', 'BGRX'))
        desktop = Desktop()
        desktop._capture = types.SimpleNamespace(grab=Mock(return_value=shot))
        desktop._image = Image
        desktop._monitor = monitor or dict(left=0, top=0, width=size[0], height=size[1])
        return desktop, source

    def test_default_capture_preserves_1920_clarity_and_does_not_adapt_or_upscale(self):
        desktop, _ = self.make_desktop((1920, 1080))
        with patch.object(desktop, '_frame_profile', side_effect=AssertionError('adaptive capture')):
            with desktop.capture_image() as captured:
                self.assertEqual(captured.size, (1920, 1080))
                self.assertEqual(captured.mode, 'RGB')
        desktop, _ = self.make_desktop((640, 400))
        with desktop.capture_image() as captured:
            self.assertEqual(captured.size, (640, 400))

    def test_retina_pixel_resize_keeps_normalized_input_in_monitor_coordinates(self):
        geometry = dict(left=-1920, top=35, width=1920, height=1080)
        desktop, _ = self.make_desktop((3840, 2160), geometry)
        with desktop.capture_image(max_width=1920) as captured:
            self.assertEqual(captured.size, (1920, 1080))
        self.assertEqual(desktop._point({'x': 1, 'y': 1}), (-1, 1114))
        self.assertEqual(desktop._monitor, geometry)

    def test_capture_bounds_tall_and_large_images_and_preserves_aspect(self):
        for size in ((200, 8000), (3840, 3000)):
            with self.subTest(size=size):
                desktop, _ = self.make_desktop(size)
                with desktop.capture_image(max_width=3840) as captured:
                    self.assertLessEqual(captured.width, 3840)
                    self.assertLessEqual(captured.height, 3840)
                    self.assertLessEqual(captured.width * captured.height, tiles.MAX_PIXELS)
                    self.assertLessEqual(captured.width, size[0])
                    self.assertLessEqual(captured.height, size[1])
                    self.assertAlmostEqual(captured.width / captured.height, size[0] / size[1], delta=.002)
                    tiles._dimensions(captured.size)

    def test_capture_returns_independent_image_and_validates_width_before_grab(self):
        desktop, source = self.make_desktop((64, 32))
        first = desktop.capture_image()
        first.putpixel((0, 0), (0, 0, 0))
        first.close()
        with desktop.capture_image() as second:
            self.assertEqual(second.getpixel((0, 0)), source.getpixel((0, 0)))
        previous_calls = desktop._capture.grab.call_count
        for width in (None, 0, -1, True, 3841, 1920.0):
            with self.subTest(width=width), self.assertRaises(ValueError):
                desktop.capture_image(width)
        self.assertEqual(desktop._capture.grab.call_count, previous_calls)

    def test_legacy_frame_keeps_adaptive_quality_and_width(self):
        desktop, source = self.make_desktop((1920, 1080))
        with patch.object(desktop, '_frame_profile', return_value=(1280, 55)) as profile, \
                patch.object(desktop, 'capture_image', wraps=desktop.capture_image) as capture:
            payload = desktop.frame()
        profile.assert_called_once_with()
        capture.assert_called_once_with(max_width=1280)
        expected = io.BytesIO()
        source.thumbnail((1280, 720), Image.Resampling.BILINEAR)
        source.save(expected, format='JPEG', quality=55)
        self.assertEqual(payload, expected.getvalue())


if __name__ == '__main__':
    unittest.main()
