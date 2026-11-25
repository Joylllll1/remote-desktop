"""Immediate, bounded H.264 desktop frames (8-bit 4:4:4, no frame queue).

Each ``H`` packet is one Annex B access unit.  The first frame, every periodic
IDR, and the first frame after a reset include SPS/PPS, so a new decoder can
start there.  Instances belong to one stream worker and are not thread-safe.
"""

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import struct

from PIL import Image

from remotedesk.network.tiles import MAX_FRAME, MAX_HEIGHT, MAX_PIXELS, MAX_WIDTH

FPS = 30
DEFAULT_MAX_BITRATE = 8_000_000
_VERSION = 1
_KEYFRAME = 1
_HEADER = struct.Struct('!BBHHI')
_MAX_NALS = 128
_MAX_PARAMETER_SET = 4096
# Cropping to an odd size still requires complete 16-pixel macroblocks.
_MAX_CODED_PIXELS = MAX_PIXELS + 15 * (MAX_WIDTH + MAX_HEIGHT) + 225


def _dimensions(size):
    width, height = size
    if not (1 <= width <= MAX_WIDTH and 1 <= height <= MAX_HEIGHT
            and width * height <= MAX_PIXELS):
        raise ValueError('桌面画面尺寸超出限制。')
    return width, height


def _bitrate(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 256_000 <= value <= 32_000_000:
        raise ValueError('桌面视频码率超出限制。')
    return value


def validate_frame_rate(value):
    if type(value) is not int or value not in (30, 60):
        raise ValueError('桌面视频帧率须为 30 或 60。')
    return value


def _av():
    try:
        import av
    except (ImportError, OSError) as error:
        raise RuntimeError('H.264 视频组件不可用。') from error
    return av


def _new_encoder(size, max_bitrate, frame_rate):
    context = _av().CodecContext.create('libx264', 'w')
    context.width, context.height = size
    context.pix_fmt = 'yuv444p'
    context.time_base = Fraction(1, frame_rate)
    context.framerate = Fraction(frame_rate, 1)
    context.thread_count = 2
    context.thread_type = 'SLICE'
    context.max_b_frames = 0
    context.gop_size = frame_rate * 2
    rate_kbps = max_bitrate // 1000
    context.options = {
        'preset': 'veryfast',
        'tune': 'zerolatency',
        'crf': '18',
        'x264-params': (
            'bframes=0:rc-lookahead=0:sync-lookahead=0:repeat-headers=1:'
            f'annexb=1:ref=1:keyint={frame_rate * 2}:min-keyint={frame_rate * 2}:scenecut=0:'
            f'vbv-maxrate={rate_kbps}:vbv-bufsize={max(1, rate_kbps // 10)}'
        ),
    }
    context.open()
    if context.options:
        raise RuntimeError('H.264 编码组件不支持所需的低延迟设置。')
    return context


def _new_decoder():
    context = _av().CodecContext.create('h264', 'r')
    context.thread_count = 2
    # FRAME/AUTO threading delays output until more packets arrive.
    context.thread_type = 'SLICE'
    context.options = {'max_pixels': str(_MAX_CODED_PIXELS), 'err_detect': 'explode'}
    context.open()
    if context.options:
        raise RuntimeError('H.264 解码组件不支持所需的安全限制。')
    return context


@lru_cache(maxsize=1)
def encoder_available():
    """Probe the actual bundled encoder, rather than only its codec name."""
    encoder = VideoEncoder()
    try:
        with Image.new('RGB', (16, 16)) as image:
            encoder.encode(image)
        return True
    except Exception:
        return False
    finally:
        encoder.close()


@lru_cache(maxsize=1)
def decoder_available():
    try:
        context = _new_decoder()
        del context
        return True
    except Exception:
        return False


class VideoEncoder:
    def __init__(self, *, max_bitrate=DEFAULT_MAX_BITRATE, frame_rate=FPS):
        self._max_bitrate = _bitrate(max_bitrate)
        self._frame_rate = validate_frame_rate(frame_rate)
        self._codec = None
        self._size = None
        self._frame_index = 0

    def close(self):
        # PyAV 16 CodecContext has no close(); dropping it frees native buffers.
        # Do not flush: there are no delayed frames, and old frames are unwanted.
        self._codec = None
        self._size = None
        self._frame_index = 0

    def set_bitrate(self, max_bitrate):
        """Apply a new VBV ceiling; the next frame starts a fresh IDR stream."""
        max_bitrate = _bitrate(max_bitrate)
        if max_bitrate != self._max_bitrate:
            self.close()
            self._max_bitrate = max_bitrate

    def set_frame_rate(self, frame_rate):
        """Reset timing and references; the next frame is a self-contained IDR."""
        frame_rate = validate_frame_rate(frame_rate)
        if frame_rate != self._frame_rate:
            self.close()
            self._frame_rate = frame_rate

    def encode(self, image):
        if not isinstance(image, Image.Image) or image.mode != 'RGB':
            raise ValueError('桌面编码需要 RGB 图像。')
        _dimensions(image.size)
        return self.encode_frame(_av().VideoFrame.from_image(image))

    def encode_frame(self, frame):
        size = _dimensions((frame.width, frame.height))
        if size != self._size:
            self.close()
        try:
            if self._codec is None:
                self._codec = _new_encoder(size, self._max_bitrate, self._frame_rate)
                self._size = size
            frame.pts = self._frame_index
            frame.time_base = Fraction(1, self._frame_rate)
            packets = self._codec.encode(frame)
            if len(packets) != 1:
                raise RuntimeError('H.264 编码组件未立即返回完整画面。')
            packet = packets[0]
            encoded = bytes(packet)
            if not encoded or len(encoded) + 1 + _HEADER.size > MAX_FRAME:
                raise ValueError('桌面画面数据超出限制。')
            self._frame_index += 1
            flags = _KEYFRAME if packet.is_keyframe else 0
            return b'H' + _HEADER.pack(_VERSION, flags, *size, len(encoded)) + encoded
        except Exception:
            # A discarded encoded frame invalidates the reference chain.
            self.close()
            raise


class _Bits:
    """Small bounded RBSP reader; never feed unvalidated SPS dimensions to C."""

    def __init__(self, data):
        self.data = data
        self.position = 0

    def read(self, count=1):
        if not 0 <= count <= 32 or self.position + count > len(self.data) * 8:
            raise ValueError('H.264 参数已截断。')
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.position // 8] >> (7 - self.position % 8)) & 1)
            self.position += 1
        return value

    def ue(self, maximum):
        zeros = 0
        while not self.read():
            zeros += 1
            if zeros > 31:
                raise ValueError('H.264 参数超出限制。')
        value = (1 << zeros) - 1 + self.read(zeros)
        if value > maximum:
            raise ValueError('H.264 参数超出限制。')
        return value

    def se(self, maximum):
        value = self.ue(maximum * 2)
        return (value + 1) // 2 if value & 1 else -(value // 2)

    def more_data(self):
        remaining = len(self.data) * 8 - self.position
        if remaining > 8:
            return True
        if remaining < 1:
            raise ValueError('H.264 参数缺少结束标记。')
        trailing = self.data[-1] & ((1 << remaining) - 1)
        return trailing != 1 << (remaining - 1)

    def finish(self):
        if not self.read():
            raise ValueError('H.264 参数缺少结束标记。')
        while self.position < len(self.data) * 8:
            if self.read():
                raise ValueError('H.264 参数包含多余数据。')


def _rbsp(data, *, partial=False):
    result = bytearray()
    zeros = 0
    for index, value in enumerate(data):
        if zeros == 2:
            if value < 3:
                raise ValueError('H.264 参数转义无效。')
            if value == 3:
                if index + 1 >= len(data) and partial:
                    break
                if index + 1 >= len(data) or data[index + 1] > 3:
                    raise ValueError('H.264 参数转义无效。')
                zeros = 0
                continue
        result.append(value)
        zeros = zeros + 1 if value == 0 else 0
    return bytes(result)


def _scaling_list(bits, count):
    last_scale = next_scale = 8
    for _ in range(count):
        if next_scale:
            next_scale = (last_scale + bits.se(255) + 256) % 256
        last_scale = next_scale or last_scale


def _hrd(bits):
    count = bits.ue(31) + 1
    bits.read(8)
    for _ in range(count):
        bits.ue(0x7fffffff)
        bits.ue(0x7fffffff)
        bits.read()
    bits.read(20)


def _vui(bits):
    if bits.read():
        if bits.read(8) == 255:
            bits.read(32)
    if bits.read():
        bits.read()
    if bits.read():
        bits.read(4)
        if bits.read():
            bits.read(24)
    if bits.read():
        bits.ue(5)
        bits.ue(5)
    if bits.read():
        bits.read(32)
        bits.read(32)
        bits.read()
    nal_hrd = bits.read()
    if nal_hrd:
        _hrd(bits)
    vcl_hrd = bits.read()
    if vcl_hrd:
        _hrd(bits)
    if nal_hrd or vcl_hrd:
        bits.read()
    bits.read()
    if bits.read():
        bits.read()
        for maximum in (16, 16, 16, 16, 0, 2):
            bits.ue(maximum)


@dataclass(frozen=True)
class _Sequence:
    size: tuple
    coded_size: tuple
    frame_num_bits: int
    poc_bits: int


def _sequence(nal, expected):
    if len(nal) > _MAX_PARAMETER_SET:
        raise ValueError('H.264 参数过长。')
    bits = _Bits(_rbsp(nal[1:]))
    # This transport deliberately accepts the format its encoder emits, rather
    # than arbitrary H.264 profiles, fields, bit depths, or reference queues.
    if bits.read(8) != 244:
        raise ValueError('H.264 桌面画面必须使用 4:4:4 格式。')
    bits.read(16)  # constraint flags and level_idc
    bits.ue(0)  # seq_parameter_set_id
    if bits.ue(3) != 3 or bits.read():
        raise ValueError('H.264 桌面色彩格式无效。')
    bits.ue(0)  # bit_depth_luma_minus8
    bits.ue(0)  # bit_depth_chroma_minus8
    if bits.read():
        raise ValueError('H.264 桌面像素格式无效。')
    if bits.read():
        for index in range(12):
            if bits.read():
                _scaling_list(bits, 16 if index < 6 else 64)
    frame_num_bits = bits.ue(12) + 4
    poc_type = bits.ue(2)
    if poc_type == 1:
        raise ValueError('H.264 桌面画面不支持重排。')
    poc_bits = bits.ue(12) + 4 if poc_type == 0 else 0
    bits.ue(2)  # max_num_ref_frames
    if bits.read():
        raise ValueError('H.264 桌面画面序号不连续。')
    coded_width = (bits.ue((MAX_WIDTH + 15) // 16 - 1) + 1) * 16
    coded_height = (bits.ue((MAX_HEIGHT + 15) // 16 - 1) + 1) * 16
    if not bits.read():
        raise ValueError('H.264 桌面画面必须逐帧编码。')
    bits.read()  # direct_8x8_inference_flag
    left = right = top = bottom = 0
    if bits.read():
        left, right = bits.ue(MAX_WIDTH), bits.ue(MAX_WIDTH)
        top, bottom = bits.ue(MAX_HEIGHT), bits.ue(MAX_HEIGHT)
    size = _dimensions((coded_width - left - right, coded_height - top - bottom))
    if (size != expected or (coded_width, coded_height) != (
            (size[0] + 15) // 16 * 16, (size[1] + 15) // 16 * 16)
            or coded_width * coded_height > _MAX_CODED_PIXELS):
        raise ValueError('H.264 参数与画面尺寸不一致。')
    if bits.read():
        _vui(bits)
    bits.finish()
    return _Sequence(size, (coded_width, coded_height), frame_num_bits, poc_bits)


def _picture(nal):
    if len(nal) > _MAX_PARAMETER_SET:
        raise ValueError('H.264 参数过长。')
    bits = _Bits(_rbsp(nal[1:]))
    bits.ue(0)  # pic_parameter_set_id
    bits.ue(0)  # seq_parameter_set_id
    bits.read()  # entropy_coding_mode_flag
    if bits.read():
        raise ValueError('H.264 桌面画面不支持场编码。')
    bits.ue(0)  # no slice groups / flexible macroblock ordering
    bits.ue(1)
    bits.ue(1)
    bits.read(3)  # weighted_pred_flag, weighted_bipred_idc
    bits.se(51)
    bits.se(51)
    bits.se(12)
    bits.read(2)
    if bits.read():
        raise ValueError('H.264 桌面画面不支持冗余图像。')
    if bits.more_data():
        bits.read()  # transform_8x8_mode_flag
        if bits.read():
            raise ValueError('H.264 桌面画面包含不支持的量化参数。')
        bits.se(12)
    bits.finish()


def _nals(data):
    position = data.find(b'\x00\x00\x01')
    if position not in (0, 1) or any(data[:position]):
        raise ValueError('H.264 画面缺少 Annex B 头部。')
    count = 0
    while position != -1:
        start = position + 3
        position = data.find(b'\x00\x00\x01', start)
        nal = data[start:position if position != -1 else len(data)].rstrip(b'\x00')
        count += 1
        if count > _MAX_NALS or not nal or nal[0] & 0x80 or nal[0] & 31 not in (1, 5, 6, 7, 8, 9):
            raise ValueError('H.264 画面数据单元无效。')
        yield nal


def _slice(nal, sequence):
    # The fields identifying an access unit fit in this bounded prefix.  The
    # compressed macroblocks need no Python scan or copy beyond NAL splitting.
    bits = _Bits(_rbsp(nal[1:65], partial=True))
    first_mb = bits.ue(sequence.coded_size[0] * sequence.coded_size[1] // 256 - 1)
    kind = bits.ue(9) % 5
    nal_type = nal[0] & 31
    if kind not in (0, 2) or (nal_type == 5 and kind != 2):
        raise ValueError('H.264 桌面画面不支持延迟重排。')
    bits.ue(0)  # pic_parameter_set_id
    frame_num = bits.read(sequence.frame_num_bits)
    idr = bits.ue(65535) if nal_type == 5 else None
    poc = bits.read(sequence.poc_bits) if sequence.poc_bits else None
    return first_mb, (nal_type, frame_num, idr, poc)


class VideoDecoder:
    def __init__(self):
        self._codec = None
        self._sequence = None

    def close(self):
        self._codec = None
        self._sequence = None

    def decode(self, payload):
        if (not isinstance(payload, bytes) or not 1 + _HEADER.size < len(payload) <= MAX_FRAME
                or payload[:1] != b'H'):
            raise ValueError('桌面视频数据长度或类型无效。')
        version, flags, width, height, length = _HEADER.unpack_from(payload, 1)
        size = _dimensions((width, height))
        if version != _VERSION or flags & ~_KEYFRAME or length != len(payload) - 1 - _HEADER.size:
            raise ValueError('桌面视频头部无效。')
        keyframe = bool(flags & _KEYFRAME)
        sequence = None if keyframe else self._sequence
        if not keyframe and (self._codec is None or sequence is None or sequence.size != size):
            raise ValueError('共享端尚未提供完整的视频关键帧。')
        encoded = payload[1 + _HEADER.size:]
        parts, slices = [], []
        seen_sps = seen_pps = False
        for nal in _nals(encoded):
            kind = nal[0] & 31
            if kind in (7, 8):
                if not keyframe or slices or not nal[0] & 0x60:
                    raise ValueError('H.264 参数出现位置无效。')
                if kind == 7:
                    if seen_sps or seen_pps:
                        raise ValueError('H.264 画面参数重复。')
                    sequence, seen_sps = _sequence(nal, size), True
                else:
                    if not seen_sps or seen_pps:
                        raise ValueError('H.264 图像参数无效。')
                    _picture(nal)
                    seen_pps = True
                parts.append(nal)
            elif kind in (1, 5):
                if (sequence is None or (keyframe and not seen_pps)
                        or (kind == 5) != keyframe or not nal[0] & 0x60):
                    raise ValueError('H.264 画面缺少完整的参数。')
                first_mb, identity = _slice(nal, sequence)
                if ((not slices and first_mb != 0) or
                        (slices and (first_mb <= slices[-1][0] or identity != slices[0][1]))):
                    raise ValueError('H.264 数据必须包含单个完整画面。')
                slices.append((first_mb, identity))
                parts.append(nal)
            # SEI and AUD carry no pixels.  Do not pass unneeded metadata from
            # the network into the native decoder.
        if not slices or (keyframe and not (seen_sps and seen_pps)):
            raise ValueError('H.264 画面缺少像素数据。')
        try:
            av = _av()
            context = _new_decoder() if keyframe else self._codec
            packet = av.Packet(b''.join(b'\x00\x00\x00\x01' + nal for nal in parts))
            frames = context.decode(packet)
            if len(frames) != 1:
                raise ValueError('H.264 解码组件未立即返回单个画面。')
            frame = frames[0]
            if frame.is_corrupt or (frame.width, frame.height) != size or frame.format.name != 'yuv444p':
                raise ValueError('H.264 解码后的尺寸或像素格式无效。')
            result = frame.to_image()
            if result.mode != 'RGB' or result.size != size:
                result.close()
                raise ValueError('H.264 解码后的画面格式无效。')
        except Exception as error:
            self.close()
            if isinstance(error, RuntimeError):
                raise
            raise ValueError('桌面视频编码无效。') from error
        self._codec, self._sequence = context, sequence
        return result
