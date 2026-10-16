# PhoneDeck: third-party notices

PhoneDeck is free software under the Apache License, Version 2.0 (see LICENSE and NOTICE).
Copyright 2026 DarkRelay. Source code: https://github.com/Crazy8697/PhoneDeck

The Windows build of PhoneDeck (the installer and the program folder it creates) includes the
components below, each under its own license. Their full license texts are in the `licenses`
folder next to PhoneDeck.exe (Help > Licenses shows them too). All of them allow distribution
together with an Apache-2.0 program.

## LGPL components: source and replacing them

Qt 6 / PySide6 / Shiboken6 (LGPL-3.0), the FFmpeg libraries (LGPL-2.1-or-later) and libusb
(LGPL-2.1-or-later) are shipped unmodified, as separate DLLs that PhoneDeck and scrcpy load
dynamically:

- Qt, PySide6, Shiboken6: `_internal\PySide6\*.dll`, `_internal\PySide6\plugins\`,
  `_internal\shiboken6\`
- FFmpeg: `scrcpy\avcodec-62.dll`, `scrcpy\avformat-62.dll`, `scrcpy\avutil-60.dll`,
  `scrcpy\swresample-6.dll`
- libusb: `scrcpy\libusb-1.0.dll`

You may replace any of them with your own modified build of the same version (or a compatible
one). The corresponding source code for the exact versions shipped is available from the
upstream locations listed below. On request the PhoneDeck maintainer will provide it too, for
at least three years after a release: open an issue at https://github.com/Crazy8697/PhoneDeck.
PhoneDeck places no restriction on reverse engineering these libraries for debugging such
modifications.

## Components

| Component | Version | License | License file | Upstream |
|---|---|---|---|---|
| scrcpy (scrcpy.exe, scrcpy-server) | 4.1 | Apache-2.0 | `scrcpy.txt` | https://github.com/Genymobile/scrcpy |
| adb (adb.exe), from Android SDK Platform-Tools | 37.0.0 | Apache-2.0 (AOSP); its own NOTICE lists the code built into it | `platform-tools.txt`, `platform-tools-notice.txt` | https://android.googlesource.com/platform/packages/modules/adb/ |
| AdbWinApi.dll, AdbWinUsbApi.dll | 2.0.0.0, 2.0.0.1 | Apache-2.0 (AOSP) | `platform-tools.txt` | https://android.googlesource.com/platform/development/+/main/host/windows/usb/ |
| FFmpeg (avcodec, avformat, avutil, swresample) | 8.1.2 | LGPL-2.1-or-later (built without --enable-gpl / --enable-nonfree) | `ffmpeg.txt` | https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz |
| dav1d (AV1 decoder, built into avcodec) | 1.5.3 | BSD-2-Clause | `dav1d.txt` | https://code.videolan.org/videolan/dav1d |
| zlib (built into avcodec) | as linked by scrcpy's FFmpeg build | Zlib | `zlib.txt` | https://zlib.net |
| SDL3 (SDL3.dll) | 3.4.12 | Zlib | `sdl3.txt` | https://github.com/libsdl-org/SDL |
| libusb (libusb-1.0.dll) | 1.0.30 | LGPL-2.1-or-later | `libusb.txt` | https://github.com/libusb/libusb |
| Qt 6 (Qt6Core, Qt6Gui, Qt6Widgets, Qt6Svg, plugins) | 6.11.2 | LGPL-3.0-only (also GPL-2.0 / GPL-3.0) | `pyside6.txt`, `gpl-3.0.txt` | https://download.qt.io/official_releases/qt/ ; third-party code inside Qt: https://doc.qt.io/qt-6/licenses-used-in-qt.html |
| PySide6 (Qt for Python) | 6.11.2 | LGPL-3.0-only (also GPL-2.0 / GPL-3.0) | `pyside6.txt`, `gpl-3.0.txt` | https://download.qt.io/official_releases/QtForPython/ |
| Shiboken6 | 6.11.2 | LGPL-3.0-only (also GPL-2.0 / GPL-3.0) | `pyside6.txt`, `gpl-3.0.txt` | https://download.qt.io/official_releases/QtForPython/ |
| pywin32 (win32gui, win32con, pywintypes) | 312 | BSD-3-Clause style (Mark Hammond) | `pywin32.txt` | https://github.com/mhammond/pywin32 |
| Python runtime and standard library | 3.11.9 | PSF-2.0, plus the licenses of code built into it (bzip2, libffi, zlib, libmpdec, expat and others) | `python.txt`, `python-third-party.txt` | https://www.python.org |
| OpenSSL (libssl-3, libcrypto-3, used by Python) | 3.0.13 | Apache-2.0 | `openssl.txt` | https://www.openssl.org |
| libffi (used by Python's ctypes) | as shipped with Python 3.11.9 | MIT | `python.txt`, `python-third-party.txt` | https://github.com/libffi/libffi |
| PyInstaller bootloader (PhoneDeck.exe's launcher) | 6.22.2 | GPL-2.0-or-later with the PyInstaller bootloader exception | `pyinstaller.txt` | https://pyinstaller.org |
| Microsoft Visual C++ runtime (VCRUNTIME140, MSVCP140, ucrtbase, api-ms-win-*) | 14.x / 10.0 | Microsoft Visual C++ Redistributable license (redistributable files) | none (Microsoft terms) | https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files |

## Notes

- The adb files are Google's unmodified Windows builds from Android SDK Platform-Tools, as
  shipped in scrcpy's own Windows release. Their source is the Android Open Source Project
  under the Apache License 2.0; the AOSP NOTICE texts are in `platform-tools.txt`. Google's own
  NOTICE.txt from platform-tools_r37.0.0-win.zip (the release these binaries come from, SHA-256
  checked) is shipped verbatim as `platform-tools-notice.txt`: it covers the code built into
  adb.exe (BoringSSL, protobuf, libusb and others).
- scrcpy-server (pushed to the phone by scrcpy) is part of scrcpy, Apache-2.0.
- The PyInstaller bootloader exception allows distributing the bootloader with programs under
  any license.
- The Visual C++ runtime files are distributed under Microsoft's redistribution terms for them.
