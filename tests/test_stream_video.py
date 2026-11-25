"""Real H.264 socket integration, negotiation, backpressure and cancellation."""
import json
import socket
import threading
import time
import unittest
from contextlib import suppress
from unittest.mock import patch

from PIL import Image

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

    def host(self, version=5, desktop=None):
        client, server = socket.socketpair()
        client.settimeout(3)
        self.peers.extend((client, server))
        desktop = desktop or RGBDesktop()
        self.desktops.append(desktop)
        def run():
            try:
                stream.serve_stream(server, desktop, version=version)
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
        self.assertFalse(client.supports_frame_rate_control)
        with self.assertRaises(RuntimeError):
            client.set_frame_rate(60)
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

    def test_v6_initial_and_changed_frame_rate_commands_are_version_gated(self):
        for version, greeting in ((6, stream.READY_V6), (5, stream.READY_V5),
                                  (4, stream.READY), (3, stream.READY_V3)):
            with self.subTest(version=version):
                local, remote = socket.socketpair()
                self.peers.extend((local, remote))
                remote.settimeout(1)
                session.send(remote, greeting, 128)
                client = session.Session(local)
                self.assertEqual(client.supports_frame_rate_control, version >= 6)
                for invalid in (True, None, 60.0, 24, '60'):
                    with self.assertRaises(ValueError):
                        client.set_frame_rate(invalid)
                if version < 6:
                    with self.assertRaises(RuntimeError):
                        client.set_frame_rate(60)
                # Inspect actual wire commands without requiring a fake decoder.
                client._stream._command([], True)
                initial = json.loads(session.receive(remote, session.MAX_INPUT))
                self.assertEqual(initial.get('fps'), 60 if version >= 6 else None)
                if version >= 6:
                    client.set_frame_rate(30)
                    changed = json.loads(session.receive(remote, session.MAX_INPUT))
                    self.assertEqual(changed, {'events': [], 'frame': False, 'fps': 30})
                    client.set_frame_rate(30)  # Same selection and input/ACKs omit fps.
                    client.send_input([])
                    self.assertEqual(json.loads(session.receive(remote, session.MAX_INPUT)),
                                     {'events': [], 'frame': False})
                client.close()

    def test_raw_capture_path_and_slow_ack_bitrate_change(self):
        from mss.screenshot import ScreenShot
        from remotedesk.host.desktop import Desktop
        from unittest.mock import Mock

        raw_desktop = Desktop()
        raw_desktop._capture = Mock()
        raw_desktop._capture.grab.return_value = ScreenShot.from_size(
            bytearray([30, 60, 190, 0]) * 320 * 180, 320, 180)
        desktop = RGBDesktop()
        desktop.capture_video_frame = raw_desktop.capture_video_frame
        desktop.capture_image = Mock(side_effect=AssertionError('RGB conversion used'))
        sock, _ = self.host(version=6, desktop=desktop)
        self.assertEqual(session.receive(sock, 128), stream.READY_V6)
        session.send(sock, json.dumps({'events': [], 'frame': True, 'codec': 'h264'}).encode(), session.MAX_INPUT)
        decoder = video.VideoDecoder()
        ceilings = []
        original = video.VideoEncoder.set_bitrate

        def record(encoder, bitrate):
            ceilings.append(bitrate)
            return original(encoder, bitrate)

        try:
            for _ in range(4):
                packet = session.receive(sock, session.MAX_FRAME)
                with decoder.decode(packet[stream.FRAME_HEADER.size:]) as image:
                    self.assertEqual(image.size, (320, 180))
            time.sleep(.3)
            with patch.object(video.VideoEncoder, 'set_bitrate', record):
                session.send(sock, json.dumps({'events': [], 'frame': False, 'ack': 4}).encode(), session.MAX_INPUT)
                packet = session.receive(sock, session.MAX_FRAME)
                with decoder.decode(packet[stream.FRAME_HEADER.size:]) as image:
                    self.assertEqual(image.size, (320, 180))
            self.assertIn(5_600_000, ceilings)
            self.assertFalse(self.errors)
        finally:
            decoder.close()

    def test_frame_rate_selected_before_start_is_sent_once_in_initial_request(self):
        local, remote = socket.socketpair()
        self.peers.extend((local, remote))
        remote.settimeout(1)
        session.send(remote, stream.READY_V6, 128)
        client = session.Session(local)
        client.set_frame_rate(30)
        client._stream._command([], True)
        initial = json.loads(session.receive(remote, session.MAX_INPUT))
        self.assertEqual(initial['fps'], 30)
        self.assertTrue(initial['frame'])
        client.close()

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

    def test_bad_frame_rate_or_legacy_fps_field_fails_before_applying_input(self):
        cases = [(6, value) for value in (True, False, None, 0, 24, 59, 61, 60.0, '60')]
        cases += [(version, 60) for version in (2, 3, 4, 5)]
        for version, frame_rate in cases:
            with self.subTest(version=version, fps=frame_rate):
                sock, desktop = self.host(version=version)
                session.receive(sock, 128)
                event = {'type': 'key', 'key': 'a', 'down': True}
                command = {'events': [event], 'frame': True, 'fps': frame_rate}
                session.send(sock, json.dumps(command).encode(), session.MAX_INPUT)
                self.assertEqual(sock.recv(1), b'')
                self.workers[-1].join(2)
                self.assertFalse(self.workers[-1].is_alive())
                self.assertEqual(desktop.calls, [[{'type': 'release'}]])
                self.assertEqual(desktop.widths, [])
        self.assertEqual(len(self.errors), len(cases))

    @unittest.skipUnless(video.encoder_available() and video.decoder_available(), 'PyAV codecs unavailable')
    @patch.object(stream, 'FrameTimer')
    def test_v6_frame_rate_switches_reset_idr_and_adjust_credit_without_stalling_input(self, frame_timer):
        desktop = RGBDesktop()
        desktop.image.close()
        desktop.image = Image.new('RGB', (64, 36), '#236d9c')
        sock, desktop = self.host(version=6, desktop=desktop)
        self.assertEqual(session.receive(sock, 128), stream.READY_V6)

        def command(**fields):
            data = {'events': [], 'frame': False}
            data.update(fields)
            session.send(sock, json.dumps(data).encode(), session.MAX_INPUT)

        decoder = video.VideoDecoder()
        last_sequence = 0

        def frame(*, keyframe=None):
            nonlocal last_sequence
            packet = session.receive(sock, session.MAX_FRAME)
            sequence, width = stream.FRAME_HEADER.unpack_from(packet)
            self.assertEqual(sequence, last_sequence + 1)
            self.assertEqual(width, 1920)
            body = packet[stream.FRAME_HEADER.size:]
            if keyframe is not None:
                self.assertEqual(bool(body[2] & 1), keyframe)
            with decoder.decode(body) as image:
                self.assertEqual(image.size, (64, 36))
            last_sequence = sequence

        def stalled():
            sock.settimeout(.15)
            try:
                with self.assertRaises(socket.timeout):
                    sock.recv(1)
            finally:
                sock.settimeout(3)

        try:
            command(frame=True, codec='h264', fps=60)
            for index in range(stream.MAX_IN_FLIGHT_FRAMES_60):
                frame(keyframe=index == 0)
            self.assertEqual(frame_timer.return_value.set_rate.call_args, ((60,),))
            self.assertEqual(len(desktop.widths), stream.MAX_IN_FLIGHT_FRAMES_60)
            stalled()

            # Four existing frames consume all 30 fps credit. One ACK still
            # leaves three; two ACKs permit exactly one new IDR at the new rate.
            command(fps=30, ack=1)
            stalled()
            self.assertEqual(frame_timer.return_value.set_rate.call_args, ((30,),))
            command(ack=2)
            frame(keyframe=True)
            stalled()
            self.assertEqual(last_sequence, 5)

            # Returning to 60 fps opens one additional slot, with a fresh IDR.
            command(fps=60)
            frame(keyframe=True)
            self.assertEqual(frame_timer.return_value.set_rate.call_args, ((60,),))
            stalled()
            self.assertEqual(last_sequence, 6)

            event = {'type': 'key', 'key': 'a', 'down': True}
            command(events=[event])
            self.assertTrue(desktop.wait_for(lambda calls: [event] in calls))
            with patch.object(stream, 'INPUT_LEASE', .05):
                self.assertTrue(desktop.wait_for(lambda calls: [{'type': 'release'}] in calls))
            self.assertFalse(desktop.has_held_input)
            self.assertTrue(self.workers[-1].is_alive())
            self.assertEqual(len(desktop.widths), 6)
            # Closing while credit is exhausted wakes both the sender and the
            # independent control reader, without a capture or native leak.
            sock.shutdown(socket.SHUT_RDWR)
            self.workers[-1].join(2)
            self.assertFalse(self.workers[-1].is_alive())
            frame_timer.return_value.close.assert_called_once_with()
            self.assertFalse(self.errors)
        finally:
            decoder.close()

    def test_v6_image_fallback_keeps_three_credit_limit_at_60_selection(self):
        with patch.object(video, 'encoder_available', return_value=False):
            sock, desktop = self.host(version=6)
            self.assertEqual(session.receive(sock, 128), stream.READY_V6)
            session.send(sock, json.dumps({'events': [], 'frame': True, 'codec': 'h264',
                                          'fps': 60}).encode(), session.MAX_INPUT)
            for _ in range(stream.MAX_IN_FLIGHT_FRAMES):
                packet = session.receive(sock, session.MAX_FRAME)
                self.assertIn(packet[stream.FRAME_HEADER.size:][:1], (b'J', b'T', b'U'))
            sock.settimeout(.15)
            with self.assertRaises(socket.timeout):
                sock.recv(1)
            self.assertEqual(len(desktop.widths), stream.MAX_IN_FLIGHT_FRAMES)
            sock.shutdown(socket.SHUT_RDWR)
            self.workers[-1].join(2)
        self.assertFalse(self.errors)

    @unittest.skipUnless(video.encoder_available() and video.decoder_available(), 'PyAV codecs unavailable')
    def test_lowering_rate_during_fourth_capture_discards_unsent_reference(self):
        desktop = RGBDesktop()
        desktop.image.close()
        desktop.image = Image.new('RGB', (64, 36), '#236d9c')
        captured = threading.Event()
        unblock = threading.Event()
        original_capture = desktop.capture_image
        capture_count = 0

        def gated_capture(max_width):
            nonlocal capture_count
            capture_count += 1
            if capture_count == 4:
                captured.set()
                if not unblock.wait(3):
                    raise AssertionError('fourth capture was not released')
            return original_capture(max_width)

        desktop.capture_image = gated_capture
        sock, _ = self.host(version=6, desktop=desktop)
        session.receive(sock, 128)

        def command(**fields):
            data = {'events': [], 'frame': False}
            data.update(fields)
            session.send(sock, json.dumps(data).encode(), session.MAX_INPUT)

        decoder = video.VideoDecoder()

        def frame(expected_sequence, keyframe):
            packet = session.receive(sock, session.MAX_FRAME)
            sequence, _ = stream.FRAME_HEADER.unpack_from(packet)
            self.assertEqual(sequence, expected_sequence)
            body = packet[stream.FRAME_HEADER.size:]
            self.assertEqual(bool(body[2] & 1), keyframe)
            with decoder.decode(body) as image:
                self.assertEqual(image.size, (64, 36))

        try:
            command(frame=True, codec='h264', fps=60)
            for sequence in range(1, 4):
                frame(sequence, sequence == 1)
            self.assertTrue(captured.wait(3))
            command(fps=30)
            marker = {'type': 'key', 'key': 'q', 'down': False}
            command(events=[marker])
            self.assertTrue(desktop.wait_for(lambda calls: [marker] in calls))
            unblock.set()
            sock.settimeout(.15)
            with self.assertRaises(socket.timeout):
                sock.recv(1)
            sock.settimeout(3)
            self.assertEqual(capture_count, 4)
            command(ack=1)
            frame(4, True)
            command(ack=2)
            frame(5, False)
            sock.shutdown(socket.SHUT_RDWR)
            self.workers[-1].join(2)
            self.assertFalse(self.errors)
        finally:
            unblock.set()
            decoder.close()


if __name__ == '__main__':
    unittest.main()
