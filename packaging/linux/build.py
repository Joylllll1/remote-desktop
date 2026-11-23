#!/usr/bin/env python3
"""Build an amd64 Debian package on macOS or Linux without root/dpkg tools."""
import argparse
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
WHEELS = {
    'mss-10.2.0-py3-none-any.whl': 'e79f428899280e7e64e38365b5bfed683851ebea807eeaeadaf06eb8e0d67197',
    'pynput-1.8.2-py2.py3-none-any.whl': '8cc38cf13a6ab2749cb375678be8a0fd705d7ce49c8001ff5db4007a723bbef1',
    'python_xlib-0.33-py2.py3-none-any.whl': 'c3534038d42e0df2f1392a1b30a15a4ff5fdc2b86cfa94f072bf11b10a164398',
    'six-1.17.0-py2.py3-none-any.whl': '4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274',
    'pillow-13.0.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl': '42d63be3af4f9b951b199b0a9d23365d37ab14687e6d77b74940432c8515c4ca',
    'pillow-13.0.0-cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl': '5f56499c1a74d2b862beb0c33b8016f99d095858455df9d21583bbbc1c15b582',
}


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    path.chmod(mode)


def archive(root, epoch):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:xz', format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(root.rglob('*')):
            info = tar.gettarinfo(str(path), arcname='./' + path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            info.mtime = epoch
            if path.is_file():
                with path.open('rb') as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
    return output.getvalue()


def ar_member(name, data):
    fields = (name + '/', '0', '0', '0', '100644', str(len(data)))
    widths = (16, 12, 6, 6, 8, 10)
    header = ''.join(value.ljust(width) for value, width in zip(fields, widths)).encode('ascii') + b'`\n'
    return header + data + (b'\n' if len(data) % 2 else b'')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel-dir', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'dist/RemoteDesktop-Linux-amd64.deb')
    parser.add_argument('--version', default='0.1.2')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='remote-desktop-deb-') as temporary:
        work = Path(temporary)
        wheels = args.wheel_dir or work/'wheels'
        wheels.mkdir(parents=True, exist_ok=True)
        if not args.wheel_dir:
            for version in ('312', '313'):
                packages = ['pillow==13.0.0']
                if version == '312':
                    packages += ['mss==10.2.0', 'pynput==1.8.2', 'python-xlib==0.33', 'six==1.17.0']
                subprocess.run([sys.executable, '-m', 'pip', 'download', '--no-deps', '--only-binary=:all:',
                    '--platform', 'manylinux_2_28_x86_64', '--python-version', version,
                    '--implementation', 'cp', '--abi', 'cp'+version, '--dest', str(wheels), *packages], check=True)
        payload = work/'data'
        app = payload/'opt/remote-desktop'
        app.mkdir(parents=True)
        shutil.copytree(ROOT/'remotedesk', app/'remotedesk', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(ROOT/'remote_desktop.py', app/'app_entry.py')
        # This installed root launcher is also LAUNCHER for child processes and
        # login registration. The real /usr/bin/python3 remains sys.executable.
        shutil.copy2(Path(__file__).with_name('bootstrap.py'), app/'remote_desktop.py')
        for filename, digest in WHEELS.items():
            wheel = wheels/filename
            if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest:
                raise ValueError('Wheel checksum mismatch: ' + filename)
            subdirectory = 'python3.12' if '-cp312-' in filename else 'python3.13' if '-cp313-' in filename else 'common'
            destination = app/'vendor'/subdirectory
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(wheel) as bundle:
                for member in bundle.infolist():
                    relative = Path(member.filename)
                    if relative.is_absolute() or '..' in relative.parts:
                        raise ValueError('Unsafe wheel path')
                bundle.extractall(destination)
        write(payload/'usr/bin/remote-desktop', '#!/bin/sh\nexec /usr/bin/python3 -I /opt/remote-desktop/remote_desktop.py "$@"\n', 0o755)
        shutil.copytree(Path(__file__).with_name('assets'), payload/'usr/share', dirs_exist_ok=True)
        shutil.copy2(ROOT/'LICENSE', app/'LICENSE')
        write(payload/'usr/share/doc/remote-desktop-personal/copyright', (ROOT/'LICENSE').read_text() + '\nBundled dependencies retain their upstream license files under /opt/remote-desktop/vendor.\n')
        write(payload/'usr/share/doc/remote-desktop-personal/README', Path(__file__).with_name('README.md').read_text())
        write(payload/'usr/share/doc/remote-desktop-personal/vendor-sha256.txt', ''.join(f'{digest}  {name}\n' for name, digest in WHEELS.items()))
        control = work/'control'
        size = sum(p.stat().st_size for p in payload.rglob('*') if p.is_file())
        write(control/'control', f'''Package: remote-desktop-personal
Version: {args.version}
Section: net
Priority: optional
Architecture: amd64
Maintainer: Remote Desktop maintainers <noreply@example.invalid>
Installed-Size: {(size + 1023)//1024}
Depends: python3 (>= 3.12), python3 (<< 3.14), python3-tk, libc6 (>= 2.28), libx11-6, libxtst6, libxext6, libxfixes3, libxrandr2, libxcb1, libxcb-randr0, libxcb-render0, libxcb-shm0, libxcb-xfixes0
Recommends: fonts-noto-cjk
Suggests: tailscale
Description: Visible personal remote desktop over Tailscale
 Device chooser, viewer and explicitly owned sharing controls.
 Python and Tk are installed automatically by apt. Capture and input libraries
 are bundled. A logged-in X11 session is required for hosting.
''')
        write(control/'md5sums', ''.join(hashlib.md5(p.read_bytes()).hexdigest()+'  '+p.relative_to(payload).as_posix()+'\n' for p in sorted(payload.rglob('*')) if p.is_file()))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Source-derived timestamps invalidate old Python bytecode after upgrades.
        # SOURCE_DATE_EPOCH can pin a release build for reproducibility.
        sources = [Path(__file__), Path(__file__).with_name('bootstrap.py'), ROOT/'remote_desktop.py',
                   *(ROOT/'remotedesk').rglob('*.py')]
        epoch = int(os.environ.get('SOURCE_DATE_EPOCH', max(p.stat().st_mtime for p in sources)))
        package = (b'!<arch>\n' + ar_member('debian-binary', b'2.0\n')
                   + ar_member('control.tar.xz', archive(control, epoch))
                   + ar_member('data.tar.xz', archive(payload, epoch)))
        descriptor, pending = tempfile.mkstemp(prefix='.'+args.output.name+'.', dir=args.output.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(package)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(pending, 0o644)
            os.replace(pending, args.output)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        print(f'Built {args.output} ({args.output.stat().st_size:,} bytes)')

if __name__ == '__main__':
    main()
