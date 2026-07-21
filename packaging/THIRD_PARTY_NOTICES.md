# Third-Party Notices

This distribution uses only the fixed artifacts recorded in
`packaging/runtime-lock.json`. That lock is the authoritative record of each
download URL and SHA-256. The entries below identify upstream source projects
and licenses; they are maintained source records, not license text copied from
a build cache. License and notice files already present in the verified Python
archive and wheels remain in their installed package directories.

## Python Runtime

### CPython 3.12.13

- Source: https://github.com/python/cpython/tree/v3.12.13
- License: Python Software Foundation License Version 2 (PSF-2.0)

### python-build-standalone 20260718

- Source: https://github.com/astral-sh/python-build-standalone/tree/20260718
- License: Mozilla Public License 2.0 (MPL-2.0)
- Use: produces the locked install-only CPython archives for macOS arm64 and
  x86_64. The archives retain CPython and bundled-component license files.

## Python Packages

### Playwright for Python 1.61.0

- Source: https://github.com/microsoft/playwright-python/tree/v1.61.0
- License: Apache License 2.0 (Apache-2.0)
- Distribution: separate official PyPI macOS arm64 and x86_64 wheels are locked
  so each runtime contains a native architecture-matched driver Node.
- Bundled driver source: https://github.com/microsoft/playwright/tree/v1.61.0
- Driver license: Apache License 2.0 (Apache-2.0), with the driver package's
  own `NOTICE` and `ThirdPartyNotices.txt` retained from the locked wheel.
- Version note: package `METADATA` version `1.61.0` is authoritative. The
  official arm64 wheel's bundled CLI reports upstream string
  `1.61.1-beta-1782139630000`; the build records that evidence without
  rewriting the Python package version.

### greenlet 3.5.3

- Source: https://github.com/python-greenlet/greenlet/tree/3.5.3
- License: MIT License and Python Software Foundation License Version 2
  (MIT AND PSF-2.0), as declared by the locked wheel.

### pyee 13.0.1

- Source: https://github.com/jfhbrook/pyee/tree/v13.0.1
- License: MIT License (MIT)

### typing_extensions 4.16.0

- Source: https://github.com/python/typing_extensions/tree/4.16.0
- License: Python Software Foundation License Version 2 (PSF-2.0)

### yt-dlp 2026.7.4

- Source: https://github.com/yt-dlp/yt-dlp/tree/2026.07.04
- License: The Unlicense (Unlicense)

## Media Tool

### imageio-ffmpeg 0.6.0

- Source: https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0
- License: BSD 2-Clause License (BSD-2-Clause)
- Use: supplies one architecture-matched FFmpeg executable per runtime. The
  imageio-ffmpeg Python package and `ffprobe` are not distributed.

### FFmpeg 7.1

- Source: https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz
- License: GNU General Public License version 2 or later (GPL-2.0-or-later).
  The locked imageio-ffmpeg executables report `--enable-gpl` and this license
  through `ffmpeg -L`.
