# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
# PyInstaller spec for PhoneDeck (onedir, windowed).
# Build: .\build.ps1   ->  dist\PhoneDeck\PhoneDeck.exe   (-Setup packages it as an installer)
#
# scrcpy + adb are NOT passed to Analysis (PyInstaller would treat their DLLs as
# Python binaries and scatter them). After COLLECT, scrcpy's Windows release
# folder is copied as-is into dist\PhoneDeck\scrcpy, one clearly separate
# folder next to PhoneDeck.exe, which phonedeck.find_tools() checks first.
# Source of the copy: $env:SCRCPY_DIR, else the newest
# "C:\Program Files\scrcpy-win64-*" release folder.
import glob
import os
import shutil
from pathlib import Path


def _scrcpy_src():
    d = os.environ.get("SCRCPY_DIR")
    if not d:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        rel = sorted(glob.glob(os.path.join(pf, "scrcpy-win64-*")), reverse=True)
        d = rel[0] if rel else ""
    d = Path(d)
    for f in ("scrcpy.exe", "adb.exe", "scrcpy-server"):
        if not (d / f).is_file():
            raise SystemExit(f"{f} not found in '{d}'; set SCRCPY_DIR to an unpacked "
                             "scrcpy Windows release")
    return d


scrcpy_src = _scrcpy_src()

# Qt modules PhoneDeck never uses (it needs QtCore, QtGui and QtWidgets only).
QT_EXCLUDE = ["PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtSql",
              "PySide6.QtTest", "PySide6.QtDBus", "PySide6.QtOpenGL",
              "PySide6.QtOpenGLWidgets", "PySide6.QtPdf"]

a = Analysis(
    ["phonedeck.py"],
    pathex=["."],
    datas=[("phonedeck.ico", "."),                                   # window / taskbar icon
           ("assets/brand/darkrelay-banner.jpg", "assets/brand")],   # About backdrop
    hiddenimports=[],
    excludes=QT_EXCLUDE + ["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)

# Binaries pulled in by Qt plugins that nothing in PhoneDeck loads.
DROP = ("opengl32sw", "qt6quick", "qt6qml", "qt6pdf", "qt6opengl", "qt6network",
        "qt6virtualkeyboard", "qt6dbus", "qpdf.dll", "qtvirtualkeyboardplugin")
dropped = [b for b in a.binaries
           if any(k in b[0].lower().replace("\\", "/").split("/")[-1] for k in DROP)]
a.binaries = [b for b in a.binaries if b not in dropped]
print("PhoneDeck spec: dropped binaries:",
      ", ".join(sorted({Path(b[0]).name for b in dropped})) or "none")

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PhoneDeck",
    icon="phonedeck.ico",
    console=False,
    upx=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PhoneDeck", upx=False)

# --- bundle scrcpy + adb once, after COLLECT ---
dest = Path(DISTPATH) / "PhoneDeck" / "scrcpy"
shutil.rmtree(dest, ignore_errors=True)
shutil.copytree(scrcpy_src, dest)
print(f"PhoneDeck spec: bundled scrcpy release ({len(list(dest.iterdir()))} files) "
      f"from {scrcpy_src.name} -> {dest}")

# --- license files next to PhoneDeck.exe (Help > Licenses reads licenses\ from there) ---
HERE = Path(SPECPATH)
app = Path(DISTPATH) / "PhoneDeck"
shutil.rmtree(app / "licenses", ignore_errors=True)
shutil.copytree(HERE / "licenses", app / "licenses")
shutil.copy2(HERE / "LICENSE", app / "LICENSE.txt")
shutil.copy2(HERE / "NOTICE", app / "NOTICE.txt")
shutil.copy2(HERE / "THIRD-PARTY-NOTICES.md", app / "THIRD-PARTY-NOTICES.md")
print(f"PhoneDeck spec: {len(list((app / 'licenses').iterdir()))} license files + LICENSE.txt, "
      "NOTICE.txt, THIRD-PARTY-NOTICES.md -> " + str(app))
