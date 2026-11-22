"""One client requests a JPEG frame and submits its pending input in each exchange."""

import json
import socket
import struct
from contextlib import suppress

PORT = 47650
MAX_INPUT = 64 * 1024
MAX_FRAME = 12 * 1024 * 1024
READY = b"remote-desktop/1 ready"
DENIED = b"remote-desktop/1 denied"


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        block = sock.recv(min(size - len(data), 64 * 1024))
        if not block:
            raise EOFError("对端已断开连接。")
        data.extend(block)
    return bytes(data)


def receive(sock, limit):
    size, = struct.unpack("!I", read_exact(sock, 4))
    if not 0 < size <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    return read_exact(sock, size)


def send(sock, data, limit):
    if not 0 < len(data) <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    sock.sendall(struct.pack("!I", len(data)) + data)


def check_events(events):
    if not isinstance(events, list) or len(events) > 128:
        raise ValueError("输入事件格式无效或数量过多。")


def serve_connection(conn, authorized, desktop):
    if not authorized:
        send(conn, DENIED, 128)
        return
    try:
        send(conn, READY, 128)
        while True:
            events = json.loads(receive(conn, MAX_INPUT))
            check_events(events)
            desktop.apply(events)
            send(conn, desktop.frame(), MAX_FRAME)
    except EOFError:
        pass
    finally:
        desktop.apply([{"type": "release"}])


class Session:
    def __init__(self, sock):
        self.sock = sock
        try:
            greeting = receive(sock, 128)
            if greeting == DENIED:
                raise ValueError("家中电脑尚未授权当前设备，请检查 host --allow 的设备名。")
            if greeting != READY:
                raise ValueError("对端运行了其他服务或协议版本不兼容。")
        except BaseException:
            self.close()
            raise

    def exchange(self, events):
        check_events(events)
        send(self.sock, json.dumps(events).encode("utf-8"), MAX_INPUT)
        return receive(self.sock, MAX_FRAME)

    def close(self):
        with suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
