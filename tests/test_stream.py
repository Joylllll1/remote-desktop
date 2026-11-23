"""Native local-socket stream regressions; all desktop/input work is fake."""
import json
import select
import socket
import threading
import time
import unittest
from contextlib import suppress
from unittest.mock import patch

from remotedesk.network import session, stream


class FakeDesktop:
    def __init__(self, frame=b'\xff\xd8fake-jpeg\xff\xd9', blocked=False):
        self.jpeg = frame
        self.condition = threading.Condition()
        self.calls = []
        self.capture_started = threading.Event()
        self.capture_gate = threading.Event()
        if not blocked:
            self.capture_gate.set()
        self._held = False

    @property
    def has_held_input(self):
        with self.condition:
            return self._held

    def apply(self, events):
        with self.condition:
            self.calls.append([dict(event) for event in events])
            for event in events:
                if event['type'] == 'release':
                    self._held = False
                elif event['type'] in ('key', 'button'):
                    self._held = event['down']
            self.condition.notify_all()

    def frame(self):
        self.capture_started.set()
        if not self.capture_gate.wait(5):
            raise RuntimeError('test failed to unblock fake capture')
        return self.jpeg

    def wait_for(self, predicate, timeout=3):
        with self.condition:
            return self.condition.wait_for(lambda: predicate(self.calls), timeout)


class OneByteSocket:
    def __init__(self, sock):
        self.sock = sock
        self.received = 0

    def fileno(self):
        return self.sock.fileno()

    def recv(self, size):
        data = self.sock.recv(min(size, 1))
        self.received += len(data)
        return data


class StepClock:
    """Advance at every progress/deadline observation, without wall-clock races."""
    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += .01
        return self.now


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []
        self.threads = []
        self.gates = []
        self.desktops = []

    def tearDown(self):
        for desktop in self.desktops:
            desktop.capture_gate.set()
        for gate in self.gates:
            gate.set()
        for sock in self.sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'local session thread leaked')

    def pair(self):
        client, server = socket.socketpair()
        client.settimeout(3)
        server.settimeout(3)
        self.sockets.extend((client, server))
        return client, server

    def serve(self, desktop, configure=None, legacy=False):
        client, server = self.pair()
        self.desktops.append(desktop)
        if configure:
            configure(client, server)
        errors = []
        finished = threading.Event()

        def run():
            try:
                if legacy:
                    session.serve_connection(server, True, desktop)
                else:
                    stream.serve_stream(server, desktop)
            except Exception as error:
                errors.append(error)
            finally:
                server.close()
                finished.set()

        thread = threading.Thread(target=run, name='test-stream-server', daemon=True)
        self.threads.append(thread)
        thread.start()
        return session.Session(client), server, errors, finished

    def exchange_in_thread(self, client):
        result = []
        errors = []
        done = threading.Event()

        def exchange():
            try:
                result.append(client.exchange([]))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=exchange, name='test-stream-exchange', daemon=True)
        self.threads.append(thread)
        thread.start()
        return result, errors, done

    def test_input_arrives_during_blocked_capture(self):
        desktop = FakeDesktop(blocked=True)
        client, _, server_errors, finished = self.serve(desktop)
        self.assertTrue(client.supports_async_input)
        result, errors, done = self.exchange_in_thread(client)
        self.assertTrue(desktop.capture_started.wait(3))
        events = [{'type': 'key', 'key': 'q', 'down': True}]
        client.send_input(events)
        self.assertTrue(desktop.wait_for(lambda calls: events in calls))
        self.assertFalse(done.is_set(), 'capture gate must still hold the frame')
        desktop.capture_gate.set()
        self.assertTrue(done.wait(3))
        self.assertEqual(result, [desktop.jpeg])
        self.assertEqual(errors, [])
        client.close()
        self.assertTrue(finished.wait(3))
        self.assertEqual(server_errors, [])

    def test_input_arrives_while_frame_sender_is_backpressured(self):
        desktop = FakeDesktop(frame=b'\xff\xd8' + b'x' * (2 * 1024 * 1024))
        allow_receive = threading.Event()
        receive_waiting = threading.Event()
        sending = threading.Event()
        send_finished = threading.Event()
        self.gates.append(allow_receive)
        original_read = stream.read_packet
        original_write = stream.write_packet
        client_socket = None
        server_socket = None

        def pause_client_read(sock, *args, **kwargs):
            if sock is client_socket:
                receive_waiting.set()
                if not allow_receive.wait(5):
                    raise RuntimeError('test failed to unblock client frame read')
            return original_read(sock, *args, **kwargs)

        def observe_server_write(sock, payload, *args, **kwargs):
            if sock is server_socket and payload.startswith(b'J'):
                sending.set()
                result = original_write(sock, payload, *args, **kwargs)
                send_finished.set()
                return result
            return original_write(sock, payload, *args, **kwargs)

        def configure(client, server):
            nonlocal client_socket, server_socket
            client_socket, server_socket = client, server
            server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)

        with patch.object(stream, 'read_packet', side_effect=pause_client_read), patch.object(
            stream, 'write_packet', side_effect=observe_server_write
        ):
            client, server, server_errors, finished = self.serve(desktop, configure)
            result, errors, done = self.exchange_in_thread(client)
            self.assertTrue(receive_waiting.wait(3))
            self.assertTrue(sending.wait(3))
            deadline = time.monotonic() + 3
            while select.select([], [server], [], 0)[1] and time.monotonic() < deadline:
                threading.Event().wait(.005)
            self.assertFalse(select.select([], [server], [], 0)[1], 'sender never reached backpressure')
            self.assertFalse(send_finished.is_set())
            events = [{'type': 'button', 'button': 'left', 'down': True, 'x': .2, 'y': .4}]
            client.send_input(events)
            self.assertTrue(desktop.wait_for(lambda calls: events in calls))
            self.assertFalse(done.is_set())
            allow_receive.set()
            self.assertTrue(done.wait(3))
            self.assertEqual(result, [desktop.jpeg])
            self.assertEqual(errors, [])
            client.close()
            self.assertTrue(finished.wait(3))
            self.assertEqual(server_errors, [])

    def test_unchanged_second_frame_uses_only_U_and_returns_cached_jpeg(self):
        desktop = FakeDesktop()
        original = stream.write_packet
        frame_payloads = []

        def observe(sock, payload, *args, **kwargs):
            if payload != stream.READY and payload[:1] in (b'J', b'U'):
                frame_payloads.append(payload)
            return original(sock, payload, *args, **kwargs)

        with patch.object(stream, 'write_packet', side_effect=observe):
            client, _, errors, finished = self.serve(desktop)
            self.assertEqual(client.exchange([]), desktop.jpeg)
            self.assertEqual(client.exchange([]), desktop.jpeg)
            self.assertEqual(frame_payloads, [b'J' + desktop.jpeg, b'U'])
            client.close()
            self.assertTrue(finished.wait(3))
            self.assertEqual(errors, [])

    def test_lease_releases_held_input_without_disconnecting(self):
        desktop = FakeDesktop()
        with patch.object(stream, 'INPUT_LEASE', .05):
            client, _, errors, finished = self.serve(desktop)
            events = [{'type': 'key', 'key': 'q', 'down': True}]
            client.send_input(events)
            self.assertTrue(desktop.wait_for(lambda calls: events in calls))
            self.assertTrue(desktop.wait_for(lambda calls: [{'type': 'release'}] in calls))
            self.assertFalse(desktop.has_held_input)
            self.assertFalse(finished.is_set(), 'lease expiry must preserve the connection')
            self.assertEqual(client.exchange([]), desktop.jpeg)
            client.close()
            self.assertTrue(finished.wait(3))
            self.assertEqual(errors, [])

    def test_malformed_control_fails_closed_releases_and_exits(self):
        malformed = [b'not-json', b'[]', b'{"events":[],"frame":"yes"}',
                     b'{"events":[],"frame":false,"extra":1}',
                     json.dumps({'events': [{}] * 129, 'frame': False}).encode()]
        for command in malformed:
            with self.subTest(command=command[:60]):
                desktop = FakeDesktop()
                client, _, errors, finished = self.serve(desktop)
                events = [{'type': 'key', 'key': 'q', 'down': True}]
                client.send_input(events)
                self.assertTrue(desktop.wait_for(lambda calls: events in calls))
                stream.write_packet(client.sock, command, session.MAX_INPUT)
                self.assertTrue(finished.wait(3))
                self.assertTrue(desktop.wait_for(lambda calls: [{'type': 'release'}] in calls))
                self.assertFalse(desktop.has_held_input)
                self.assertTrue(errors, 'bad control must be reported by the host')
                self.assertEqual(desktop.calls, [events, [{'type': 'release'}]])
                client.close()

    def test_client_close_unblocks_host_control_receiver(self):
        desktop = FakeDesktop()
        client, _, errors, finished = self.serve(desktop)
        client.close()
        self.assertTrue(finished.wait(3))
        self.assertEqual(errors, [])
        self.assertEqual(desktop.calls, [[{'type': 'release'}]])

    def test_first_frame_cannot_be_unchanged_or_invalid(self):
        for payload in (b'U', b'J', b'Xfake'):
            with self.subTest(payload=payload):
                client_socket, server = self.pair()
                session.send(server, stream.READY, 128)
                client = session.Session(client_socket)
                session.send(server, payload, session.MAX_FRAME + 1)
                with self.assertRaises(ValueError):
                    client.exchange([])
                client.close()

    def test_busy_greeting_is_a_retryable_connection_error(self):
        client, server = self.pair()
        session.send(server, stream.BUSY, 128)
        with self.assertRaises(ConnectionError):
            session.Session(client)
        self.assertEqual(client.fileno(), -1)

    def test_v1_peer_keeps_exchange_compatibility_without_async_input(self):
        desktop = FakeDesktop()
        client, _, errors, finished = self.serve(desktop, legacy=True)
        self.assertFalse(client.supports_async_input)
        self.assertEqual(client.exchange([]), desktop.jpeg)
        with self.assertRaises(RuntimeError):
            client.send_input([])
        client.close()
        self.assertTrue(finished.wait(3))
        self.assertEqual(errors, [])
        self.assertEqual(desktop.calls, [[], [{'type': 'release'}]])

    def test_total_read_deadline_bounds_continual_chunk_progress(self):
        client, server = self.pair()
        client.setblocking(False)
        session.send(server, b'x' * 100, 100)
        one_byte = OneByteSocket(client)
        clock = StepClock()
        with patch.object(stream.time, 'monotonic', side_effect=clock):
            with self.assertRaises(TimeoutError):
                stream.read_packet(one_byte, 100, idle=1, total=.15)
        self.assertGreater(one_byte.received, 4, 'body must make real socket progress')
        self.assertLess(one_byte.received, 104)
        self.assertLess(clock.now, .25, 'progress must not reset the overall deadline')

    def test_write_deadline_bounds_backpressure_after_partial_progress(self):
        client, server = self.pair()
        server.setblocking(False)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        clock = StepClock()
        with patch.object(stream.time, 'monotonic', side_effect=clock):
            with self.assertRaises(TimeoutError):
                stream.write_packet(server, b'x' * (2 * 1024 * 1024), session.MAX_FRAME, idle=1, total=.1)
        partial = client.recv(65536)
        self.assertGreater(len(partial), 4)
        self.assertLess(len(partial), 2 * 1024 * 1024)
        self.assertLess(clock.now, .2)


if __name__ == '__main__':
    unittest.main()
