"""Bounded RGB tile deltas; JPEG keyframes and lossless PNG dirty rectangles."""

import io
import struct
import zlib

from PIL import Image, ImageChops

MAX_WIDTH = MAX_HEIGHT = 3840
MAX_PIXELS = 8_294_400
MAX_PATCHES = 512
MAX_FRAME = 12 * 1024 * 1024
TILE_SIZE = 128
_HEADER = struct.Struct('!HHH')
_PATCH = struct.Struct('!HHHHI')
_PNG = b'\x89PNG\r\n\x1a\n'


def _dimensions(size):
    width, height = size
    if not (1 <= width <= MAX_WIDTH and 1 <= height <= MAX_HEIGHT and width * height <= MAX_PIXELS):
        raise ValueError('桌面画面尺寸超出限制。')
    return width, height


def _open_rgb(data, format_name, expected=None):
    try:
        with Image.open(io.BytesIO(data), formats=(format_name,)) as source:
            if source.format != format_name:
                raise ValueError('画面编码格式无效。')
            _dimensions(source.size)
            if expected is not None and source.size != expected:
                raise ValueError('补丁尺寸与数据不一致。')
            source.load()
            return source.convert('RGB')
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('桌面画面编码无效。') from error


def _validate_png(data, expected):
    """Reject metadata/extra data before Pillow can inflate any PNG content."""
    if data[:8] != _PNG:
        raise ValueError('补丁必须为 PNG。')
    position, seen_header, seen_data = 8, False, False
    while position < len(data):
        if len(data) - position < 12:
            raise ValueError('PNG 补丁已截断。')
        length, = struct.unpack_from('!I', data, position)
        kind = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data) or kind not in (b'IHDR', b'IDAT', b'IEND'):
            raise ValueError('PNG 补丁包含无效数据块。')
        body = data[position + 8:position + 8 + length]
        checksum, = struct.unpack_from('!I', data, position + 8 + length)
        if zlib.crc32(kind + body) & 0xffffffff != checksum:
            raise ValueError('PNG 补丁校验失败。')
        if not seen_header:
            if kind != b'IHDR' or length != 13:
                raise ValueError('PNG 补丁缺少有效头部。')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('!IIBBBBB', body)
            if (width, height) != expected or (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ValueError('PNG 补丁尺寸或像素格式无效。')
            seen_header = True
        elif kind == b'IDAT':
            seen_data = True
        elif kind == b'IEND':
            if length or not seen_data or end != len(data):
                raise ValueError('PNG 补丁结束标记无效。')
            return
        else:
            raise ValueError('PNG 补丁头部重复。')
        position = end
    raise ValueError('PNG 补丁未完整结束。')


class TileEncoder:
    def __init__(self):
        self._previous = None
        self._jpeg_size = 0

    def close(self):
        if self._previous is not None:
            self._previous.close()
            self._previous = None
        self._jpeg_size = 0

    @staticmethod
    def _jpeg(image):
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=75, subsampling=0)
        payload = b'J' + output.getvalue()
        if len(payload) > MAX_FRAME:
            raise ValueError('桌面画面数据超出限制。')
        return payload

    def _remember(self, image):
        previous = self._previous
        self._previous = image.copy()
        if previous is not None:
            previous.close()

    def encode(self, image):
        if not isinstance(image, Image.Image) or image.mode != 'RGB':
            raise ValueError('桌面编码需要 RGB 图像。')
        width, height = _dimensions(image.size)
        rectangles = []
        dense = self._previous is None or self._previous.size != image.size
        if not dense:
            with ImageChops.difference(image, self._previous) as difference:
                changed = difference.getbbox()
                if changed is None:
                    return b'U'
                area = 0
                for top in range(changed[1] // TILE_SIZE * TILE_SIZE, changed[3], TILE_SIZE):
                    for left in range(changed[0] // TILE_SIZE * TILE_SIZE, changed[2], TILE_SIZE):
                        box = (left, top, min(left + TILE_SIZE, width), min(top + TILE_SIZE, height))
                        with difference.crop(box) as tile:
                            dirty = tile.getbbox()
                        if dirty is None:
                            continue
                        x, y = left + dirty[0], top + dirty[1]
                        w, h = dirty[2] - dirty[0], dirty[3] - dirty[1]
                        rectangles.append((x, y, w, h))
                        area += w * h
                        if len(rectangles) > MAX_PATCHES or area >= width * height * 0.4:
                            dense = True
                            break
                    if dense:
                        break
        if dense:
            payload = self._jpeg(image)
            jpeg_size = len(payload)
        else:
            parts = [b'T' + _HEADER.pack(width, height, len(rectangles))]
            total = len(parts[0])
            for x, y, w, h in rectangles:
                with image.crop((x, y, x + w, y + h)) as patch:
                    output = io.BytesIO()
                    patch.save(output, format='PNG', compress_level=3)
                data = output.getvalue()
                parts.extend((_PATCH.pack(x, y, w, h, len(data)), data))
                total += _PATCH.size + len(data)
                if total > MAX_FRAME:
                    dense = True
                    break
            jpeg_size = self._jpeg_size
            if dense:
                payload = self._jpeg(image)
                jpeg_size = len(payload)
            else:
                payload = b''.join(parts)
                # Tiny cursor/text updates do not pay for a full JPEG encode.
                # Compare larger/high-entropy patches against an actual JPEG.
                if total >= min(16 * 1024, max(1, self._jpeg_size // 4)):
                    keyframe = self._jpeg(image)
                    jpeg_size = len(keyframe)
                    if len(keyframe) <= total:
                        payload = keyframe
        self._remember(image)
        self._jpeg_size = jpeg_size
        return payload


class TileDecoder:
    def __init__(self):
        self._base = None

    def close(self):
        if self._base is not None:
            self._base.close()
            self._base = None

    def _commit(self, image):
        try:
            result = image.copy()
        except BaseException:
            image.close()
            raise
        previous = self._base
        self._base = image
        if previous is not None:
            previous.close()
        return result

    def decode(self, payload):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_FRAME:
            raise ValueError('桌面画面数据长度无效。')
        if payload == b'U':
            if self._base is None:
                raise ValueError('共享端尚未提供完整画面。')
            return self._base.copy()
        if payload[:1] == b'J':
            return self._commit(_open_rgb(payload[1:], 'JPEG'))
        if payload[:1] != b'T' or len(payload) < 1 + _HEADER.size or self._base is None:
            raise ValueError('桌面补丁缺少完整的基础画面。')
        width, height, count = _HEADER.unpack_from(payload, 1)
        _dimensions((width, height))
        if self._base.size != (width, height) or not 1 <= count <= MAX_PATCHES:
            raise ValueError('桌面补丁尺寸或数量无效。')
        position, area = 1 + _HEADER.size, 0
        patches, rectangles = [], []
        try:
            for _ in range(count):
                if len(payload) - position < _PATCH.size:
                    raise ValueError('桌面补丁头部已截断。')
                x, y, w, h, length = _PATCH.unpack_from(payload, position)
                position += _PATCH.size
                if not (1 <= w <= TILE_SIZE and 1 <= h <= TILE_SIZE and x + w <= width and y + h <= height):
                    raise ValueError('桌面补丁超出画面范围。')
                area += w * h
                if area > width * height or not 0 < length <= len(payload) - position:
                    raise ValueError('桌面补丁长度或总面积无效。')
                if any(x < rx + rw and rx < x + w and y < ry + rh and ry < y + h for rx, ry, rw, rh in rectangles):
                    raise ValueError('桌面补丁区域重叠。')
                data = payload[position:position + length]
                position += length
                _validate_png(data, (w, h))
                patches.append((x, y, _open_rgb(data, 'PNG', (w, h))))
                rectangles.append((x, y, w, h))
            if position != len(payload):
                raise ValueError('桌面补丁包含多余数据。')
            # All packet structure, PNG headers, checksums and pixels are valid
            # before touching even this private candidate copy.
            candidate = self._base.copy()
            try:
                for x, y, patch in patches:
                    candidate.paste(patch, (x, y))
            except BaseException:
                candidate.close()
                raise
            return self._commit(candidate)
        finally:
            for _, _, patch in patches:
                patch.close()
