"""Integration of device selection, panel-owned host shutdown, and reconnect checks."""

import io
import os
import socket
import sys
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from PIL import Image

import remote_desktop as app
from desktop_session import Session


class PanelHostTest(unittest.TestCase):
    def test_reused_hostname_cannot_replace_authorized_device(self):
        replacement = {"HostName": "travel-mac", "ID": "new-node"}
        with self.assertRaises(ValueError):
            app.peer_by_id([replacement], "old-node")
        self.assertIs(app.peer_by_id([replacement], "new-node"), replacement)

    def test_login_child_rechecks_revoked_or_replaced_device_before_opening_desktop(self):
        status = {"Self": {}, "Peer": {"key": {"ID": "old", "HostName": "Mac"}}}
        with patch.object(sys, "argv", ["remote_desktop.py", "host-login-id", "old"]), \
                patch.object(app, "network_status", return_value=status), patch.object(app, "host") as host:
            for choice in (None, ("replacement", "Mac")):
                with self.subTest(choice=choice), patch("desktop_preferences.login_peer", return_value=choice):
                    with self.assertRaisesRegex(ValueError, "授权已变更"):
                        app.main()
            host.assert_not_called()

    def test_check_reconnects_and_panel_close_releases_active_session(self):
        ready = threading.Event()
        output = io.StringIO()
        frame = io.BytesIO()
        Image.new("RGB", (24, 16)).save(frame, "JPEG")
        events = []
        failures = []

        class Desktop:
            def __enter__(self):
                return self

            def frame(self):
                return frame.getvalue()

            def apply(self, batch):
                events.extend(batch)

            def __exit__(self, *args):
                pass

        def run():
            try:
                app.host(local, peer, panel_input=control)
            except BaseException as error:
                failures.append(error)
                ready.set()

        peer = {"HostName": "controller", "ID": "permitted"}
        local = {"HostName": "host", "TailscaleIPs": ["127.0.0.1"], "Online": True}
        addresses = []

        class TestSocket(socket.socket):
            def listen(self, backlog):
                super().listen(backlog)
                addresses.append(self.getsockname())
                ready.set()

        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as control, os.fdopen(write_fd, "wb") as owner, ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(app, "PORT", 0))
            stack.enter_context(patch("desktop_host.Desktop", Desktop))
            stack.enter_context(patch.object(app, "identify", return_value="permitted"))
            stack.enter_context(patch.object(app.socket, "socket", TestSocket))
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertFalse(failures, failures)
                with patch.object(app, "PORT", addresses[0][1]):
                    app.check_connection(local)
                sess = Session(socket.create_connection(addresses[0], timeout=3))
                try:
                    sess.exchange([{"type": "key", "key": "a", "down": True}])
                    owner.close()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(events[-1], {"type": "release"})
                finally:
                    sess.close()
            finally:
                owner.close()
                thread.join(3)
            self.assertFalse(failures, failures)
        self.assertIn("重新连接成功", output.getvalue())
        self.assertIn("24×16", output.getvalue())


if __name__ == "__main__":
    unittest.main()
