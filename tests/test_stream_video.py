"""Real H.264 socket integration, negotiation, backpressure and cancellation."""
import json
import socket
import threading
import time
import unittest
from contextlib import suppress
from unittest.mock import patch

from remotedesk.network import session, stream, video
from tests.test_stream_tiles import RGBDesktop


class VideoStreamTests(unittest.TestCase):
    def setUp(self):
        self.peers = []
        self.workers = []
        self.desktops = []
        self.errors = []

    def tearDown(self):
        for peer in self.peers:
            with suppress(OSError):
                peer.shutdown(socket.SHUT_RDWR)
            peer.close()
        for worker in self.workers:
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'video worker leaked')
        for desktop in self.desktops:
            desktop.image.close()

    def host(self):
        client, server = socket.socketpair()
        client.settimeout(3)
        self.peers.extend((client, server))
        desktop = RGBDesktop()
        self.desktops.append(desktop)
        def run():
            try:
                stream.serve_stream(server, desktop, version=5)
            except (OSError, EOFError):
                pass
            except Exception as error:
                self.errors.append(error)
        worker = threading.Thread(target=run, daemon=True)
        self.workers.append(worker)
        worker.start()
        return client, desktop

    @unittest.skipUnless(video.encoder_available() and video.decoder_available(), 'PyAV codecs unavailable')
    def test_real_video_first_frame_inputs_and_resolution_changes(self):
        sock, desktop = self.host()
        client = session.Session(sock)
        self.assertTrue(client.supports_video_frames)
        self.assertTrue(client.supports_async_input)
        for width in (1920, 1280, 1920):
            client.set_resolution(width)
            for _ in range(3):
                with client.exchange_image([]) as image:
                    self.assertEqual(image.width, width)
                    self.assertEqual(image.mode, 'RGB')
                self.assertEqual(client.media_format, 'H.264 · 4:4:4')
        event = {'type': 'key', 'key': 'shift_l', 'down': True}
        client.send_input([event])
        self.assertTrue(desktop.wait_for(lambda calls: [event] in calls))
        client.close()
        self.workers[0].join(2)
        self.assertFalse(desktop.has_held_input)
        self.assertFalse(self.errors)

    def test_missing_client_decoder_negotiates_image_fallback(self):
        sock, _ = self.host()
        with patch.object(video, 'decoder_available', return_value=False):
            client = session.Session(sock)
        with client.exchange_image([]) as image:
            self.assertEqual(image.size, (1920, 1080))
        self.assertEqual(client.media_format, '图片')
        client.close()

    def test_missing_host_encoder_falls_back_without_reconnect(self):
        with patch.object(video, 'encoder_available', return_value=False):
            sock, _ = self.host()
            with patch.object(video, 'decoder_available', return_value=True):
                client = session.Session(sock)
            with client.exchange_image([]) as image:
                self.assertEqual(image.size, (1920, 1080))
            self.assertEqual(client.media_format, '图片')
            client.close()
            self.workers[0].join(2)

    @unittest.skipUnless(video.encoder_available() and video.decoder_available(), 'PyAV codecs unavailable')
    def test_small_byte_window_resets_unsent_video_reference(self):
        # Force an IDR larger than the window. It is permitted alone; subsequent
        # captures must wait for credit or reset before any discarded reference.
        with patch.object(stream, 'MAX_IN_FLIGHT_BYTES', 1000):
            sock, _ = self.host()
            client = session.Session(sock)
            for _ in range(8):
                with client.exchange_image([]) as image:
                    self.assertEqual(image.size, (1920, 1080))
            client.close()
            self.workers[0].join(2)
        self.assertFalse(self.errors)

    @unittest.skipUnless(video.encoder_available() and video.decoder_available(), 'PyAV codecs unavailable')
    def test_no_ack_bounds_captures_but_input_and_release_still_work(self):
        sock, desktop = self.host()
        self.assertEqual(session.receive(sock, 128), stream.READY_V5)
        session.send(sock, json.dumps({'events': [], 'frame': True, 'codec': 'h264'}).encode(), session.MAX_INPUT)
        decoder = video.VideoDecoder()
        try:
            for expected in range(1, stream.MAX_IN_FLIGHT_FRAMES + 1):
                packet = session.receive(sock, session.MAX_FRAME)
                sequence, width = stream.FRAME_HEADER.unpack_from(packet)
                self.assertEqual(sequence, expected)
                with decoder.decode(packet[stream.FRAME_HEADER.size:]) as image:
                    self.assertEqual(image.width, width)
            before = len(desktop.widths)
            event = {'type': 'key', 'key': 'a', 'down': True}
            session.send(sock, json.dumps({'events': [event], 'frame': False}).encode(), session.MAX_INPUT)
            self.assertTrue(desktop.wait_for(lambda calls: [event] in calls))
            # With no ACK, a fourth frame must not be produced.
            sock.settimeout(.15)
            with self.assertRaises(socket.timeout):
                sock.recv(1)
            self.assertEqual(len(desktop.widths), before)
            sock.shutdown(socket.SHUT_RDWR)
            self.workers[0].join(2)
            self.assertFalse(desktop.has_held_input)
        finally:
            decoder.close()

    def test_codec_must_be_negotiated_on_first_frame_request(self):
        sock, _ = self.host()
        self.assertEqual(session.receive(sock, 128), stream.READY_V5)
        session.send(sock, json.dumps({'events': [], 'frame': False, 'codec': 'h264'}).encode(), session.MAX_INPUT)
        self.assertEqual(sock.recv(1), b'')
        self.workers[0].join(2)
        self.assertTrue(any('首次画面请求' in str(error) for error in self.errors))


if __name__ == '__main__':
    unittest.main()
