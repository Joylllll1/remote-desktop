"""Loopback-only integration checks for desktop sessions."""
import json
import socket
import struct
import threading
import unittest
from unittest.mock import Mock, patch

from remotedesk.network.session import Session, receive, serve_connection


class FakeDesktop:
    def __init__(self):
        self.calls = []
        self.jpeg = b'\xff\xd8fake-jpeg\xff\xd9'

    def apply(self, events):
        self.calls.append(('apply', events))

    def frame(self):
        self.calls.append(('frame',))
        return self.jpeg


class SessionTests(unittest.TestCase):
    def test_slow_header_and_body_share_one_deadline(self):
        sock = Mock()
        sock.gettimeout.return_value = 12
        sock.recv.side_effect = [struct.pack('!I', 4), b'a', b'bcd']
        with patch('remotedesk.network.session.time.monotonic', side_effect=[100, 101, 103, 111]):
            with self.assertRaises(TimeoutError):
                receive(sock, 32, frame_timeout=10)
        self.assertEqual(sock.recv.call_count, 2)
        self.assertEqual([call.args[0] for call in sock.settimeout.call_args_list], [9, 7, 12])

    def test_host_gives_slow_transfers_longer_only_while_no_input_is_held(self):
        conn, desktop = Mock(), Mock()
        requests = (b'[]', b'[{"type":"key","key":"x","down":true}]')
        conn.recv.side_effect = [part for body in requests for part in (struct.pack('!I', len(body)), body)] + [b'']
        desktop.frame.return_value = b'picture'
        desktop.apply.side_effect = lambda events: setattr(desktop, 'has_held_input', bool(events))
        serve_connection(conn, True, desktop, idle_transfer_timeout=45)
        self.assertEqual([call.args[0] for call in conn.settimeout.call_args_list], [45, 12])

    def setUp(self):
        self.desktop = FakeDesktop()
        self.errors = []
        self.threads = []
        self.sockets = []

    def tearDown(self):
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'server did not stop after disconnect')

    def connect(self, authorized=True):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname(), timeout=3)
        server, _ = listener.accept()
        listener.close()
        server.settimeout(3)
        self.sockets.extend([client, server])

        def run():
            try:
                serve_connection(server, authorized, self.desktop)
            except Exception as error:
                self.errors.append(error)
            finally:
                server.close()

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return client, thread

    def test_valid_exchange_returns_frame_and_applies_events(self):
        sock, thread = self.connect()
        session = Session(sock)
        events = [{'type': 'release'}]
        self.assertEqual(session.exchange(events), self.desktop.jpeg)
        self.assertEqual(self.desktop.calls[:2], [('apply', events), ('frame',)])
        session.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_unauthorized_greeting_rejects_without_desktop_access(self):
        sock, thread = self.connect(authorized=False)
        with self.assertRaises(ValueError):
            Session(sock)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.desktop.calls, [])
        self.assertEqual(self.errors, [])

    def test_disconnect_releases_authorized_input(self):
        sock, thread = self.connect()
        session = Session(sock)
        session.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.desktop.calls, [('apply', [{'type': 'release'}])])
        self.assertEqual(self.errors, [])

    def test_more_than_128_events_is_rejected(self):
        sock, thread = self.connect()
        session = Session(sock)
        with self.assertRaises(ValueError):
            session.exchange([{'type': 'release'} for _ in range(129)])
        session.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(any(call[0] == 'frame' for call in self.desktop.calls))
        self.assertFalse(any(call[0] == 'apply' and len(call[1]) > 128
                             for call in self.desktop.calls))

    def test_server_rejects_oversized_batch_received_on_wire(self):
        sock, thread = self.connect()
        session = Session(sock)
        payload = json.dumps([{'type': 'release'} for _ in range(129)]).encode()
        sock.sendall(struct.pack('!I', len(payload)) + payload)
        self.assertEqual(sock.recv(1), b'')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.desktop.calls, [('apply', [{'type': 'release'}])])
        session.close()


if __name__ == '__main__':
    unittest.main()
