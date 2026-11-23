#!/bin/bash
# Use a macOS Python 3.12 with Tk, runtime dependencies and PyInstaller installed.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-python3}"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
arch="$("$python" -c 'import platform; print(platform.machine())')"
"$python" -m PyInstaller --noconfirm --distpath "$build_dir/output" --workpath "$build_dir/work" \
    "$root/packaging/macos/RemoteDesktop.spec"
app="$build_dir/output/RemoteDesktop.app"
codesign --verify --deep --strict "$app"
mkdir "$build_dir/image"
mv "$app" "$build_dir/image/"
ln -s /Applications "$build_dir/image/Applications"
hdiutil create -quiet -volname RemoteDesktop -format UDZO -fs HFS+ \
    -srcfolder "$build_dir/image" "$build_dir/RemoteDesktop.dmg"
hdiutil verify -quiet "$build_dir/RemoteDesktop.dmg"
mkdir -p "$root/dist"
mv "$build_dir/RemoteDesktop.dmg" "$root/dist/RemoteDesktop-macOS-$arch.dmg"
