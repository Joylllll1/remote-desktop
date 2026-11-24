"""通过 Tailscale 控制自己的远程桌面。"""

import argparse
import subprocess
import sys

from remotedesk.host.server import host
from remotedesk.network.client import check_connection, open_viewer_session
from remotedesk.network.tailscale import address, aliases, executable, find_peer, network_status, peer_by_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action")
    gui = sub.add_parser("gui", help="打开设备和共享管理窗口（默认）")
    gui.add_argument("--login", action="store_true", help="使用本机保存的登录共享设置")
    sub.add_parser("list", help="列出当前网络中的其他设备")
    codec_check = sub.add_parser("codec-check", help="用合成画面检查视频组件，不采集屏幕或连接设备")
    codec_check.add_argument("--output", help="将检查结果写入 JSON 文件")
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
    if args.action == "codec-check":
        import json
        from pathlib import Path
        from PIL import Image
        from remotedesk.network.video import VideoEncoder, VideoDecoder
        encoder, decoder = VideoEncoder(), VideoDecoder()
        try:
            for color in ("#285d9c", "#b34e37"):
                with Image.new("RGB", (321, 181), color) as source:
                    with decoder.decode(encoder.encode(source)) as result:
                        if result.size != source.size:
                            raise RuntimeError("视频组件返回了错误的画面尺寸。")
            report = json.dumps({"ok": True, "codec": "H.264 4:4:4", "frames": 2,
                                 "resolution": [321, 181]}, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(report + "\n", encoding="utf-8")
            print(report)
        finally:
            encoder.close()
            decoder.close()
        return
    if args.action in (None, "gui"):
        from remotedesk.ui.panel import run_panel
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
        from remotedesk.login.preferences import login_peer
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
        from remotedesk.ui.viewer import run_viewer
        from remotedesk.network.viewer_lock import viewer_lock
        if not peer.get("ID"):
            raise ValueError("目标设备缺少稳定身份，请刷新设备列表。")
        with viewer_lock(peer["ID"]):
            run_viewer(lambda: open_viewer_session(peer["ID"]), peer["HostName"])
