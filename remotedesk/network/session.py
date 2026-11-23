"""One client requests a JPEG frame and submits its pending input in each exchange."""

import json
import socket
import struct
import threading
import time
from contextlib import suppress

PORT = 47650
MAX_INPUT = 64 * 1024
MAX_FRAME = 12 * 1024 * 1024
FRAME_IDLE_TIMEOUT = 25
FRAME_TOTAL_TIMEOUT = 60
READY = b"remote-desktop/1 ready"
DENIED = b"remote-desktop/1 denied"


def read_exact(sock, size, *, deadline=None):
    data = bytearray()
    while len(data) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("整帧传输超过等待时间。")
            sock.settimeout(min(FRAME_IDLE_TIMEOUT, remaining))
        block = sock.recv(min(size - len(data), 64 * 1024))
        if not block:
            raise EOFError("对端已断开连接。")
        data.extend(block)
    return bytes(data)


def receive(sock, limit, *, frame_timeout=None):
    deadline = time.monotonic() + frame_timeout if frame_timeout is not None else None
    previous_timeout = sock.gettimeout() if deadline is not None else None
    try:
        size, = struct.unpack("!I", read_exact(sock, 4, deadline=deadline))
        if not 0 < size <= limit:
            raise ValueError("远程桌面数据包长度无效。")
        return read_exact(sock, size, deadline=deadline)
    finally:
        if deadline is not None:
            with suppress(OSError):
                sock.settimeout(previous_timeout)


def send(sock, data, limit):
    if not 0 < len(data) <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    sock.sendall(struct.pack("!I", len(data)) + data)


def check_events(events):
    if not isinstance(events, list) or len(events) > 128:
        raise ValueError("输入事件格式无效或数量过多。")


def serve_connection(conn, authorized, desktop, *, idle_transfer_timeout=None):
    if not authorized:
        send(conn, DENIED, 128)
        return
    try:
        send(conn, READY, 128)
        while True:
            events = json.loads(receive(conn, MAX_INPUT))
            check_events(events)
            desktop.apply(events)
            if idle_transfer_timeout is not None:
                # A slow viewer can still be downloading the previous frame
                # after sendall returns. Keep a shorter release deadline when
                # it is holding a key or dragging with a mouse button.
                conn.settimeout(12 if desktop.has_held_input else idle_transfer_timeout)
            send(conn, desktop.frame(), MAX_FRAME)
    except EOFError:
        pass
    finally:
        desktop.apply([{"type": "release"}])


class Session:
    def __init__(self, sock):
        self.sock = sock
        self._close_lock = threading.Lock()
        self._stream = None
        self.supports_async_input = False
        try:
            try:
                greeting = receive(sock, 128)
            except TimeoutError as error:
                raise TimeoutError("已接通共享端口，等待设备授权响应超时。") from error
            if greeting == DENIED:
                raise ValueError("这台电脑未获授权。请在对方的「共享本机」中选中这台电脑，再开始共享。")
            from remotedesk.network.stream import BUSY, READY as STREAM_READY, StreamClient
            if greeting == BUSY:
                raise ConnectionError("共享端正在结束上一次会话，请稍候重试。")
            if greeting == STREAM_READY:
                self._stream = StreamClient(sock)
                self.supports_async_input = True
            elif greeting != READY:
                raise ValueError("对端运行了其他服务或协议版本不兼容。")
        except BaseException:
            self.close()
            raise

    def exchange(self, events):
        if self._stream is not None:
            return self._stream.exchange(events)
        check_events(events)
        try:
            send(self.sock, json.dumps(events).encode("utf-8"), MAX_INPUT)
        except TimeoutError as error:
            raise TimeoutError("发送画面请求超时。") from error
        try:
            return receive(self.sock, MAX_FRAME, frame_timeout=FRAME_TOTAL_TIMEOUT)
        except TimeoutError as error:
            raise TimeoutError("已获授权，接收桌面画面超时。") from error

    def send_input(self, events):
        if self._stream is None:
            raise RuntimeError("请升级共享端以使用独立控制通道。")
        self._stream.send_input(events)

    def close(self):
        with self._close_lock:
            with suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
