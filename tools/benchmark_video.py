"""Reproducible synthetic 1080p desktop codec and optional loopback benchmark.

    .venv/bin/python tools/benchmark_video.py --seconds 10 --output /tmp/video.json
    .venv/bin/python tools/benchmark_video.py --loopback --seconds 10

No screen capture, input injection, external host, or user data is used. Both
codecs see the same elapsed-time motion: tiles sample it at 10 Hz and H.264 at
30 or 60 Hz. Codec trials run without pacing; their FPS measures CPU capacity, not
delivered/display FPS. Payload bitrate includes initial frames and every update
over the same synthetic duration. Fidelity compares each decoded image with its
own exact source. Codec trials pool every frame over the same elapsed duration,
with each codec's uniform 10/30/60 Hz sampling; no IDR phase is overrepresented.
RGB error/PSNR is not an OCR or text-legibility measurement; a foreground-only
colored-text/one-pixel-line metric avoids dilution by the white background.

Optional loopback uses the actual v4/v5/v6 stream and Session through socketpairs,
with independent byte-chunk propagation, downstream shaping and bounded queues.
It is a controlled local simulation, not a real-network result. Its latency is
synthetic capture start to decoded-image return, not physical input-to-photon.
All image/payload storage is bounded; no full frame sequence is retained.
"""

import argparse
from collections import Counter, deque
from contextlib import redirect_stdout, suppress
from datetime import datetime, timezone
import hashlib
from functools import partial
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import queue
import socket
import sys
import threading
import time
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from remotedesk.network.tiles import TileDecoder, TileEncoder

SIZE = (1920, 1080)
TEXT_ROI = (1250, 166, 1860, 416)
SCENES = ('static_text', 'scrolling', 'window_drag')
FPS = {'tiles': 10, 'video': 30, 'video60': 60}
PROTOCOLS = {'tiles': 4, 'video': 5, 'video60': 6}


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def timing(values, scale=1000):
    return {
        'mean_ms': round(sum(values) / len(values) * scale, 3) if values else None,
        'p50_ms': round(percentile(values, .5) * scale, 3) if values else None,
        'p95_ms': round(percentile(values, .95) * scale, 3) if values else None,
        'max_ms': round(max(values) * scale, 3) if values else None,
    }


class SyntheticDesktop:
    """Pillow-only application UI with fixed text, scrolling and window motion."""

    def __init__(self, scene):
        self.scene = scene
        self.font = ImageFont.load_default(size=17)
        self.small = ImageFont.load_default(size=14)
        self.title = ImageFont.load_default(size=22)
        self.base = Image.new('RGB', SIZE, '#e7ecf2')
        draw = ImageDraw.Draw(self.base)
        draw.rectangle((0, 0, 1919, 53), fill='#15384e')
        draw.text((24, 15), 'DESKTOP WORKSPACE    Files    Edit    View    Tools', font=self.title, fill='white')
        draw.text((1580, 20), 'Synthetic benchmark', font=self.font, fill='#9bc7d7')
        draw.rectangle((20, 75, 218, 1038), fill='#d1dce5')
        for row, label in enumerate(('Workspace', 'Documents', 'Source code', 'Downloads', 'Reports', 'Settings')):
            draw.text((35, 106 + row * 52), label, font=self.font, fill='#294757')
        draw.rectangle((240, 75, 1198, 1038), fill='white', outline='#c0cbd5')
        draw.text((262, 96), 'Application log / code editor', font=self.title, fill='#24475e')
        draw.rectangle((1220, 75, 1898, 1038), fill='white', outline='#c0cbd5')
        draw.text((1244, 96), 'Text and color fidelity reference', font=self.title, fill='#24475e')
        draw.text((1248, 440), 'Foreground-only RGB error is reported separately.', font=self.small, fill='#4b6070')
        self.mask = Image.new('L', SIZE)
        mask_draw = ImageDraw.Draw(self.mask)
        colors = ('#d52532', '#147d39', '#205add', '#903fc4', '#d95716')
        for row, color in enumerate(colors):
            xy = (1262, 178 + row * 38)
            text = f'RGB text {row + 1}: AaBb 0123456789 <>[] _ /'
            draw.text(xy, text, font=self.font, fill=color)
            mask_draw.text(xy, text, font=self.font, fill=255)
            box = (1262, 201 + row * 38, 1830, 201 + row * 38)
            draw.line(box, fill=color, width=1)
            mask_draw.line(box, fill=255, width=1)
        self.text_mask = self.mask.crop(TEXT_ROI)
        self.mask.close()
        self.document = Image.new('RGB', (936, 2048), 'white')
        doc_draw = ImageDraw.Draw(self.document)
        lines = (
            ('def render_frame(elapsed_seconds):', '#8d3797'),
            ('    # Synthetic desktop: no personal information', '#367847'),
            ('    position = elapsed_seconds * 120', '#1955aa'),
            ('    return compose(document, window, cursor)', '#293d4c'),
            ('INFO  worker ready; request=0173  status=200', '#267347'),
            ('WARN  queue depth=003  retry_delay=0.020s', '#b64e16'),
            ('const text = "colored edges and fine lines";', '#bb2c42'),
            ('Table row    00128    00752    35.281 ms', '#294b64'),
        )
        for row, y in enumerate(range(10, 2020, 29)):
            text, color = lines[row % len(lines)]
            doc_draw.text((12, y), f'{row:03d}  {text}', font=self.font, fill=color)
            doc_draw.line((0, y + 25, 935, y + 25), fill='#edf0f4', width=1)
        self.window = Image.new('RGB', (680, 420), '#f8fafc')
        win_draw = ImageDraw.Draw(self.window)
        win_draw.rectangle((0, 0, 679, 419), outline='#678395', width=2)
        win_draw.rectangle((2, 2, 677, 43), fill='#335e79')
        win_draw.text((17, 13), 'Moving application window', font=self.font, fill='white')
        for row in range(11):
            win_draw.text((20, 65 + row * 29), f'Project item {row:02d}   ready   123.456 KB',
                          font=self.font, fill=colors[row % len(colors)])

    def render(self, elapsed):
        image = self.base.copy()
        # Motion is a function of elapsed seconds, never codec/frame index.
        offset = int(elapsed * 145) % 1100 if self.scene == 'scrolling' else 0
        with self.document.crop((0, offset, 936, offset + 880)) as content:
            image.paste(content, (251, 145))
        if self.scene == 'window_drag':
            x = 260 + round(430 * (1 - math.cos(elapsed * .9)))
            y = 528 + round(35 * math.sin(elapsed * 1.3))
            image.paste(self.window, (x, y))
            draw = ImageDraw.Draw(image)
            draw.polygon(((x + 300, y + 23), (x + 300, y + 42), (x + 305, y + 37),
                          (x + 310, y + 47), (x + 315, y + 44), (x + 309, y + 35),
                          (x + 318, y + 35)), fill='white', outline='#17384e')
        return image

    def close(self):
        for image in (self.base, self.document, self.window, self.text_mask):
            image.close()


class Fidelity:
    def __init__(self, text_mask):
        self.text_mask = text_mask
        self.totals = {'full_rgb': [0., 0., 0], 'colored_text_and_lines_foreground_rgb': [0., 0., 0]}
        self.sample_times = []

    def add(self, source, decoded, source_seconds):
        if decoded.size != source.size or decoded.mode != 'RGB':
            raise AssertionError(f'Unexpected decoded image: {decoded.mode} {decoded.size}')
        with ImageChops.difference(source, decoded) as difference:
            self._add('full_rgb', ImageStat.Stat(difference))
            with difference.crop(TEXT_ROI) as text_difference:
                self._add('colored_text_and_lines_foreground_rgb', ImageStat.Stat(text_difference, self.text_mask))
        self.sample_times.append(round(source_seconds, 6))

    def _add(self, name, stat):
        totals = self.totals[name]
        totals[0] += sum(stat.sum)
        totals[1] += sum(stat.sum2)
        totals[2] += sum(stat.count)

    def report(self):
        preview = self.sample_times if len(self.sample_times) <= 10 else self.sample_times[:5] + self.sample_times[-5:]
        result = {
            'sample_count': len(self.sample_times),
            'first_sample_source_s': self.sample_times[0] if self.sample_times else None,
            'last_sample_source_s': self.sample_times[-1] if self.sample_times else None,
            'sample_source_seconds_preview': preview,
        }
        for name, (absolute, squared, count) in self.totals.items():
            mse = squared / count if count else None
            result[name] = {
                'mae_0_255': round(absolute / count, 5) if count else None,
                'psnr_db': round(10 * math.log10(255 ** 2 / mse), 4) if mse else None,
                'exact': mse == 0 if count else None,
                'channel_samples': count,
            }
        result['note'] = 'PSNR null with exact=true means zero error; these are RGB fidelity metrics, not OCR.'
        return result


def codec_types(condition):
    if condition == 'tiles':
        return TileEncoder, TileDecoder
    from remotedesk.network.video import VideoDecoder, VideoEncoder
    return (partial(VideoEncoder, frame_rate=60) if condition == 'video60' else VideoEncoder), VideoDecoder


def codec_trial(condition, scene_name, seconds):
    scene = SyntheticDesktop(scene_name)
    encoder_type, decoder_type = codec_types(condition)
    encoder, decoder = encoder_type(), decoder_type()
    fidelity = Fidelity(scene.text_mask)
    render_times, encode_times, decode_times = [], [], []
    payload_types, payload_bytes = Counter(), 0
    fps = FPS[condition]
    frames = round(seconds * fps)
    started = time.perf_counter()
    try:
        for index in range(frames):
            source_seconds = index / fps
            before = time.perf_counter()
            with scene.render(source_seconds) as source:
                rendered = time.perf_counter()
                payload = encoder.encode(source)
                encoded = time.perf_counter()
                with decoder.decode(payload) as decoded:
                    finished = time.perf_counter()
                    fidelity.add(source, decoded, source_seconds)
                render_times.append(rendered - before)
                encode_times.append(encoded - rendered)
                decode_times.append(finished - encoded)
                payload_bytes += len(payload)
                payload_types[payload[:1].decode('ascii')] += 1
        wall = time.perf_counter() - started
    finally:
        encoder.close()
        decoder.close()
        scene.close()
    codec_seconds = sum(encode_times) + sum(decode_times)
    return {
        'kind': 'codec_capacity', 'condition': condition, 'scene': scene_name,
        'resolution': list(SIZE), 'synthetic_duration_s': seconds, 'target_fps': fps,
        'frames': frames, 'wall_s': round(wall, 4),
        'render': timing(render_times), 'encode': timing(encode_times), 'decode': timing(decode_times),
        'codec_pair': timing([a + b for a, b in zip(encode_times, decode_times)]),
        'encode_capacity_fps': round(frames / sum(encode_times), 3),
        'decode_capacity_fps': round(frames / sum(decode_times), 3),
        'serial_codec_capacity_fps': round(frames / codec_seconds, 3),
        'serial_codec_time_fraction_at_target_fps': round(codec_seconds / seconds, 4),
        'payload_bytes': payload_bytes, 'payload_mbps': round(payload_bytes * 8 / seconds / 1e6, 5),
        'mean_payload_bytes': round(payload_bytes / frames, 2), 'payload_types': dict(payload_types),
        'fidelity_sampling': 'Every frame, uniformly spaced over the same elapsed-time duration; error is normalized per channel sample.',
        'fidelity': fidelity.report(),
    }


def close_socket(sock):
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with suppress(OSError):
        sock.close()


def loopback_trial(condition, scene_name, args):
    from remotedesk.network import stream
    from remotedesk.network.session import Session

    scene = SyntheticDesktop(scene_name)
    version = PROTOCOLS[condition]
    stopped = threading.Event()
    lock = threading.Lock()
    threads, errors = [], []
    captures = deque(maxlen=16)
    input_delays, capture_ages, frame_times, progress_times = [], [], [], []
    payload_sizes = []
    wire_bytes = {'downstream': 0, 'upstream': 0}
    last_capture = [None]
    epoch = time.monotonic()
    fidelity = Fidelity(scene.text_mask)
    client_socket, proxy_client = socket.socketpair()
    proxy_host, host_socket = socket.socketpair()
    sockets = (client_socket, proxy_client, proxy_host, host_socket)
    for sock in sockets:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    client_socket.settimeout(10)

    class Desktop:
        has_held_input = False

        def capture_image(self, max_width):
            captured = time.monotonic()
            elapsed = captured - epoch
            image = scene.render(elapsed)
            if max_width != SIZE[0]:
                image.close()
                raise AssertionError(f'Unexpected resolution request: {max_width}')
            with lock:
                last_capture[0] = (captured, elapsed)
            return image

        def apply(self, events):
            # Synthetic events are observed only; no OS APIs are imported.
            for event in events:
                if event.get('type') == 'key' and event.get('down'):
                    with lock:
                        input_delays.append(time.monotonic() - float(event['key']))

    def spawn(function, name, *params):
        thread = threading.Thread(target=function, name=name, args=params, daemon=True)
        threads.append(thread)
        thread.start()

    def abort():
        stopped.set()
        for sock in sockets:
            close_socket(sock)

    original_write = stream.write_packet

    def observe_write(sock, payload, *positional, **keywords):
        body = payload[stream.FRAME_HEADER.size:]
        if sock is host_socket and body[:1] in (b'J', b'T', b'U', b'H'):
            with lock:
                if len(captures) == captures.maxlen:
                    raise AssertionError('Capture observation queue overflow')
                captures.append(last_capture[0])
                payload_sizes.append(len(body))
        return original_write(sock, payload, *positional, **keywords)

    def relay(source, destination, downstream):
        chunks = queue.Queue(maxsize=8)

        def read():
            try:
                while not stopped.is_set():
                    data = source.recv(4096)
                    if not data:
                        return
                    ready = time.monotonic() + args.rtt_ms / 2000
                    while not stopped.is_set():
                        try:
                            chunks.put((ready, data), timeout=.1)
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
                        ready, data = chunks.get(timeout=.1)
                    except queue.Empty:
                        continue
                    available = max(available, ready, time.monotonic())
                    if downstream and args.mbps:
                        available += len(data) * 8 / (args.mbps * 1e6)
                    if stopped.wait(max(0, available - time.monotonic())):
                        return
                    destination.sendall(data)
                    with lock:
                        wire_bytes['downstream' if downstream else 'upstream'] += len(data)
                        if downstream:
                            progress_times.append(time.monotonic())
            except OSError:
                pass

        spawn(read, 'benchmark-relay-read')
        spawn(write, 'benchmark-relay-write')

    def host():
        try:
            stream.serve_stream(host_socket, Desktop(), version=version)
        except Exception as error:
            if not stopped.is_set():
                errors.append('host: ' + repr(error))

    def input_loop(client):
        try:
            while not stopped.is_set():
                client.send_input([{'type': 'key', 'key': str(time.monotonic()), 'down': True},
                                   {'type': 'release'}])
                stopped.wait(.2)
        except Exception as error:
            if not stopped.is_set():
                errors.append('input: ' + repr(error))

    first = finished = None
    client = None
    next_sample, next_progress = 0., 0.
    observer = patch.object(stream, 'write_packet', side_effect=observe_write)
    observer.start()
    watchdog = threading.Timer(args.seconds + 10, abort)
    watchdog.daemon = True
    try:
        relay(proxy_client, proxy_host, False)
        relay(proxy_host, proxy_client, True)
        spawn(host, 'benchmark-synthetic-host')
        watchdog.start()
        client = Session(client_socket)
        if client._stream.version != version:
            raise AssertionError(f'Protocol negotiation returned {client._stream.version}, expected {version}')
        spawn(input_loop, 'benchmark-synthetic-input', client)
        while first is None or time.monotonic() - first < args.seconds:
            with client.exchange_image([]) as image:
                now = time.monotonic()
                with lock:
                    if not captures:
                        raise AssertionError('No matching source capture for decoded frame')
                    captured, source_seconds = captures.popleft()
                frame_times.append(now)
                capture_ages.append(now - captured)
                if first is None:
                    first = now
                    next_sample = first
                    next_progress = first + 2
                if now >= next_sample:
                    with scene.render(source_seconds) as source:
                        fidelity.add(source, image, source_seconds)
                    next_sample += 1
                if now >= next_progress:
                    print(f'loopback {condition}/{scene_name}: {len(frame_times)} decoded, '
                          f'{wire_bytes["downstream"]} downstream bytes', file=sys.stderr, flush=True)
                    next_progress += 2
        finished = time.monotonic()
        # Snapshot before shutdown; late in-flight bytes are excluded consistently.
        with lock:
            bytes_at_finish = wire_bytes.copy()
            progress_at_finish = list(progress_times)
    except Exception as error:
        errors.append('client: ' + repr(error))
        finished = time.monotonic()
        with lock:
            bytes_at_finish = wire_bytes.copy()
            progress_at_finish = list(progress_times)
    finally:
        watchdog.cancel()
        abort()
        if client is not None:
            client.close()
        for thread in threads:
            thread.join(2)
        observer.stop()
        leaked = [thread.name for thread in threads if thread.is_alive()]
        if not leaked:
            scene.close()
    span = frame_times[-1] - first if len(frame_times) > 1 else 0
    transport_span = finished - epoch
    progress_boundaries = [epoch] + progress_at_finish + [finished]
    progress_gaps = [b - a for a, b in zip(progress_boundaries, progress_boundaries[1:])]
    return {
        'kind': 'shaped_loopback', 'condition': condition, 'scene': scene_name, 'protocol': version,
        'resolution': list(SIZE), 'target_fps': FPS[condition], 'rtt_ms': args.rtt_ms,
        'downstream_limit_mbps': args.mbps, 'upstream_bandwidth': 'unlimited',
        'requested_after_first_frame_s': args.seconds, 'measured_after_first_frame_s': round(span, 4),
        'transport_elapsed_s': round(transport_span, 4), 'decoded_frames': len(frame_times),
        'decoded_fps': round((len(frame_times) - 1) / span, 3) if span else 0,
        'first_frame_ms': round((first - epoch) * 1000, 3) if first else None,
        'capture_to_decoded': timing(capture_ages),
        'last_capture_age_ms': round(capture_ages[-1] * 1000, 3) if capture_ages else None,
        'max_frame_gap_ms': round(max((b - a for a, b in zip(frame_times, frame_times[1:])), default=0) * 1000, 3),
        'input_events_received': len(input_delays), 'synthetic_input_to_host': timing(input_delays),
        'downstream_bytes': bytes_at_finish['downstream'], 'upstream_bytes': bytes_at_finish['upstream'],
        'downstream_mbps': round(bytes_at_finish['downstream'] * 8 / transport_span / 1e6, 5),
        'downstream_progress_events': len(progress_at_finish),
        'max_downstream_progress_gap_ms': round(max(progress_gaps, default=0) * 1000, 3),
        'first_downstream_byte_ms': round((progress_at_finish[0] - epoch) * 1000, 3) if progress_at_finish else None,
        'host_frame_payload_bytes_started': sum(payload_sizes),
        'fidelity_sampling': 'Approximately 1Hz source-matched samples; QA work is included in loopback wall times.',
        'fidelity': fidelity.report(), 'errors': errors, 'leaked_threads': leaked,
    }


def environment():
    versions = {}
    for name in ('Pillow', 'av'):
        with suppress(metadata.PackageNotFoundError):
            versions[name] = metadata.version(name)
    hashes = {}
    for relative in ('tools/benchmark_video.py', 'remotedesk/network/tiles.py',
                     'remotedesk/network/video.py', 'remotedesk/network/stream.py',
                     'remotedesk/network/session.py'):
        path = ROOT / relative
        if path.exists():
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'python': sys.version, 'platform': platform.platform(), 'cpu_count': os.cpu_count(),
        'packages': versions, 'source_sha256': hashes, 'font': 'Pillow bundled default at 14/17/22 px',
        'method': 'Equal elapsed-time motion; 10fps tiles vs 30/60fps video; no real desktop or network.',
        'memory_bound': 'One source/decoded frame at a time; 8 x 4096-byte proxy chunks per direction; '
                        '16 source timestamps; at most 60 synthetic seconds per trial.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--seconds', type=int, default=10, help='synthetic/loopback seconds per scene and codec (1..60)')
    parser.add_argument('--conditions', nargs='+', choices=tuple(FPS), default=['tiles', 'video'])
    parser.add_argument('--scenes', nargs='+', choices=SCENES, default=list(SCENES))
    parser.add_argument('--loopback', action='store_true', help='also run each condition through real v4/v5 stream code')
    parser.add_argument('--loopback-only', action='store_true', help='omit unpaced codec trials')
    parser.add_argument('--rtt-ms', type=float, default=40, help='simulated RTT; half applied independently per direction')
    parser.add_argument('--mbps', type=float, default=8, help='simulated downstream Mbps; 0 means unlimited')
    parser.add_argument('--output', type=Path, help='write full reproducible JSON report; stdout remains JSON lines')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or not math.isfinite(args.rtt_ms) or not math.isfinite(args.mbps) or min(args.rtt_ms, args.mbps) < 0:
        parser.error('seconds must be 1..60; RTT and bandwidth must be finite and nonnegative')
    for condition in args.conditions:
        try:
            codec_types(condition)
        except ImportError as error:
            parser.error(f'{condition} codec unavailable: {error}; install requirements in the active Python environment')
    report = {'environment': environment(), 'settings': vars(args).copy(), 'results': []}
    report['settings']['output'] = str(args.output) if args.output else None
    for scene in args.scenes:
        for condition in args.conditions:
            if not args.loopback_only:
                row = codec_trial(condition, scene, args.seconds)
                report['results'].append(row)
                print(json.dumps(row), flush=True)
            if args.loopback or args.loopback_only:
                # The production server prints diagnostics; preserve JSONL on
                # stdout while its host thread is active in this local trial.
                with redirect_stdout(sys.stderr):
                    row = loopback_trial(condition, scene, args)
                report['results'].append(row)
                print(json.dumps(row), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    if any(row.get('errors') or row.get('leaked_threads') or row.get('decoded_frames', 2) < 2 for row in report['results']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
