"""Serve the local desktop to one authorized Tailscale device."""

from contextlib import suppress
import os
import socket
import subprocess
import sys
import threading

from remotedesk.host.desktop import Desktop
from remotedesk.network.session import PORT, serve_connection
from remotedesk.network.tailscale import address, identify


def host(self_node, peer, *, panel_input=None):
    expected = peer.get("ID")
    if not expected:
        raise ValueError("无法确定允许设备的 Tailscale 节点身份。")
    ip = address(self_node)
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    stopped = threading.Event()
    active = None

    def watch_panel():
        try:
            panel_input.read()
        finally:
            stopped.set()
            current = active
            if current is not None:
                with suppress(OSError):
                    current.shutdown(socket.SHUT_RDWR)

    with socket.socket(family, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((ip, PORT))
        listener.settimeout(0.5)
        if panel_input is not None:
            threading.Thread(target=watch_panel, daemon=True).start()
        with Desktop() as desktop:
            desktop.frame()
            listener.listen(2)
            print(f"正在共享桌面 {ip}，仅接受 {peer['HostName']} 的连接。", flush=True)
            while not stopped.is_set():
                try:
                    conn, remote = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    active = conn
                    try:
                        if stopped.is_set():
                            break
                        conn.settimeout(12)
                        authorized = identify(remote) == expected
                        if authorized:
                            desktop.reset_stream()
                            print(f"{peer['HostName']} 已连接。", flush=True)
                        serve_connection(conn, authorized, desktop, idle_transfer_timeout=45)
                        if authorized:
                            print("会话已结束，等待重新连接。", flush=True)
                    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                        if not stopped.is_set():
                            print(f"连接已关闭：{error}", file=sys.stderr, flush=True)
                    finally:
                        active = None
