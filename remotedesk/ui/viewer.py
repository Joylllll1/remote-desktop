"""Tkinter 远程桌面窗口；网络收发在后台串行执行。"""

from collections import deque
import io
import queue
import sys
import threading
import time


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


class _InputBuffer:
    """Coalesce motion; an explicit release cancels stale pending input."""

    def __init__(self, enabled=True):
        self._events = deque()
        self._lock = threading.Lock()
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

    def disable(self):
        self.begin(None)

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

    def take(self):
        with self._lock:
            return [self._events.popleft() for _ in range(min(128, len(self._events)))]


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
        self._terminal_error = None
        self._shown_epoch = None
        self._failures = 0
        self._created_at = time.monotonic()
        self._displayed_frames = self._total_displayed = 0
        self._shown_started = self._last_checkpoint = self._created_at
        self.inputs.disable()

    @property
    def terminal_error(self):
        with self._lock:
            return self._terminal_error

    def cancel(self):
        self._cancelled.set()
        self.inputs.disable()
        with self._lock:
            session = self._session
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
        control_ready = self.inputs.enable(epoch)
        with self._lock:
            if self._terminal_error is not None or self._cancelled.is_set():
                self.inputs.disable()
                return False
            now = time.monotonic()
            first = self._shown_epoch != epoch
            if first:
                self._displayed_frames = 0
                self._shown_started = self._last_checkpoint = now
            self._displayed_frames += 1
            self._total_displayed += 1
            self._shown_epoch = epoch
            if control_ready:
                self._failures = 0
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

    def run(self):
        epoch = 0
        while not self._cancelled.is_set():
            epoch += 1
            self.inputs.begin(epoch)
            self.states.put(("connecting", epoch))
            _lifecycle(("正在连接 %s…" if epoch == 1 else "正在重新连接 %s…") % self.title)
            session = None
            failure = None
            try:
                session = self._factory()
                with self._lock:
                    if not self._cancelled.is_set():
                        self._session = session
                if self._cancelled.is_set():
                    break
                self.states.put(("receiving", epoch))
                _lifecycle("已连接 %s，正在接收画面。" % self.title)
                while not self._cancelled.is_set():
                    started = time.monotonic()
                    jpeg = session.exchange(self.inputs.take())
                    if self._cancelled.is_set():
                        break
                    try:
                        with self.Image.open(io.BytesIO(jpeg), formats=("JPEG",)) as source:
                            source.load()
                            frame = source.convert("RGB")
                    except Exception as error:
                        raise ValueError("远程桌面画面无效：" + str(error)) from error
                    if self._cancelled.is_set():
                        frame.close()
                        break
                    self._discard_frames()
                    self.frames.put_nowait((epoch, frame))
                    self._cancelled.wait(max(0, 0.1 - (time.monotonic() - started)))
            except ValueError as error:
                self.fail(error)
            except Exception as error:
                if not self._cancelled.is_set():
                    if isinstance(error, (OSError, EOFError)) and self._can_reconnect:
                        failure = error
                    else:
                        self.fail(error if isinstance(error, (OSError, EOFError, RuntimeError)) else RuntimeError(str(error)))
            finally:
                self.inputs.disable()
                if self._cancelled.is_set():
                    self._discard_frames()
                with self._lock:
                    self._session = None
                if session is not None:
                    self._close_session(session)
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
        self.root.title(title)
        width = min(1100, max(420, self.root.winfo_screenwidth() - 80))
        height = min(740, max(280, self.root.winfo_screenheight() - 110))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(420, 280)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        bar = tk.Frame(self.root, bg="#f7faf9", padx=14, pady=7)
        bar.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value="正在连接…")
        self._disconnect = ttk.Button(bar, text="断开连接", command=self.close)
        self._disconnect.pack(side="right", padx=(12, 0))
        self._indicator = tk.Canvas(bar, width=10, height=10, bg="#f7faf9", highlightthickness=0)
        self._indicator.pack(side="left", padx=(0, 8))
        self._dot = self._indicator.create_oval(1, 1, 9, 9, fill="#84918d", outline="")
        status_label = tk.Label(bar, textvariable=self.status, anchor="w", justify="left",
                                bg="#f7faf9", fg="#253f39")
        status_label.pack(side="left", fill="x", expand=True)
        status_label.bind("<Configure>", lambda event: status_label.configure(wraplength=max(1, event.width - 4)))
        self.canvas = tk.Canvas(self.root, background="#182321", highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
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
        self._keys_down = {}
        self._buttons_down = set()
        self._key_release_jobs = {}
        self._wheel_remainders = {"x": 0, "y": 0}
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
        self._poll_job = self.root.after(20, self._poll)
        self._focus_job = self.root.after_idle(self.canvas.focus_set)
        try:
            self.root.mainloop()
        finally:
            self.close()
            self._worker.join(timeout=1)
        if self._worker.terminal_error is not None:
            raise self._worker.terminal_error

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        try:
            self._poll_updates()
        except Exception as cause:
            self._fail_display(cause)
        if not self._closed and not self._ended:
            self._poll_job = self.root.after(20, self._poll)

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
            self.canvas.itemconfigure(self._message_item, state="normal", text=message)
        else:
            # Retain the last complete picture, with offline state in the footer.
            self.canvas.itemconfigure(self._message_item, state="hidden")

    def _show_terminal(self, error):
        self._ended = True
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
        self._frame = frame
        self._render()
        if self._worker.frame_shown(epoch):
            self._indicator.itemconfigure(self._dot, fill="#11645a")
            self._update_status()

    def _resized(self, event):
        if self._closed:
            return
        self.canvas.coords(self._message_item, event.width / 2, event.height / 2)
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(35, self._safe_render)

    def _safe_render(self):
        try:
            self._render()
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
            image = self._frame.resize(size, resampling)
            self._photo = self.ImageTk.PhotoImage(image, master=self.root)
            self.canvas.itemconfigure(self._image_item, image=self._photo, state="normal")
            self._rendered_frame, self._rendered_size = self._frame, size
        left, top = (width - size[0]) // 2, (height - size[1]) // 2
        self.canvas.coords(self._image_item, left, top)
        self.canvas.itemconfigure(self._message_item, state="hidden")
        self._image_rect = (left, top, *size)

    def _update_status(self):
        if self._closed or self._ended or self._frame is None or not self._input.ready:
            return
        hint = "键鼠控制中" if self.canvas.focus_get() == self.canvas else "点击画面控制"
        text = "已连接   ·   %s   ·   %d × %d" % (hint, self._frame.width, self._frame.height)
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

    def _key_press(self, event):
        if self._closed or self._ended or self._image_rect is None or not self._input.ready:
            return "break"
        identity = event.keycode
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
        identity = event.keycode
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
