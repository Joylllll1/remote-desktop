# 自用远程桌面

从外面的 Mac 查看和控制家中 Windows 或 Linux 已登录的桌面。两台电脑运行同一份程序。Tailscale 负责设备身份和加密传输，项目的被控端采集屏幕并执行键鼠操作，控制端在本地窗口里显示画面。

## 两台电脑的准备工作

安装 [Tailscale](https://tailscale.com/download)，加入同一个私人网络并连接。安装带 Tk 的 Python 3.12，把本项目放到两台电脑上，在各自的项目目录创建虚拟环境。

macOS / Linux：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

以下示例使用已激活的环境。Windows 可把 `python` 换成 `.\.venv\Scripts\python.exe`。控制端需能运行 `python -m tkinter`。若缺少 Tk，Linux 使用发行版提供的 Python Tk 包，Mac 可使用 python.org 的带 Tk 安装包。

## 出门前在家里电脑上开启共享

```sh
python remote_desktop.py list
python remote_desktop.py host --allow travel-laptop
```

把 `travel-laptop` 换成外出电脑在列表中的名称。也可以填写完整 DNS 名或 Tailscale IP。被控端在接受连接前通过本机 Tailscale 的 `whois` 核对来源设备的稳定节点 ID。授权以设备为单位，持有该电脑并能使用其 Tailscale 连接的人可以控制会话。

看到“正在共享桌面”后保留这个终端，让电脑保持登录、联网和唤醒。会话断开后可以重新连接。按 Ctrl+C 即可停止共享。注销、重启或关闭这个进程后需要手动重新启动。

- **macOS 被控端**：在系统设置的「隐私与安全性」中，给运行程序的终端或 Python 授予屏幕录制、辅助功能权限，更改后重启终端。文件保险箱的开机解锁界面不在支持范围内。
- **Windows 被控端**：从已登录用户的桌面运行程序。首版针对常规应用窗口，UAC 安全桌面和登录界面不在支持范围内。
- **Linux 被控端**：使用已登录的 X11 图形会话。Wayland 被控端暂未支持。

## 在外面的 Mac 上连接 Windows 或 Linux

```sh
python remote_desktop.py list
python remote_desktop.py connect home-windows
# 连接另一台 Linux 电脑时改用它的名称
python remote_desktop.py connect home-linux
```

两条连接命令按需选一条，用列表中的实际名称替换示例名。远程窗口显示一块显示器的画面，支持点击、拖动、滚动、键盘输入。在 Mac 上控制 Windows 或 Linux 时，复制、粘贴使用被控系统的 Ctrl+C 和 Ctrl+V，Mac 的 Command 键作为系统键发送。关闭窗口结束本次会话。若此前已经配置了 RustDesk，可用 `connect home-pc --rustdesk` 启动那个客户端。

要反向控制，在新的被控机运行 `host --allow 控制机名称`，在控制机运行 `connect 被控机名称`。例如用 Linux 控制 Mac 时，先给 Mac 授予屏幕录制和辅助功能权限。

## 用手机热点验收

1. 家中电脑打开一个空白文本编辑器，保持共享进程运行。
2. 外出电脑断开家里的 Wi-Fi，连接手机热点，然后运行 `connect`。
3. 在远程窗口点击文本编辑器，输入几行文字，拖动选中一段。确认操作结果出现在传回的画面里。
4. 关闭远程窗口后再次连接，确认家中无人操作也能恢复会话。

若提示 `Stopped`，先连接 Tailscale。若显示在线但连接超时，核对被控端是否正在共享，以及主机防火墙和 Tailscale 访问规则是否允许外出电脑访问被控端的 TCP 47650。服务监听家中电脑的 Tailscale 地址，路由器无需配置端口转发。若提示设备未获授权，检查家中终端中 `--allow` 后面的设备名。

[Tailscale 连接方式](https://tailscale.com/docs/reference/connection-types) · [pynput 平台限制](https://pynput.readthedocs.io/en/latest/limitations.html)

## 当前验证范围

| 路径 | 已验证内容 |
| --- | --- |
| Mac 控制 Linux X11 | 在隔离虚拟网络中，Mac 实际控制窗口显示 Linux 的 800×600 桌面。在该窗口执行的点击和输入传到 Linux，文本框记录坐标 (200, 300) 和文字 `world`。 |
| Mac 控制 Windows | 已实现被控端代码，待 Windows 实机验证。 |
| 反向控制 Mac | 已验证本机屏幕采集和权限检查，完整键鼠会话待具备辅助功能权限的 Mac 验证。 |
| 通过手机热点访问家庭 Tailscale 网络 | 待实际设备联网验收。 |

运行 `python -m unittest discover -v` 可复验本地协议的基本行为，包括拒绝未授权会话、断线释放按键和限制单批事件数量。
