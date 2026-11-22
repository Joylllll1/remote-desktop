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

    def __init__(self):
        self._events = deque()
        self._lock = threading.Lock()

    def append(self, event):
        with self._lock:
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


class _Viewer:
    def __init__(self, session, title, tk, ttk, Image, ImageTk):
        self.session = session
        self.Image = Image
        self.ImageTk = ImageTk
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("1100x740")
        self.root.minsize(420, 280)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(self.root, background="#171b20", highlightthickness=0, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        bar = ttk.Frame(self.root, padding=(10, 7))
        bar.pack(fill="x")
        self.status = tk.StringVar(value="正在接收远程桌面…")
        ttk.Label(bar, textvariable=self.status).pack(side="left", fill="x", expand=True)
        ttk.Button(bar, text="断开连接", command=self.close).pack(side="right", padx=(10, 0))
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._message_item = self.canvas.create_text(
            0, 0, text="正在等待远程画面…", fill="#e7ebee", justify="center",
        )
        self._input = _InputBuffer()
        self._frames = queue.Queue(maxsize=1)
        self._errors = queue.Queue(maxsize=1)
        self._stop = threading.Event()
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
        self._worker = threading.Thread(target=self._exchange_loop, name="desktop-viewer", daemon=True)

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
        self.canvas.bind("<FocusIn>", lambda event: self._update_status())
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

    def _exchange_loop(self):
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                jpeg = self.session.exchange(self._input.take())
                if self._stop.is_set():
                    break
                with self.Image.open(io.BytesIO(jpeg)) as source:
                    source.load()
                    frame = source.convert("RGB")
                if self._stop.is_set():
                    frame.close()
                    break
                try:
                    old_frame = self._frames.get_nowait()
                    old_frame.close()
                except queue.Empty:
                    pass
                self._frames.put_nowait(frame)
                self._stop.wait(max(0, 0.1 - (time.monotonic() - started)))
        except Exception as error:
            if not self._stop.is_set():
                self._errors.put_nowait(str(error) or error.__class__.__name__)
        finally:
            try:
                self.session.close()
            except Exception:
                pass

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        try:
            error = self._errors.get_nowait()
        except queue.Empty:
            error = None
        if error is not None:
            self._ended = True
            self._release_all()
            self._image_rect = None
            self.canvas.itemconfigure(self._image_item, state="hidden")
            self.canvas.itemconfigure(self._message_item, state="normal", text="连接已断开\n请关闭窗口后重新连接。")
            self.status.set("连接已断开：" + error)
            return
        try:
            frame = self._frames.get_nowait()
        except queue.Empty:
            pass
        else:
            self._frame = frame
            self._render()
            self._update_status()
        self._poll_job = self.root.after(20, self._poll)

    def _resized(self, event):
        self.canvas.coords(self._message_item, event.width / 2, event.height / 2)
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(35, self._render)

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
        if self._closed or self._ended or self._frame is None:
            return
        hint = "键鼠控制中" if self.canvas.focus_get() == self.canvas else "点击画面控制"
        self.status.set("已连接 · %d × %d · %s" % (self._frame.width, self._frame.height, hint))

    def _position(self, event, clamp=False):
        if self._closed or self._ended or self._image_rect is None:
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
        if self._closed or self._ended or self._image_rect is None:
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

    def _release_all(self, event=None):
        for job in self._key_release_jobs.values():
            self.root.after_cancel(job)
        self._key_release_jobs.clear()
        self._keys_down.clear()
        self._buttons_down.clear()
        self._wheel_remainders = {"x": 0, "y": 0}
        if not self._closed and not self._ended:
            self._input.append({"type": "release"})
        self._update_status()

    def _unmapped(self, event):
        if event.widget == self.root:
            self._release_all()

    def close(self):
        if self._closed:
            return
        self._release_all()
        self._closed = True
        self._stop.set()
        for job in (self._poll_job, self._resize_job, self._focus_job):
            if job is not None:
                self.root.after_cancel(job)
        try:
            # The session interrupts any blocked exchange; the peer releases input
            # on disconnect, including if a final release cannot be transmitted.
            self.session.close()
        finally:
            self.root.destroy()


def run_viewer(session, title):
    """Run on the main thread with an authenticated exchange/close session."""
    try:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("远程桌面窗口必须在主线程中运行。")
        import tkinter as tk
        from tkinter import ttk
        from PIL import Image, ImageTk

        viewer = _Viewer(session, title, tk, ttk, Image, ImageTk)
        viewer.run()
    finally:
        session.close()
