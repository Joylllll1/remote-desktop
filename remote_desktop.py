"""Start the remote desktop application."""

import subprocess
import sys

from remotedesk.cli import main


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
