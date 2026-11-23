#!/usr/bin/env python3
"""Cross-build a self-contained Windows x64 NSIS installer; never run it."""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
VERSION = "0.1.0"
WORK = HERE / ".build"
CACHE = HERE / ".cache"
PAYLOAD = WORK / "payload"
OUTPUT = PROJECT / "dist" / "RemoteDesktop-Windows-x64.exe"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(item, offline):
    if Path(item["filename"]).name != item["filename"]:
        raise ValueError("Unsafe locked archive filename")
    url = urllib.parse.urlparse(item["url"])
    if url.scheme != "https" or url.netloc not in {"github.com", "files.pythonhosted.org"}:
        raise ValueError("Downloads must use the pinned official HTTPS sources")
    target = CACHE / item["filename"]
    if target.is_file() and digest(target) == item["sha256"]:
        print("Verified cached " + target.name, flush=True)
        return target
    if offline:
        raise RuntimeError("Missing verified cached download: " + target.name)
    temporary = target.with_suffix(target.suffix + ".part")
    print("Downloading " + target.name, flush=True)
    try:
        request = urllib.request.Request(item["url"], headers={"User-Agent": "RemoteDesktop-local-builder"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if digest(temporary) != item["sha256"]:
            raise RuntimeError("SHA-256 mismatch: " + target.name)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def safe_parts(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Unsafe archive member: " + name)
    return path.parts


def extract_runtime(archive_path):
    destination = PAYLOAD / "runtime"
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            parts = safe_parts(member.name)
            if not parts or parts[0] != "python":
                raise ValueError("Unexpected runtime layout: " + member.name)
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError("Unexpected runtime member: " + member.name)
            target = destination.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def install_wheel(archive_path):
    destination = PAYLOAD / "runtime" / "Lib" / "site-packages"
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Invalid wheel ZIP: " + archive_path.name)
        for member in archive.infolist():
            parts = safe_parts(member.filename)
            if parts and parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in {"purelib", "platlib"}:
                    raise ValueError("Unsupported wheel data layout: " + member.filename)
                parts = parts[2:]
            target = destination.joinpath(*parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def pe_info(path):
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError("Missing DOS/PE header: " + str(path))
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0":
        raise ValueError("Invalid PE signature: " + str(path))
    machine, sections = struct.unpack_from("<HH", data, offset + 4)
    optional_size = struct.unpack_from("<H", data, offset + 20)[0]
    optional = offset + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    subsystem = struct.unpack_from("<H", data, optional + 68)[0]
    section_table = optional + optional_size
    section_list = []
    for index in range(sections):
        position = section_table + index * 40
        size, address, raw_size, raw_offset = struct.unpack_from("<IIII", data, position + 8)
        section_list.append((address, max(size, raw_size), raw_offset))

    def file_offset(rva):
        for address, size, raw in section_list:
            if address <= rva < address + size:
                return raw + rva - address
        raise ValueError("PE RVA outside sections: " + str(path))

    imports = []
    directory = optional + (112 if magic == 0x20B else 96)
    import_rva = struct.unpack_from("<I", data, directory + 8)[0]
    if import_rva:
        position = file_offset(import_rva)
        while any(data[position:position + 20]):
            name_rva = struct.unpack_from("<I", data, position + 12)[0]
            name_offset = file_offset(name_rva)
            end = data.index(b"\0", name_offset)
            imports.append(data[name_offset:end].decode("ascii").lower())
            position += 20
    return {"machine": hex(machine), "subsystem": subsystem, "imports": imports}


def validate_payload():
    required = [
        "remote_desktop.py", "remotedesk/ui/panel.py", "remotedesk/host/desktop.py",
        "runtime/python.exe", "runtime/pythonw.exe", "runtime/python312.dll",
        "runtime/vcruntime140.dll", "runtime/vcruntime140_1.dll",
        "runtime/DLLs/_tkinter.pyd", "runtime/DLLs/tcl86t.dll", "runtime/DLLs/tk86t.dll",
        "runtime/Lib/tkinter/__init__.py", "runtime/tcl/tcl8.6/init.tcl", "runtime/tcl/tk8.6/tk.tcl",
        "runtime/Lib/site-packages/PIL/ImageTk.py", "runtime/Lib/site-packages/mss/__init__.py",
        "runtime/Lib/site-packages/pynput/keyboard/_win32.py", "runtime/Lib/site-packages/six.py",
    ]
    for relative in required:
        if not (PAYLOAD / relative).is_file():
            raise RuntimeError("Required bundled component missing: " + relative)
    native = {}
    for path in sorted(PAYLOAD.rglob("*")):
        if path.suffix.lower() in {".exe", ".dll", ".pyd"}:
            information = pe_info(path)
            if information["machine"] != "0x8664":
                raise RuntimeError("Non-AMD64 native payload: " + str(path))
            native[path.relative_to(PAYLOAD).as_posix()] = information
    if native["runtime/python.exe"]["subsystem"] != 3 or native["runtime/pythonw.exe"]["subsystem"] != 2:
        raise RuntimeError("Console and GUI Python launchers have incorrect PE subsystems")
    for path in [PAYLOAD / "remote_desktop.py", *(PAYLOAD / "remotedesk").rglob("*.py")]:
        compile(path.read_bytes(), str(path), "exec")
    imaging = list((PAYLOAD / "runtime/Lib/site-packages/PIL").glob("_imaging.cp312-win_amd64.pyd"))
    imaging_tk = list((PAYLOAD / "runtime/Lib/site-packages/PIL").glob("_imagingtk.cp312-win_amd64.pyd"))
    if len(imaging) != 1 or len(imaging_tk) != 1:
        raise RuntimeError("Pillow CPython 3.12 AMD64 imaging/Tk extensions missing")
    return native


def nsis_string(value):
    return value.replace("$", "$$").replace('"', '$\\"')


def deletion_manifest():
    files = sorted(path for path in PAYLOAD.rglob("*") if path.is_file())
    directories = {parent for path in files for parent in path.parents if parent != PAYLOAD and PAYLOAD in parent.parents}
    cache_parents = {path.parent for path in files if path.suffix == ".py"}
    commands = ["; Generated from this build's payload. Never recursively delete the install root."]
    for path in files:
        relative = nsis_string(str(path.relative_to(PAYLOAD)).replace("/", "\\"))
        commands.append('Delete "$INSTDIR\\' + relative + '"')
    for directory in sorted(cache_parents, key=lambda value: len(value.parts), reverse=True):
        relative = nsis_string(str(directory.relative_to(PAYLOAD)).replace("/", "\\"))
        prefix = "$INSTDIR" + ("\\" + relative if relative != "." else "")
        commands.extend(['Delete "' + prefix + '\\__pycache__\\*.pyc"', 'RMDir "' + prefix + '\\__pycache__"'])
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        relative = nsis_string(str(directory.relative_to(PAYLOAD)).replace("/", "\\"))
        commands.append('RMDir "$INSTDIR\\' + relative + '"')
    target = WORK / "delete-files.nsh"
    target.write_text("\n".join(commands) + "\n", encoding="utf-8")
    return target


def verify_installer(seven_zip, payload_manifest):
    subprocess.run([seven_zip, "t", str(OUTPUT)], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    destination = WORK / "extracted-installer"
    subprocess.run([seven_zip, "x", "-y", "-o" + str(destination), str(OUTPUT)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for relative, expected in payload_manifest.items():
        extracted = destination / relative
        if not extracted.is_file() or digest(extracted) != expected["sha256"]:
            raise RuntimeError("Installer extraction differs from payload: " + relative)
    installer = pe_info(OUTPUT)
    if b'level="asInvoker"' not in OUTPUT.read_bytes():
        raise RuntimeError("Installer does not declare non-admin asInvoker execution")
    return installer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Use only verified cached inputs")
    args = parser.parse_args()
    compiler = shutil.which("makensis")
    seven_zip = shutil.which("7zz") or shutil.which("7z")
    if not compiler or not seven_zip:
        raise SystemExit("Install makensis and 7-Zip first (macOS: brew install makensis sevenzip).")
    lock = json.loads((HERE / "lock.json").read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        inputs = list(pool.map(lambda item: download(item, args.offline), [lock["runtime"], *lock["wheels"]]))
    if WORK.exists():
        shutil.rmtree(WORK)
    PAYLOAD.mkdir(parents=True)
    print("Extracting verified Windows runtime and wheels", flush=True)
    extract_runtime(inputs[0])
    # pip is not needed by the offline application. Removing its vendored
    # multi-architecture launcher EXEs also keeps the application payload AMD64.
    for path in (PAYLOAD / "runtime/Lib/site-packages").glob("pip*"):
        if path.is_dir():
            shutil.rmtree(path)
    for wheel in inputs[1:]:
        install_wheel(wheel)
    shutil.copy2(PROJECT / "remote_desktop.py", PAYLOAD)
    shutil.copytree(PROJECT / "remotedesk", PAYLOAD / "remotedesk",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    for filename in ("README.md", "requirements.txt", "LICENSE"):
        shutil.copy2(PROJECT / filename, PAYLOAD)
    shutil.copytree(PROJECT / "docs/images", PAYLOAD / "docs/images")
    shutil.copy2(HERE / "lock.json", PAYLOAD / "build-lock.json")
    shutil.copy2(HERE / "QUICKSTART.txt", PAYLOAD)
    icon = PROJECT / "packaging/icons/RemoteDesktop.ico"
    shutil.copy2(icon, PAYLOAD / "RemoteDesktop.ico")
    (PAYLOAD / "THIRD-PARTY-NOTICES.txt").write_text(
        "CPython/python-build-standalone and its bundled Tcl/Tk, OpenSSL and other licenses are in runtime/LICENSE.txt.\n"
        "Python wheel licenses are retained in runtime/Lib/site-packages/*.dist-info/.\n"
        "Exact versions, source URLs and SHA-256 digests are in build-lock.json.\n", encoding="utf-8")
    native = validate_payload()
    manifest = {path.relative_to(PAYLOAD).as_posix(): {"sha256": digest(path), "size": path.stat().st_size}
                for path in sorted(PAYLOAD.rglob("*")) if path.is_file()}
    (PAYLOAD / "payload-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    delete_manifest = deletion_manifest()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [compiler, "-V2", "-WX", "-DVERSION=" + VERSION, "-DPAYLOAD=" + str(PAYLOAD), "-DAPP_ICON=" + str(icon),
               "-DOUTPUT=" + str(OUTPUT), "-DDELETE_MANIFEST=" + str(delete_manifest),
               "-DPACKAGE_SIZE_KB=" + str(sum(item["size"] for item in manifest.values()) // 1024),
               str(HERE / "installer.nsi")]
    print("Compiling user-level NSIS installer", flush=True)
    subprocess.run(command, check=True)
    print("Testing and extracting installer to compare every bundled file", flush=True)
    # The manifest does not hash itself, but its bytes must also be identical.
    manifest["payload-manifest.json"] = {"sha256": digest(PAYLOAD / "payload-manifest.json")}
    installer = verify_installer(seven_zip, manifest)
    report = {"version": VERSION, "artifact": OUTPUT.name, "size": OUTPUT.stat().st_size,
              "sha256": digest(OUTPUT), "runtime": lock["runtime"], "wheels": lock["wheels"],
              "payload_files_verified": len(manifest), "native_amd64_files": len(native),
              "installer_pe": installer, "native_payload": native,
              "source_sha256": {relative: item["sha256"] for relative, item in manifest.items()
                                if relative == "remote_desktop.py" or relative.startswith("remotedesk/")},
              "verified": ["locked download digests", "native AMD64 PE architecture", "GUI/console Python subsystems",
                           "Tk/Tcl and Pillow native modules", "Python source syntax", "NSIS compressed archive test",
                           "every extracted file hash", "asInvoker installer manifest"],
              "limitations": ["Not executed on Windows; install/uninstall, shortcuts, Tk UI and input injection require Windows validation",
                              "Unsigned installer; no Authenticode signature", "Tailscale must already be installed and signed in"]}
    (WORK / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("artifact", "size", "sha256", "payload_files_verified", "native_amd64_files")}, indent=2))


if __name__ == "__main__":
    main()
