"""Full-duplex input with versioned JPEG or regional image updates.

Only one requested image can be in flight. Socket mode stays nonblocking so
one direction cannot change the other direction's timeout. Capture stays in
its owning thread; the control reader owns input injection and its lease.
"""

import json
import select
import socket
import struct
import threading
import time
from contextlib import suppress

from remotedesk.network.session import MAX_FRAME, MAX_INPUT, check_events

READY_V2 = b"remote-desktop/2 ready"
READY = b"remote-desktop/3 ready"
BUSY = b"remote-desktop/2 busy"
INPUT_LEASE = 3.0
CONTROL_IDLE = 45.0
TRANSFER_IDLE = 25.0
TRANSFER_TOTAL = 60.0


def _wait(sock, writing, timeout):
    try:
        reads, writes, _ = select.select([] if writing else [sock], [sock] if writing else [], [], timeout)
        return bool(writes if writing else reads)
    except (ValueError, OSError) as error:
        raise ConnectionError("连接已关闭。") from error


def _read(sock, size, *, deadline, idle, tick=None, label="数据"):
    data = bytearray()
    last_progress = time.monotonic()
    while len(data) < size:
        now = time.monotonic()
        if tick is not None:
            tick(now)
        remaining = min(deadline - now, idle - (now - last_progress))
        if remaining <= 0:
            raise TimeoutError(f"接收{label}超时（{len(data)}/{size} 字节）。")
        if not _wait(sock, False, min(0.2, remaining)):
            continue
        try:
            block = sock.recv(min(65536, size - len(data)))
        except (BlockingIOError, InterruptedError):
            continue
        if not block:
            raise EOFError(f"对端已断开连接（{label} {len(data)}/{size} 字节）。")
        data.extend(block)
        last_progress = time.monotonic()
    return bytes(data)


def read_packet(sock, limit, *, idle=TRANSFER_IDLE, total=TRANSFER_TOTAL, tick=None, label="数据"):
    deadline = time.monotonic() + total
    header = _read(sock, 4, deadline=deadline, idle=idle, tick=tick, label=label + "长度")
    size, = struct.unpack("!I", header)
    if not 0 < size <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    return _read(sock, size, deadline=deadline, idle=idle, tick=tick, label=label)


def write_packet(sock, payload, limit, *, idle=TRANSFER_IDLE, total=TRANSFER_TOTAL):
    if not 0 < len(payload) <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    data = memoryview(struct.pack("!I", len(payload)) + payload)
    started = last_progress = time.monotonic()
    sent = 0
    while sent < len(data):
        now = time.monotonic()
        remaining = min(total - (now - started), idle - (now - last_progress))
        if remaining <= 0:
            raise TimeoutError(f"发送画面或指令超时（{sent}/{len(data)} 字节）。")
        if not _wait(sock, True, min(0.2, remaining)):
            continue
        try:
            count = sock.send(data[sent:sent + 65536])
        except (BlockingIOError, InterruptedError):
            continue
        if count == 0:
            raise EOFError("对端已断开连接。")
        sent += count
        last_progress = time.monotonic()


def _shutdown(sock):
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _configure(sock):
    sock.setblocking(False)
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class StreamClient:
    """One receiver and a serialized, independent control writer."""

    def __init__(self, sock, *, version=3):
        self.sock = sock
        self.version = version
        _configure(self.sock)
        self._writer = threading.Lock()
        self._previous = None
        self._decoder = None
        self._width = 1920

    def set_resolution(self, width):
        if type(width) is not int or width not in (1280, 1920):
            raise ValueError("画面宽度须为 1280 或 1920。")
        self._width = width

    def _command(self, events, frame):
        check_events(events)
        command = {"events": events, "frame": frame}
        if self.version >= 3 and frame:
            command["width"] = self._width
        payload = json.dumps(command).encode("utf-8")
        with self._writer:
            write_packet(self.sock, payload, MAX_INPUT, idle=12, total=12)

    def send_input(self, events):
        self._command(events, False)

    def exchange(self, events):
        if self.version >= 3:
            import io
            with self.exchange_image(events) as image:
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=75, subsampling=0)
                return output.getvalue()
        self._command(events, True)
        payload = read_packet(self.sock, MAX_FRAME + 1, label="桌面画面")
        if payload == b"U":
            if self._previous is None:
                raise ValueError("共享端尚未提供完整画面。")
            return self._previous
        if payload[:1] != b"J" or len(payload) == 1:
            raise ValueError("共享端返回了无效的画面格式。")
        self._previous = payload[1:]
        return self._previous

    def exchange_image(self, events):
        from remotedesk.network.tiles import TileDecoder
        if self._decoder is None:
            self._decoder = TileDecoder()
        if self.version < 3:
            return self._decoder.decode(b"J" + self.exchange(events))
        self._command(events, True)
        payload = read_packet(self.sock, MAX_FRAME + 1, label="桌面画面")
        return self._decoder.decode(payload)


def serve_stream(conn, desktop, *, version=3):
    """Serve one authorized client. The caller owns Desktop's lifetime."""
    if version >= 3 and not callable(getattr(desktop, "capture_image", None)):
        raise RuntimeError("共享端缺少新版屏幕采集组件，请重新安装完整版本。")
    _configure(conn)
    write_packet(conn, READY if version >= 3 else READY_V2, 128, idle=12, total=12)
    if version < 3 and hasattr(desktop, "reset_stream"):
        desktop.reset_stream(minimum_width=1280)
    stopped = threading.Event()
    requested = threading.Event()
    failures = []
    last_command = time.monotonic()
    requested_width = 1920
    request_lock = threading.Lock()

    def release_if_expired(now):
        if now - last_command >= INPUT_LEASE and desktop.has_held_input:
            desktop.apply([{"type": "release"}])
            print("控制心跳延迟，已释放按键和鼠标；继续等待连接恢复。", flush=True)

    def controls():
        nonlocal last_command, requested_width
        try:
            while not stopped.is_set():
                raw = read_packet(conn, MAX_INPUT, idle=CONTROL_IDLE, total=CONTROL_IDLE,
                                  tick=release_if_expired, label="控制指令")
                command = json.loads(raw)
                if (type(command) is not dict
                        or set(command) not in ({"events", "frame"}, {"events", "frame", "width"})
                        or type(command["frame"]) is not bool):
                    raise ValueError("控制指令格式无效。")
                width = command.get("width", 1920)
                if (type(width) is not int or width not in (1280, 1920)
                        or ("width" in command and (version < 3 or not command["frame"]))):
                    raise ValueError("画面宽度指令无效。")
                check_events(command["events"])
                desktop.apply(command["events"])
                last_command = time.monotonic()
                if command["frame"]:
                    with request_lock:
                        requested_width = width
                        requested.set()
        except EOFError:
            pass
        except Exception as error:
            if not stopped.is_set():
                failures.append(error)
        finally:
            stopped.set()
            _shutdown(conn)
            desktop.apply([{"type": "release"}])

    reader = threading.Thread(target=controls, name="desktop-controls", daemon=True)
    encoder = None
    if version >= 3:
        from remotedesk.network.tiles import TileEncoder
        encoder = TileEncoder()
    reader.start()
    previous = None
    phase = "等待画面请求"
    try:
        while not stopped.is_set():
            if not requested.wait(0.2):
                continue
            with request_lock:
                requested.clear()
                width = requested_width
            if stopped.is_set():
                break
            phase = "采集桌面"
            started = time.monotonic()
            if encoder is not None:
                with desktop.capture_image(max_width=width) as frame:
                    payload = encoder.encode(frame)
            else:
                frame = desktop.frame()
                payload = b"U" if frame == previous else b"J" + frame
            captured = time.monotonic()
            phase = "发送桌面画面"
            write_packet(conn, payload, MAX_FRAME + 1)
            finished = time.monotonic()
            if version < 3 and hasattr(desktop, "frame_transmitted"):
                desktop.frame_transmitted(payload != b"U")
            if finished - started >= 1:
                print("慢帧：采集 %.2f 秒，发送 %.2f 秒，%d 字节。" %
                      (captured - started, finished - captured, len(payload)), flush=True)
            if encoder is None:
                previous = frame
            phase = "等待画面请求"
    except (OSError, EOFError) as error:
        if not stopped.is_set():
            raise type(error)(f"{phase}失败：{error}") from error
    except Exception as error:
        raise RuntimeError(f"{phase}失败：{error}") from error
    finally:
        stopped.set()
        _shutdown(conn)
        # Never let a replacement desktop overlap a reader still applying input.
        reader.join()
        if encoder is not None:
            encoder.close()
    if failures:
        error = failures[0]
        raise RuntimeError(f"接收控制指令失败：{error}") from error
