"""V4 bounded streaming over real local sockets; no desktop/input injection."""

import io
import json
import select
import socket
import threading
import time
import unittest
from contextlib import suppress
from unittest.mock import patch

from PIL import Image, ImageDraw

from remotedesk.network import session, stream
from remotedesk.network.tiles import TileDecoder, TileEncoder


class PipelineDesktop:
    def __init__(self):
        self.condition = threading.Condition()
        self.image = Image.new('RGB', (1920, 120), '#eeeeee')
        self.calls = []
        self.captures = []
        self.held = False

    @property
    def has_held_input(self):
        with self.condition:
            return self.held

    def apply(self, events):
        with self.condition:
            self.calls.append(events)
            for event in events:
                if event['type'] == 'release':
                    self.held = False
                elif event['type'] == 'key':
                    self.held = event['down']
            self.condition.notify_all()

    def capture_image(self, max_width):
        with self.condition:
            self.captures.append((max_width, time.monotonic()))
            image = self.image.copy()
            # A changing corner identifies an unsent candidate in admission tests.
            image.putpixel((0, 0), (len(self.captures) % 256, 0, 0))
            self.condition.notify_all()
        if image.width > max_width:
            image.thumbnail((max_width, image.height), Image.Resampling.LANCZOS)
        return image

    def wait_for(self, predicate, timeout=3):
        with self.condition:
            return self.condition.wait_for(predicate, timeout)


class FixedEncoder:
    """Predictable packet lengths, preserving capture identity for credit tests."""
    def __init__(self, size):
        self.size = size
        self.closes = 0

    def encode(self, image):
        return b'J' + bytes([image.getpixel((0, 0))[0]]) * (self.size - 1)

    def close(self):
        self.closes += 1


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []
        self.threads = []
        self.desktops = []

    def tearDown(self):
        for sock in self.sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'pipeline worker leaked')
        for desktop in self.desktops:
            desktop.image.close()

    def pair(self):
        client, server = socket.socketpair()
        client.settimeout(3)
        server.settimeout(3)
        self.sockets.extend((client, server))
        return client, server

    def start(self, *, version=4, small_buffers=False, negotiate=False):
        client, server = self.pair()
        if small_buffers:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        desktop = PipelineDesktop()
        self.desktops.append(desktop)
        errors = []
        finished = threading.Event()

        def serve():
            try:
                stream.serve_stream(server, desktop, version=version)
            except Exception as error:
                errors.append(error)
            finally:
                server.close()
                finished.set()

        thread = threading.Thread(target=serve, name='test-v4-host', daemon=True)
        self.threads.append(thread)
        thread.start()
        if negotiate:
            client = session.Session(client)
        else:
            self.assertEqual(session.receive(client, 128), stream.READY if version == 4 else stream.READY_V3)
            client.setblocking(False)
        return client, server, desktop, errors, finished

    def command(self, sock, *, frame=False, events=None, **fields):
        fields.update(events=[] if events is None else events, frame=frame)
        stream.write_packet(sock, json.dumps(fields).encode(), session.MAX_INPUT)

    def frame(self, sock):
        payload = stream.read_packet(sock, session.MAX_FRAME + 1 + stream.FRAME_HEADER.size)
        sequence, width = stream.FRAME_HEADER.unpack_from(payload)
        return sequence, width, payload[stream.FRAME_HEADER.size:]

    def quiet(self, sock, timeout=.16):
        self.assertFalse(select.select([sock], [], [], timeout)[0], 'frame exceeded available credit')

    def disconnect(self, client, finished):
        if isinstance(client, session.Session):
            client.close()
        else:
            client.shutdown(socket.SHUT_RDWR)
        self.assertTrue(finished.wait(3), 'host did not stop after cancellation')

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('local socket condition did not complete')
            time.sleep(.005)

    def test_one_start_sends_three_paced_frames_without_ack_or_new_request(self):
        client, _, desktop, errors, finished = self.start()
        self.command(client, frame=True, width=1920)
        frames = [self.frame(client) for _ in range(3)]
        self.assertEqual([frame[0] for frame in frames], [1, 2, 3])
        self.quiet(client)
        self.assertEqual(len(desktop.captures), stream.MAX_IN_FLIGHT_FRAMES)
        self.assertTrue(all(b[1] - a[1] >= .09 for a, b in zip(desktop.captures, desktop.captures[1:])))
        # A repeated start is idempotent and cannot enlarge the window.
        self.command(client, frame=True, width=1920)
        self.quiet(client)
        self.command(client, ack=3)
        self.assertEqual(self.frame(client)[0], 4)
        self.disconnect(client, finished)
        self.assertEqual(errors, [])

    def test_byte_budget_discards_unsent_candidate_then_recaptures_fresh(self):
        encoder = FixedEncoder(26)  # 40 wire bytes including packet + envelope
        with patch.object(stream, 'MAX_IN_FLIGHT_BYTES', 100), patch(
            'remotedesk.network.tiles.TileEncoder', return_value=encoder
        ):
            client, _, desktop, errors, finished = self.start()
            self.command(client, frame=True)
            first, second = self.frame(client), self.frame(client)
            self.assertEqual((first[0], second[0]), (1, 2))
            self.assertEqual(len(first[2]) + len(second[2]) + 2 * (4 + stream.FRAME_HEADER.size), 80)
            self.assertTrue(desktop.wait_for(lambda: len(desktop.captures) == 3))
            self.quiet(client)
            self.command(client, ack=1)
            self.quiet(client)
            self.assertEqual(len(desktop.captures), 3, 'must not poll captures while draining byte credit')
            self.command(client, ack=2)
            third = self.frame(client)
            self.assertEqual(third[0], 3)
            self.assertEqual(third[2][1], 4, 'must capture again after credit, not queue rejected image 3')
            self.assertGreaterEqual(encoder.closes, 1, 'discarded delta base must be reset')
            self.disconnect(client, finished)
            self.assertEqual(errors, [])

    def test_oversized_frame_is_allowed_only_as_sole_outstanding_frame(self):
        with patch.object(stream, 'MAX_IN_FLIGHT_BYTES', 64), patch(
            'remotedesk.network.tiles.TileEncoder', return_value=FixedEncoder(100)
        ):
            client, _, desktop, errors, finished = self.start()
            self.command(client, frame=True)
            first = self.frame(client)
            self.assertGreater(len(first[2]), stream.MAX_IN_FLIGHT_BYTES)
            self.quiet(client)
            self.assertEqual(len(desktop.captures), 1)
            self.command(client, ack=1)
            self.assertEqual(self.frame(client)[0], 2)
            self.disconnect(client, finished)
            self.assertEqual(errors, [])

    def test_byte_admission_reset_keeps_real_tile_decoder_valid(self):
        encoder = TileEncoder()
        with Image.new('RGB', (1920, 120), '#eeeeee') as image:
            image.putpixel((0, 0), (1, 0, 0))
            budget = 4 + stream.FRAME_HEADER.size + len(encoder.encode(image)) + 1
        encoder.close()
        decoder = TileDecoder()
        try:
            with patch.object(stream, 'MAX_IN_FLIGHT_BYTES', budget):
                client, _, desktop, errors, finished = self.start()
                self.command(client, frame=True)
                sequence, _, first = self.frame(client)
                with decoder.decode(first):
                    pass
                self.assertTrue(desktop.wait_for(lambda: len(desktop.captures) == 2))
                self.quiet(client)
                self.command(client, ack=sequence)
                sequence, _, fresh = self.frame(client)
                self.assertEqual(sequence, 2)
                self.assertEqual(fresh[:1], b'J', 'discarded delta must force a new full base')
                with decoder.decode(fresh) as image:
                    self.assertEqual(image.size, (1920, 120))
                self.assertEqual(len(desktop.captures), 3)
                self.disconnect(client, finished)
                self.assertEqual(errors, [])
        finally:
            decoder.close()

    def test_invalid_or_future_ack_fails_closed_without_applying_its_events(self):
        held = [{'type': 'key', 'key': 'q', 'down': True}]
        forbidden = [{'type': 'key', 'key': 'x', 'down': True}]
        for ack in (0, -1, True, 1.0, '1', None, 1, 100):
            with self.subTest(ack=ack):
                client, _, desktop, errors, finished = self.start()
                self.command(client, events=held)
                self.assertTrue(desktop.wait_for(lambda: held in desktop.calls))
                self.command(client, events=forbidden, ack=ack)
                self.assertTrue(finished.wait(3))
                self.assertTrue(errors)
                self.assertNotIn(forbidden, desktop.calls)
                self.assertEqual(desktop.calls[-1], [{'type': 'release'}])
                self.assertFalse(desktop.held)

    def test_duplicate_and_out_of_range_ack_cannot_free_window(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                client, _, _, errors, finished = self.start()
                self.command(client, frame=True)
                for _ in range(3):
                    self.frame(client)
                if duplicate:
                    self.command(client, ack=1)
                    self.frame(client)
                    self.command(client, ack=1)
                else:
                    self.command(client, ack=4)
                self.assertTrue(finished.wait(3))
                self.assertTrue(errors)

    def test_credit_wait_keeps_input_and_lease_alive_and_cancels_promptly(self):
        with patch.object(stream, 'INPUT_LEASE', .05):
            client, _, desktop, errors, finished = self.start()
            self.command(client, frame=True)
            for _ in range(3):
                self.frame(client)
            held = [{'type': 'key', 'key': 'q', 'down': True}]
            self.command(client, events=held)
            self.assertTrue(desktop.wait_for(lambda: held in desktop.calls))
            self.assertTrue(desktop.wait_for(lambda: [{'type': 'release'}] in desktop.calls))
            self.assertFalse(finished.is_set())
            self.assertEqual(len(desktop.captures), 3)
            self.disconnect(client, finished)
            self.assertEqual(errors, [])

    def test_input_reaches_desktop_during_socket_send_backpressure(self):
        with patch('remotedesk.network.tiles.TileEncoder', return_value=FixedEncoder(2 * 1024 * 1024)):
            client, server, desktop, _, finished = self.start(small_buffers=True)
            self.command(client, frame=True)
            self.assertTrue(desktop.wait_for(lambda: len(desktop.captures) == 1))
            self.wait_until(lambda: not select.select([], [server], [], 0)[1])
            events = [{'type': 'key', 'key': 'q', 'down': True}]
            self.command(client, events=events)
            self.assertTrue(desktop.wait_for(lambda: events in desktop.calls))
            self.assertEqual(len(desktop.captures), 1)
            self.disconnect(client, finished)
            self.assertEqual(desktop.calls[-1], [{'type': 'release'}])

    def test_decode_acks_preserve_delta_reconstruction(self):
        client, _, desktop, errors, finished = self.start()
        decoder = TileDecoder()
        try:
            self.command(client, frame=True)
            for _ in range(3):
                sequence, _, payload = self.frame(client)
                with decoder.decode(payload) as image:
                    self.assertEqual(image.size, (1920, 120))
            with desktop.condition:
                ImageDraw.Draw(desktop.image).rectangle((420, 30, 440, 55), fill='#195b9b')
            self.command(client, ack=sequence)
            sequence, _, payload = self.frame(client)
            self.assertEqual(payload[:1], b'T')
            with decoder.decode(payload) as image:
                self.assertEqual(image.getpixel((430, 40)), (25, 91, 155))
            self.disconnect(client, finished)
            self.assertEqual(errors, [])
        finally:
            decoder.close()

    def test_resolution_switch_decodes_and_acks_buffered_old_profile_frames(self):
        client, _, desktop, errors, finished = self.start(negotiate=True)
        self.assertTrue(client.supports_continuous_frames)
        with client.exchange_image([]) as image:
            self.assertEqual(image.width, 1920)
        self.assertTrue(desktop.wait_for(lambda: len(desktop.captures) >= 4))
        client.set_resolution(1280)
        with client.exchange_image([]) as image:
            self.assertEqual(image.width, 1280)
        client.set_resolution(1920)
        with client.exchange_image([]) as image:
            self.assertEqual(image.width, 1920)
        self.disconnect(client, finished)
        self.assertEqual(errors, [])
        self.assertIsNone(client._stream._decoder, 'close must release the cached image')

    def test_v3_client_keeps_request_response_behavior_and_capabilities(self):
        client, _, desktop, errors, finished = self.start(version=3, negotiate=True)
        self.assertTrue(client.supports_async_input)
        self.assertTrue(client.supports_resolution_control)
        self.assertFalse(client.supports_continuous_frames)
        with client.exchange_image([]) as image:
            self.assertEqual(image.width, 1920)
        self.quiet(client.sock)
        self.assertEqual(len(desktop.captures), 1)
        client.set_resolution(1280)
        with client.exchange_image([]) as image:
            self.assertEqual(image.width, 1280)
        self.disconnect(client, finished)
        self.assertEqual(errors, [])

    def test_bad_sequence_or_undecodable_frame_never_gets_acknowledged(self):
        for sequence, payload in ((2, b'U'), (1, b'Jinvalid'), (1, b'U')):
            with self.subTest(sequence=sequence, payload=payload):
                raw_client, server = self.pair()
                session.send(server, stream.READY, 128)
                client = session.Session(raw_client)
                session.send(server, stream.FRAME_HEADER.pack(sequence, 1920) + payload, session.MAX_FRAME)
                with self.assertRaises(ValueError):
                    client.exchange_image([])
                command = json.loads(session.receive(server, session.MAX_INPUT))
                self.assertEqual(command, {'events': [], 'frame': True, 'width': 1920})
                self.assertEqual(server.recv(1), b'', 'invalid images must close without an ACK')
                client.close()

    def test_close_interrupts_ack_waiting_behind_a_backpressured_input_writer(self):
        raw_client, server = self.pair()
        raw_client.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        session.send(server, stream.READY, 128)
        client = session.Session(raw_client)
        ack_waiting = threading.Event()
        received, sent = threading.Event(), threading.Event()
        errors = []
        original_command = client._stream._command

        def observed_command(*args, **kwargs):
            if 'ack' in kwargs:
                ack_waiting.set()
            return original_command(*args, **kwargs)

        def receive():
            try:
                with client.exchange_image([]):
                    pass
            except Exception as error:
                errors.append(error)
            finally:
                received.set()

        def fill_input():
            try:
                client.send_input([{'type': 'key', 'key': 'x' * 60000, 'down': True}])
            except Exception as error:
                errors.append(error)
            finally:
                sent.set()

        with patch.object(client._stream, '_command', side_effect=observed_command):
            receiver = threading.Thread(target=receive, name='test-v4-client-receive', daemon=True)
            self.threads.append(receiver)
            receiver.start()
            self.assertTrue(json.loads(session.receive(server, session.MAX_INPUT))['frame'])
            sender = threading.Thread(target=fill_input, name='test-v4-client-input', daemon=True)
            self.threads.append(sender)
            sender.start()
            self.wait_until(lambda: not select.select([], [raw_client], [], 0)[1])
            output = io.BytesIO()
            with Image.new('RGB', (32, 32), 'white') as image:
                image.save(output, format='JPEG')
            session.send(server, stream.FRAME_HEADER.pack(1, 1920) + b'J' + output.getvalue(), session.MAX_FRAME)
            self.assertTrue(ack_waiting.wait(3))
            client.close()
            self.assertTrue(received.wait(2))
            self.assertTrue(sent.wait(2))
            self.assertEqual(len(errors), 2)
            self.assertIsNone(client._stream._decoder)


if __name__ == '__main__':
    unittest.main()
