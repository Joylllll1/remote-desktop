# Bundled media libraries

Remote Desktop's original source is under the MIT license in the project root. That license does not replace the licenses of the binary distribution's bundled libraries.

The installers include PyAV 16.1.0 (BSD-3-Clause), FFmpeg 8.0.1 and its codec dependencies. The selected official PyAV wheels include x264 and x265 under GPL version 2 or later. FFmpeg and other bundled libraries also have LGPL and permissive licenses. Distribution of the combined codec binaries must respect those licenses, including applicable GPL version 3 terms when combined with LGPL version 3 components. Retained upstream notices and license texts are included in this directory and in the component source archives. There is no warranty for these components; see their licenses.

The upstream FFmpeg build patches its license detection to classify x264/x265 as version-3 dependencies. Its library API consequently reports “LGPL version 3 or later” while the actual binaries link x264/x265. That reported string and PyAV's BSD wheel metadata do not eliminate the codec libraries' GPL obligations. The original patch is preserved in the corresponding build-recipe source archive.

Every binary release containing these codecs must provide clear links beside the installers to both the matching Remote Desktop source and the pinned codec sources. Keep both downloads available with the corresponding binaries. This is source delivery, not a written offer.

For releases reusing the unchanged PyAV 16.1.0 wheels and this `sources.json`, the third-party sources and build recipes are in [the shared codec source archive](https://github.com/Joylllll1/remote-desktop/releases/download/v0.1.7/RemoteDesktop-Codec-Sources.zip) (SHA-256 `a3b0469f7804e500057bd18a4f73412328515004ff0978ab268f20e83518112a`). Use its `upstream/` directory, which contains all 32 component/build-recipe archives. Use the **current release's Source code ZIP or tag** for Remote Desktop, its packaging scripts and notices; the older `remote-desktop/` copy inside the shared archive is not the source for later application releases. The two downloads together supply the matching application and unchanged dependency sources. If dependencies change, publish a new corresponding archive.

Build the archive from the same source tree used for the installers:

```sh
python3 packaging/build_codec_sources.py
```

`sources.json` pins each upstream source URL and SHA-256 digest. `SHA256SUMS` inside the ZIP covers every source and project file. In the ZIP, the project source is in `remote-desktop/`, this notice and manifest are in `remote-desktop/packaging/codec-licenses/`, and unchanged upstream archives are in `upstream/`. Unzip it, unpack the upstream archives, and follow `pyav-build-recipes`' `.github/workflows/tests.yml` for PyAV and `ffmpeg-build-recipes`' `.github/workflows/build-ffmpeg.yml` and `scripts/build-ffmpeg.py` for FFmpeg and dependencies. The FFmpeg recipe uses `--community` and applies the included patches through `scripts/cibuildpkg.py`. The Remote Desktop builders install the unchanged PyPI wheels; they do not compile a separate FFmpeg build or modify its libraries. MSYS2 `.src.tar.zst` archives contain component sources, patches and `PKGBUILD`; extract them with a zstd-capable archive tool.

Provenance:

- [PyAV 16.1.0 source and wheel metadata](https://pypi.org/project/av/16.1.0/).
- [PyAV 16.1.0 wheel build workflow](https://github.com/PyAV-Org/PyAV/blob/v16.1.0/.github/workflows/tests.yml).
- [PyAV 16.1.0 FFmpeg selection](https://github.com/PyAV-Org/PyAV/blob/v16.1.0/scripts/ffmpeg-latest.json): `pyav-ffmpeg` tag `8.0.1-3`, commit `9e63121cf6ef9bc1744956c7a4cb772589c80806`.
- [Exact FFmpeg/dependency recipe and source hashes](https://github.com/PyAV-Org/pyav-ffmpeg/blob/9e63121cf6ef9bc1744956c7a4cb772589c80806/scripts/build-ffmpeg.py).
- [Exact upstream license-detection patch](https://github.com/PyAV-Org/pyav-ffmpeg/blob/9e63121cf6ef9bc1744956c7a4cb772589c80806/patches/ffmpeg.patch).
- [FFmpeg license information](https://ffmpeg.org/legal.html), [x264 source](https://code.videolan.org/videolan/x264), [x265 source](https://bitbucket.org/multicoreware/x265_git/).

The Windows wheel's MinGW runtime components are libiconv 1.18-1 (LGPL-2.1-or-later), GCC runtime libraries 15.2.0-8 (GPL with the GCC Runtime Library Exception), winpthreads 13.0.0.r380.gb83511db8-1 (MIT and BSD-3-Clause-Clear), and zlib 1.3.1-1 (Zlib). The corresponding exact MSYS2 source packages, patches and build recipes are included too. `windows-runtime-provenance.json` records the official binary packages and the matching complete PE code-section digests used to identify the wheel's repacked DLLs. Their license files and runtime exception are retained here. Native operating-system libraries remain system dependencies. The Linux package uses its bundled CPython-ABI-specific PyAV wheel; it does not use the distribution's `python3-av` package.
