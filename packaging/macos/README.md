# macOS 安装包

在 Mac 上使用带 Tk 的 Python 3.12 构建。以下命令在仓库根目录执行。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt 'PyInstaller==6.22.0'
PYTHON=.venv/bin/python bash packaging/macos/build.sh
```

输出为 `dist/RemoteDesktop-macOS-arm64.dmg` 或 `dist/RemoteDesktop-macOS-x86_64.dmg`，取决于构建解释器的架构。要在 Apple 芯片 Mac 上构建 Intel 版，使用通过 Rosetta 运行的 x86_64 Python 环境并将 `PYTHON` 指向它。两个架构分别安装对应的依赖。

脚本生成包含 Python、Tk、桌面捕获和输入库的应用，校验本地签名，再制作并校验磁盘镜像。安装时将应用拖到“应用程序”。应用使用本地 ad hoc 签名，尚未经过 Apple 公证；开启共享时需要为应用授予屏幕录制和辅助功能权限。用作控制端时，直接在设备面板中选择对端连接。
