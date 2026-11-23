"""Loopback-only, synthetic desktop comparison; never connects to a real host.

Run from the repository: .venv/bin/python tools/benchmark_stream.py --help
The relay delays byte chunks independently of reads and bounds its queues so
it models propagation and backpressure, rather than sleeping once per frame.
"""

import argparse
from collections import deque
from contextlib import suppress
import json
from pathlib import Path
import queue
import socket
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import ImageDraw
from remotedesk.network import stream
from remotedesk.network.session import Session
from tests.test_stream_tiles import RGBDesktop


def close(sock):
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with suppress(OSError):
        sock.close()


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))], 4)


def trial(version, args):
    stopped = threading.Event()
    threads = []
    captures = deque()
    input_delays = []
    errors = []
    capture_lock = threading.Lock()
    last_capture = [None]
    start = time.monotonic()
    stall_at = [None]
    wire_bytes = [0]

    class Desktop(RGBDesktop):
        def capture_image(self, max_width):
            now = time.monotonic()
            with self.condition:
                draw = ImageDraw.Draw(self.image)
                draw.rectangle((400, 180, 850, 270), fill='white')
                x = 420 + int((now - start) * 100) % 350
                draw.rectangle((x, 200, x + 14, 230), fill='#174f9b')
                draw.text((420, 245), f'Synthetic capture {now - start:.3f}', fill='black')
            image = super().capture_image(max_width)
            with capture_lock:
                last_capture[0] = now
            return image

        def apply(self, events):
            # Synthetic only: no OS keyboard/mouse APIs are called.
            super().apply(events)
            for event in events:
                if event.get('type') == 'key' and event.get('down'):
                    input_delays.append(time.monotonic() - float(event['key']))

    def spawn(fn, name, *params):
        thread = threading.Thread(target=fn, name=name, args=params, daemon=True)
        threads.append(thread)
        thread.start()

    client_sock, proxy_client = socket.socketpair()
    proxy_server, host_sock = socket.socketpair()
    sockets = (client_sock, proxy_client, proxy_server, host_sock)
    for sock in sockets:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    client_sock.settimeout(30)
    desktop = Desktop()
    original_write = stream.write_packet

    def observe_write(sock, payload, *positional, **keywords):
        body = payload[stream.FRAME_HEADER.size:] if version == 4 else payload
        if sock is host_sock and body[:1] in (b'J', b'T', b'U'):
            with capture_lock:
                captures.append(last_capture[0])
        return original_write(sock, payload, *positional, **keywords)

    observer = patch.object(stream, 'write_packet', side_effect=observe_write)
    observer.start()

    def relay(source, destination, downstream):
        packets = queue.Queue(maxsize=8)

        def read():
            try:
                while not stopped.is_set():
                    data = source.recv(4096)
                    if not data:
                        return
                    packet = (time.monotonic() + args.rtt_ms / 2000, data)
                    while not stopped.is_set():
                        try:
                            packets.put(packet, timeout=.1)
                            break
                        except queue.Full:
                            pass
            except OSError:
                pass

        def write():
            available = time.monotonic()
            try:
                while not stopped.is_set():
                    try:
                        ready, data = packets.get(timeout=.1)
                    except queue.Empty:
                        continue
                    available = max(available, ready, time.monotonic())
                    if downstream and args.kib_s:
                        available += len(data) / (args.kib_s * 1024)
                    if stopped.wait(max(0, available - time.monotonic())):
                        return
                    if downstream and stall_at[0] is not None:
                        now = time.monotonic()
                        if stall_at[0] <= now < stall_at[0] + args.stall_seconds:
                            if stopped.wait(stall_at[0] + args.stall_seconds - now):
                                return
                            available = time.monotonic()
                    destination.sendall(data)
                    if downstream:
                        wire_bytes[0] += len(data)
            except OSError:
                pass

        spawn(read, 'relay-read')
        spawn(write, 'relay-write')

    def host():
        try:
            stream.serve_stream(host_sock, desktop, version=version)
        except Exception as error:
            if not stopped.is_set():
                errors.append('host: ' + str(error))

    def input_loop(client):
        try:
            while not stopped.is_set():
                now = time.monotonic()
                client.send_input([{'type': 'key', 'key': str(now), 'down': True},
                                   {'type': 'release'}])
                stopped.wait(.2)
        except Exception as error:
            if not stopped.is_set():
                errors.append('input: ' + str(error))

    def abort():
        stopped.set()
        for sock in sockets:
            close(sock)

    relay(proxy_client, proxy_server, False)
    relay(proxy_server, proxy_client, True)
    spawn(host, 'synthetic-host')
    watchdog = threading.Timer(args.seconds + args.stall_seconds + 45, abort)
    watchdog.daemon = True
    watchdog.start()
    frame_times, ages = [], []
    first = None
    client = None
    try:
        client = Session(client_sock)
        spawn(input_loop, 'synthetic-input', client)
        while first is None or time.monotonic() - first < args.seconds:
            iteration = time.monotonic()
            with client.exchange_image([]) as image:
                if image.size != (1920, 1080):
                    raise AssertionError(f'Unexpected resolution: {image.size}')
            now = time.monotonic()
            with capture_lock:
                ages.append(now - captures.popleft())
            frame_times.append(now)
            if first is None:
                first = now
                if args.stall_seconds:
                    stall_at[0] = first + args.seconds / 3
            # Match the existing viewer's 10 Hz receive loop.
            stopped.wait(max(0, .1 - (time.monotonic() - iteration)))
    except Exception as error:
        errors.append('client: ' + str(error))
    finally:
        watchdog.cancel()
        abort()
        if client is not None:
            client.close()
        for thread in threads:
            thread.join(2)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if not alive:
            desktop.image.close()
        observer.stop()

    span = frame_times[-1] - first if len(frame_times) > 1 else 0
    result = {
        'protocol': version, 'rtt_ms': args.rtt_ms, 'downstream_kib_s': args.kib_s,
        'stall_seconds': args.stall_seconds, 'resolution': [1920, 1080],
        'decoded_frames': len(frame_times), 'first_frame_s': round(first - start, 4) if first else None,
        'measured_after_first_s': round(span, 4),
        'decoded_fps': round((len(frame_times) - 1) / span, 3) if span else 0,
        'frame_age_p95_s': percentile(ages, .95),
        'frame_age_last_s': round(ages[-1], 4) if ages else None,
        'max_frame_gap_s': round(max((b - a for a, b in zip(frame_times, frame_times[1:])), default=0), 4),
        'input_events_received': len(input_delays), 'input_delay_p95_s': percentile(input_delays, .95),
        'input_delay_max_s': round(max(input_delays), 4) if input_delays else None,
        'downstream_bytes': wire_bytes[0], 'errors': errors, 'leaked_threads': alive,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--versions', nargs='+', type=int, choices=(3, 4), default=[3, 4])
    parser.add_argument('--seconds', type=float, default=12, help='measurement time after first frame')
    parser.add_argument('--rtt-ms', type=float, default=300)
    parser.add_argument('--kib-s', type=float, default=256, help='downstream rate; 0 means unlimited')
    parser.add_argument('--stall-seconds', type=float, default=0, help='pause downstream once after first frame')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.seconds <= 0 or min(args.rtt_ms, args.kib_s, args.stall_seconds) < 0:
        parser.error('duration must be positive; latency, bandwidth and stall must be nonnegative')
    results = []
    for version in args.versions:
        result = trial(version, args)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    if any(row['errors'] or row['leaked_threads'] or row['decoded_frames'] < 2 for row in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
