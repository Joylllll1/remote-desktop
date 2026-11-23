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


def network_status():
    result = subprocess.run(
        [executable("tailscale"), "status", "--json"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode and not result.stdout.lstrip().startswith("{"):
        raise ValueError("无法读取 Tailscale 状态，请打开 Tailscale 并检查连接。")
    status = json.loads(result.stdout)
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
    result = subprocess.run([executable("tailscale"), "whois", "--json", endpoint],
                            capture_output=True, timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        return None
    return (json.loads(result.stdout).get("Node") or {}).get("StableID")


def peer_by_id(devices, node_id):
    for peer in devices:
        if node_id and peer.get("ID") == node_id:
            return peer
    raise ValueError("尚未在当前 Tailscale 网络中找到已选设备，请检查设备连接。")
