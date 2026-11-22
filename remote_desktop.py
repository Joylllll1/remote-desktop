"""通过 Tailscale 控制自己的远程桌面。"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from desktop_session import PORT, Session, serve_connection


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
                            capture_output=True, timeout=5)
    if result.returncode:
        return None
    return (json.loads(result.stdout).get("Node") or {}).get("StableID")


def host(self_node, peer):
    from desktop_host import Desktop

    expected = peer.get("ID")
    if not expected:
        raise ValueError("无法确定允许设备的 Tailscale 节点身份。")
    ip = address(self_node)
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((ip, PORT))
        with Desktop() as desktop:
            desktop.frame()  # Verify screen access while the owner is still at home.
            listener.listen(2)
            print(f"正在共享桌面 {ip}，仅接受 {peer['HostName']} 的连接。按 Ctrl+C 停止。", flush=True)
            while True:
                conn, remote = listener.accept()
                with conn:
                    conn.settimeout(12)
                    try:
                        authorized = identify(remote) == expected
                        if authorized:
                            print(f"{peer['HostName']} 已连接。", flush=True)
                        serve_connection(conn, authorized, desktop)
                        if authorized:
                            print("会话已结束，等待重新连接。", flush=True)
                    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                        print(f"连接已关闭：{error}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="列出当前网络中的其他设备")
    host_cmd = sub.add_parser("host", help="共享当前已登录桌面")
    host_cmd.add_argument("--allow", required=True, metavar="DEVICE", help="允许控制本机的 Tailscale 设备")
    connect = sub.add_parser("connect", help="在窗口中打开远程桌面")
    connect.add_argument("device", help="目标设备名称、完整 DNS 名或 Tailscale IP")
    connect.add_argument("--rustdesk", action="store_true", help="使用已配置的 RustDesk 客户端")
    args = parser.parse_args()
    status = network_status()
    devices = sorted((status.get("Peer") or {}).values(), key=lambda p: p["HostName"].casefold())
    if args.action == "list":
        if not devices:
            print("当前网络中没有其他可见设备，请确认两台电脑已加入同一网络。")
        for peer in devices:
            print("在线" if peer.get("Online") else "离线", *filter(None, aliases(peer)), sep="  ")
        return
    if args.action == "host":
        host(status["Self"], find_peer(devices, args.allow))
        return
    peer = find_peer(devices, args.device)
    if not peer.get("Online"):
        raise ValueError("目标设备离线，请检查家中电脑的电源和 Tailscale 连接。")
    ip = address(peer)
    if args.rustdesk:
        endpoint = ip if ":" not in ip else f"[{ip}]:21118"
        subprocess.Popen([executable("rustdesk"), "--connect", endpoint])
        return
    from desktop_viewer import run_viewer

    with socket.create_connection((ip, PORT), timeout=12) as sock:
        session = Session(sock)
        try:
            run_viewer(session, peer["HostName"])
        finally:
            session.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("远程桌面已停止。")
    except subprocess.TimeoutExpired:
        sys.exit("Tailscale 响应超时，请检查本机服务。")
    except ImportError as error:
        sys.exit(f"缺少运行组件，请按 README 安装依赖：{error}")
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        sys.exit(str(error))
