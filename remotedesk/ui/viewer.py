"""Tkinter 远程桌面窗口；网络收发在后台串行执行。"""

from collections import deque
import io
import queue
import sys
import threading
import time
from remotedesk.network.timing import FrameTimer


_SPECIAL_KEYS = {
    "Alt_L": "alt_l", "Alt_R": "alt_r", "ISO_Level3_Shift": "alt_gr",
    "Control_L": "ctrl_l", "Control_R": "ctrl_r",
    "Shift_L": "shift_l", "Shift_R": "shift_r",
    "Meta_L": "cmd_l", "Meta_R": "cmd_r",
    "Super_L": "cmd_l", "Super_R": "cmd_r", "Command": "cmd",
    "BackSpace": "backspace", "Tab": "tab", "ISO_Left_Tab": "tab",
    "Return": "enter", "KP_Enter": "enter", "Escape": "esc",
    "Delete": "delete", "KP_Delete": "delete", "Insert": "insert",
    "KP_Insert": "insert", "Home": "home", "KP_Home": "home",
    "End": "end", "KP_End": "end", "Prior": "page_up",
    "KP_Prior": "page_up", "Next": "page_down", "KP_Next": "page_down",
    "Left": "left", "KP_Left": "left", "Right": "right", "KP_Right": "right",
    "Up": "up", "KP_Up": "up", "Down": "down", "KP_Down": "down",
    "Caps_Lock": "caps_lock", "Num_Lock": "num_lock",
    "Scroll_Lock": "scroll_lock", "Pause": "pause", "Print": "print_screen",
    "Menu": "menu", "space": "space",
}
_SYMBOL_KEYS = dict(zip(
    "exclam quotedbl numbersign dollar percent ampersand apostrophe parenleft "
    "parenright asterisk plus comma minus period slash colon semicolon less "
    "equal greater question at bracketleft backslash bracketright asciicircum "
    "underscore grave braceleft bar braceright asciitilde".split(),
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",
))
_SYMBOL_KEYS.update({
    "quoteleft": "`", "quoteright": "'", "KP_Decimal": ".",
    "KP_Add": "+", "KP_Subtract": "-", "KP_Multiply": "*",
    "KP_Divide": "/", "KP_Equal": "=",
})
_SHORTCUT_MODIFIERS = {"ctrl_l", "ctrl_r", "cmd", "cmd_l", "cmd_r", "alt_l", "alt_r"}
_MODIFIER_KEYS = _SHORTCUT_MODIFIERS | {"shift_l", "shift_r", "alt_gr"}
_RETRY_DELAYS = (1, 2, 5, 10, 20)
_RESOLUTIONS = {"清晰 · 1920": 1920, "省流量 · 1280": 1280}
_FRAME_RATES = {"流畅 · 60 帧": 60, "标准 · 30 帧": 30}
_DISPLAY_POLL_MS = 8
# Tk 9 touchpad deltas are pixel-like movement, whereas the wire protocol and
# pynput use wheel steps. Accumulate small movements rather than dropping them
# or sending one whole wheel step per pixel.
_TOUCHPAD_PIXELS_PER_STEP = 40


def _lifecycle(message):
    try:
        if sys.stdout is not None:
            print("[查看器] " + message, flush=True)
    except (OSError, ValueError):
        # An independent viewer may outlive the panel reading its output pipe.
        pass


def _key_name(event):
    """Use keysyms for Ctrl+C (whose Tk character is a control code)."""
    keysym = event.keysym
    if keysym in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[keysym]
    if keysym.startswith("F") and keysym[1:].isdigit():
        if 1 <= int(keysym[1:]) <= 20:
            return keysym.lower()
    if keysym.startswith("KP_") and len(keysym) == 4 and keysym[-1].isdigit():
        return keysym[-1]
    character = event.char
    if len(character) == 1 and character.isprintable():
        return character
    if len(keysym) == 1 and keysym.isprintable():
        return keysym
    return _SYMBOL_KEYS.get(keysym)


def _key_identity(event):
    """Match key-up to key-down even when modifiers change on macOS.

    Aqua Tk 8.6/9 packs (virtual << 24) | (option_shift << 22) |
    character into %k. In particular, Shift's own release has a different
    %k from its press. Only the high byte identifies the physical key.
    """
    if sys.platform == "darwin":
        virtual = (event.keycode >> 24) & 0xff
        if virtual < 0x80:  # Higher values represent synthetic/IME text.
            return ("mac", virtual)
        return ("text", event.keysym)
    return event.keycode


class _InputBuffer:
    """Coalesce motion; an explicit release cancels stale pending input."""

    def __init__(self, enabled=True):
        self._events = deque()
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._enabled = enabled
        self._epoch = None

    @property
    def ready(self):
        with self._lock:
            return self._enabled

    def begin(self, epoch):
        with self._lock:
            self._epoch, self._enabled = epoch, False
            self._events.clear()
            self._changed.notify_all()

    def disable(self):
        self.begin(None)

    def pause(self, epoch=None):
        """Drop pending controls but keep this session eligible for a new frame."""
        with self._lock:
            if not self._enabled or (epoch is not None and self._epoch != epoch):
                return False
            self._enabled = False
            self._events.clear()
            if self._epoch is not None:
                self._events.append({"type": "release"})
            self._changed.notify_all()
            return True

    def matches(self, epoch):
        with self._lock:
            return epoch is not None and self._epoch == epoch

    def enable(self, epoch):
        with self._lock:
            if epoch is None or self._epoch != epoch:
                return False
            self._enabled = True
            return True

    def append(self, event):
        with self._lock:
            if not self._enabled:
                return
            if event["type"] == "release":
                # Do not replay buffered presses after the user leaves the window.
                self._events.clear()
                self._events.append(event)
            elif event["type"] == "move" and self._events and self._events[-1]["type"] == "move":
                self._events[-1] = event
            else:
                self._events.append(event)
            self._changed.notify_all()

    def take(self, epoch=None):
        with self._lock:
            if epoch is not None and self._epoch != epoch:
                return []
            return [self._events.popleft() for _ in range(min(128, len(self._events)))]

    def wait(self, epoch, timeout):
        """Wake for pending input or a new session, including before we wait."""
        with self._changed:
            self._changed.wait_for(
                lambda: bool(self._events) or self._epoch != epoch, timeout)


class _RecoveryWorker(threading.Thread):
    """Own blocking sessions and recovery; never access a Tk object."""

    def __init__(self, open_session, title, inputs, Image):
        super().__init__(name="desktop-viewer", daemon=True)
        self._factory = open_session if callable(open_session) else lambda: open_session
        self._can_reconnect = callable(open_session)
        self.title, self.inputs, self.Image = title, inputs, Image
        self.states, self.frames = queue.Queue(), queue.Queue(maxsize=1)
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._session = None
        self._sender = self._sender_stop = None
        self._terminal_error = None
        self._shown_epoch = None
        self._input_shown_epoch = self._input_shown_at = None
        self._failures = 0
        self._created_at = time.monotonic()
        self._displayed_frames = self._total_displayed = 0
        self._shown_started = self._last_checkpoint = self._created_at
        self._resolution = 1920
        self._frame_rate = 60
        self._display_times = deque(maxlen=120)
        self._media_format = "图片"
        self.inputs.disable()

    @property
    def display_stats(self):
        with self._lock:
            times = self._display_times
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0
            return self._media_format, fps

    @property
    def resolution(self):
        with self._lock:
            return self._resolution

    def set_resolution(self, width):
        if type(width) is not int or width not in (1280, 1920):
            raise ValueError("画质宽度只能选择 1280 或 1920。")
        with self._lock:
            self._resolution = width

    def _apply_resolution(self, session):
        session.set_resolution(self.resolution)
        if getattr(session, "supports_frame_rate_control", False):
            session.set_frame_rate(self.frame_rate)

    @property
    def frame_rate(self):
        with self._lock:
            return self._frame_rate

    def set_frame_rate(self, fps):
        if type(fps) is not int or fps not in (30, 60):
            raise ValueError("帧率只能选择 30 或 60。")
        with self._lock:
            self._frame_rate = fps

    @property
    def terminal_error(self):
        with self._lock:
            return self._terminal_error

    def cancel(self):
        self._cancelled.set()
        self.inputs.disable()
        self._discard_frames()
        with self._lock:
            session = self._session
            sender_stop = self._sender_stop
        if sender_stop is not None:
            sender_stop.set()
        if session is not None:
            self._close_session(session)

    @staticmethod
    def _close_session(session):
        try:
            session.close()
        except Exception:
            pass

    def fail(self, error):
        with self._lock:
            first = self._terminal_error is None
            if first:
                self._terminal_error = error
        self.cancel()
        if first:
            _lifecycle("连接已终止：" + (str(error) or error.__class__.__name__))
            self.states.put(("terminal", error))

    def frame_shown(self, epoch):
        with self._lock:
            if self._terminal_error is not None or self._cancelled.is_set():
                self.inputs.disable()
                return False
            now = time.monotonic()
            control_ready = self.inputs.enable(epoch)
            first = self._shown_epoch != epoch
            if first:
                self._displayed_frames = 0
                self._shown_started = self._last_checkpoint = now
                self._display_times.clear()
            self._display_times.append(now)
            while len(self._display_times) > 1 and self._display_times[0] < now - 2:
                self._display_times.popleft()
            self._displayed_frames += 1
            self._total_displayed += 1
            self._shown_epoch = epoch
            if control_ready:
                self._failures = 0
                self._input_shown_epoch, self._input_shown_at = epoch, now
            checkpoint = now - self._last_checkpoint >= 30
            count, elapsed = self._displayed_frames, int(now - self._shown_started)
            if checkpoint:
                self._last_checkpoint = now
        if first:
            _lifecycle("已显示 %s 的桌面画面。" % self.title)
        elif checkpoint:
            _lifecycle("已持续显示 %d 帧（本次连接 %d 秒）。" % (count, elapsed))
        return control_ready

    def close_summary(self):
        with self._lock:
            return "已关闭 %s 的查看器（共显示 %d 帧，用时 %d 秒）。" % (
                self.title, self._total_displayed, max(0, int(time.monotonic() - self._created_at)))

    def _discard_frames(self):
        while True:
            try:
                _, frame = self.frames.get_nowait()
            except queue.Empty:
                return
            frame.close()

    def _send_inputs(self, session, epoch, stopped, errors):
        heartbeat_at = time.monotonic()
        try:
            while not stopped.is_set() and not self._cancelled.is_set():
                if not self.inputs.matches(epoch):
                    return
                self._watch_input_freshness(epoch)
                events = self.inputs.take(epoch)
                if stopped.is_set() or self._cancelled.is_set():
                    return
                if events or time.monotonic() >= heartbeat_at:
                    session.send_input(events)
                    heartbeat_at = time.monotonic() + 0.05
                self.inputs.wait(epoch, max(0, heartbeat_at - time.monotonic()))
        except Exception as error:
            if not stopped.is_set() and not self._cancelled.is_set():
                errors.put_nowait(error)
                stopped.set()
                self.inputs.disable()
                self._close_session(session)  # Interrupt a blocked frame read.

    def _watch_input_freshness(self, epoch):
        # The sender must release held input even if Tk stops polling entirely.
        with self._lock:
            if (self._input_shown_epoch == epoch and self._input_shown_at is not None
                    and time.monotonic() - self._input_shown_at >= 2 and self.inputs.ready):
                return self.inputs.pause(epoch)
        return False

    @staticmethod
    def _raise_sender_error(errors):
        try:
            error = errors.get_nowait()
        except queue.Empty:
            return
        raise error

    def _receive_frame(self, session, events):
        exchange_image = getattr(session, "exchange_image", None)
        if callable(exchange_image):
            frame = exchange_image(events)
            if (not isinstance(frame, self.Image.Image) or frame.mode != "RGB"
                    or frame.width < 1 or frame.height < 1):
                if hasattr(frame, "close"):
                    frame.close()
                raise ValueError("远程桌面返回的图像无效。")
            return frame
        jpeg = session.exchange(events)
        try:
            with self.Image.open(io.BytesIO(jpeg), formats=("JPEG",)) as source:
                source.load()
                return source.convert("RGB")
        except Exception as error:
            raise ValueError("远程桌面画面无效：" + str(error)) from error

    def run(self):
        epoch = 0
        while not self._cancelled.is_set():
            epoch += 1
            self.inputs.begin(epoch)
            self.states.put(("connecting", epoch))
            _lifecycle(("正在连接 %s…" if epoch == 1 else "正在重新连接 %s…") % self.title)
            session = None
            failure = None
            caught = None
            sender = None
            sender_stop = threading.Event()
            sender_errors = queue.Queue(maxsize=1)
            try:
                session = self._factory()
                with self._lock:
                    if not self._cancelled.is_set():
                        self._session = session
                if self._cancelled.is_set():
                    break
                resolution_control = bool(getattr(session, "supports_resolution_control", False))
                if resolution_control:
                    self._apply_resolution(session)
                self.states.put(("capabilities", resolution_control))
                self.states.put(("frame_rate_capability", bool(getattr(session, "supports_frame_rate_control", False))))
                continuous_frames = bool(getattr(session, "supports_continuous_frames", False))
                if getattr(session, "supports_frame_rate_control", False):
                    _lifecycle("已连接视频协议 v6，可选择 30/60 帧。")
                elif getattr(session, "supports_video_frames", False):
                    _lifecycle("已连接视频协议 v5；正在协商清晰视频传输。")
                elif continuous_frames:
                    _lifecycle("已启用连续画面传输（协议 v4），限制在途画面以避免积压。")
                else:
                    _lifecycle("共享端使用逐帧请求传输；高延迟下刷新率受网络往返限制。")
                asynchronous = bool(getattr(session, "supports_async_input", False))
                if asynchronous:
                    sender = threading.Thread(target=self._send_inputs,
                        args=(session, epoch, sender_stop, sender_errors),
                        name="desktop-input-%d" % epoch, daemon=True)
                    with self._lock:
                        self._sender, self._sender_stop = sender, sender_stop
                    sender.start()
                    _lifecycle("已启用独立键鼠输入通道。")
                else:
                    _lifecycle("对方共享端使用旧版传输，键鼠输入需等待画面传输。")
                self.states.put(("receiving", epoch))
                _lifecycle("已连接 %s，正在接收画面。" % self.title)
                previous_format = None
                while not self._cancelled.is_set():
                    started = time.monotonic()
                    self._raise_sender_error(sender_errors)
                    if resolution_control:
                        self._apply_resolution(session)
                    frame = None
                    try:
                        frame = self._receive_frame(session, [] if asynchronous else self.inputs.take(epoch))
                        self._raise_sender_error(sender_errors)
                        if self._cancelled.is_set():
                            break
                        media_format = getattr(session, "media_format", "图片")
                        with self._lock:
                            self._media_format = media_format
                        if media_format != previous_format:
                            _lifecycle("当前画面格式：%s。" % media_format)
                            previous_format = media_format
                        self._discard_frames()
                        self.frames.put_nowait((epoch, frame))
                        frame = None  # The queue/UI now owns the independent image.
                    finally:
                        if frame is not None:
                            frame.close()
                    if not continuous_frames:
                        self._cancelled.wait(max(0, 0.1 - (time.monotonic() - started)))
            except Exception as error:
                caught = error
            finally:
                sender_stop.set()
                self.inputs.disable()
                if self._cancelled.is_set():
                    self._discard_frames()
                if session is not None:
                    self._close_session(session)
                sender_stuck = False
                if sender is not None:
                    sender.join(timeout=1)
                    if sender.is_alive():
                        sender_stuck = True
                        caught = RuntimeError("键鼠发送未能停止，请关闭窗口后重新连接。")
                try:
                    sender_error = sender_errors.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if not sender_stuck:
                        caught = sender_error
                with self._lock:
                    self._session = self._sender = self._sender_stop = None
            if isinstance(caught, ValueError):
                self.fail(caught)
            elif caught is not None and not self._cancelled.is_set():
                if isinstance(caught, (OSError, EOFError)) and self._can_reconnect:
                    failure = caught
                else:
                    self.fail(caught if isinstance(caught, (OSError, EOFError, RuntimeError)) else RuntimeError(str(caught)))
            if failure is not None and not self._cancelled.is_set():
                with self._lock:
                    delay = _RETRY_DELAYS[min(self._failures, len(_RETRY_DELAYS) - 1)]
                    self._failures += 1
                    shown = self._shown_epoch is not None
                reason = str(failure) or failure.__class__.__name__
                message = ("连接中断" if shown else "暂时无法连接") + "，将在 %s 秒后重试：%s" % (delay, reason)
                _lifecycle(message)
                self.states.put(("retry", (delay, reason)))
                self._cancelled.wait(delay)


class _Viewer:
    def __init__(self, open_session, title, tk, ttk, Image, ImageTk):
        self.title = title
        self.Image = Image
        self.ImageTk = ImageTk
        self.root = tk.Tk()
        self._frame_timer = FrameTimer()
        self.root.title(title)
        width = min(1100, max(420, self.root.winfo_screenwidth() - 80))
        height = min(740, max(280, self.root.winfo_screenheight() - 110))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(420, 280)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        bar = tk.Frame(self.root, bg="#f7faf9", padx=14, pady=7)
        bar.pack(side="bottom", fill="x")
        quality_bar = tk.Frame(bar, bg="#f7faf9")
        quality_bar.pack(fill="x", pady=(0, 5))
        tk.Label(quality_bar, text="画质", bg="#f7faf9", fg="#253f39").pack(side="left")
        self._resolution_choice = tk.StringVar(value="清晰 · 1920")
        self._resolution_combo = ttk.Combobox(quality_bar, textvariable=self._resolution_choice,
            values=tuple(_RESOLUTIONS), state="disabled", width=13)
        self._resolution_combo.pack(side="left", padx=(6, 10))
        self._resolution_combo.bind("<<ComboboxSelected>>", self._resolution_changed)
        self._frame_rate_choice = tk.StringVar(value="流畅 · 60 帧")
        self._frame_rate_combo = ttk.Combobox(quality_bar, textvariable=self._frame_rate_choice,
            values=tuple(_FRAME_RATES), state="disabled", width=14)
        self._frame_rate_combo.pack(side="left", padx=(0, 10))
        self._frame_rate_combo.bind("<<ComboboxSelected>>", self._frame_rate_changed)
        self._resolution_hint = tk.StringVar(value="连接后可选择画质")
        hint = tk.Label(quality_bar, textvariable=self._resolution_hint, anchor="w", justify="left",
                        bg="#f7faf9", fg="#536d65")
        hint.pack(side="left", fill="x", expand=True)
        hint.bind("<Configure>", lambda event: hint.configure(wraplength=max(1, event.width - 4)))
        status_bar = tk.Frame(bar, bg="#f7faf9")
        status_bar.pack(fill="x")
        self.status = tk.StringVar(value="正在连接…")
        self._disconnect = ttk.Button(status_bar, text="断开连接", command=self.close)
        self._disconnect.pack(side="right", padx=(12, 0))
        self._indicator = tk.Canvas(status_bar, width=10, height=10, bg="#f7faf9", highlightthickness=0)
        self._indicator.pack(side="left", padx=(0, 8))
        self._dot = self._indicator.create_oval(1, 1, 9, 9, fill="#84918d", outline="")
        status_label = tk.Label(status_bar, textvariable=self.status, anchor="w", justify="left",
                                bg="#f7faf9", fg="#253f39")
        status_label.pack(side="left", fill="x", expand=True)
        status_label.bind("<Configure>", lambda event: status_label.configure(wraplength=max(1, event.width - 4)))
        self.canvas = tk.Canvas(self.root, background="#182321", highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._message_background = self.canvas.create_rectangle(0, 0, 0, 0, fill="#182321", outline="")
        self._message_item = self.canvas.create_text(
            0, 0, text="正在连接远程桌面…", fill="#e7ebee", justify="center",
        )
        self._input = _InputBuffer(enabled=False)
        self._closed = False
        self._ended = False
        self._frame = None
        self._photo = None
        self._image_rect = None
        self._rendered_frame = None
        self._rendered_size = None
        self._last_rendered_at = None
        self._stale = False
        self._keys_down = {}
        self._buttons_down = set()
        self._key_release_jobs = {}
        self._wheel_remainders = {"x": 0, "y": 0}
        self._touchpad_remainders = {"x": 0, "y": 0}
        self._poll_job = None
        self._resize_job = None
        self._focus_job = None
        self._worker = _RecoveryWorker(open_session, title, self._input, Image)
        self._frames = self._worker.frames

        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<Motion>", self._mouse_move)
        buttons = {1: "left", 2: "middle", 3: "right"}
        if sys.platform == "darwin":
            buttons = {1: "left", 2: "right", 3: "middle"}
        for number, button in buttons.items():
            self.canvas.bind("<ButtonPress-%d>" % number,
                             lambda event, name=button: self._mouse_button(event, name, True))
            self.canvas.bind("<ButtonRelease-%d>" % number,
                             lambda event, name=button: self._mouse_button(event, name, False))
        self.canvas.bind("<MouseWheel>", self._mouse_wheel)
        try:
            # Tk 9 sends this instead of MouseWheel for precise trackpads.
            self.canvas.bind("<TouchpadScroll>", self._touchpad_scroll)
        except tk.TclError:
            # Tk 8.6 continues to deliver gestures through MouseWheel.
            pass
        for number, dx, dy in ((4, 0, 1), (5, 0, -1), (6, -1, 0), (7, 1, 0)):
            try:
                self.canvas.bind("<Button-%d>" % number,
                                 lambda event, x=dx, y=dy: self._scroll(event, x, y))
            except tk.TclError:
                if number <= 5:
                    raise
                # Tk 8.6 builds may only support five mouse buttons.
        self.canvas.bind("<KeyPress>", self._key_press)
        self.canvas.bind("<KeyRelease>", self._key_release)
        self.canvas.bind("<FocusOut>", self._release_all)
        self.canvas.bind("<FocusIn>", lambda event: self._safe_status())
        self.root.bind("<Unmap>", self._unmapped)

    def run(self):
        self._worker.start()
        self._poll_job = self.root.after(_DISPLAY_POLL_MS, self._poll)
        self._focus_job = self.root.after_idle(self.canvas.focus_set)
        try:
            self.root.mainloop()
        finally:
            self.close()
            self._worker.join(timeout=1)
        if self._worker.terminal_error is not None:
            raise self._worker.terminal_error

    def _resolution_changed(self, event=None):
        label = self._resolution_choice.get()
        self._worker.set_resolution(_RESOLUTIONS[label])
        _lifecycle("画质选择：%s，下一帧请求生效。" % label)

    def _frame_rate_changed(self, event=None):
        label = self._frame_rate_choice.get()
        self._worker.set_frame_rate(_FRAME_RATES[label])
        self._frame_timer.set_rate(_FRAME_RATES[label])
        _lifecycle("帧率选择：%s，下一帧请求生效。" % label)

    def _set_frame_rate_capability(self, supported):
        self._frame_rate_combo.configure(state="readonly" if supported else "disabled")
        self._frame_timer.set_rate(self._worker.frame_rate if supported else 30)
        if supported:
            self._frame_rate_choice.set(next(label for label, fps in _FRAME_RATES.items()
                                             if fps == self._worker.frame_rate))
        elif supported is False:
            self._frame_rate_choice.set("由共享端决定")

    def _set_resolution_capability(self, supported):
        self._resolution_combo.configure(state="readonly" if supported else "disabled")
        self._resolution_hint.set("" if supported else (
            "需升级共享端才能选择画质" if supported is False else "连接后可选择画质"))

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        started = time.monotonic()
        try:
            self._poll_updates()
            self._check_stale()
        except Exception as cause:
            self._fail_display(cause)
        if not self._closed and not self._ended:
            # Include rendering time in the period. Sleeping 20 ms after each
            # render would cap display below 50 fps even on a fast connection.
            delay = max(1, _DISPLAY_POLL_MS - int((time.monotonic() - started) * 1000))
            self._poll_job = self.root.after(delay, self._poll)

    def _fail_display(self, cause):
        error = RuntimeError("无法显示远程桌面：%s。请关闭窗口后重新连接。" % (str(cause) or cause.__class__.__name__))
        self._worker.fail(error)
        try:
            self._show_terminal(error)
        except Exception:
            self.close()

    def _pause_input(self, message):
        self._clear_local_input()
        self._image_rect = None
        if self._rendered_frame is None:
            self.canvas.itemconfigure(self._image_item, state="hidden")
            self._show_overlay(message)
        else:
            self._show_overlay("正在显示旧画面\n" + message)

    def _show_overlay(self, message):
        self.canvas.itemconfigure(self._message_item, state="normal", text=message)
        self.canvas.itemconfigure(self._message_background, state="normal")
        self._overlay_bounds()

    def _overlay_bounds(self):
        box = self.canvas.bbox(self._message_item)
        if box:
            left, top, right, bottom = box
            self.canvas.coords(self._message_background, left - 14, top - 10, right + 14, bottom + 10)

    def _check_stale(self):
        if self._closed or self._ended or self._last_rendered_at is None:
            return False
        age = max(0, int(time.monotonic() - self._last_rendered_at))
        if age < 2:
            return False
        if not self._stale:
            self._stale = True
            self._input.pause()
            self._pause_input("等待新画面，控制已暂停。")
            self._indicator.itemconfigure(self._dot, fill="#a16d28")
            _lifecycle("画面延迟，已暂停控制，正在等待新画面。")
        text = "画面延迟 · 已 %d 秒未更新" % age
        if self.status.get() != text:
            self.status.set(text)
        return True

    def _show_terminal(self, error):
        self._ended = True
        self._set_resolution_capability(None)
        self._set_frame_rate_capability(None)
        self._pause_input("连接已终止\n请查看下方提示。")
        self._indicator.itemconfigure(self._dot, fill="#a54a32")
        self._disconnect.configure(text="关闭窗口")
        self.status.set("连接已终止：" + (str(error) or error.__class__.__name__))

    def _poll_updates(self):
        while True:
            try:
                state, detail = self._worker.states.get_nowait()
            except queue.Empty:
                break
            if state == "terminal":
                self._show_terminal(detail)
                return
            if state == "capabilities":
                self._set_resolution_capability(detail)
                continue
            if state == "frame_rate_capability":
                self._set_frame_rate_capability(detail)
                continue
            if state in ("connecting", "retry"):
                self._set_resolution_capability(None)
                self._set_frame_rate_capability(None)
            self._indicator.itemconfigure(self._dot, fill="#84918d")
            if state == "connecting":
                text = "正在连接远程桌面…" if detail == 1 else "正在重新连接…"
            elif state == "receiving":
                text = "已连接，正在接收画面…"
            else:
                delay, reason = detail
                text = "连接中断，%s 秒后重试：%s" % (delay, reason)
                self._indicator.itemconfigure(self._dot, fill="#a16d28")
            self._pause_input(text if state != "retry" else "连接暂时不可用\n正在自动重连…")
            self.status.set(text)
        try:
            epoch, frame = self._frames.get_nowait()
        except queue.Empty:
            return
        previous = self._frame
        self._frame = frame
        try:
            self._render()
        finally:
            if previous is not None and previous is not frame:
                previous.close()
        was_stale = self._stale
        self._last_rendered_at = time.monotonic()
        self._stale = False
        if self._worker.frame_shown(epoch):
            self._indicator.itemconfigure(self._dot, fill="#11645a")
            self._update_status()
            if was_stale:
                _lifecycle("画面已更新，已恢复控制。")
        else:
            self._show_overlay("正在显示旧画面\n等待连接恢复，控制已暂停。")

    def _resized(self, event):
        if self._closed:
            return
        self.canvas.coords(self._message_item, event.width / 2, event.height / 2)
        self._overlay_bounds()
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(35, self._safe_render)

    def _safe_render(self):
        try:
            self._render()
            if self._rendered_frame is not None and not self._input.ready:
                self._show_overlay("正在显示旧画面\n等待新画面，控制已暂停。")
        except Exception as cause:
            self._fail_display(cause)

    def _render(self):
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = None
        if self._closed or self._ended or self._frame is None:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        scale = min(width / self._frame.width, height / self._frame.height)
        size = (max(1, int(self._frame.width * scale)), max(1, int(self._frame.height * scale)))
        if self._rendered_frame is not self._frame or self._rendered_size != size:
            resampling = getattr(self.Image, "Resampling", self.Image).BILINEAR
            with self._frame.resize(size, resampling) as image:
                self._photo = self.ImageTk.PhotoImage(image, master=self.root)
            self.canvas.itemconfigure(self._image_item, image=self._photo, state="normal")
            self._rendered_frame, self._rendered_size = self._frame, size
        left, top = (width - size[0]) // 2, (height - size[1]) // 2
        self.canvas.coords(self._image_item, left, top)
        self.canvas.itemconfigure(self._message_item, state="hidden")
        self.canvas.itemconfigure(self._message_background, state="hidden")
        self._image_rect = (left, top, *size)

    def _update_status(self):
        if self._closed or self._ended or self._check_stale() or self._frame is None or not self._input.ready:
            return
        hint = "键鼠控制中" if self.canvas.focus_get() == self.canvas else "点击画面控制"
        text = "已连接   ·   %s   ·   %d × %d" % (hint, self._frame.width, self._frame.height)
        media_format, fps = self._worker.display_stats
        text += "   ·   %s   ·   %.0f 帧/秒" % (media_format, fps)
        if self.status.get() != text:
            self.status.set(text)

    def _safe_status(self):
        try:
            self._update_status()
        except Exception as cause:
            self._fail_display(cause)

    def _position(self, event, clamp=False):
        if self._closed or self._ended or self._image_rect is None or not self._input.ready:
            return None
        left, top, width, height = self._image_rect
        if not clamp and not (left <= event.x < left + width and top <= event.y < top + height):
            return None
        return {
            "x": max(0.0, min(1.0, (event.x - left) / max(1, width - 1))),
            "y": max(0.0, min(1.0, (event.y - top) / max(1, height - 1))),
        }

    def _mouse_move(self, event):
        if self.canvas.focus_get() != self.canvas and not self._buttons_down:
            return "break"
        position = self._position(event, clamp=bool(self._buttons_down))
        if position is not None:
            self._flush_key_releases()
            self._input.append({"type": "move", **position})
        return "break"

    def _mouse_button(self, event, button, down):
        if not down and button not in self._buttons_down:
            return "break"
        position = self._position(event, clamp=not down)
        if position is None:
            return "break"
        if down:
            self.canvas.focus_set()
            self._buttons_down.add(button)
        else:
            self._buttons_down.discard(button)
        self._flush_key_releases()
        self._input.append({"type": "button", "button": button, "down": down, **position})
        return "break"

    def _scroll(self, event, dx, dy):
        position = self._position(event)
        dx, dy = max(-100, min(100, int(dx))), max(-100, min(100, int(dy)))
        if position is not None and (dx or dy):
            self.canvas.focus_set()
            self._flush_key_releases()
            self._input.append({"type": "scroll", "dx": dx, "dy": dy, **position})
        return "break"

    def _mouse_wheel(self, event):
        if self._position(event) is None:
            return "break"
        horizontal = bool(event.state & 0x0001)
        axis = "x" if horizontal else "y"
        unit = 1 if sys.platform == "darwin" else 120
        total = self._wheel_remainders[axis] + event.delta
        steps = int(total / unit)
        self._wheel_remainders[axis] = total - steps * unit
        return self._scroll(event, -steps if horizontal else 0, 0 if horizontal else steps)

    def _touchpad_scroll(self, event):
        if self._position(event) is None:
            self._touchpad_remainders = {"x": 0, "y": 0}
            return "break"
        # %D packs two signed 16-bit values: X in the high half, Y in
        # the low half. Mask both halves because Python/Tk builds can expose
        # the packed value as either a signed or unsigned 32-bit integer.
        packed = int(event.delta)
        deltas = ((packed >> 16) & 0xffff, packed & 0xffff)
        steps = []
        for axis, delta in zip(("x", "y"), deltas):
            if delta >= 0x8000:
                delta -= 0x10000
            total = self._touchpad_remainders[axis] + delta
            count = int(total / _TOUCHPAD_PIXELS_PER_STEP)
            self._touchpad_remainders[axis] = total - count * _TOUCHPAD_PIXELS_PER_STEP
            steps.append(count)
        # Tk positive X scrolls left; pynput positive dx scrolls right.
        # Positive Y means up in both APIs. macOS already applies the user's
        # natural-scrolling preference before delivering the event.
        return self._scroll(event, -steps[0], steps[1])

    def _key_press(self, event):
        if self._closed or self._ended or self._image_rect is None or not self._input.ready:
            return "break"
        identity = _key_identity(event)
        self._flush_key_releases(except_identity=identity)
        pending = self._key_release_jobs.pop(identity, None)
        if pending is not None:
            self.root.after_cancel(pending)
        key = self._keys_down.get(identity)
        if key is None:
            key = _key_name(event)
            if (len(event.keysym) == 1 and event.keysym.isascii() and event.keysym.isalpha()
                    and _SHORTCUT_MODIFIERS.intersection(self._keys_down.values())):
                # Preserve the letter's virtual key for Ctrl/Command+Shift shortcuts.
                key = event.keysym.lower()
        if key is not None:
            self._keys_down[identity] = key
            self._input.append({"type": "key", "key": key, "down": True})
        return "break"

    def _key_release(self, event):
        identity = _key_identity(event)
        if identity in self._keys_down and identity not in self._key_release_jobs:
            if self._keys_down[identity] in _MODIFIER_KEYS:
                self._flush_key_releases()
                self._finish_key_release(identity)
            else:
                # X11 autorepeat sends a release immediately followed by another press.
                self._key_release_jobs[identity] = self.root.after_idle(self._finish_key_release, identity)
        return "break"

    def _flush_key_releases(self, except_identity=None):
        for identity in list(self._key_release_jobs):
            if identity != except_identity:
                self.root.after_cancel(self._key_release_jobs[identity])
                self._finish_key_release(identity)

    def _finish_key_release(self, identity):
        self._key_release_jobs.pop(identity, None)
        key = self._keys_down.pop(identity, None)
        if key is not None:
            self._input.append({"type": "key", "key": key, "down": False})

    def _clear_local_input(self):
        for job in self._key_release_jobs.values():
            self.root.after_cancel(job)
        self._key_release_jobs.clear()
        self._keys_down.clear()
        self._buttons_down.clear()
        self._wheel_remainders = {"x": 0, "y": 0}
        self._touchpad_remainders = {"x": 0, "y": 0}

    def _release_all(self, event=None):
        self._clear_local_input()
        if not self._closed and not self._ended:
            self._input.append({"type": "release"})
        self._safe_status()

    def _unmapped(self, event):
        if event.widget == self.root:
            self._release_all()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._frame_timer.close()
        self._worker.cancel()
        try:
            self._clear_local_input()
        except Exception:
            pass
        for job in (self._poll_job, self._resize_job, self._focus_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        try:
            self.root.destroy()
        except Exception:
            pass
        if self._frame is not None:
            self._frame.close()
        if self._rendered_frame is not None and self._rendered_frame is not self._frame:
            self._rendered_frame.close()
        self._frame = self._rendered_frame = None
        if self._worker.terminal_error is None:
            _lifecycle(self._worker.close_summary())


def run_viewer(open_session, title):
    """Show immediately; a zero-argument session factory enables recovery.

    A pre-opened session remains supported, but cannot be reopened after failure.
    Terminal authentication, protocol and rendering failures are raised on close.
    """
    try:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("远程桌面窗口必须在主线程中运行。")
        import tkinter as tk
        from tkinter import ttk
        from PIL import Image, ImageTk

        viewer = _Viewer(open_session, title, tk, ttk, Image, ImageTk)
        viewer.run()
    finally:
        if not callable(open_session):
            _RecoveryWorker._close_session(open_session)
