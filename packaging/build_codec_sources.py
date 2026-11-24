#!/usr/bin/env python3
"""Prepare the pinned codec sources that must accompany binary releases."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "packaging/codec-licenses"


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(item, cache, offline):
    filename = item["filename"]
    if Path(filename).name != filename:
        raise ValueError("Unsafe source filename: " + filename)
    target = cache / filename
    if target.is_file() and sha256(target) == item["sha256"]:
        print("Verified " + filename, flush=True)
        return target
    if offline:
        raise RuntimeError("Missing verified source: " + filename)
    pending = target.with_suffix(target.suffix + ".part")
    errors = []
    for url in [item["url"], *item.get("mirrors", [])]:
        if urllib.parse.urlparse(url).scheme != "https":
            raise ValueError("Source downloads require HTTPS")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "RemoteDesktop-source-builder"})
            with urllib.request.urlopen(request, timeout=60) as source, pending.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            if sha256(pending) != item["sha256"]:
                raise ValueError("Source SHA-256 mismatch: " + filename)
            pending.replace(target)
            print("Downloaded " + filename, flush=True)
            return target
        except Exception as error:
            errors.append(str(error))
        finally:
            pending.unlink(missing_ok=True)
    raise RuntimeError(filename + ": " + "; ".join(errors))


def project_sources():
    for name in ("LICENSE", "README.md", "requirements.txt", "remote_desktop.py"):
        yield ROOT / name
    for name in ("remotedesk", "packaging", "tests", "tools", "docs"):
        for path in sorted((ROOT / name).rglob("*")):
            relative = path.relative_to(ROOT / name)
            if (path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
                    and path.suffix not in {".pyc", ".pyo"}):
                yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "packaging/.codec-source-cache")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/RemoteDesktop-Codec-Sources.zip")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((NOTICES / "sources.json").read_text(encoding="utf-8"))
    if len({item["filename"] for item in manifest["sources"]}) != len(manifest["sources"]):
        raise ValueError("Source manifest has duplicate archive filenames")
    args.cache.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        sources = list(pool.map(lambda item: download(item, args.cache, args.offline), manifest["sources"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codec-source-", dir=args.output.parent) as temporary:
        pending = Path(temporary) / args.output.name
        entries = {"upstream/" + path.name: path for path in sources}
        entries["README.md"] = NOTICES / "README.md"
        entries.update({"remote-desktop/" + path.relative_to(ROOT).as_posix(): path for path in project_sources()})
        # Store already-compressed upstream archives unchanged; all component
        # sources, license texts, original patches and build scripts are inside.
        with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, path in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 11, 0, 0, 0))
                info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
                archive.writestr(info, path.read_bytes())
            hashes = "".join(sha256(path) + "  " + name + "\n" for name, path in sorted(entries.items()))
            archive.writestr(zipfile.ZipInfo("SHA256SUMS", date_time=(2026, 1, 11, 0, 0, 0)), hashes)
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("Source ZIP integrity verification failed")
            for name, path in entries.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != sha256(path):
                    raise RuntimeError("Source ZIP member differs: " + name)
        pending.replace(args.output)
    print(json.dumps({"artifact": str(args.output), "sha256": sha256(args.output),
                      "bytes": args.output.stat().st_size, "upstream_sources": len(sources),
                      "project_files": len(entries) - len(sources)}, indent=2))


if __name__ == "__main__":
    main()
