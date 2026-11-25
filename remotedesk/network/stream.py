"""Full-duplex input with versioned image updates or H.264 video.

V4+ send fresh images/video within a decode-acknowledged window.
Older peers retain request/reply framing. Socket mode stays nonblocking so one
direction cannot change the other's timeout. Capture stays in its owning
thread; the independent control reader owns input injection and its lease.
"""

import json
import select
import socket
import struct
import threading
import time
from collections import deque
from contextlib import suppress

from remotedesk.network.session import MAX_FRAME, MAX_INPUT, check_events
from remotedesk.network.timing import FrameTimer
from remotedesk.network.rate import VideoRateControl
from remotedesk.network.video import validate_frame_rate

READY_V2 = b"remote-desktop/2 ready"
READY_V3 = b"remote-desktop/3 ready"
READY = b"remote-desktop/4 ready"
READY_V5 = b"remote-desktop/5 ready"
READY_V6 = b"remote-desktop/6 ready"
BUSY = b"remote-desktop/2 busy"
INPUT_LEASE = 3.0
CONTROL_IDLE = 20.0
TRANSFER_IDLE = 12.0
TRANSFER_TOTAL = 60.0
# The byte window includes the packet length and frame envelope. A frame larger
# than this budget is allowed only as the sole outstanding frame (<= MAX_FRAME).
MAX_IN_FLIGHT_FRAMES = 3
# At 60 fps four credits cover a 40 ms RTT plus encoding/decoding without
# expanding the byte budget. Higher-RTT healthy paths may scale up to 8 frames
# (still capped by MAX_IN_FLIGHT_BYTES); at 30 fps the three-credit limit applies.
MAX_IN_FLIGHT_FRAMES_60 = 4
MAX_IN_FLIGHT_FRAMES_60_HIGH_RTT = 8
MAX_IN_FLIGHT_BYTES = 512 * 1024
FRAME_INTERVAL = 0.1
FRAME_HEADER = struct.Struct("!QH")  # sequence, requested resolution profile


def _next_capture_deadline(previous, started, interval):
    # Keep normal timer overshoot from accumulating into a lower frame rate.
    # After real backpressure, reset instead of bursting to catch up old slots.
    if previous <= 0 or started - previous >= interval:
        return started + interval
    return previous + interval


def _wait(sock, writing, timeout):
    try:
        reads, writes, _ = select.select([] if writing else [sock], [sock] if writing else [], [], timeout)
        return bool(writes if writing else reads)
    except (ValueError, OSError) as error:
        raise ConnectionError("连接已关闭。") from error


def _read(sock, size, *, deadline, idle, tick=None, label="数据", cancelled=None):
    data = bytearray()
    last_progress = time.monotonic()
    while len(data) < size:
        if cancelled is not None and cancelled.is_set():
            raise EOFError("连接已关闭。")
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


def read_packet(sock, limit, *, idle=TRANSFER_IDLE, total=TRANSFER_TOTAL, tick=None, label="数据", cancelled=None):
    deadline = time.monotonic() + total
    header = _read(sock, 4, deadline=deadline, idle=idle, tick=tick, label=label + "长度", cancelled=cancelled)
    size, = struct.unpack("!I", header)
    if not 0 < size <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    return _read(sock, size, deadline=deadline, idle=idle, tick=tick, label=label, cancelled=cancelled)


def write_packet(sock, payload, limit, *, idle=TRANSFER_IDLE, total=TRANSFER_TOTAL, cancelled=None):
    if not 0 < len(payload) <= limit:
        raise ValueError("远程桌面数据包长度无效。")
    data = memoryview(struct.pack("!I", len(payload)) + payload)
    started = last_progress = time.monotonic()
    sent = 0
    while sent < len(data):
        if cancelled is not None and cancelled.is_set():
            raise EOFError("连接已关闭。")
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
        with suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPALIVE", 15), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, option):
                with suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)


class StreamClient:
    """One receiver and a serialized, independent control writer."""

    def __init__(self, sock, *, version=4):
        self.sock = sock
        self.version = version
        _configure(self.sock)
        self._writer = threading.Lock()
        self._receiver = threading.Lock()
        self._closed = threading.Event()
        self._previous = None
        self._decoder = None
        self._video_decoder = None
        self._video_requested = False
        self.media_format = "图片"
        if version >= 5:
            from remotedesk.network.video import decoder_available
            self._video_requested = decoder_available()
        self._width = 1920
        self._frame_rate = 60 if version >= 6 else 30
        self._started = False
        self._received_sequence = 0

    def set_resolution(self, width):
        if type(width) is not int or width not in (1280, 1920):
            raise ValueError("画面宽度须为 1280 或 1920。")
        with self._writer:
            changed = self._width != width
            self._width = width
            if self.version >= 4 and self._started and changed:
                self._write_command({"events": [], "frame": False, "width": width})

    def set_frame_rate(self, frame_rate):
        frame_rate = validate_frame_rate(frame_rate)
        if self.version < 6:
            raise RuntimeError("请升级共享端以选择视频帧率。")
        with self._writer:
            changed = self._frame_rate != frame_rate
            self._frame_rate = frame_rate
            if self._started and changed:
                self._write_command({"events": [], "frame": False, "fps": frame_rate})

    def _write_command(self, command):
        if self._closed.is_set():
            raise ConnectionError("连接已关闭。")
        payload = json.dumps(command).encode("utf-8")
        write_packet(self.sock, payload, MAX_INPUT, idle=12, total=12, cancelled=self._closed)

    def _command(self, events, frame, *, ack=None):
        check_events(events)
        with self._writer:
            command = {"events": events, "frame": frame}
            if self.version >= 3 and frame:
                command["width"] = self._width
            if self.version >= 5 and frame and not self._started:
                command["codec"] = "h264" if self._video_requested else "tiles"
            if self.version >= 6 and frame and not self._started:
                command["fps"] = self._frame_rate
            if ack is not None:
                command["ack"] = ack
            self._write_command(command)
            if self.version >= 4 and frame:
                self._started = True

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
        payload = read_packet(self.sock, MAX_FRAME + 1, label="桌面画面", cancelled=self._closed)
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
        check_events(events)
        with self._receiver:
            if self._closed.is_set():
                raise ConnectionError("连接已关闭。")
            if self._decoder is None:
                self._decoder = TileDecoder()
            if self.version < 3:
                return self._decoder.decode(b"J" + self.exchange(events))
            if self.version < 4:
                self._command(events, True)
                payload = read_packet(self.sock, MAX_FRAME + 1, label="桌面画面", cancelled=self._closed)
                return self._decoder.decode(payload)
            try:
                if not self._started:
                    self._command(events, True)
                elif events:
                    self.send_input(events)
                while True:
                    payload = read_packet(self.sock, MAX_FRAME + 1 + FRAME_HEADER.size,
                                          label="桌面画面", cancelled=self._closed)
                    if len(payload) <= FRAME_HEADER.size:
                        raise ValueError("共享端返回了无效的画面封装。")
                    sequence, width = FRAME_HEADER.unpack_from(payload)
                    if sequence != self._received_sequence + 1 or width not in (1280, 1920):
                        raise ValueError("共享端返回了无效的画面序号或宽度。")
                    body = payload[FRAME_HEADER.size:]
                    if body[:1] == b"H":
                        if self.version < 5 or not self._video_requested:
                            raise ValueError("共享端发送了未协商的视频格式。")
                        if self._video_decoder is None:
                            from remotedesk.network.video import VideoDecoder
                            self._video_decoder = VideoDecoder()
                        image = self._video_decoder.decode(body)
                        self.media_format = "H.264 · 4:4:4"
                    else:
                        image = self._decoder.decode(body)
                        self.media_format = "图片"
                    try:
                        # Decode every delta in order, including buffered images
                        # from the old profile. Only validated images free credit.
                        self._command([], False, ack=sequence)
                        self._received_sequence = sequence
                    except BaseException:
                        image.close()
                        raise
                    if width == self._width:
                        return image
                    image.close()
            except BaseException:
                _shutdown(self.sock)
                raise

    def close(self):
        self._closed.set()
        _shutdown(self.sock)
        # Shutdown interrupts a reader or ACK writer before waiting for its
        # decoder, so cross-thread cancellation cannot strand this lock.
        with self._receiver:
            if self._decoder is not None:
                self._decoder.close()
                self._decoder = None
            if self._video_decoder is not None:
                self._video_decoder.close()
                self._video_decoder = None


def serve_stream(conn, desktop, *, version=4):
    """Serve one authorized client. The caller owns Desktop's lifetime."""
    if version >= 3 and not callable(getattr(desktop, "capture_image", None)):
        raise RuntimeError("共享端缺少新版屏幕采集组件，请重新安装完整版本。")
    _configure(conn)
    greeting = (READY_V6 if version >= 6 else READY_V5 if version >= 5 else
                READY if version >= 4 else READY_V3 if version >= 3 else READY_V2)
    write_packet(conn, greeting, 128, idle=12, total=12)
    if version < 3 and hasattr(desktop, "reset_stream"):
        desktop.reset_stream(minimum_width=1280)
    stopped = threading.Event()
    requested = threading.Event()
    failures = []
    last_command = time.monotonic()
    requested_width = 1920
    request_lock = threading.Condition()
    streaming = False
    requested_codec = "tiles"
    requested_frame_rate = 60 if version >= 6 else 30
    outstanding = deque()
    outstanding_bytes = 0
    last_sequence = last_ack = 0
    drain_window = False
    rate_control = VideoRateControl()

    def release_if_expired(now):
        if now - last_command >= INPUT_LEASE and desktop.has_held_input:
            desktop.apply([{"type": "release"}])
            print("控制心跳延迟，已释放按键和鼠标；继续等待连接恢复。", flush=True)

    def controls():
        nonlocal last_command, requested_width, streaming, outstanding_bytes, last_ack, requested_codec, requested_frame_rate
        try:
            while not stopped.is_set():
                raw = read_packet(conn, MAX_INPUT, idle=CONTROL_IDLE, total=CONTROL_IDLE,
                                  tick=release_if_expired, label="控制指令", cancelled=stopped)
                command = json.loads(raw)
                allowed = {"events", "frame", "width", "ack"} if version >= 4 else {"events", "frame", "width"}
                if version >= 5:
                    allowed.add("codec")
                if version >= 6:
                    allowed.add("fps")
                if (type(command) is not dict or not {"events", "frame"} <= set(command)
                        or not set(command) <= allowed or type(command["frame"]) is not bool):
                    raise ValueError("控制指令格式无效。")
                if "codec" in command:
                    if streaming or not command["frame"] or command["codec"] not in ("h264", "tiles"):
                        raise ValueError("视频格式只能在首次画面请求中协商。")
                    requested_codec = command["codec"]
                width = command.get("width", 1920)
                if (type(width) is not int or width not in (1280, 1920)
                        or ("width" in command and (version < 3 or (version < 4 and not command["frame"])))):
                    raise ValueError("画面宽度指令无效。")
                if "fps" in command:
                    validate_frame_rate(command["fps"])
                check_events(command["events"])
                if "ack" in command:
                    with request_lock:
                        if type(command["ack"]) is not int or not last_ack < command["ack"] <= last_sequence:
                            raise ValueError("画面确认序号无效。")
                desktop.apply(command["events"])
                last_command = time.monotonic()
                with request_lock:
                    if "ack" in command:
                        last_ack = command["ack"]
                        while outstanding and outstanding[0][0] <= last_ack:
                            _, size, sent_at = outstanding.popleft()
                            outstanding_bytes -= size
                            if video:
                                rate_control.acknowledge(last_command - sent_at, last_command)
                    if version >= 4:
                        if "width" in command:
                            requested_width = width
                        if "fps" in command:
                            requested_frame_rate = command["fps"]
                        streaming = streaming or command["frame"]
                        request_lock.notify_all()
                    elif command["frame"]:
                        requested_width = width
                        requested.set()
        except EOFError:
            pass
        except Exception as error:
            if not stopped.is_set():
                failures.append(error)
        finally:
            stopped.set()
            with request_lock:
                request_lock.notify_all()
            _shutdown(conn)
            with suppress(Exception):
                desktop.apply([{"type": "release"}])

    reader = threading.Thread(target=controls, name="desktop-controls", daemon=True)
    encoder = None
    if version >= 3:
        from remotedesk.network.tiles import TileEncoder
        encoder = TileEncoder()
    frame_timer = FrameTimer()
    reader.start()
    previous = None
    phase = "等待画面请求"
    next_capture = 0.0
    codec_selected = False
    video = False
    report_at = time.monotonic()
    report_frames = report_bytes = 0
    report_capture = report_send = 0.0
    report_encode = 0.0
    applied_bitrate = rate_control.bitrate
    def _frame_limit():
        if video and requested_frame_rate == 60:
            return rate_control.frame_window(MAX_IN_FLIGHT_FRAMES_60, MAX_IN_FLIGHT_FRAMES_60_HIGH_RTT, 60)
        return MAX_IN_FLIGHT_FRAMES

    try:
        while not stopped.is_set():
            if version >= 4:
                with request_lock:
                    while not stopped.is_set():
                        frame_timer.set_rate(requested_frame_rate if video else 30)
                        frame_limit = _frame_limit()
                        available = (streaming and len(outstanding) < frame_limit
                                     and outstanding_bytes < MAX_IN_FLIGHT_BYTES
                                     and (not drain_window or not outstanding))
                        delay = next_capture - time.monotonic()
                        if available and delay <= 0:
                            break
                        request_lock.wait(min(0.2, delay) if available and delay > 0 else 0.2)
                if stopped.is_set():
                    break
                # Do not capture ahead while the previous write is stalled or
                # the socket has no send space. Input has its own reader.
                wait_started = time.monotonic()
                while not stopped.is_set() and not _wait(conn, True, 0.2):
                    if time.monotonic() - wait_started >= TRANSFER_IDLE:
                        raise TimeoutError("发送缓冲区持续拥塞，等待发送空间超时。")
                with request_lock:
                    width = requested_width
                    frame_rate = requested_frame_rate
                    bitrate = rate_control.bitrate
                    if not outstanding:
                        drain_window = False
            else:
                if not requested.wait(0.2):
                    continue
                with request_lock:
                    requested.clear()
                    width = requested_width
                    frame_rate = requested_frame_rate
            if stopped.is_set():
                break
            if version >= 5 and not codec_selected:
                if requested_codec == "h264":
                    from remotedesk.network.video import VideoEncoder, encoder_available
                    if encoder_available():
                        encoder.close()
                        encoder = VideoEncoder(frame_rate=frame_rate)
                        video = True
                codec_selected = True
                print(f"画面传输：H.264 4:4:4，最高 {frame_rate} 帧/秒，保留所选分辨率。" if video else
                      "画面传输：图片兼容模式，最高 10 帧/秒。", flush=True)
            if video:
                encoder.set_frame_rate(frame_rate)
                encoder.set_bitrate(bitrate)
                if bitrate != applied_bitrate:
                    print("根据画面接收耗时调整视频码率上限：%.1f Mbps。" %
                          (bitrate / 1_000_000), flush=True)
                    applied_bitrate = bitrate
            frame_timer.set_rate(frame_rate if video else 30)
            phase = "采集桌面"
            started = time.monotonic()
            interval = 1 / frame_rate if video else FRAME_INTERVAL
            next_capture = _next_capture_deadline(next_capture, started, interval)
            if video and callable(getattr(desktop, "capture_video_frame", None)):
                frame = desktop.capture_video_frame(max_width=width)
                captured = time.monotonic()
                payload = encoder.encode_frame(frame)
                del frame
            elif encoder is not None:
                with desktop.capture_image(max_width=width) as frame:
                    captured = time.monotonic()
                    payload = encoder.encode(frame)
            else:
                frame = desktop.frame()
                payload = b"U" if frame == previous else b"J" + frame
                captured = time.monotonic()
            encoded = time.monotonic()
            if version >= 4:
                wire_size = 4 + FRAME_HEADER.size + len(payload)
                with request_lock:
                    frame_limit = _frame_limit()
                    if len(outstanding) >= frame_limit:
                        # The reader can lower 60 -> 30 while capture/encoding
                        # is running. Do not reserve a fourth frame afterward.
                        sequence = None
                    elif outstanding and outstanding_bytes + wire_size > MAX_IN_FLIGHT_BYTES:
                        drain_window = True
                        sequence = None
                    else:
                        # Reserve before writing: the peer may decode and ACK
                        # the last bytes before write_packet returns here.
                        last_sequence += 1
                        sequence = last_sequence
                        outstanding.append((sequence, wire_size, encoded))
                        outstanding_bytes += wire_size
                if sequence is None:
                    # Encoded length is unknowable before capture. Never keep
                    # an unsent old image while waiting for byte credit, and
                    # reset its unsent delta base before the next fresh capture.
                    del payload
                    encoder.close()
                    continue
                payload = FRAME_HEADER.pack(sequence, width) + payload
            phase = "发送桌面画面"
            write_packet(conn, payload, MAX_FRAME + 1 + (FRAME_HEADER.size if version >= 4 else 0),
                         cancelled=stopped)
            finished = time.monotonic()
            report_frames += 1
            report_bytes += len(payload)
            report_capture += captured - started
            report_encode += encoded - captured
            report_send += finished - encoded
            elapsed = finished - report_at
            if version >= 5 and elapsed >= 5:
                print("画面统计：%.1f 帧/秒，%.2f Mbps，采集转换 %.1f ms/帧，编码 %.1f ms/帧，发送 %.1f ms/帧。" %
                      (report_frames / elapsed, report_bytes * 8 / elapsed / 1_000_000,
                       report_capture * 1000 / report_frames, report_encode * 1000 / report_frames,
                       report_send * 1000 / report_frames), flush=True)
                report_at, report_frames, report_bytes = finished, 0, 0
                report_capture = report_encode = report_send = 0.0
            if version < 3 and hasattr(desktop, "frame_transmitted"):
                desktop.frame_transmitted(payload != b"U")
            if finished - started >= 1:
                print("慢帧：采集 %.2f 秒，发送 %.2f 秒，%d 字节。" %
                      (encoded - started, finished - encoded, len(payload)), flush=True)
            if encoder is None:
                previous = frame
            del payload
            phase = "等待画面请求"
    except (OSError, EOFError) as error:
        if not stopped.is_set():
            raise type(error)(f"{phase}失败：{error}") from error
    except Exception as error:
        raise RuntimeError(f"{phase}失败：{error}") from error
    finally:
        frame_timer.close()
        stopped.set()
        with request_lock:
            request_lock.notify_all()
        _shutdown(conn)
        # Never let a replacement desktop overlap a reader still applying input.
        reader.join()
        if encoder is not None:
            encoder.close()
    if failures:
        error = failures[0]
        raise RuntimeError(f"接收控制指令失败：{error}") from error
