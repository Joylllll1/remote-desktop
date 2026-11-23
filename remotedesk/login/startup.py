"""Explicit per-user registration of the visible GUI for the next login.

No service is installed, no running application is started or stopped, and
registration is never changed by importing this module or calling enabled().
"""

import json
import ntpath
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

from remotedesk import LAUNCHER, application_command, command_directory


_LABEL = "local.remote-desktop.gui"
_WINDOWS_NAME = "Remote Desktop GUI.lnk"
_LINUX_NAME = "remote-desktop-gui.desktop"
_PS_PREFIX = """
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
"""
_PS_STARTUP_DIRECTORY = """
$directory = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::Startup,
    [Environment+SpecialFolderOption]::DoNotVerify)
if ([String]::IsNullOrEmpty($directory)) { throw 'User Startup folder unavailable' }
[Console]::Write(($directory | ConvertTo-Json -Compress))
"""
_PS_CREATE_SHORTCUT = """
$data = [Console]::In.ReadToEnd() | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut([string]$data.shortcut)
$link.TargetPath = [string]$data.executable
$link.Arguments = [string]$data.arguments
$link.WorkingDirectory = [string]$data.directory
$link.Description = 'Open the visible remote desktop control window'
$link.WindowStyle = 1
$link.Save()
"""


def _command():
    return application_command("gui", "--login", launcher=LAUNCHER)


def _powershell(script, data=None):
    """Run only our fixed script; paths and arguments are JSON data on stdin."""
    executable = ntpath.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    try:
        result = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PS_PREFIX + script],
            input=json.dumps(data or {}), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("无法运行 Windows PowerShell 来配置登录自启。") from error
    if result.returncode:
        raise RuntimeError("配置登录自启失败：" + result.stderr.strip()[:1000])
    return result.stdout


def _entry_path():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / (_LABEL + ".plist")
    if sys.platform == "win32":
        try:
            directory = json.loads(_powershell(_PS_STARTUP_DIRECTORY))
        except (ValueError, TypeError) as error:
            raise RuntimeError("无法读取 Windows 用户的启动文件夹。") from error
        if not isinstance(directory, str) or not directory or not Path(directory).is_absolute():
            raise RuntimeError("Windows 用户的启动文件夹路径无效。")
        return Path(directory) / _WINDOWS_NAME
    if sys.platform.startswith("linux"):
        configured = os.environ.get("XDG_CONFIG_HOME", "")
        root = Path(configured) if configured else Path.home() / ".config"
        if not root.is_absolute():
            root = Path.home() / ".config"
        return root / "autostart" / _LINUX_NAME
    raise RuntimeError("登录自启支持 Windows、macOS 和 Linux 桌面。")


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _desktop_string(value):
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def _desktop_argument(value):
    # Exec quoting is decoded after desktop-entry string escapes, then %% is
    # expanded to a literal %. No shell interprets this command line.
    quoted = "".join("\\" + character if character in '"`$\\' else character for character in value)
    return _desktop_string('"' + quoted.replace("%", "%%") + '"')


def enabled() -> bool:
    """Whether our per-user next-login entry is registered on disk."""
    return _entry_path().is_file()


def install() -> None:
    """Register the existing visible GUI entry point for the next login."""
    command = _command()
    directory = command_directory(command)
    path = _entry_path()
    if sys.platform == "darwin":
        _atomic_write(path, plistlib.dumps({
            "Label": _LABEL,
            "ProgramArguments": command,
            "WorkingDirectory": directory,
            "LimitLoadToSessionType": "Aqua",
            "RunAtLoad": True,
        }))
    elif sys.platform == "win32":
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the unfinished .lnk in a subdirectory, outside the Startup folder's
        # direct entries. Replacing on the same volume preserves any old entry
        # if shortcut creation fails.
        with tempfile.TemporaryDirectory(prefix=".remote-desktop-", dir=path.parent) as staging:
            temporary = Path(staging) / _WINDOWS_NAME
            _powershell(_PS_CREATE_SHORTCUT, {
                "shortcut": str(temporary),
                "executable": command[0],
                "arguments": subprocess.list2cmdline(command[1:]),
                "directory": directory,
            })
            if not temporary.is_file():
                raise RuntimeError("Windows 未生成登录启动快捷方式。")
            os.replace(temporary, path)
    else:
        # The desktop-entry spec forbids '=' in the executable name itself.
        # env -- handles that case without invoking a shell.
        arguments = command if "=" not in command[0] else ["/usr/bin/env", "--", *command]
        text = (
            "[Desktop Entry]\nType=Application\nVersion=1.0\nName=自用远程桌面\n"
            "Comment=登录时打开可见的远程桌面控制窗口\n"
            "Exec=" + " ".join(map(_desktop_argument, arguments)) + "\n"
            "Path=" + _desktop_string(directory).replace(" ", "\\s") + "\n"
            "Terminal=false\nHidden=false\nX-GNOME-Autostart-enabled=true\n"
        )
        _atomic_write(path, text.encode("utf-8"))


def uninstall() -> None:
    """Remove our next-login entry; leave the current visible window running."""
    try:
        _entry_path().unlink()
    except FileNotFoundError:
        pass
