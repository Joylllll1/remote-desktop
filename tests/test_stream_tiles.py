"""V3 image/profile integration checks using socketpairs and synthetic RGB UI."""
import io
import json
import socket
import threading
import time
import unittest
from contextlib import suppress
from unittest.mock import patch

from PIL import Image, ImageDraw
from remotedesk.network import session, stream


def synthetic_ui():
    image = Image.new('RGB', (1920, 1080), '#e9edf3')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1919, 45), fill='#21415a')
    draw.rectangle((20, 65, 215, 1055), fill='#d3dce5')
    draw.rectangle((235, 65, 1895, 1055), fill='white')
    for row, y in enumerate(range(85, 1030, 24)):
        for column, x in enumerate((250, 660, 1070, 1480)):
            draw.text((x, y), f'Item {row:02d}/{column}   desktop status: ready', fill='#18354a')
    return image


class RGBDesktop:
    def __init__(self, blocked=False):
        self.image = synthetic_ui()
        self.condition = threading.Condition()
        self.calls = []
        self.widths = []
        self.held = False
        self.capture_started = threading.Event()
        self.capture_gate = threading.Event()
        if not blocked:
            self.capture_gate.set()

    @property
    def has_held_input(self):
        with self.condition:
            return self.held

    def apply(self, events):
        with self.condition:
            self.calls.append([dict(event) for event in events])
            for event in events:
                if event['type'] == 'release':
                    self.held = False
                elif event['type'] in ('key', 'button'):
                    self.held = event['down']
            self.condition.notify_all()

    def wait_for(self, predicate):
        with self.condition:
            return self.condition.wait_for(lambda: predicate(self.calls), timeout=3)

    def capture_image(self, max_width):
        self.capture_started.set()
        if not self.capture_gate.wait(5):
            raise RuntimeError('test did not unblock synthetic capture')
        with self.condition:
            self.widths.append(max_width)
            copy = self.image.copy()
        if copy.width > max_width:
            copy.thumbnail((max_width, round(copy.height * max_width / copy.width)), Image.Resampling.LANCZOS)
        return copy

    def frame(self):
        with self.capture_image(1920) as image:
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=75)
            return output.getvalue()

    def update_small_region(self):
        with self.condition:
            draw = ImageDraw.Draw(self.image)
            draw.rectangle((420, 210, 440, 235), fill='#195b9b')
            draw.rectangle((500, 250, 680, 267), fill='white')
            draw.text((500, 250), 'Cursor/text updated', fill='#103050')


class TileStreamTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []
        self.threads = []
        self.desktops = []

    def tearDown(self):
        for desktop in self.desktops:
            desktop.capture_gate.set()
        for sock in self.sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'synthetic tile server thread leaked')
        for desktop in self.desktops:
            desktop.image.close()

    def pair(self):
        client, server = socket.socketpair()
        client.settimeout(3)
        server.settimeout(3)
        self.sockets.extend((client, server))
        return client, server

    def serve(self, desktop=None, version=3):
        desktop = desktop or RGBDesktop()
        self.desktops.append(desktop)
        client, server = self.pair()
        errors = []
        finished = threading.Event()

        def run():
            try:
                stream.serve_stream(server, desktop, version=version)
            except Exception as error:
                errors.append(error)
            finally:
                server.close()
                finished.set()

        thread = threading.Thread(target=run, name='test-tile-server', daemon=True)
        self.threads.append(thread)
        thread.start()
        return session.Session(client), desktop, errors, finished

    def finish(self, client, errors, finished):
        client.close()
        self.assertTrue(finished.wait(3))
        self.assertEqual(errors, [])

    def test_first_delayed_frame_is_1920_without_fast_frame_warmup(self):
        original = stream.write_packet
        frame_types = []

        def delayed(sock, payload, *args, **kwargs):
            if payload[:1] in (b'J', b'T', b'U'):
                frame_types.append(payload[:1])
                threading.Event().wait(.31)
            return original(sock, payload, *args, **kwargs)

        with patch.object(stream, 'write_packet', side_effect=delayed):
            client, desktop, errors, finished = self.serve()
            self.assertTrue(client.supports_resolution_control)
            started = time.monotonic()
            with client.exchange_image([]) as image:
                self.assertEqual(image.size, (1920, 1080))
            self.assertGreaterEqual(time.monotonic() - started, .3)
            self.assertEqual(desktop.widths, [1920])
            self.assertEqual(frame_types, [b'J'])
            self.finish(client, errors, finished)

    def test_small_cursor_and_text_changes_use_smaller_tile_patch(self):
        original = stream.write_packet
        payloads = []

        def observed(sock, payload, *args, **kwargs):
            if payload[:1] in (b'J', b'T', b'U'):
                payloads.append(payload)
            return original(sock, payload, *args, **kwargs)

        with patch.object(stream, 'write_packet', side_effect=observed):
            client, desktop, errors, finished = self.serve()
            first = client.exchange_image([])
            before = first.tobytes()
            desktop.update_small_region()
            second = client.exchange_image([])
            self.assertEqual(second.size, first.size)
            self.assertIsNot(first, second)
            self.assertNotEqual(first.tobytes(), second.tobytes())
            self.assertEqual(first.tobytes(), before, 'updating decoder must not mutate displayed image')
            self.assertEqual([payload[:1] for payload in payloads], [b'J', b'T'])
            self.assertLess(len(payloads[1]), len(payloads[0]))
            first.close()
            second.close()
            self.finish(client, errors, finished)

    def test_profile_switch_1280_then_1920_replaces_full_bases(self):
        original = stream.write_packet
        frame_types = []

        def observed(sock, payload, *args, **kwargs):
            if payload[:1] in (b'J', b'T', b'U'):
                frame_types.append(payload[:1])
            return original(sock, payload, *args, **kwargs)

        with patch.object(stream, 'write_packet', side_effect=observed):
            client, desktop, errors, finished = self.serve()
            with client.exchange_image([]) as first:
                self.assertEqual(first.size, (1920, 1080))
            client.set_resolution(1280)
            with client.exchange_image([]) as small:
                self.assertEqual(small.size, (1280, 720))
            client.set_resolution(1920)
            with client.exchange_image([]) as restored:
                self.assertEqual(restored.size, (1920, 1080))
            self.assertEqual(desktop.widths, [1920, 1280, 1920])
            self.assertEqual(frame_types, [b'J', b'J', b'J'])
            self.finish(client, errors, finished)

    def test_returned_images_are_independent_of_cache_and_caller_mutation(self):
        client, _, errors, finished = self.serve()
        first = client.exchange_image([])
        expected = first.getpixel((1850, 1040))
        first.putpixel((1850, 1040), (255, 0, 255))
        with client.exchange_image([]) as second:
            self.assertEqual(second.getpixel((1850, 1040)), expected)
            self.assertNotEqual(second.getpixel((1850, 1040)), first.getpixel((1850, 1040)))
        first.close()
        with client.exchange_image([]) as third:
            self.assertEqual(third.size, (1920, 1080))
        self.finish(client, errors, finished)

    def test_exchange_exports_a_valid_jpeg_for_check_cli(self):
        client, _, errors, finished = self.serve()
        payload = client.exchange([])
        with Image.open(io.BytesIO(payload), formats=('JPEG',)) as image:
            image.load()
            self.assertEqual(image.size, (1920, 1080))
        self.finish(client, errors, finished)

    def test_invalid_wire_width_releases_without_applying_its_events(self):
        for width in (True, 1920.0, None, 0, 1600, 10000):
            with self.subTest(width=width):
                client, desktop, errors, finished = self.serve()
                held = [{'type': 'key', 'key': 'a', 'down': True}]
                forbidden = [{'type': 'key', 'key': 'b', 'down': True}]
                client.send_input(held)
                self.assertTrue(desktop.wait_for(lambda calls: held in calls))
                command = json.dumps({'events': forbidden, 'frame': True, 'width': width}).encode()
                stream.write_packet(client.sock, command, session.MAX_INPUT)
                self.assertTrue(finished.wait(3))
                self.assertTrue(errors)
                self.assertEqual(desktop.calls, [held, [{'type': 'release'}]])
                self.assertFalse(desktop.has_held_input)
                self.assertEqual(desktop.widths, [])
                client.close()

    def test_valid_width_is_rejected_on_input_only_command(self):
        client, desktop, errors, finished = self.serve()
        command = json.dumps({'events': [{'type': 'key', 'key': 'b', 'down': True}],
                              'frame': False, 'width': 1920}).encode()
        stream.write_packet(client.sock, command, session.MAX_INPUT)
        self.assertTrue(finished.wait(3))
        self.assertTrue(errors)
        self.assertEqual(desktop.calls, [[{'type': 'release'}]])
        self.assertEqual(desktop.widths, [])
        client.close()

    def test_bad_local_profile_does_not_send_a_command(self):
        client, desktop, errors, finished = self.serve()
        for width in (True, 1280.0, 1600, '1920'):
            with self.subTest(width=width):
                with self.assertRaises(ValueError):
                    client.set_resolution(width)
        self.assertEqual(desktop.calls, [])
        with client.exchange_image([]) as image:
            self.assertEqual(image.size, (1920, 1080))
        self.finish(client, errors, finished)

    def test_v2_image_fallback_has_no_resolution_control(self):
        client, _, errors, finished = self.serve(version=2)
        self.assertTrue(client.supports_async_input)
        self.assertFalse(client.supports_resolution_control)
        with self.assertRaises(RuntimeError):
            client.set_resolution(1280)
        first = client.exchange_image([])
        second = client.exchange_image([])
        self.assertIsNot(first, second)
        self.assertEqual(first.size, (1920, 1080))
        self.assertEqual(second.size, first.size)
        first.close()
        second.close()
        self.finish(client, errors, finished)

    def test_independent_input_reaches_desktop_while_rgb_capture_is_blocked(self):
        desktop = RGBDesktop(blocked=True)
        client, _, server_errors, finished = self.serve(desktop)
        images, errors = [], []
        done = threading.Event()

        def receive():
            try:
                images.append(client.exchange_image([]))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=receive, name='test-tile-receiver', daemon=True)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(desktop.capture_started.wait(3))
        controls = [{'type': 'key', 'key': 'q', 'down': True}]
        client.send_input(controls)
        self.assertTrue(desktop.wait_for(lambda calls: controls in calls))
        self.assertFalse(done.is_set())
        desktop.capture_gate.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(errors, [])
        self.assertEqual(images[0].size, (1920, 1080))
        images[0].close()
        self.finish(client, server_errors, finished)

    def test_new_connection_starts_with_full_base_and_no_old_decoder_state(self):
        original = stream.write_packet
        payloads = []

        def observed(sock, payload, *args, **kwargs):
            if payload[:1] in (b'J', b'T', b'U'):
                payloads.append(payload)
            return original(sock, payload, *args, **kwargs)

        with patch.object(stream, 'write_packet', side_effect=observed):
            client, desktop, errors, finished = self.serve()
            with client.exchange_image([]):
                pass
            desktop.update_small_region()
            with client.exchange_image([]):
                pass
            patch_payload = payloads[-1]
            self.assertEqual(patch_payload[:1], b'T')
            self.finish(client, errors, finished)
            payloads.clear()
            replacement, _, errors, finished = self.serve()
            with replacement.exchange_image([]) as image:
                self.assertEqual(image.size, (1920, 1080))
            self.assertEqual(payloads[0][:1], b'J')
            self.finish(replacement, errors, finished)
        # A patch recorded on the old connection must not become a valid base
        # on a fresh Session, even when the stream dimensions happen to match.
        raw_client, raw_server = self.pair()
        session.send(raw_server, stream.READY, 128)
        fresh = session.Session(raw_client)
        session.send(raw_server, patch_payload, session.MAX_FRAME + 1)
        with self.assertRaises(ValueError):
            fresh.exchange_image([])
        fresh.close()


if __name__ == '__main__':
    unittest.main()
