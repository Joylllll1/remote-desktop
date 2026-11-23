<h1 align="center">自用远程桌面</h1>

<p align="center">在外也能通过 Tailscale 连接家里的电脑，用键盘和鼠标操作它的桌面。</p>
<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB" alt="Python 3.12">
  <img src="https://img.shields.io/badge/GUI-Tkinter-456B59" alt="Tkinter 图形界面">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#共享与授权">共享与授权</a> ·
  <a href="#平台限制">平台限制</a> ·
  <a href="#命令与排障">命令与排障</a> ·
  <a href="#目录结构">目录结构</a>
</p>

两台电脑运行同一份程序。家里的电脑开启共享，随身电脑打开查看窗口。要反向控制，交换两台电脑的操作即可。设备面板会列出同一 Tailscale 网络中可见的设备及其在线状态。

## 快速开始

在两台电脑上安装并连接 [Tailscale](https://tailscale.com/download)，加入同一个私人网络。准备带 Tkinter 的 Python 3.12，把项目源码放到两台电脑上。以下命令都在各自包含 `remote_desktop.py` 和 `requirements.txt` 的目录中执行。

**macOS 或 Linux 终端**

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python remote_desktop.py
```

**Windows PowerShell**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe remote_desktop.py
```

两边都出现设备面板后：

1. **在家里的电脑上**，选中随身电脑，点击「允许所选设备，开始共享」。看到「正在共享本机」后保留这个窗口。
2. **在随身电脑上**，选中家里的电脑，点击「连接所选设备」。新窗口会显示家中电脑的主屏幕，点进画面即可输入、拖动或滚动。
3. 关闭查看窗口会结束本次连接，之后可以重新连入。要结束共享，在家里的面板中点击「停止本次共享」。

## 共享与授权

每次开启共享时指定一台允许连接的设备。被控端通过 Tailscale 核对来源设备的稳定节点 ID，同名设备不能继承授权。

如果希望重启后继续使用，在被控端选中控制设备，在「登录后自动共享」中点击「启用所选设备」。下次用户登录图形桌面时，面板会打开并恢复共享。默认需要手动开启。移动项目或虚拟环境后需重新设置登录启动项。

## 平台限制

被控端需要开机、联网、保持唤醒，并有已登录的图形桌面。

| 被控端 | 首次使用须知 |
| --- | --- |
| macOS | 在「系统设置 → 隐私与安全性」中，为运行程序的终端或 Python 授予屏幕录制和辅助功能权限，然后重新打开程序。 |
| Windows | 在已登录用户的桌面上运行。登录界面和 UAC 安全桌面不在控制范围内。 |
| Linux | 使用 X11 桌面会话。当前被控端不支持 Wayland。 |

Mac 控制 Linux X11 已在隔离环境中验证。Windows 后端已有模拟测试，Mac 控制 Windows 实机、反向控制 Mac 的完整键鼠会话，以及各系统重启登录后的恢复仍待实机验证。

## 命令与排障

下表中的参数接在 `remote_desktop.py` 后面，解释器沿用上面的启动命令。将 `travel-laptop` 和 `home-pc` 换成设备列表中的实际名称。重名时可用完整 DNS 名或 Tailscale IP。

| 参数 | 执行位置和结果 |
| --- | --- |
| `list` | 任一端。列出可见设备的名称、地址和在线状态。 |
| `host --allow travel-laptop` | 被控端。只允许指定电脑连接，按 Ctrl+C 停止共享。 |
| `connect home-pc` | 控制端。打开远程桌面窗口。若已另行配置 RustDesk，可追加 `--rustdesk` 调用其客户端。 |
| `check home-pc` | 控制端。连续连接两次，分别接收并解码一帧画面，输出分辨率和耗时，全程不发送键鼠事件。 |

**设备在线但连不上时**，先确认被控端面板显示「正在共享本机」，且允许的是当前控制设备。连接超时再检查主机防火墙和 Tailscale 访问规则是否允许控制端访问被控端的 TCP 47650。提示 `Stopped` 时先连接 Tailscale。若同时使用其他 VPN，可参考 [Tailscale 的共存说明](https://tailscale.com/docs/reference/faq/other-vpns)。

**出门前验证外网连接**，可以先让随身电脑切换到手机热点，运行 `check home-pc`。看到「首次连接成功」和「重新连接成功」后，打开远程窗口，在家中电脑的空白文本编辑器里输入一行字，确认画面和键鼠都可用。

完整命令选项见 `remote_desktop.py --help`。在项目目录中，用同一虚拟环境的 Python 执行 `-m unittest discover -v` 可运行仓库测试。[依赖清单](requirements.txt) · [会话协议测试](tests/test_session.py)

## 目录结构

```text
remote_desktop.py       启动图形面板或执行命令
remotedesk/
├── cli.py              解析命令，调用控制端或被控端
├── ui/                 设备面板和远程桌面窗口
├── host/               共享服务、屏幕采集和键鼠输入
├── network/            Tailscale 设备发现、连接和会话协议
└── login/              登录共享设置和系统自启动项
tests/                  会话、授权、登录启动和平台行为测试
```

## 许可证

[MIT 许可证](LICENSE)
