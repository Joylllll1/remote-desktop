"""Discover Tailscale devices and verify incoming peer identities."""

import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def executable(name):
    command = shutil.which(name)
    if command:
        return command

    if sys.platform == "darwin":
        app = {"tailscale": "Tailscale", "rustdesk": "RustDesk"}[name]
        for root in (Path("/Applications"), Path.home() / "Applications"):
            binary = root / f"{app}.app" / "Contents" / "MacOS" / app
            if binary.is_file() and os.access(binary, os.X_OK):
                return str(binary)
    if os.name == "nt":
        folder = {"tailscale": "Tailscale", "rustdesk": "RustDesk"}[name]
        for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            if root := os.environ.get(key):
                binary = Path(root) / folder / f"{name}.exe"
                if binary.is_file():
                    return str(binary)
    raise ValueError(f"找不到 {name}。请安装桌面版，或将可执行文件加入 PATH。")


def _run_tailscale(*args, timeout):
    # Finder doesn't supply the shell variables that the macOS app uses to select CLI mode.
    env = dict(os.environ, TAILSCALE_BE_CLI="1") if sys.platform == "darwin" else None
    return subprocess.run(
        [executable("tailscale"), *args], env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def network_status():
    result = _run_tailscale("status", "--json", timeout=10)
    if result.returncode and not result.stdout.lstrip().startswith("{"):
        raise ValueError("无法读取 Tailscale 状态，请打开 Tailscale 并检查连接。")
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        status = None
    if not isinstance(status, dict) or not status.get("BackendState"):
        raise ValueError("Tailscale 没有返回有效的设备状态。请打开 Tailscale，确认已连接后点击「刷新」。")
    state = status.get("BackendState")
    if state != "Running":
        raise ValueError(f"Tailscale 状态为 {state}，请先在 Tailscale 中登录并连接。")
    if result.returncode:
        raise ValueError("读取设备列表失败，请检查 Tailscale 状态。")
    return status


def aliases(peer):
    return [peer["HostName"], peer.get("DNSName", "").rstrip("."), *(peer.get("TailscaleIPs") or [])]


def find_peer(devices, device):
    target = device.rstrip(".").casefold()
    matches = [p for p in devices if target and any(a.casefold() == target for a in aliases(p))]
    if not matches:
        raise ValueError("当前网络中找不到这台设备，请用 list 查看设备名称。")
    if len(matches) > 1:
        raise ValueError("设备名称重复，请使用 list 中的 Tailscale IP。")
    return matches[0]


def address(node):
    addresses = sorted(map(ipaddress.ip_address, node.get("TailscaleIPs") or []), key=lambda ip: ip.version)
    if not addresses:
        raise ValueError("设备没有 Tailscale 地址。")
    return str(addresses[0])


def identify(remote):
    ip, port = remote[:2]
    endpoint = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    result = _run_tailscale("whois", "--json", endpoint, timeout=5)
    if result.returncode:
        return None
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    node = identity.get("Node") if isinstance(identity, dict) else None
    return node.get("StableID") if isinstance(node, dict) else None


def peer_by_id(devices, node_id):
    for peer in devices:
        if node_id and peer.get("ID") == node_id:
            return peer
    raise ValueError("尚未在当前 Tailscale 网络中找到已选设备，请检查设备连接。")
