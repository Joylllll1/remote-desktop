"""Open and check viewer connections over the tailnet."""

import io
import socket
import time

from remotedesk.network.session import PORT, Session
from remotedesk.network.tailscale import address


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
