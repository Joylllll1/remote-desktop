"""Accept reconnects while retaining one authorized desktop/input owner."""

from contextlib import suppress
import os
import socket
import subprocess
import sys
import threading

from remotedesk.host.desktop import Desktop
from remotedesk.network.session import DENIED, PORT, send
from remotedesk.network.stream import BUSY, serve_stream
from remotedesk.network.tailscale import address, identify


_SESSION_JOIN_TIMEOUT = 2


def _shutdown(conn):
    # Cross-thread close can race a timeout-mode recv/select and leave it
    # sleeping on a reused/invalid descriptor. Only its owner closes the socket.
    with suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)


def host(self_node, peer, *, panel_input=None):
    expected = peer.get("ID")
    if not expected:
        raise ValueError("无法确定允许设备的 Tailscale 节点身份。")
    ip = address(self_node)
    name = peer.get("HostName", "允许的设备")
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    stopped = threading.Event()
    state_lock = threading.Lock()
    active = None
    pending = None

    def stop():
        stopped.set()
        with state_lock:
            current, candidate = active, pending
        if current is not None:
            current[2].set()
            _shutdown(current[0])
        if candidate is not None:
            _shutdown(candidate)

    def watch_panel():
        try:
            panel_input.read()
        finally:
            stop()

    def session(conn, cancelled):
        phase = "桌面初始化"
        try:
            # MSS is created, used and closed on this thread, never transferred
            # from the accept loop or from a previous session.
            with conn, Desktop() as desktop:
                phase = "桌面传输"
                print(f"{name} 已连接。", flush=True)
                serve_stream(conn, desktop, version=6)
        except Exception as error:
            if not stopped.is_set() and not cancelled.is_set():
                print(f"{phase}失败：{error}", file=sys.stderr, flush=True)
        finally:
            with suppress(OSError):
                conn.close()
            if not stopped.is_set():
                print("会话已结束，等待重新连接。", flush=True)
            # Keep the active record until the accept loop observes this thread
            # has exited. A closed socket alone never permits a second owner.

    with socket.socket(family, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((ip, PORT))
        listener.settimeout(0.5)
        with Desktop() as desktop:
            desktop.frame()
        listener.listen(8)
        if panel_input is not None:
            threading.Thread(target=watch_panel, name="desktop-panel-owner", daemon=True).start()
        print(f"正在共享桌面 {ip}，仅接受 {name} 的连接。", flush=True)
        try:
            while not stopped.is_set():
                try:
                    conn, remote = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stopped.is_set():
                        break
                    raise
                handed_off = False
                with state_lock:
                    pending = conn
                phase = "连接授权检查"
                try:
                    if stopped.is_set():
                        break
                    conn.settimeout(2)
                    if identify(remote) != expected:
                        send(conn, DENIED, 128)
                        continue
                    # An unverified or denied client never touches the owner.
                    with state_lock:
                        previous = active
                    if previous is not None:
                        previous[2].set()
                        _shutdown(previous[0])
                        previous[1].join(_SESSION_JOIN_TIMEOUT)
                        if previous[1].is_alive():
                            phase = "回复会话忙碌状态"
                            send(conn, BUSY, 128)
                            continue
                    phase = "启动桌面会话"
                    cancelled = threading.Event()
                    worker = threading.Thread(target=session, args=(conn, cancelled),
                                              name="desktop-session", daemon=True)
                    with state_lock:
                        if stopped.is_set():
                            break
                        conn.settimeout(12)
                        active = (conn, worker, cancelled)
                        try:
                            worker.start()
                        except BaseException:
                            active = None
                            raise
                        handed_off = True
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                    if not stopped.is_set():
                        print(f"{phase}失败：{error}", file=sys.stderr, flush=True)
                finally:
                    with state_lock:
                        if pending is conn:
                            pending = None
                    if not handed_off:
                        _shutdown(conn)
                        with suppress(OSError):
                            conn.close()
        finally:
            stop()
            with state_lock:
                current = active
            if current is not None:
                current[1].join(_SESSION_JOIN_TIMEOUT)
                if current[1].is_alive():
                    print("桌面会话尚未完成清理，主机正在退出。", file=sys.stderr, flush=True)
