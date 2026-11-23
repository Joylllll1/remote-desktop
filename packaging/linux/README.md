# Linux amd64 installer

Install on Ubuntu 24.04 LTS amd64 or Debian 13 amd64 with the desktop package installer, or:

    sudo apt install ./RemoteDesktop-Linux-amd64.deb

Apt installs the system Python and Tk GUI runtime automatically. The package recommends fonts-noto-cjk, which apt installs by default to render the Chinese interface on an English Linux installation. End users never run pip or create a virtual environment. Launch “Remote Desktop / 远程桌面” from the application menu, or run `remote-desktop`. Tailscale must separately be installed, signed in and connected. Hosting requires an unlocked, logged-in X11 session; Wayland hosting is unsupported. Login sharing remains an explicit opt-in from the panel. Installation itself does not register startup, change Tailscale, open a port, or start sharing.

Remove with `sudo apt remove remote-desktop-personal`. Disable automatic login sharing in the panel before removal; package removal deliberately preserves personal preferences and startup choices. Those entries reference the removed program and become inactive until it is reinstalled.

## Build

    python3 packaging/linux/build.py

The builder needs Python with pip and internet access to download six pinned wheels from PyPI; no Debian tooling or root is needed. For an offline build, pass `--wheel-dir /path/to/wheels`. SHA-256 digests are checked before extraction. Output: `dist/RemoteDesktop-Linux-amd64.deb`.

The package contains unchanged application sources plus an installed root `remote_desktop.py` bootstrap. It selects the CPython 3.12 or 3.13 Pillow binary, loads the bundled pure Python libraries, then executes the original root launcher. `remotedesk.LAUNCHER`, child-process launches, and login startup all return through that bootstrap. `sys.executable` remains the actual system interpreter.

Bundled versions: MSS 10.2.0, pynput 1.8.2, Pillow 13.0.0, python-xlib 0.33, six 1.17.0. Wheel metadata and upstream licenses are retained under `/opt/remote-desktop/vendor`. Pynput's optional uinput/evdev backend is not included: this application explicitly uses X11. Pillow wheels bundle their native codec libraries and require glibc 2.27 or later; package dependencies conservatively require glibc 2.28. Native X11/Tk libraries are apt dependencies.

Ubuntu 22.04 and Debian 12 are rejected by Python version dependencies. ARM64, newer Python defaults and non-Debian distributions need separate packages. Ubuntu 24.04 ships Python/Tk 3.12; Debian 13 ships Python/Tk 3.13. This is a system-dependency installer, not a bundled CPython distribution.

Sources: https://packages.ubuntu.com/noble/python3-tk ; https://packages.debian.org/trixie/python3-tk ; https://pypi.org/project/mss/10.2.0/ ; https://pypi.org/project/pynput/1.8.2/ ; https://pypi.org/project/pillow/13.0.0/

## Local verification (0.1.0)

Installed the actual amd64 package with apt in two fresh, disposable official cloud-image VMs under QEMU:

- Debian 13: Python 3.13.5, system python3-tk 3.13.5-1.
- Ubuntu 24.04.5 LTS: Python 3.12.3, system python3-tk 3.12.3-0ubuntu1.

Both passed dependency installation, menu-entry validation (`desktop-file-validate`), native MSS capture plus pynput keyboard and mouse under an isolated Xvfb X11 display, actual Pillow 13 JPEG decoding (800×600), ImageTk display construction, installed public GUI panel open/close, temporary login enable/disable registration, and launching a subprocess through the installed root bootstrap. No screen or input from the macOS host was used. This verifies application/runtime behavior on X11; it does not test Wayland, a physical GPU, a complete GNOME/KDE menu session, or a real Tailscale connection. Native Tailscale/network behavior is outside installer verification.

The .deb includes all native library dependencies required by MSS's default XCB backend (xcb, randr, render, shm, xfixes), plus Xlib/XTest and the Xlib fallback's Xext/Xfixes/Xrandr libraries. A source archive comparison verified every installed application `.py` file against the repository version. Versioned Pillow wheels prevent a CPython ABI mismatch. The bootstrap avoids writing application bytecode; source-derived archive timestamps also invalidate stale cached bytecode on upgrades. The output is replaced atomically after both package archives have been built.
