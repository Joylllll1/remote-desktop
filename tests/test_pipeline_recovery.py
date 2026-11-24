"""Real v4 framing through the recovery worker; no Tk or OS input injection."""

from contextlib import suppress
import queue
import socket
import threading
import time
import unittest

from PIL import Image

from remotedesk.network.session import Session
from remotedesk.network.stream import serve_stream
from remotedesk.ui.viewer import _InputBuffer, _RecoveryWorker


class Desktop:
    def __init__(self):
        self.has_held_input = False
        self.key_arrived = threading.Event()

    def capture_image(self, max_width):
        return Image.new('RGB', (64, 36), '#227799')

    def apply(self, events):
        for event in events:
            if event['type'] == 'key':
                self.has_held_input = event['down']
                self.key_arrived.set()
            elif event['type'] == 'release':
                self.has_held_input = False


class PipelineRecoveryTests(unittest.TestCase):
    def test_dropped_v4_connection_restarts_with_full_frame_and_input(self):
        self.check_reconnect(4)

    def test_dropped_v5_connection_restarts_with_idr_and_input(self):
        self.check_reconnect(5)

    def check_reconnect(self, version):
        sockets, hosts, desktops = [], [], []
        host_errors = []

        def factory():
            local, remote = socket.socketpair()
            local.settimeout(3)
            sockets.extend((local, remote))
            desktop = Desktop()
            desktops.append(desktop)

            def host():
                try:
                    serve_stream(remote, desktop, version=version)
                except (OSError, EOFError):
                    pass  # Intentional transport shutdown during this test.
                except Exception as error:
                    host_errors.append(error)
                finally:
                    remote.close()

            thread = threading.Thread(target=host, daemon=True)
            hosts.append(thread)
            thread.start()
            return Session(local)

        inputs = _InputBuffer(enabled=False)
        worker = _RecoveryWorker(factory, 'synthetic v%d recovery' % version, inputs, Image)

        def next_frame(minimum_epoch=0):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    epoch, frame = worker.frames.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    self.assertEqual(frame.size, (64, 36))
                    if epoch > minimum_epoch:
                        worker.frame_shown(epoch)
                        return epoch
                finally:
                    frame.close()
            self.fail('No complete frame after reconnect')

        worker.start()
        try:
            first_epoch = next_frame()
            sockets[1].shutdown(socket.SHUT_RDWR)
            next_epoch = next_frame(first_epoch)
            self.assertGreater(next_epoch, first_epoch)
            self.assertGreaterEqual(len(desktops), 2)
            inputs.append({'type': 'key', 'key': 'q', 'down': True})
            inputs.append({'type': 'key', 'key': 'q', 'down': False})
            self.assertTrue(desktops[-1].key_arrived.wait(2))
            self.assertIsNone(worker.terminal_error)
        finally:
            worker.cancel()
            for sock in sockets:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
            worker.join(3)
            for host in hosts:
                host.join(3)
            for sock in sockets:
                sock.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(any(host.is_alive() for host in hosts))
        self.assertFalse(any(desktop.has_held_input for desktop in desktops))
        self.assertEqual(host_errors, [])


if __name__ == '__main__':
    unittest.main()
