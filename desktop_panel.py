"""Visible Tailscale device chooser and explicitly owned hosting controls."""

from collections import deque
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time


_RETRY_DELAYS = (2, 5, 10, 20, 30)


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
        self.root.geometry("820x690")
        self.root.minsize(650, 570)
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
        self._retry_job = None
        self._retry_deadline = None
        self._retry_attempt = 0
        self._poll_job = None
        self._saved_peer = None
        self._auto_target = None
        self._auto_intent = bool(login)
        self._preferences_loaded = False
        self._preference_revision = 0

        body = ttk.Frame(self.root, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="远程桌面", font=("TkDefaultFont", 18, "bold")).pack(anchor="w")
        ttk.Label(body, text="选择一台 Tailscale 设备，连接它，或允许它控制本机。",
                  wraplength=740).pack(anchor="w", pady=(4, 12))
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x")
        self.network_text = tk.StringVar(value="正在读取 Tailscale 设备…")
        ttk.Label(toolbar, textvariable=self.network_text, wraplength=590).pack(side="left", fill="x", expand=True)
        self.refresh_button = ttk.Button(toolbar, text="刷新设备", command=self.refresh)
        self.refresh_button.pack(side="right", padx=(10, 0))
        device_frame = ttk.Frame(body)
        device_frame.pack(fill="both", expand=True, pady=(8, 8))
        self.devices = ttk.Treeview(device_frame, columns=("name", "online", "address"),
                                    show="headings", selectmode="browse", height=7)
        for column, title, width in (("name", "设备", 210), ("online", "状态", 65), ("address", "Tailscale 地址", 300)):
            self.devices.heading(column, text=title)
            self.devices.column(column, width=width, minwidth=60, stretch=column != "online")
        scrollbar = ttk.Scrollbar(device_frame, orient="vertical", command=self.devices.yview)
        self.devices.configure(yscrollcommand=scrollbar.set)
        self.devices.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.devices.bind("<<TreeviewSelect>>", lambda event: self._update_controls())
        self.devices.bind("<Double-1>", lambda event: self.connect())
        self.devices.bind("<Return>", lambda event: self.connect())
        self.root.bind("<F5>", lambda event: self.refresh())

        connect_row = ttk.Frame(body)
        connect_row.pack(fill="x", pady=(0, 12))
        self.selection_text = tk.StringVar(value="请选择一台设备。")
        ttk.Label(connect_row, textvariable=self.selection_text, wraplength=550).pack(side="left", fill="x", expand=True)
        self.connect_button = ttk.Button(connect_row, text="连接所选设备", command=self.connect)
        self.connect_button.pack(side="right", padx=(10, 0))

        host_box = ttk.LabelFrame(body, text="共享本机", padding=10)
        host_box.pack(fill="x")
        self.host_text = tk.StringVar(value="此窗口尚未启动共享。只有所选设备可以连接它启动的共享。")
        ttk.Label(host_box, textvariable=self.host_text, wraplength=720).pack(anchor="w", fill="x")
        host_buttons = ttk.Frame(host_box)
        host_buttons.pack(fill="x", pady=(8, 0))
        self.start_button = ttk.Button(host_buttons, text="允许所选设备，开始共享", command=self.start_host)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(host_buttons, text="停止本次共享", command=self.stop_host)
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(host_box, text="关闭此窗口仅停止本窗口启动的共享。其他共享请到原窗口查看或停止，远程查看器会保留。",
                  wraplength=720).pack(anchor="w", pady=(7, 0))

        login_box = ttk.LabelFrame(body, text="登录后自动共享", padding=10)
        login_box.pack(fill="x", pady=(10, 0))
        self.preference_text = tk.StringVar()
        ttk.Label(login_box, textvariable=self.preference_text, wraplength=720).pack(anchor="w", fill="x")
        login_buttons = ttk.Frame(login_box)
        login_buttons.pack(fill="x", pady=(8, 0))
        self.enable_button = ttk.Button(login_buttons, text="启用所选设备", command=self.enable_login)
        self.enable_button.pack(side="left")
        self.disable_button = ttk.Button(login_buttons, text="关闭自动共享", command=self.disable_login)
        self.disable_button.pack(side="left", padx=(8, 0))

        ttk.Label(body, text="运行消息").pack(anchor="w", pady=(10, 4))
        log_frame = ttk.Frame(body)
        log_frame.pack(fill="x")
        self.log = tk.Text(log_frame, height=5, wrap="word", state="disabled", takefocus=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="x", expand=True)
        log_scroll.pack(side="right", fill="y")
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
        selection = self.devices.selection()
        return self._rows.get(selection[0]) if selection else None

    def _update_controls(self):
        peer = self._selected()
        has_identity = bool(peer and peer.get("ID"))
        active = bool(self._host_process and self._host_process.poll() is None)
        viewer = self._viewers.get(peer.get("ID")) if peer else None
        viewing = bool(viewer and viewer.poll() is None)
        disabled = self._closing or self._closed
        controls = (
            (self.refresh_button, not self._refreshing),
            (self.connect_button, has_identity and peer.get("Online") and not viewing),
            (self.start_button, has_identity and not active and self._host_state != "stopping"),
            (self.stop_button, active or self._retry_job is not None or self._auto_intent),
            (self.enable_button, has_identity and self._preferences_loaded and not self._preference_busy
             and (not self._saved_peer or self._saved_peer[0] != peer["ID"])),
            (self.disable_button, self._preferences_loaded and bool(self._saved_peer) and not self._preference_busy),
        )
        for button, enabled in controls:
            button.configure(state="normal" if enabled and not disabled else "disabled")
        self.start_button.configure(text="重试启动共享" if self._host_state == "external" else "允许所选设备，开始共享")
        if not peer:
            self.selection_text.set("请选择一台设备。")
        elif not has_identity:
            self.selection_text.set("此设备缺少稳定身份，请刷新设备列表。")
        elif viewing:
            self.selection_text.set("%s 的查看器已打开。" % peer["HostName"])
        else:
            state = "在线，可连接" if peer.get("Online") else "离线，可先授权它控制本机"
            self.selection_text.set("已选择 %s · %s" % (peer["HostName"], state))

    def _update_preferences(self):
        if not self._preferences_loaded:
            self.preference_text.set("正在读取登录设置…")
        elif self._saved_peer:
            self.preference_text.set("已启用：登录后仅允许 %s。本次共享可单独停止。" % self._saved_peer[1])
        else:
            self.preference_text.set("未启用。启用后，本机登录时会打开此面板并共享给指定设备。")

    def refresh(self):
        if self._closed or self._closing or self._refreshing:
            return
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
        if error:
            self._clear_devices()
            self.network_text.set("无法读取设备：" + error)
            self._log("设备刷新失败：" + error)
            self._schedule_retry("等待 Tailscale 就绪")
            self._update_controls()
            return
        selected = self._selected()
        selected_id = selected.get("ID") if selected else None
        preferred_id = selected_id or (self._auto_target[0] if self._auto_target else None)
        self._clear_devices()
        peers = sorted((status.get("Peer") or {}).values(),
                       key=lambda peer: (not peer.get("Online"), peer.get("HostName", "").casefold()))
        preferred_row = None
        for index, peer in enumerate(peers):
            row = "peer-%d" % index
            self._rows[row] = peer
            self.devices.insert("", "end", iid=row, values=(peer.get("HostName", "未命名设备"),
                "在线" if peer.get("Online") else "离线", ", ".join(peer.get("TailscaleIPs") or [])))
            if peer.get("ID") == preferred_id:
                preferred_row = row
        if peers:
            self.devices.selection_set(preferred_row or "peer-0")
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

    def _child_options(self):
        options = {
            "cwd": str(self.script.parent), "shell": False,
            "env": dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1"),
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        return options

    def connect(self):
        peer = self._selected()
        if self._closing or not peer or not peer.get("ID") or not peer.get("Online"):
            return
        existing = self._viewers.get(peer["ID"])
        if existing and existing.poll() is None:
            return
        output = tempfile.TemporaryFile(mode="w+b")
        try:
            process = subprocess.Popen([sys.executable, str(self.script), "connect-id", peer["ID"]],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       **self._child_options())
        except Exception as error:
            output.close()
            self._log("无法打开 %s 的查看器：%s" % (peer["HostName"], error))
            self.selection_text.set("查看器启动失败，请查看运行消息。")
            return
        self._viewers[peer["ID"]] = process
        self._log("正在打开 %s 的远程桌面。" % peer["HostName"])
        self._update_controls()

        def watch():
            try:
                code = process.wait()
                output.seek(0)
                message = output.read()[-12000:].decode("utf-8", errors="replace").strip()
            finally:
                output.close()
            self._messages.put(("viewer_exit", peer["ID"], peer["HostName"], process, code, message))
        threading.Thread(target=watch, name="desktop-viewer-status", daemon=True).start()

    def start_host(self):
        peer = self._selected()
        if self._closing or not peer or not peer.get("ID"):
            return
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
            command = "host-login-id" if automatic else "host-id"
            process = subprocess.Popen([sys.executable, str(self.script), command, peer["ID"]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **self._child_options())
        except Exception as error:
            self._auto_intent = False
            self._auto_target = None
            self._host_state = "failed"
            self.host_text.set("共享启动失败：" + str(error))
            self._log(self.host_text.get())
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
        if "正在共享桌面" in line:
            self._host_state = "running"
            self._retry_attempt = 0
            self.host_text.set("正在共享本机 · 仅允许 %s。" % self._host_peer[1])
        elif "已连接" in line:
            self.host_text.set("%s 正在控制本机。" % self._host_peer[1])
        elif "等待重新连接" in line:
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
            self.host_text.set("共享端口已被其他窗口占用。请到原窗口确认允许的设备或停止共享；此窗口不会停止它。")
            self._log(self.host_text.get())
        elif self._auto_intent and (code == 0 or _transient_host_error(output) or "登录共享授权已变更" in output):
            self._host_state = "stopped"
            self._schedule_retry("共享暂时不可用，等待 Tailscale 恢复")
        else:
            self._host_state = "failed" if code else "stopped"
            self._auto_intent = False
            self._auto_target = None
            detail = next((line for line in reversed(self._host_tail) if line.strip()), "进程已退出（%s）" % code)
            self.host_text.set("共享已停止：%s。修复运行消息中的问题后，可重新开始共享。" % detail)
        self._update_controls()

    def _schedule_retry(self, reason):
        if self._closing or not self._auto_intent or self._retry_job is not None or self._host_process:
            return
        delay = _RETRY_DELAYS[min(self._retry_attempt, len(_RETRY_DELAYS) - 1)]
        self._retry_attempt += 1
        self._retry_deadline = time.monotonic() + delay
        self._retry_reason = reason
        self._host_state = "retry"
        self.host_text.set("%s；%d 秒后重试。可点击“停止本次共享”取消。" % (reason, delay))
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
            elif kind == "viewer_exit":
                identity, name, process, code, text = values
                if self._viewers.get(identity) is process:
                    self._viewers.pop(identity, None)
                self._log(("%s 的连接失败：%s" % (name, text or "进程退出码 %s" % code))
                          if code else "%s 的查看器已关闭。" % name)
                self._update_controls()
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
                self._update_controls()
            elif kind == "stop_error":
                self._log(values[0])
                self.host_text.set(values[0])
        if self._retry_deadline and self._auto_intent and not self._closing:
            seconds = max(0, math.ceil(self._retry_deadline - time.monotonic()))
            self.host_text.set("%s；%d 秒后重试。可点击“停止本次共享”取消。" % (self._retry_reason, seconds))
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
    import desktop_preferences
    from remote_desktop import network_status

    panel = _Panel(network_status, desktop_preferences,
                   Path(__file__).with_name("remote_desktop.py"), tk, ttk, login=login)
    panel.run()


if __name__ == "__main__":
    run_panel(login="--login" in sys.argv[1:])
