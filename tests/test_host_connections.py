"""Bounded accept/replacement checks using fake desktops and localhost only."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from remotedesk.host import server
from remotedesk.network.session import DENIED, receive, send


READY_V2 = b"remote-desktop/2 ready"
_SOCKET = socket.socket


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("local host condition did not complete")
        time.sleep(0.005)


class HostHarness:
    def __init__(self, serve=None):
        self.serve = serve or self.echo_stream
        self.ready = threading.Event()
        self.unblock = threading.Event()
        self.lock = threading.Lock()
        self.identities = {}
        self.instances = []
        self.calls = []
        self.errors = []
        self.clients = []
        self.session_sockets = []
        self.socket_closes = []
        self.live_desktops = self.maximum_desktops = 0
        self.output, self.error_output = io.StringIO(), io.StringIO()
        self.address = None
        self.backlog = None

    def echo_stream(self, conn, desktop):
        self.session_sockets.append(conn)
        send(conn, READY_V2, 128)
        desktop.frame()
        try:
            while conn.recv(1):
                conn.sendall(b"!")
        finally:
            desktop.apply([{"type": "release"}])

    def identify(self, remote):
        identity = self.identities[remote[1]]
        if isinstance(identity, Exception):
            raise identity
        return identity

    def __enter__(self):
        harness = self

        class FakeDesktop:
            def __init__(self):
                self.index = len(harness.instances)
                self.closed = False
                harness.instances.append(self)
                self.record("create")

            def record(self, operation):
                harness.calls.append((self.index, operation, threading.get_ident()))

            def __enter__(self):
                self.record("enter")
                with harness.lock:
                    harness.live_desktops += 1
                    harness.maximum_desktops = max(harness.maximum_desktops, harness.live_desktops)
                return self

            def frame(self):
                self.record("frame")
                return b"synthetic frame; no capture"

            def apply(self, events):
                self.record("release" if events == [{"type": "release"}] else "input")

            def __exit__(self, *args):
                self.record("exit")
                with harness.lock:
                    harness.live_desktops -= 1
                self.closed = True

        class Listener(_SOCKET):
            def listen(self, backlog):
                super().listen(backlog)
                harness.address = self.getsockname()
                harness.backlog = backlog
                harness.ready.set()

            def close(self):
                if self.fileno() >= 0:
                    harness.socket_closes.append((id(self), threading.current_thread().name))
                super().close()

        read_fd, write_fd = os.pipe()
        self.control = os.fdopen(read_fd, "rb")
        self.owner = os.fdopen(write_fd, "wb")
        self.stack = ExitStack()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.error_output))
        self.stack.enter_context(patch.object(server, "PORT", 0))
        self.stack.enter_context(patch.object(server, "Desktop", FakeDesktop))
        def negotiated_stream(conn, desktop, *, version):
            if version != 6:
                raise AssertionError("Production host must advertise frame-rate protocol v6")
            return self.serve(conn, desktop)
        self.stack.enter_context(patch.object(server, "serve_stream", negotiated_stream))
        self.stack.enter_context(patch.object(server, "identify", self.identify))
        self.stack.enter_context(patch.object(server.socket, "socket", Listener))

        def run():
            self.host_thread_id = threading.get_ident()
            try:
                server.host({"TailscaleIPs": ["127.0.0.1"]},
                            {"ID": "authorized-node", "HostName": "controller"}, panel_input=self.control)
            except BaseException as error:
                self.errors.append(error)
                self.ready.set()

        self.thread = threading.Thread(target=run, name="test-host", daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.errors:
            self.__exit__(None, None, None)
            raise AssertionError("fake host failed to start: " + repr(self.errors))
        return self

    def connect(self, identity="authorized-node"):
        client = _SOCKET()
        client.bind(("127.0.0.1", 0))
        self.identities[client.getsockname()[1]] = identity
        client.settimeout(4)
        self.clients.append(client)
        client.connect(self.address)
        return client

    def stop(self):
        if not self.owner.closed:
            self.owner.close()
        self.thread.join(3)

    def __exit__(self, *args):
        self.unblock.set()
        self.stop()
        for client in self.clients:
            client.close()
        self.control.close()
        self.stack.close()
        if self.thread.is_alive():
            raise AssertionError("host did not stop after panel EOF")
        if self.errors:
            raise AssertionError("host failed: " + repr(self.errors))


class HostConnectionTests(unittest.TestCase):
    def test_blocked_session_does_not_block_authorized_reconnect_or_overlap_desktops(self):
        with HostHarness() as host:
            self.assertEqual(host.backlog, 8)
            self.assertEqual(len(host.instances), 1)
            self.assertTrue(host.instances[0].closed)
            first = host.connect()
            self.assertEqual(receive(first, 128), READY_V2)
            started = time.monotonic()
            replacement = host.connect()
            self.assertEqual(receive(replacement, 128), READY_V2)
            self.assertLess(time.monotonic() - started, 2.8)
            self.assertEqual(first.recv(1), b"")
            self.assertEqual(host.maximum_desktops, 1)
            self.assertEqual(len(host.instances), 3)
            for instance in host.instances:
                threads = {thread for index, _, thread in host.calls if index == instance.index}
                self.assertEqual(len(threads), 1)
                if instance.index == 0:
                    self.assertEqual(threads, {host.host_thread_id})
                else:
                    self.assertNotIn(host.host_thread_id, threads)
        self.assertEqual(host.live_desktops, 0)

    def test_unauthorized_connection_cannot_cancel_the_current_owner(self):
        with HostHarness() as host:
            owner = host.connect()
            self.assertEqual(receive(owner, 128), READY_V2)
            denied = host.connect("different-node")
            self.assertEqual(receive(denied, 128), DENIED)
            self.assertEqual(denied.recv(1), b"")
            owner.sendall(b"x")
            self.assertEqual(owner.recv(1), b"!")
            self.assertEqual(len(host.instances), 2)
            self.assertEqual(host.maximum_desktops, 1)
            self.assertNotIn("different-node", host.output.getvalue())

    def test_authorization_failure_is_closed_and_cannot_cancel_current_owner(self):
        with HostHarness() as host:
            owner = host.connect()
            self.assertEqual(receive(owner, 128), READY_V2)
            unverified = host.connect(subprocess.TimeoutExpired("whois", 5))
            self.assertEqual(unverified.recv(1), b"")
            owner.sendall(b"x")
            self.assertEqual(owner.recv(1), b"!")
            self.assertEqual(len(host.instances), 2)
            self.assertIn("连接授权检查失败", host.error_output.getvalue())

    def test_stuck_desktop_returns_busy_without_spawning_more_workers(self):
        calls = []
        host = HostHarness()

        def stuck_first(conn, desktop):
            calls.append(desktop)
            if len(calls) == 1:
                send(conn, READY_V2, 128)
                try:
                    host.unblock.wait(10)
                finally:
                    desktop.apply([{"type": "release"}])
            else:
                host.echo_stream(conn, desktop)

        host.serve = stuck_first
        with host:
            owner = host.connect()
            self.assertEqual(receive(owner, 128), READY_V2)
            started = time.monotonic()
            contender = host.connect()
            self.assertEqual(receive(contender, 128), server.BUSY)
            self.assertLess(time.monotonic() - started, 2.8)
            self.assertEqual(contender.recv(1), b"")
            with patch.object(server, "_SESSION_JOIN_TIMEOUT", 0.03):
                for _ in range(4):
                    retry = host.connect()
                    self.assertEqual(receive(retry, 128), server.BUSY)
                    self.assertEqual(retry.recv(1), b"")
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(host.instances), 2)
            self.assertEqual(host.maximum_desktops, 1)
            host.unblock.set()
            wait_until(lambda: host.instances[1].closed)
            successor = host.connect()
            self.assertEqual(receive(successor, 128), READY_V2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(host.maximum_desktops, 1)

    def test_panel_eof_stops_idle_accept_loop_without_an_extra_desktop(self):
        with HostHarness() as host:
            started = time.monotonic()
            host.stop()
            self.assertFalse(host.thread.is_alive())
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(host.instances), 1)
            self.assertTrue(host.instances[0].closed)
            self.assertEqual(host.live_desktops, 0)

    def test_panel_eof_shutdown_releases_active_session_before_host_exits(self):
        with HostHarness() as host:
            owner = host.connect()
            self.assertEqual(receive(owner, 128), READY_V2)
            host.stop()
            self.assertFalse(host.thread.is_alive())
            self.assertEqual(owner.recv(1), b"")
            self.assertTrue(all(desktop.closed for desktop in host.instances))
            self.assertTrue(any(index == 1 and operation == "release" for index, operation, _ in host.calls))
            self.assertEqual(host.live_desktops, 0)

    def test_cancellation_interrupts_but_only_session_owner_closes_active_socket(self):
        with HostHarness() as host:
            owner = host.connect()
            self.assertEqual(receive(owner, 128), READY_V2)
            first_socket = host.session_sockets[0]
            replacement = host.connect()
            self.assertEqual(receive(replacement, 128), READY_V2)
            host.stop()
            self.assertFalse(host.thread.is_alive())
            for conn in host.session_sockets:
                closers = [name for identity, name in host.socket_closes if identity == id(conn)]
                self.assertEqual(closers, ["desktop-session"])
            self.assertEqual(first_socket.fileno(), -1)
            self.assertEqual(host.live_desktops, 0)

    def test_transfer_error_has_its_phase_and_next_connection_still_receives_denial(self):
        def broken_stream(conn, desktop):
            send(conn, READY_V2, 128)
            raise RuntimeError("synthetic encoding failure")

        with HostHarness(broken_stream) as host:
            viewer = host.connect()
            self.assertEqual(receive(viewer, 128), READY_V2)
            self.assertEqual(viewer.recv(1), b"")
            wait_until(lambda: "桌面传输失败" in host.error_output.getvalue())
            denied = host.connect("different-node")
            self.assertEqual(receive(denied, 128), DENIED)
            self.assertIn("synthetic encoding failure", host.error_output.getvalue())
            self.assertIn("会话已结束", host.output.getvalue())
            self.assertEqual(host.maximum_desktops, 1)


if __name__ == "__main__":
    unittest.main()
