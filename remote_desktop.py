"""通过 Tailscale 控制自己的远程桌面。"""

import argparse
from contextlib import suppress
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

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


def peer_by_id(devices, node_id):
    for peer in devices:
        if node_id and peer.get("ID") == node_id:
            return peer
    raise ValueError("尚未在当前 Tailscale 网络中找到已选设备，请检查设备连接。")


def host(self_node, peer, *, panel_input=None):
    from desktop_host import Desktop

    expected = peer.get("ID")
    if not expected:
        raise ValueError("无法确定允许设备的 Tailscale 节点身份。")
    ip = address(self_node)
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    stopped = threading.Event()
    active = None

    def watch_panel():
        try:
            panel_input.read()
        finally:
            stopped.set()
            current = active
            if current is not None:
                with suppress(OSError):
                    current.shutdown(socket.SHUT_RDWR)

    with socket.socket(family, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((ip, PORT))
        listener.settimeout(0.5)
        if panel_input is not None:
            threading.Thread(target=watch_panel, daemon=True).start()
        with Desktop() as desktop:
            desktop.frame()
            listener.listen(2)
            print(f"正在共享桌面 {ip}，仅接受 {peer['HostName']} 的连接。", flush=True)
            while not stopped.is_set():
                try:
                    conn, remote = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    active = conn
                    try:
                        if stopped.is_set():
                            break
                        conn.settimeout(12)
                        authorized = identify(remote) == expected
                        if authorized:
                            print(f"{peer['HostName']} 已连接。", flush=True)
                        serve_connection(conn, authorized, desktop)
                        if authorized:
                            print("会话已结束，等待重新连接。", flush=True)
                    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                        if not stopped.is_set():
                            print(f"连接已关闭：{error}", file=sys.stderr, flush=True)
                    finally:
                        active = None


def connect_to(peer):
    if not peer.get("Online"):
        raise ValueError("目标设备离线，请检查家中电脑的电源和 Tailscale 连接。")
    try:
        sock = socket.create_connection((address(peer), PORT), timeout=12)
    except OSError as error:
        raise ConnectionError(
            f"无法连接 {peer['HostName']} 的桌面服务。请检查对方是否已开启共享、"
            f"Tailscale 访问规则和防火墙是否允许 TCP {PORT}。系统提示：{error}"
        ) from error
    return Session(sock)


def check_connection(peer):
    from PIL import Image

    for attempt in ("首次连接", "重新连接"):
        started = time.monotonic()
        session = connect_to(peer)
        try:
            frame = session.exchange([])
            with Image.open(io.BytesIO(frame), formats=("JPEG",)) as image:
                image.load()
                width, height = image.size
        finally:
            session.close()
        elapsed = round((time.monotonic() - started) * 1000)
        print(f"{attempt}成功，已通过授权并收到 {width}×{height} 桌面画面，用时 {elapsed} 毫秒。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action")
    gui = sub.add_parser("gui", help="打开设备和共享管理窗口（默认）")
    gui.add_argument("--login", action="store_true", help="使用本机保存的登录共享设置")
    sub.add_parser("list", help="列出当前网络中的其他设备")
    host_cmd = sub.add_parser("host", help="共享当前已登录桌面")
    host_cmd.add_argument("--allow", required=True, metavar="DEVICE", help="允许控制本机的 Tailscale 设备")
    connect = sub.add_parser("connect", help="在窗口中打开远程桌面")
    connect.add_argument("device", help="目标设备名称、完整 DNS 名或 Tailscale IP")
    connect.add_argument("--rustdesk", action="store_true", help="使用已配置的 RustDesk 客户端")
    sub.add_parser("check", help="验证设备授权、画面传输和重新连接").add_argument("device")
    # The GUI pins the original device identity even if DNS or a hostname is reused.
    sub.add_parser("connect-id", help="按节点 ID 打开远程桌面").add_argument("node_id")
    sub.add_parser("host-id", help="为面板选定的节点共享桌面").add_argument("node_id")
    sub.add_parser("host-login-id", help="按本机登录设置恢复共享").add_argument("node_id")
    args = parser.parse_args()
    if args.action in (None, "gui"):
        from desktop_panel import run_panel
        run_panel(login=getattr(args, "login", False))
        return

    status = network_status()
    devices = sorted((status.get("Peer") or {}).values(), key=lambda p: p["HostName"].casefold())
    if args.action == "list":
        if not devices:
            print("当前网络中没有其他可见设备，请确认两台电脑已加入同一网络。")
        for peer in devices:
            print("在线" if peer.get("Online") else "离线", *filter(None, aliases(peer)), sep="  ")
        return
    if args.action in ("host-id", "host-login-id", "connect-id"):
        peer = peer_by_id(devices, args.node_id)
    else:
        peer = find_peer(devices, args.allow if args.action == "host" else args.device)
    if args.action == "host-login-id":
        from desktop_preferences import login_peer
        current = login_peer()
        if not current or current[0] != peer["ID"]:
            raise ValueError("登录共享授权已变更，请在设备面板中查看当前设置。")
    if args.action in ("host", "host-id", "host-login-id"):
        host(status["Self"], peer, panel_input=None if args.action == "host" else sys.stdin)
    elif args.action == "check":
        check_connection(peer)
    elif getattr(args, "rustdesk", False):
        if not peer.get("Online"):
            raise ValueError("目标设备离线，请检查家中电脑的电源和 Tailscale 连接。")
        ip = address(peer)
        endpoint = ip if ":" not in ip else f"[{ip}]:21118"
        subprocess.Popen([executable("rustdesk"), "--connect", endpoint])
    else:
        from desktop_viewer import run_viewer
        session = connect_to(peer)
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
