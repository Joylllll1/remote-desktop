"""Remote desktop over Tailscale."""

import ntpath
import os
from pathlib import Path
import sys

LAUNCHER = Path(__file__).resolve().parent.parent / "remote_desktop.py"


def application_command(*arguments, worker=False, launcher=None):
    """Route existing CLI arguments through source Python or the frozen app."""
    if not sys.executable:
        raise RuntimeError("无法确定当前应用程序的位置。")
    # Resolving this symlink would leave the active venv or embedded runtime.
    executable = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        return [executable, *arguments]
    if worker and sys.platform == "win32" and Path(executable).name.casefold() == "pythonw.exe":
        console_python = Path(executable).with_name("python.exe")
        if not console_python.is_file():
            raise RuntimeError("找不到配套的 python.exe，无法启动共享或查看器。请重新安装应用。")
        executable = str(console_python)
    script = Path(LAUNCHER if launcher is None else launcher).resolve()
    if not script.is_file():
        raise RuntimeError("找不到 remote_desktop.py，请重新安装应用或恢复项目文件。")
    return [executable, str(script), *arguments]


def command_directory(command):
    """Derive cwd from the actual entry point, outside a frozen extraction dir."""
    path = command[0 if getattr(sys, "frozen", False) else 1]
    return (ntpath if sys.platform == "win32" else os.path).dirname(path)


def configure_worker_stdio():
    """Captured GUI workers use UTF-8; ordinary terminal commands keep defaults."""
    if os.environ.get("REMOTEDESK_WORKER_STDIO") == "1":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
