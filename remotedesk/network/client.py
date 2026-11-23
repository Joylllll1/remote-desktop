"""Open and check viewer connections over the tailnet."""

import io
import socket
import subprocess
import time

from remotedesk.network.session import PORT, Session
from remotedesk.network.tailscale import address, network_status, peer_by_id


def connect_to(peer):
    if not peer.get("Online"):
        raise ConnectionError("目标设备离线，请检查家中电脑的电源和 Tailscale 连接。")
    try:
        sock = socket.create_connection((address(peer), PORT), timeout=12)
    except TimeoutError as error:
        raise TimeoutError(f"连接 {peer['HostName']} 的共享端口 TCP {PORT} 超时。") from error
    except OSError as error:
        raise ConnectionError(
            f"无法连接 {peer['HostName']} 的桌面服务。请检查对方是否已开启共享、"
            f"Tailscale 访问规则和防火墙是否允许 TCP {PORT}。系统提示：{error}"
        ) from error
    return Session(sock)


def open_viewer_session(node_id):
    """Resolve the same device again after network changes, before each attempt."""
    try:
        status = network_status()
    except (ValueError, subprocess.TimeoutExpired) as error:
        raise ConnectionError(f"暂时无法读取 Tailscale 设备列表：{error}") from error
    try:
        peer = peer_by_id((status.get("Peer") or {}).values(), node_id)
    except ValueError as error:
        raise ConnectionError("当前 Tailscale 设备列表中暂时看不到选定的电脑。") from error
    return connect_to(peer)


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
