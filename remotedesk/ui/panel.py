"""Visible Tailscale device chooser and explicitly owned hosting controls."""

from collections import Counter, deque
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from remotedesk import application_command, command_directory


_RETRY_DELAYS = (2, 5, 10, 20, 30)
_VIEWER_LINE_LIMIT = 2000
_VIEWER_PENDING_LINES = 64
_VIEWER_HISTORY_LIMIT = 32


class _ViewerOutput:
    """Bound each worker's retained output and queued UI notification count."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = deque(maxlen=_VIEWER_PENDING_LINES)
        self._tail = deque(maxlen=30)
        self._notified = False
        self._dropped = 0

    def append(self, line):
        line = str(line).strip()
        if not line:
            return False
        if len(line) > _VIEWER_LINE_LIMIT:
            line = line[:_VIEWER_LINE_LIMIT - 1] + "…"
        with self._lock:
            if len(self._pending) == self._pending.maxlen:
                self._dropped += 1
            self._pending.append(line)
            self._tail.append(line)
            notify = not self._notified
            self._notified = True
            return notify

    def take(self):
        with self._lock:
            lines, dropped = list(self._pending), self._dropped
            self._pending.clear()
            self._dropped = 0
            self._notified = False
            return lines, dropped

    def last_detail(self):
        with self._lock:
            return next((line for line in reversed(self._tail)
                         if not line.startswith("[查看器] 已关闭 ")), "")


def _watch_viewer(process, output, messages, identity, name):
    """Drain captured UTF-8 output even when the panel is busy or has closed."""
    def publish(line):
        if output.append(line):
            messages.put(("viewer_output", identity, name, process, output))

    try:
        while True:
            chunk = process.stdout.readline(_VIEWER_LINE_LIMIT + 1)
            if not chunk:
                break
            line = chunk.rstrip("\r\n")
            if len(line) > _VIEWER_LINE_LIMIT:
                line = line[:_VIEWER_LINE_LIMIT - 15] + "…（此行过长，已截断）"
                # Discard the rest of this physical line without an unbounded
                # read or flooding the UI with pieces of a single long line.
                while chunk and not chunk.endswith("\n"):
                    chunk = process.stdout.readline(_VIEWER_LINE_LIMIT + 1)
            publish(line)
    except (OSError, ValueError) as error:
        publish("无法读取查看器状态：" + str(error))
    finally:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
    try:
        code = process.wait()
    except (OSError, ValueError) as error:
        publish("无法确认查看器退出状态：" + str(error))
        code = 1
    messages.put(("viewer_exit", identity, name, process, code, output))


def _port_in_use(text):
    text = text.casefold()
    return any(part in text for part in (
        "address already in use", "only one usage", "10048", "errno 48",
        "errno 98", "地址已被使用", "端口已被占用", "套接字地址",
    ))


def _transient_host_error(text):
    text = text.casefold()
    if any(part in text for part in (
        "权限", "屏幕录制", "辅助功能", "permission", "accessibility",
        "无法打开桌面", "无法截取桌面", "wayland", "x11", "缺少运行组件",
    )):
        return False
    return any(part in text for part in (
        "tailscale 状态", "无法读取 tailscale 状态", "读取设备列表失败",
        "tailscale 响应超时", "timed out", "timeout", "离线",
        "network is unreachable", "network unreachable", "cannot assign requested address",
        "errno 49", "errno 99", "10049", "10051",
    ))


def _stop_owned_process(process):
    """EOF asks host-id to release input before the bounded fallback."""
    if process is None or process.poll() is not None:
        return
    try:
        if process.stdin is not None:
            process.stdin.close()
    except (OSError, ValueError):
        pass
    for timeout, action in ((2.5, None), (1, "terminate"), (1, "kill")):
        try:
            if action and process.poll() is None:
                getattr(process, action)()
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            continue
        except ProcessLookupError:
            return
    raise RuntimeError("本次共享进程未能停止，请在任务管理器中检查该进程。")


class _Panel:
    def __init__(self, network_status, preferences, script, tk, ttk, login=False):
        self.network_status = network_status
        self.preferences = preferences
        self.script = Path(script).resolve()
        self.tk = tk
        self.root = tk.Tk()
        self.root.title("远程桌面")
        self.root.geometry("760x560")
        self.root.minsize(640, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._messages = queue.Queue()
        self._closed = False
        self._closing = False
        self._refreshing = False
        self._preference_busy = False
        self._rows = {}
        self._host_process = None
        self._host_peer = None
        self._host_state = "stopped"
        self._host_tail = deque(maxlen=30)
        self._viewers = {}
        self._viewer_status = {}
        self._retry_job = None
        self._retry_deadline = None
        self._retry_attempt = 0
        self._poll_job = None
        self._saved_peer = None
        self._auto_target = None
        self._auto_intent = bool(login)
        self._preferences_loaded = False
        self._preference_revision = 0
        self._share_rows = []
        self._details_visible = False
        self._notice_error = None

        from tkinter import font
        normal = font.nametofont("TkDefaultFont")
        heading, strong = normal.copy(), normal.copy()
        size = normal.cget("size")
        heading.configure(size=size + 3 if size > 0 else size - 3, weight="bold")
        strong.configure(weight="bold")
        self._fonts = (heading, strong)
        style = ttk.Style(self.root)
        try:
            background = style.lookup("TFrame", "background") or self.root.cget("background")
            dark = sum(self.root.winfo_rgb(background)) < 3 * 32768
        except tk.TclError:
            dark = False
        accent = "#82c9b8" if dark else "#11645a"
        style.configure("Remote.Title.TLabel", font=heading, foreground=accent)
        style.configure("Remote.Muted.TLabel", font=normal)
        style.configure("Remote.Error.TLabel", foreground="#f4a593" if dark else "#983d31")
        style.configure("Remote.Sharing.TLabel", font=strong, foreground=accent)
        style.configure("Remote.Accent.TButton", font=strong, foreground=accent, padding=(12, 5))
        style.configure("Remote.Treeview", rowheight=max(26, normal.metrics("linespace") + 10))
        style.map("Remote.Treeview", background=[("selected", "#11645a")],
                  foreground=[("selected", "#f1f6f4")])

        body = ttk.Frame(self.root, padding=(18, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="我的设备", style="Remote.Title.TLabel").pack(side="left")
        self.refresh_button = ttk.Button(header, text="刷新", command=self.refresh)
        self.refresh_button.pack(side="right")
        self.network_text = tk.StringVar(value="正在读取 Tailscale 设备…")
        self._notice_text = tk.StringVar(value=self.network_text.get())
        self._notice_label = ttk.Label(body, textvariable=self._notice_text,
                                      style="Remote.Muted.TLabel", wraplength=590)
        self._notice_label.grid(row=1, column=0, sticky="ew", pady=(5, 10))
        self.network_text.trace_add("write", lambda *args: self._refresh_notice())

        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.connect_page = ttk.Frame(self.notebook, padding=(12, 12))
        self.share_page = ttk.Frame(self.notebook, padding=(14, 12))
        self.notebook.add(self.connect_page, text="连接设备")
        self.notebook.add(self.share_page, text="共享本机")
        self.notebook.enable_traversal()
        self.notebook.bind("<<NotebookTabChanged>>", lambda event: self._update_controls())
        self.connect_page.columnconfigure(0, weight=1)
        self.connect_page.rowconfigure(0, weight=1)
        device_frame = ttk.Frame(self.connect_page)
        device_frame.grid(row=0, column=0, sticky="nsew")
        self.devices = ttk.Treeview(device_frame, columns=("name", "online", "address"),
                                    show="headings", selectmode="browse", height=6, style="Remote.Treeview")
        for column, title, width in (("name", "设备", 210), ("online", "状态", 60), ("address", "地址", 270)):
            anchor = "center" if column == "online" else "w"
            self.devices.heading(column, text=title, anchor=anchor)
            self.devices.column(column, width=width, minwidth=60, stretch=column != "online", anchor=anchor)
        scrollbar = ttk.Scrollbar(device_frame, orient="vertical", command=self.devices.yview)
        self.devices.configure(yscrollcommand=scrollbar.set)
        self.devices.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.devices.bind("<<TreeviewSelect>>", lambda event: self._update_controls())
        self.devices.bind("<Double-1>", lambda event: "break")
        self.devices.bind("<Return>", self._connect_key)
        self.root.bind("<F5>", lambda event: self.refresh())

        connect_row = ttk.Frame(self.connect_page)
        connect_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.selection_text = tk.StringVar(value="选择在线设备，然后连接。")
        ttk.Label(connect_row, textvariable=self.selection_text, style="Remote.Muted.TLabel",
                  wraplength=420).pack(side="left", fill="x", expand=True)
        self.connect_button = ttk.Button(connect_row, text="连接", command=self.connect, style="Remote.Accent.TButton")
        self.connect_button.pack(side="right", padx=(10, 0))

        self.share_page.columnconfigure(0, weight=1)
        ttk.Label(self.share_page, text="允许的设备", font=strong).grid(row=0, column=0, sticky="w")
        self.allowed_device = ttk.Combobox(self.share_page, state="readonly", width=32)
        self.allowed_device.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.allowed_device.bind("<<ComboboxSelected>>", lambda event: self._update_controls())
        self.start_button = ttk.Button(self.share_page, text="开始共享", command=self.start_host,
                                       style="Remote.Accent.TButton")
        self.start_button.grid(row=1, column=1, padx=(10, 0), pady=(6, 0))
        self.host_text = tk.StringVar(value="只有所选设备可以控制本机。关闭此窗口会停止本次共享。")
        self._share_details = tk.StringVar(value=self.host_text.get())
        ttk.Label(self.share_page, textvariable=self._share_details, style="Remote.Muted.TLabel",
                  wraplength=545).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Separator(self.share_page).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 12))
        ttk.Label(self.share_page, text="登录后自动共享", font=strong).grid(row=4, column=0, columnspan=2, sticky="w")
        self.preference_text = tk.StringVar()
        self._preference_display = tk.StringVar()
        self.preference_text.trace_add("write", lambda *args: self._preference_display.set(self._compact(self.preference_text.get(), 76)))
        ttk.Label(self.share_page, textvariable=self._preference_display, style="Remote.Muted.TLabel",
                  wraplength=545).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        login_buttons = ttk.Frame(self.share_page)
        login_buttons.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.enable_button = ttk.Button(login_buttons, text="启用所选设备", command=self.enable_login)
        self.enable_button.pack(side="left")
        self.disable_button = ttk.Button(login_buttons, text="关闭自动共享", command=self.disable_login)
        self.disable_button.pack(side="left", padx=(8, 0))

        self.details_button = ttk.Button(body, text="显示详情", command=self._toggle_details)
        self.details_button.grid(row=3, column=0, sticky="w", pady=(7, 5))
        self._details_frame = ttk.Frame(body)
        self.log = tk.Text(self._details_frame, height=4, wrap="word", state="disabled", takefocus=True,
                           font=normal, relief="flat", highlightthickness=1)
        log_scroll = ttk.Scrollbar(self._details_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="x", expand=True)
        log_scroll.pack(side="right", fill="y")
        footer = ttk.Frame(body)
        footer.grid(row=5, column=0, sticky="ew")
        ttk.Separator(footer).pack(fill="x", pady=(1, 9))
        self._sharing_summary = tk.StringVar(value="此窗口未共享")
        self._sharing_label = ttk.Label(footer, textvariable=self._sharing_summary,
                                        style="Remote.Muted.TLabel", wraplength=440)
        self._sharing_label.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(footer, text="停止共享", command=self.stop_host)
        self.stop_button.pack(side="right", padx=(10, 0))
        self.host_text.trace_add("write", lambda *args: self._sync_sharing())
        if login:
            self.notebook.select(self.share_page)
        if login:
            self.host_text.set("正在读取登录共享授权并等待 Tailscale 就绪…")
        self._update_preferences()
        self._update_controls()

    def run(self):
        self._poll_job = self.root.after(80, self._poll)
        self.refresh()
        try:
            self.root.mainloop()
        finally:
            self._closed = True
            _stop_owned_process(self._host_process)

    @staticmethod
    def _compact(text, limit=76):
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _mode(self):
        if not hasattr(self, "notebook"):
            return "connect"
        return "share" if self.notebook.select() == str(self.share_page) else "connect"

    def _connect_key(self, event):
        if self._mode() == "connect":
            self.connect()
        return "break"

    def _refresh_notice(self):
        if hasattr(self, "_notice_text"):
            self._notice_text.set(self._compact(self._notice_error or self.network_text.get()))
            self._notice_label.configure(style="Remote.Error.TLabel" if self._notice_error else "Remote.Muted.TLabel")

    def _show_failure(self, text):
        self._notice_error = str(text)
        self._refresh_notice()
        if hasattr(self, "details_button") and not self._details_visible:
            self.details_button.configure(text="查看问题详情")

    def _clear_notice(self):
        self._notice_error = None
        self._refresh_notice()
        if hasattr(self, "details_button") and not self._details_visible:
            self.details_button.configure(text="显示详情")

    def _toggle_details(self):
        self._details_visible = not self._details_visible
        width, height = max(640, self.root.winfo_width()), max(500, self.root.winfo_height())
        if self._details_visible:
            self._details_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
            self.root.minsize(640, 620)
            self.root.geometry("%dx%d" % (width, max(620, height + 120)))
            self.details_button.configure(text="收起详情")
        else:
            self._details_frame.grid_remove()
            self.root.minsize(640, 500)
            self.root.geometry("%dx%d" % (width, max(500, height - 120)))
            self.details_button.configure(text="查看问题详情" if self._notice_error else "显示详情")

    def _sync_sharing(self):
        if not hasattr(self, "_sharing_summary"):
            return
        state = self._host_state
        name = self._compact(self._host_peer[1], 28) if self._host_peer else "所选设备"
        if state == "running":
            summary = "%s 正在控制本机" % name if "正在控制" in self.host_text.get() else "正在共享 · 仅允许 %s" % name
        elif state == "starting":
            summary = "正在启动共享…"
        elif state == "stopping":
            summary = "正在停止共享…"
        elif state == "retry" and self._auto_intent:
            summary = "等待自动共享"
        elif state == "external":
            summary = "共享端口被占用"
        elif state == "failed":
            summary = "共享未启动"
        else:
            summary = "此窗口未共享"
        self._sharing_summary.set(summary)
        self._sharing_label.configure(style="Remote.Sharing.TLabel" if state in ("running", "starting") else "Remote.Muted.TLabel")
        self._share_details.set(self._compact(self.host_text.get()))

    def _log(self, text):
        if self._closed:
            return
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S") + "  " + str(text).strip()[-4000:] + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 250:
            self.log.delete("1.0", "60.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _selected(self):
        if self._mode() == "share":
            index = self.allowed_device.current()
            return self._rows.get(self._share_rows[index]) if 0 <= index < len(self._share_rows) else None
        selection = self.devices.selection()
        return self._rows.get(selection[0]) if selection else None

    def _update_controls(self):
        peer = self._selected()
        has_identity = bool(peer and peer.get("ID"))
        active = bool(self._host_process and self._host_process.poll() is None)
        viewer = self._viewers.get(peer.get("ID")) if peer else None
        viewing = bool(viewer and viewer.poll() is None)
        viewer_status = getattr(self, "_viewer_status", {}).get(peer.get("ID")) if peer else None
        disabled = self._closing or self._closed
        sharing_mode = self._mode() == "share"
        controls = (
            (self.refresh_button, not self._refreshing),
            (self.connect_button, not sharing_mode and has_identity and peer.get("Online") and not viewing),
            (self.start_button, sharing_mode and has_identity and not active and self._host_state != "stopping"),
            (self.stop_button, self._host_state != "stopping" and (active or self._retry_job is not None or self._auto_intent)),
            (self.enable_button, sharing_mode and has_identity and self._preferences_loaded and not self._preference_busy
             and (not self._saved_peer or self._saved_peer[0] != peer["ID"])),
            (self.disable_button, sharing_mode and self._preferences_loaded and bool(self._saved_peer) and not self._preference_busy),
        )
        for button, enabled in controls:
            button.configure(state="normal" if enabled and not disabled else "disabled")
        if active or self._retry_job is not None or self._auto_intent or self._host_state == "stopping":
            if not self.stop_button.winfo_manager():
                self.stop_button.pack(side="right", padx=(10, 0))
        else:
            self.stop_button.pack_forget()
        self.start_button.configure(text="重试共享" if self._host_state == "external" else "开始共享")
        if not peer:
            self.selection_text.set("选择在线设备，然后连接。")
        elif not has_identity:
            self.selection_text.set("设备身份不可用，请刷新。")
        elif viewer_status and (not viewing or viewer_status[0] is viewer):
            self.selection_text.set(self._compact("%s：%s" % (peer["HostName"], viewer_status[1]), 108))
        elif viewing:
            self.selection_text.set("%s 的查看器已打开。" % peer["HostName"])
        else:
            self.selection_text.set("按“连接”打开远程桌面。" if peer.get("Online") else "设备离线，请检查它的电源和 Tailscale。")

    def _update_preferences(self):
        if not self._preferences_loaded:
            self.preference_text.set("正在读取登录设置…")
        elif self._saved_peer:
            self.preference_text.set("已启用：登录后仅允许 %s。本次共享可单独停止。" % self._saved_peer[1])
        else:
            self.preference_text.set("默认关闭。启用后，登录本机时自动允许这台设备连接。")

    def refresh(self):
        if self._closed or self._closing or self._refreshing:
            return
        self._clear_notice()
        self._cancel_retry()
        self._refreshing = True
        self.network_text.set("正在读取 Tailscale 设备…")
        self._update_controls()
        revision = self._preference_revision

        def read():
            try:
                result = self.network_status()
            except Exception as error:
                result, network_error = None, str(error) or error.__class__.__name__
            else:
                network_error = None
            try:
                saved = self.preferences.login_peer()
            except Exception as error:
                saved, preference_error = None, str(error) or error.__class__.__name__
            else:
                preference_error = None
            self._messages.put(("network", result, network_error, saved, preference_error, revision))
        threading.Thread(target=read, name="desktop-devices", daemon=True).start()

    def _network_result(self, status, error, saved, preference_error, revision):
        self._refreshing = False
        fresh_authorization = revision == self._preference_revision and not self._preference_busy
        if fresh_authorization:
            self._preferences_loaded = preference_error is None
            if not preference_error:
                self._saved_peer = saved
                if self._auto_intent:
                    self._auto_target = saved
                    if saved is None:
                        self._auto_intent = False
                        self._cancel_retry()
                        if not self._host_process or self._host_process.poll() is not None:
                            self.host_text.set("未启用登录后自动共享，本次不会自动启动。")
            elif self._auto_intent:
                self._auto_target = None
            self._update_preferences()
            if preference_error:
                self.preference_text.set("无法读取登录设置：" + preference_error)
                self._log(self.preference_text.get())
                self._show_failure(self.preference_text.get())
        if error:
            self._clear_devices()
            self.network_text.set("无法读取设备：" + error)
            self._log("设备刷新失败：" + error)
            self._show_failure("无法读取设备：" + error)
            self._schedule_retry("等待 Tailscale 就绪")
            self._update_controls()
            return
        selection = self.devices.selection()
        selected = self._rows.get(selection[0]) if selection else None
        preferred_id = selected.get("ID") if selected else None
        share_index = self.allowed_device.current()
        allowed = self._rows.get(self._share_rows[share_index]) if 0 <= share_index < len(self._share_rows) else None
        allowed_id = (allowed.get("ID") if allowed else None) or (self._auto_target or self._saved_peer or (None,))[0]
        self._clear_devices()
        peers = sorted((status.get("Peer") or {}).values(),
                       key=lambda peer: (not peer.get("Online"), peer.get("HostName", "").casefold()))
        preferred_row = None
        allowed_index = 0
        allowed_names = []
        duplicate_names = {name for name, count in Counter(peer.get("HostName") for peer in peers).items() if count > 1}
        for index, peer in enumerate(peers):
            row = "peer-%d" % index
            self._rows[row] = peer
            self.devices.insert("", "end", iid=row, values=(peer.get("HostName", "未命名设备"),
                "在线" if peer.get("Online") else "离线", ", ".join(peer.get("TailscaleIPs") or [])))
            if peer.get("ID") == preferred_id:
                preferred_row = row
            self._share_rows.append(row)
            name = peer.get("HostName", "未命名设备")
            if name in duplicate_names:
                name += " · " + next(iter(peer.get("TailscaleIPs") or []), "身份不可用")
            allowed_names.append(name + (" · 在线" if peer.get("Online") else " · 离线"))
            if peer.get("ID") == allowed_id:
                allowed_index = index
        self.allowed_device.configure(values=allowed_names)
        if peers:
            self.devices.selection_set(preferred_row or "peer-0")
            self.allowed_device.current(allowed_index)
        own = status.get("Self") or {}
        self.network_text.set("本机：%s · 找到 %d 台设备" % (own.get("HostName", "当前电脑"), len(peers)))
        if not peers:
            self.network_text.set("当前网络没有其他设备，请确认两台电脑已加入同一 Tailscale 网络。")
        if self._auto_intent and not self._host_process and not self._closing:
            if not fresh_authorization or preference_error:
                self._schedule_retry("等待重新确认登录共享授权")
                self._update_controls()
                return
            target = next((peer for peer in peers if peer.get("ID") == self._auto_target[0]), None)
            if own.get("Online") is False:
                self._schedule_retry("本机 Tailscale 尚未在线")
            elif target is None:
                self._schedule_retry("尚未找到已授权设备 %s" % self._auto_target[1])
            else:
                self._start_host(target, automatic=True)
        self._update_controls()

    def _clear_devices(self):
        for row in self.devices.get_children():
            self.devices.delete(row)
        self._rows.clear()
        self._share_rows.clear()
        self.allowed_device.configure(values=())
        self.allowed_device.set("")

    def _child_options(self, command, independent=False):
        options = {
            "cwd": command_directory(command), "shell": False,
            "env": dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                        REMOTEDESK_WORKER_STDIO="1"),
        }
        if independent and getattr(sys, "frozen", False):
            # The viewer may outlive the panel and its temporary bundle files.
            options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        return options

    def connect(self):
        if self._mode() != "connect":
            return
        peer = self._selected()
        if self._closing or not peer or not peer.get("ID") or not peer.get("Online"):
            return
        existing = self._viewers.get(peer["ID"])
        if existing and existing.poll() is None:
            return
        self._clear_notice()
        identity, name = peer["ID"], peer["HostName"]
        self._viewers.pop(identity, None)
        try:
            command = application_command("connect-id", identity, worker=True, launcher=self.script)
            process = subprocess.Popen(command,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1,
                                       **self._child_options(command, independent=True))
        except Exception as error:
            self._remember_viewer_status(identity, None, "连接失败：" + str(error))
            self._log("无法打开 %s 的查看器：%s" % (name, error))
            self._show_failure("无法连接 %s：%s" % (name, error))
            self.selection_text.set("连接失败，可查看问题详情。")
            return
        self._viewers[identity] = process
        self._remember_viewer_status(identity, process, "正在打开远程桌面…")
        self._log("正在打开 %s 的远程桌面。" % name)
        self._update_controls()
        output = _ViewerOutput()
        threading.Thread(target=_watch_viewer,
                         args=(process, output, self._messages, identity, name),
                         name="desktop-viewer-status", daemon=True).start()

    def _remember_viewer_status(self, identity, process, text):
        if not hasattr(self, "_viewer_status"):
            self._viewer_status = {}
        self._viewer_status.pop(identity, None)
        self._viewer_status[identity] = (process, text)
        while len(self._viewer_status) > _VIEWER_HISTORY_LIMIT:
            self._viewer_status.pop(next(iter(self._viewer_status)))

    def _viewer_output(self, identity, name, process, output):
        lines, dropped = output.take()
        current = self._viewers.get(identity) is process
        label = name if current else name + "（上次连接）"
        if dropped:
            self._log("%s：输出较多，已省略较早的 %d 条状态。" % (label, dropped))
        for line in lines:
            self._log("%s：%s" % (label, line))
        if current and lines:
            self._remember_viewer_status(identity, process, lines[-1])
            self._update_controls()

    def _viewer_exit(self, identity, name, process, code, output):
        self._viewer_output(identity, name, process, output)
        current = self._viewers.get(identity) is process
        detail = output.last_detail()
        if code:
            status = "连接已终止：%s" % (detail or "查看器退出码 %s" % code)
        else:
            status = "查看器已关闭；连接记录保留在详情中。"
            if detail:
                status += "关闭前：" + self._compact(detail, 180)
        label = name if current else name + "（上次连接）"
        self._log("%s：%s" % (label, status))
        if current:
            self._viewers.pop(identity, None)
            self._remember_viewer_status(identity, process, status)
            if code:
                self._show_failure("%s：%s" % (name, status))
            self._update_controls()

    def start_host(self):
        peer = self._selected()
        if self._closing or not peer or not peer.get("ID"):
            return
        self._clear_notice()
        self._auto_intent = False
        self._auto_target = None
        self._cancel_retry()
        self._start_host(peer)

    def _start_host(self, peer, automatic=False):
        if self._closing or self._host_state == "stopping":
            return
        if automatic and (not self._auto_intent or not self._auto_target or self._auto_target[0] != peer["ID"]):
            return
        if self._host_process and self._host_process.poll() is None:
            return
        self._cancel_retry()
        self._host_tail.clear()
        try:
            action = "host-login-id" if automatic else "host-id"
            command = application_command(action, peer["ID"], worker=True, launcher=self.script)
            process = subprocess.Popen(command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **self._child_options(command))
        except Exception as error:
            self._auto_intent = False
            self._auto_target = None
            self._host_state = "failed"
            self.host_text.set("共享启动失败：" + str(error))
            self._log(self.host_text.get())
            self._show_failure(self.host_text.get())
            self._update_controls()
            return
        self._host_process, self._host_peer = process, (peer["ID"], peer["HostName"])
        self._host_state = "starting"
        self.host_text.set("正在启动本机共享，仅允许 %s…" % peer["HostName"])
        self._log(self.host_text.get())
        self._update_controls()

        def watch():
            try:
                for line in process.stdout:
                    self._messages.put(("host_line", process, line.rstrip()))
                code = process.wait()
            finally:
                process.stdout.close()
            self._messages.put(("host_exit", process, code))
        threading.Thread(target=watch, name="desktop-host-status", daemon=True).start()

    def _host_line(self, process, line):
        if process is not self._host_process:
            return
        self._host_tail.append(line)
        self._log(line)
        if self._host_state == "stopping":
            return
        if line.startswith("正在共享桌面 "):
            self._host_state = "running"
            self._retry_attempt = 0
            self.host_text.set("正在共享本机 · 仅允许 %s。" % self._host_peer[1])
        elif line == "%s 已连接。" % self._host_peer[1]:
            self.host_text.set("%s 正在控制本机。" % self._host_peer[1])
        elif line == "会话已结束，等待重新连接。":
            self.host_text.set("正在共享本机 · 等待 %s 连接。" % self._host_peer[1])

    def _host_exit(self, process, code):
        if process is not self._host_process:
            return
        stopped = self._host_state == "stopping" or self._closing
        self._host_process = None
        output = "\n".join(self._host_tail)
        if stopped:
            self._host_state = "stopped"
            self.host_text.set("本次共享已停止；登录后自动共享设置可单独更改。")
        elif _port_in_use(output):
            self._host_state = "external"
            self._auto_intent = False
            self._auto_target = None
            self.host_text.set("共享端口被占用。请检查其他窗口或服务；此窗口不会停止它们。")
            self._log(self.host_text.get())
            self._show_failure(self.host_text.get())
        elif self._auto_intent and (code == 0 or _transient_host_error(output) or "登录共享授权已变更" in output):
            self._host_state = "stopped"
            self._schedule_retry("共享暂时不可用，等待 Tailscale 恢复")
        else:
            self._host_state = "failed" if code else "stopped"
            self._auto_intent = False
            self._auto_target = None
            detail = next((line for line in reversed(self._host_tail) if line.strip()), "进程已退出（%s）" % code)
            self.host_text.set("共享已停止：%s。请查看问题详情，修复后重新共享。" % detail)
            if code:
                self._show_failure(self.host_text.get())
        self._update_controls()

    def _schedule_retry(self, reason):
        if self._closing or not self._auto_intent or self._retry_job is not None or self._host_process:
            return
        delay = _RETRY_DELAYS[min(self._retry_attempt, len(_RETRY_DELAYS) - 1)]
        self._retry_attempt += 1
        self._retry_deadline = time.monotonic() + delay
        self._retry_reason = reason
        self._host_state = "retry"
        self.host_text.set("%s；%d 秒后重试。可点击“停止共享”取消。" % (reason, delay))
        self._retry_job = self.root.after(delay * 1000, self._retry)

    def _retry(self):
        self._retry_job = None
        self._retry_deadline = None
        if not self._closing and self._auto_intent:
            self.refresh()

    def _cancel_retry(self):
        if self._retry_job is not None:
            self.root.after_cancel(self._retry_job)
        self._retry_job = None
        self._retry_deadline = None

    def stop_host(self):
        self._auto_intent = False
        self._auto_target = None
        self._cancel_retry()
        process = self._host_process
        if process is None or process.poll() is not None:
            self._host_state = "stopped"
            self.host_text.set("本次共享已停止。即使启用了自动共享，本次也不会再自动启动。")
            self._update_controls()
            return
        if self._host_state == "stopping":
            return
        self._host_state = "stopping"
        self.host_text.set("正在停止本次共享并释放远程按键…")
        self._update_controls()

        def stop():
            try:
                _stop_owned_process(process)
            except Exception as error:
                self._messages.put(("stop_error", str(error)))
        threading.Thread(target=stop, name="desktop-host-stop", daemon=True).start()

    def enable_login(self):
        peer = self._selected()
        if peer and peer.get("ID") and not self._preference_busy and not self._closing:
            self._set_preference((peer["ID"], peer["HostName"]))

    def disable_login(self):
        if not self._preference_busy and not self._closing:
            self._auto_intent = False
            self._auto_target = None
            self._cancel_retry()
            if not self._host_process:
                self._host_state = "stopped"
                self.host_text.set("已取消本次自动重试。登录设置正在更新。")
            self._set_preference(None)

    def _set_preference(self, peer):
        self._clear_notice()
        self._preference_busy = True
        self._preference_revision += 1
        self.preference_text.set("正在保存登录设置…")
        self._update_controls()

        def save():
            try:
                if peer:
                    self.preferences.enable_login(*peer)
                else:
                    self.preferences.disable_login()
            except Exception as error:
                operation_error = str(error) or error.__class__.__name__
            else:
                operation_error = None
            try:
                effective = self.preferences.login_peer()
            except Exception as error:
                effective, lookup_error = None, str(error) or error.__class__.__name__
            else:
                lookup_error = None
            self._messages.put(("preference", effective, operation_error, lookup_error))
        threading.Thread(target=save, name="desktop-login-setting", daemon=True).start()

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        for _ in range(200):
            try:
                message = self._messages.get_nowait()
            except queue.Empty:
                break
            kind, *values = message
            if kind == "network":
                self._network_result(*values)
            elif kind == "host_line":
                self._host_line(*values)
            elif kind == "host_exit":
                self._host_exit(*values)
            elif kind == "viewer_output":
                self._viewer_output(*values)
            elif kind == "viewer_exit":
                self._viewer_exit(*values)
            elif kind == "preference":
                peer, error, lookup_error = values
                self._preference_busy = False
                self._preference_revision += 1
                self._preferences_loaded = lookup_error is None
                if not lookup_error:
                    self._saved_peer = peer
                    if self._auto_intent:
                        self._auto_target = peer
                        if peer is None:
                            self._auto_intent = False
                if error:
                    self._log("登录设置保存失败：" + error)
                elif lookup_error:
                    self._log("登录设置已提交，但无法确认当前状态：" + lookup_error)
                else:
                    self._log("已启用登录后自动共享给 %s。" % peer[1] if peer else "已关闭登录后自动共享。")
                self._update_preferences()
                if error or lookup_error:
                    self.preference_text.set("登录设置操作未完成：" + (error or lookup_error))
                    self._show_failure(self.preference_text.get())
                self._update_controls()
            elif kind == "stop_error":
                self._log(values[0])
                self.host_text.set(values[0])
                self._show_failure(values[0])
        if self._retry_deadline and self._auto_intent and not self._closing:
            seconds = max(0, math.ceil(self._retry_deadline - time.monotonic()))
            self.host_text.set("%s；%d 秒后重试。可点击“停止共享”取消。" % (self._retry_reason, seconds))
        if self._closing and not self._preference_busy:
            if not self._host_process or self._host_process.poll() is not None:
                self._closed = True
                self.root.destroy()
                return
        self._poll_job = self.root.after(80, self._poll)

    def close(self):
        if self._closing or self._closed:
            return
        self._closing = True
        self.stop_host()
        if self._preference_busy:
            self.preference_text.set("正在保存登录设置，完成后关闭窗口。")
        self._update_controls()
        if self._poll_job is None:
            self._poll_job = self.root.after(20, self._poll)


def run_panel(login=False):
    """Show the panel; only a saved opt-in permits hosting on a login launch."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("远程桌面面板必须在主线程中运行。")
    import tkinter as tk
    from tkinter import ttk
    from remotedesk import LAUNCHER
    from remotedesk.login import preferences
    from remotedesk.network.tailscale import network_status

    panel = _Panel(network_status, preferences, LAUNCHER, tk, ttk, login=login)
    panel.run()


if __name__ == "__main__":
    run_panel(login="--login" in sys.argv[1:])
