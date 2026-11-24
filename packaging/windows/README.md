# Windows x64 installer

`build.py` creates `dist/RemoteDesktop-Windows-x64.exe` locally on macOS or Linux. It includes the application source, relocatable CPython 3.12.16 with Tk/Tcl, Microsoft C runtime DLLs, and exact Windows-compatible wheels. The installer performs no downloads, uses the current user's LocalAppData by default, creates visible GUI shortcuts, and provides a user-level uninstaller.

The NSIS installer bootstrap itself is PE32, as usual for NSIS; the Python runtime and every bundled native application component are AMD64. The installer rejects 32-bit Windows and requires Windows 10 or later.

On macOS:

```sh
brew install makensis sevenzip
python3 packaging/windows/build.py
```

The first build downloads only the official HTTPS URLs pinned in `lock.json` and checks every SHA-256 digest before extraction. Rebuild without network access with:

```sh
python3 packaging/windows/build.py --offline
```

The lock pins the PBS release and archive digest plus every wheel version, URL and digest. Input bytes are reproducible; NSIS timestamps and compiler versions can still make the resulting executable differ between builds. NSIS 3.13 was used for the first build. No Windows VM, Wine or Windows SDK is needed to compile this installer.

`runtime/pythonw.exe remote_desktop.py gui` opens the visible application window without a console. The application must use sibling `runtime/python.exe` for children that exchange stdin/stdout; the `pythonw.exe` process's standard streams can be `None`. Python paths retain the runtime's local layout, and Tk/Tcl support files remain in the distribution's original directories. The installer does not register login startup or enable desktop sharing.

All shortcuts and uninstall entries belong to the current user. The uninstaller uses a generated list of app-owned files, removes only known Python bytecode cache files and empty known directories, and never recursively removes the selected installation root. User configuration remains. Tailscale is a separately installed prerequisite; it is not downloaded or installed by this package.

The builder validates PE architecture and launcher subsystems, Tk/Tcl and Pillow components, PyAV codec extensions, the recursive FFmpeg/codec DLL import closure (including delayed imports), source syntax, NSIS archive integrity, non-admin execution metadata, and hashes of every file extracted from the finished installer. The PyAV check permits only explicitly listed Windows system DLLs and Windows API sets outside the bundled DLLs. Results and artifact SHA-256 are recorded in `.build/build-report.json`. This structural verification does not replace a Windows test of installation, shortcuts, GUI, sharing and uninstall. The installer is unsigned.

PyAV 16.1.0 bundles FFmpeg 8.0.1 and GPL-licensed x264/x265 libraries. Their notices are retained in `licenses/codecs` and the original wheel metadata stays in `runtime/Lib/site-packages`. Publish `RemoteDesktop-Codec-Sources.zip` alongside the installer, following the [codec source and license instructions](../codec-licenses/README.md). The project's MIT license does not replace these dependency licenses.

Official references: [PBS release](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110), [PBS runtime documentation](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst), [Tcl/Tk distribution layout](https://gregoryszorc.com/docs/python-build-standalone/20240224/quirks.html), [NSIS manual](https://nsis.sourceforge.io/Docs/), and package metadata on [PyPI](https://pypi.org/).

Build caches and expanded payloads stay in this directory's ignored `.cache/` and `.build/` folders. They are not needed to use the installer.
