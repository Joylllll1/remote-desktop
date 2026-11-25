"""Build with a native macOS Python 3.12 installation that includes Tk."""
from pathlib import Path
import platform
import sysconfig
from importlib.metadata import distribution
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve().parents[1]
notices = [(str(root / 'LICENSE'), 'licenses/RemoteDesktop')]
notices.append((str(root / 'packaging/codec-licenses'), 'licenses/codecs'))
python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
if python_license.is_file():
    notices.append((str(python_license), 'licenses/Python'))
for name in ('mss', 'Pillow', 'pynput', 'six', 'av', 'pyobjc-core', 'pyobjc-framework-Cocoa',
             'pyobjc-framework-Quartz', 'pyobjc-framework-ApplicationServices', 'pyobjc-framework-CoreText'):
    dist = distribution(name)
    for item in dist.files or ():
        if '.dist-info/' in str(item) and item.name.lower().startswith(('license', 'copying')):
            notices.append((str(dist.locate_file(item)), 'licenses/' + name))

# PyAV imports compiled modules dynamically; its wheel also keeps FFmpeg and
# codec libraries in av/.dylibs. Retain the complete package and native closure.
av_datas, av_binaries, av_hiddenimports = collect_all('av', include_py_files=False)

analysis = Analysis(
    [str(root / 'remote_desktop.py')], pathex=[str(root)], datas=notices + av_datas,
    binaries=av_binaries,
    hiddenimports=['pynput.keyboard._darwin', 'pynput.mouse._darwin', 'mss.darwin',
                   'PIL.JpegImagePlugin', 'PIL.PngImagePlugin', 'PIL._imagingtk'] + av_hiddenimports,
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], name='RemoteDesktop',
                 exclude_binaries=True, console=False, upx=False)
files = COLLECT(executable, analysis.binaries, analysis.datas, name='RemoteDesktop', upx=False)
app = BUNDLE(
    files, name='RemoteDesktop.app', icon=str(root / 'packaging/icons/RemoteDesktop.icns'),
    bundle_identifier='io.github.joylllll1.remote-desktop',
    info_plist={
        'CFBundleDisplayName': '自用远程桌面',
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '14.0' if platform.machine() == 'arm64' else '11.0',
        'NSScreenCaptureUsageDescription': '共享时采集本机屏幕，发送给已授权的 Tailscale 设备。',
        'CFBundleShortVersionString': '0.1.7',
        'CFBundleVersion': '13',
    },
)
