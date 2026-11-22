"""Keep the device selected for login sharing by its Tailscale stable ID."""

import json
import os
from pathlib import Path
import sys
import tempfile

import desktop_startup


def settings_file():
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        root = Path(os.environ["LOCALAPPDATA"])
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not root.is_absolute():
            root = Path.home() / ".config"
    return root / "remote-desktop-personal" / "login-peer.json"


def login_peer():
    if not desktop_startup.enabled():
        return None
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data[key].strip() for key in ("id", "name")
    ):
        raise ValueError("登录共享设置无效，请重新选择允许控制的设备。")
    return data["id"], data["name"]


def enable_login(node_id, name):
    if not all(isinstance(value, str) and value.strip() for value in (node_id, name)):
        raise ValueError("请选择具有 Tailscale 节点身份的设备。")
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False,
    )
    temporary = Path(pending.name)
    try:
        with pending:
            json.dump({"id": node_id, "name": name}, pending, ensure_ascii=False)
        # An old preference without a registered launcher must not be reactivated.
        if not desktop_startup.enabled():
            path.unlink(missing_ok=True)
        desktop_startup.install()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def disable_login():
    # If OS removal fails, a leftover launcher may open the panel, but cannot host.
    settings_file().unlink(missing_ok=True)
    desktop_startup.uninstall()
