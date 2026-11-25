"""Visible desktop capture and input for an explicitly started host.

Requires mss>=10.2,<11, Pillow and pynput. Linux hosts need a real X11
desktop session; Wayland and headless sessions are deliberately unsupported.
No input listeners, background service or permission prompts are installed.
"""

from __future__ import annotations

import io
import logging
import math
import os
import sys
import time


class Desktop:
    """Capture one display and apply normalized input while inside ``with``.

    The primary display is used, or the first when it cannot be identified.
    Calls are serialized by the host. JPEG resizing never changes the
    coordinate space used for input.
    """

    MAX_EVENTS = 128
    MAX_SCROLL = 100
    MAX_HELD_KEYS = 64
    JPEG_QUALITY = 70
    MAX_WIDTH = 1920
    MAX_CAPTURE_SIDE = 3840
    MAX_CAPTURE_PIXELS = 8_294_400
    # Start with a small first frame. The next request arrives only after the
    # viewer has received and decoded the previous one (including on v1 clients).
    _FRAME_PROFILES = ((800, 42), (1280, 55), (1920, 70))
    # These valid pynput names are absent from some platforms' Key enums.
    _OPTIONAL_KEYS = {"insert", "menu", "num_lock", "pause", "print_screen", "scroll_lock"}
    _FIELDS = {
        "move": {"type", "x", "y"},
        "button": {"type", "button", "down", "x", "y"},
        "scroll": {"type", "dx", "dy", "x", "y"},
        "key": {"type", "key", "down"},
        "release": {"type"},
    }

    def __init__(self):
        self._capture = None
        self._image = None
        self._monitor = None
        self._mouse = None
        self._keyboard = None
        self._button_names = {}
        self._key_names = {}
        self._held_buttons = {}
        self._held_keys = {}
        self.reset_stream()

    def reset_stream(self, *, minimum_width=800):
        self._minimum_level = next((i for i, (width, _) in enumerate(self._FRAME_PROFILES)
                                    if width >= minimum_width), len(self._FRAME_PROFILES) - 1)
        self._frame_level = self._minimum_level
        self._fast_frames = 0
        self._last_frame_start = None
        self._measure_previous_frame = True

    def frame_transmitted(self, changed):
        # A tiny unchanged reply measures RTT, not full-image bandwidth.
        self._measure_previous_frame = changed

    def _frame_profile(self):
        now = time.monotonic()
        if self._last_frame_start is not None and self._measure_previous_frame:
            interval = now - self._last_frame_start
            if interval >= 0.65:
                self._frame_level = max(self._minimum_level, self._frame_level - 1)
                self._fast_frames = 0
            elif interval <= 0.30:
                self._fast_frames += 1
                if self._fast_frames >= 8:
                    self._frame_level = min(len(self._FRAME_PROFILES) - 1, self._frame_level + 1)
                    self._fast_frames = 0
            else:
                self._fast_frames = 0
        self._last_frame_start = now
        self._measure_previous_frame = True
        width, quality = self._FRAME_PROFILES[self._frame_level]
        return min(self.MAX_WIDTH, width), min(self.JPEG_QUALITY, quality)

    @staticmethod
    def _platform_hint():
        if sys.platform == "darwin":
            application = "RemoteDesktop 应用" if getattr(sys, "frozen", False) else "运行此主机的终端或 Python"
            return (
                f"请在「系统设置 → 隐私与安全性」中，为{application}"
                "开启「屏幕录制」（部分版本为「屏幕与系统音频录制」）和「辅助功能」权限，"
                "然后退出并重新打开该应用。"
            )
        if sys.platform.startswith("linux"):
            return "请在已登录的 X11 桌面会话中启动主机，并确保 DISPLAY 环境变量正确。"
        return "请在已登录的 Windows 桌面会话中启动主机。"

    @classmethod
    def _preflight(cls):
        if sys.platform.startswith("linux"):
            if (
                os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
                or os.environ.get("WAYLAND_DISPLAY")
            ):
                raise RuntimeError("暂不支持 Wayland，请先登录 X11 桌面会话。")
            if not os.environ.get("DISPLAY"):
                raise RuntimeError(cls._platform_hint())
            for name in (
                "PYNPUT_BACKEND", "PYNPUT_BACKEND_MOUSE", "PYNPUT_BACKEND_KEYBOARD"
            ):
                if os.environ.get(name) not in (None, "", "xorg"):
                    raise RuntimeError("Linux 输入需要 pynput 的 xorg 后端，请移除其他后端设置。")
        elif sys.platform == "darwin":
            try:
                import HIServices
                import Quartz
            except ImportError as error:
                raise RuntimeError(
                    "请安装 pynput 及其 macOS PyObjC 依赖。"
                ) from error
            screen_check = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
            missing = []
            if screen_check is not None and not screen_check():
                missing.append("屏幕录制")
            if not HIServices.AXIsProcessTrusted():
                missing.append("辅助功能")
            if missing:
                raise RuntimeError(
                    "缺少 macOS 权限：" + "、".join(missing) + "。"
                    + cls._platform_hint()
                )
        elif sys.platform != "win32":
            raise RuntimeError("被控端支持 Windows、macOS 和 Linux X11。")

    def __enter__(self):
        if self._capture is not None:
            raise RuntimeError("Desktop is already open")
        self._preflight()
        try:
            from mss import MSS
            from PIL import Image
        except ImportError as error:
            raise RuntimeError("Install mss>=10.2,<11, Pillow and pynput.") from error
        capture = None
        try:
            # MSS establishes Windows DPI awareness before input controllers
            # are created, so capture and pointer coordinates agree there.
            capture = MSS()
            monitor = capture.primary_monitor
            geometry = {
                name: int(monitor[name])
                for name in ("left", "top", "width", "height")
            }
            if geometry["width"] < 1 or geometry["height"] < 1:
                raise RuntimeError("The selected display has no usable area")
            # Fail during startup if screen capture is unavailable. This
            # preflight never presses a key or moves the user's pointer.
            capture.grab(geometry)
            from pynput import keyboard, mouse

            self._mouse = mouse.Controller()
            self._keyboard = keyboard.Controller()
            self._button_names = {
                name: getattr(mouse.Button, name)
                for name in ("left", "right", "middle")
            }
            self._key_names = dict(keyboard.Key.__members__)
            self._image = Image
            self._monitor = geometry
            self._capture = capture
            return self
        except BaseException as error:
            if capture is not None:
                try:
                    capture.close()
                except Exception:
                    pass
            self._mouse = self._keyboard = None
            if not isinstance(error, Exception):
                raise
            raise RuntimeError(
                "无法打开桌面：" + str(error) + "。" + self._platform_hint()
            ) from error

    def __exit__(self, exc_type, exc, traceback):
        errors = self._release_all()
        try:
            if self._capture is not None:
                self._capture.close()
        except Exception as error:
            errors.append(error)
        finally:
            self._capture = self._monitor = self._image = None
            self._mouse = self._keyboard = None
            self._held_buttons.clear()
            self._held_keys.clear()
        if errors:
            message = "Desktop cleanup failed; some input releases may not have reached the OS"
            if exc_type is None:
                raise RuntimeError(message) from errors[0]
            logging.getLogger(__name__).warning("%s: %s", message, errors[0])

    def _require_open(self):
        if self._capture is None:
            raise RuntimeError("Use Desktop inside a with block")

    @property
    def has_held_input(self):
        return bool(self._held_keys or self._held_buttons)

    def _grab(self, max_width):
        self._require_open()
        if type(max_width) is not int or not 1 <= max_width <= self.MAX_CAPTURE_SIDE:
            raise ValueError("Capture width must be an integer from 1 to 3840")
        try:
            shot = self._capture.grab(self._monitor)
        except Exception as error:
            raise RuntimeError("无法截取桌面。" + self._platform_hint()) from error
        width, height = shot.size
        scale = min(1, max_width / width, self.MAX_CAPTURE_SIDE / height,
                    math.sqrt(self.MAX_CAPTURE_PIXELS / (width * height)))
        return shot, (max(1, int(width * scale)), max(1, int(height * scale)))

    def capture_video_frame(self, max_width):
        """Convert MSS pixels directly into the encoder's scaled 4:4:4 format."""
        import av

        shot, size = self._grab(max_width)
        width, height = shot.size
        frame = av.VideoFrame(width, height, "bgr0")
        plane = frame.planes[0]
        stride = width * 4
        if plane.line_size == stride:
            plane.update(shot.raw)
        else:
            # FFmpeg aligns rows, including on odd-width monitors.
            source, target = memoryview(shot.raw), memoryview(plane)
            for row in range(height):
                offset = row * plane.line_size
                target[offset:offset + stride] = source[row * stride:(row + 1) * stride]
        return frame.reformat(*size, format="yuv444p", interpolation="BILINEAR")

    def capture_image(self, max_width=1920):
        """Return an owned RGB image; explicit sizing never changes input coordinates."""
        shot, size = self._grab(max_width)
        image = None
        try:
            image = self._image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if image.size != size:
                image.thumbnail(size, self._image.Resampling.BILINEAR)
            return image
        except Exception as error:
            if image is not None:
                image.close()
            raise RuntimeError("无法截取桌面。" + self._platform_hint()) from error

    def frame(self) -> bytes:
        """Legacy JPEG with its existing adaptive width and quality profiles."""
        self._require_open()
        max_width, quality = self._frame_profile()
        try:
            with self.capture_image(max_width=max_width) as image:
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=quality)
                return output.getvalue()
        except Exception as error:
            raise RuntimeError(
                "无法截取桌面。" + self._platform_hint()
            ) from error

    def _point(self, event):
        values = []
        for name in ("x", "y"):
            value = event[name]
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise ValueError("x and y must be finite numbers from 0 to 1")
            values.append(value)
        return (
            self._monitor["left"] + round(values[0] * (self._monitor["width"] - 1)),
            self._monitor["top"] + round(values[1] * (self._monitor["height"] - 1)),
        )

    def _decode(self, events):
        if type(events) is not list or len(events) > self.MAX_EVENTS:
            raise ValueError("events must be a list of at most 128 events")
        decoded = []
        held_keys = set(self._held_keys)
        for event in events:
            if type(event) is not dict:
                raise ValueError("Each event must be an object")
            kind = event.get("type")
            if not isinstance(kind, str) or kind not in self._FIELDS:
                raise ValueError("Unsupported input event type")
            if set(event) != self._FIELDS[kind]:
                raise ValueError("Unexpected or missing fields in " + kind + " event")
            if kind == "release":
                decoded.append((kind,))
                held_keys.clear()
            elif kind == "move":
                decoded.append((kind, self._point(event)))
            elif kind == "scroll":
                for name in ("dx", "dy"):
                    if type(event[name]) is not int or abs(event[name]) > self.MAX_SCROLL:
                        raise ValueError("Scroll dx and dy must be integers from -100 to 100")
                decoded.append((kind, self._point(event), event["dx"], event["dy"]))
            else:
                if type(event["down"]) is not bool:
                    raise ValueError("down must be a boolean")
                if kind == "button":
                    name = event["button"]
                    if not isinstance(name, str) or name not in self._button_names:
                        raise ValueError("button must be left, right or middle")
                    decoded.append((kind, self._point(event), self._button_names[name], event["down"]))
                else:
                    name = event["key"]
                    if not isinstance(name, str) or not 1 <= len(name) <= 32:
                        raise ValueError("key must be a pynput key name or one printable character")
                    key = self._key_names.get(name)
                    if key is None:
                        if name in self._OPTIONAL_KEYS:
                            continue
                        if len(name) != 1 or not name.isprintable():
                            raise ValueError("Unknown or non-printable key")
                        key = name
                    if event["down"]:
                        held_keys.add(key)
                    else:
                        held_keys.discard(key)
                    if len(held_keys) > self.MAX_HELD_KEYS:
                        raise ValueError("At most 64 keys may be held simultaneously")
                    decoded.append((kind, key, event["down"]))
        return decoded

    def _release_all(self):
        errors = []
        for held, controller in (
            (self._held_buttons, self._mouse), (self._held_keys, self._keyboard)
        ):
            for value in reversed(tuple(held)):
                try:
                    controller.release(value)
                except Exception as error:
                    errors.append(error)
                else:
                    held.pop(value, None)
        return errors

    def apply(self, events: list[dict]) -> None:
        """Validate the entire batch before injecting it; release on OS errors.

        Positive scroll dy follows pynput's convention (up). Unmatched releases
        are ignored, so they cannot release a key held only by the local user.
        """
        self._require_open()
        decoded = self._decode(events)
        try:
            for event in decoded:
                kind = event[0]
                if kind == "release":
                    errors = self._release_all()
                    if errors:
                        raise RuntimeError("Could not release all held inputs") from errors[0]
                elif kind == "move":
                    self._mouse.position = event[1]
                elif kind == "scroll":
                    self._mouse.position = event[1]
                    self._mouse.scroll(event[2], event[3])
                else:
                    if kind == "button":
                        self._mouse.position = event[1]
                        value, down = event[2:]
                        controller, held = self._mouse, self._held_buttons
                    else:
                        value, down = event[1:]
                        controller, held = self._keyboard, self._held_keys
                    if down:
                        # Track before posting: an OS call could post input
                        # successfully and then fail while returning.
                        held[value] = None
                        controller.press(value)
                    elif value in held:
                        controller.release(value)
                        held.pop(value, None)
        except Exception as error:
            self._release_all()
            raise RuntimeError(
                "无法传递键鼠输入。" + self._platform_hint()
            ) from error
