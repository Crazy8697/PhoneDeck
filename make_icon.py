# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
"""Rebuild phonedeck.ico (and assets/icon/phonedeck-icon-512.png) from the
icon SVGs in assets/icon.

    python make_icon.py            (build.ps1 -Icon runs this before building)

Each .ico layer is rendered from its own hand-tuned SVG at its exact pixel
size, never scaled down from a bigger render:
    256, 128, 64, 512 png  phonedeck-icon.svg     full art, 3 speed lines
    48                     phonedeck-icon-48.svg  2 speed lines, relay as a Y
    32                     phonedeck-icon-32.svg  1 speed line, relay as a dot
    24                     phonedeck-icon-24.svg  as 32, heavier strokes
    16                     phonedeck-icon-16.svg  phone on the board only
Renderer: Inkscape's command line ($env:INKSCAPE, PATH, or Program Files).
"""
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(HERE, "assets", "icon")
LAYERS = [(256, "phonedeck-icon.svg"), (128, "phonedeck-icon.svg"),
          (64, "phonedeck-icon.svg"), (48, "phonedeck-icon-48.svg"),
          (32, "phonedeck-icon-32.svg"), (24, "phonedeck-icon-24.svg"),
          (16, "phonedeck-icon-16.svg")]
ICO = os.path.join(HERE, "phonedeck.ico")
PNG512 = os.path.join(ICON_DIR, "phonedeck-icon-512.png")


def find_inkscape():
    for c in (os.environ.get("INKSCAPE"), shutil.which("inkscape"),
              os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                           "Inkscape", "bin", "inkscape.exe")):
        if c and os.path.isfile(c):
            return c
    raise SystemExit("Inkscape not found; install it or set INKSCAPE to inkscape.exe")


def render(inkscape, svg, size, out):
    subprocess.run([inkscape, svg, "--export-type=png", f"--export-filename={out}",
                    "-w", str(size), "-h", str(size), "--export-background-opacity=0"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    im = Image.open(out).convert("RGBA")
    if im.size != (size, size):
        raise SystemExit(f"{svg} rendered at {im.size}, wanted {size}x{size}")
    return im


def main():
    ink = find_inkscape()
    with tempfile.TemporaryDirectory(prefix="phonedeck-icon-") as tmp:
        imgs = []
        for size, name in LAYERS:
            imgs.append(render(ink, os.path.join(ICON_DIR, name), size,
                               os.path.join(tmp, f"{size}.png")))
        big = render(ink, os.path.join(ICON_DIR, "phonedeck-icon.svg"), 512,
                     os.path.join(tmp, "512.png"))
    big.save(PNG512, optimize=True)
    # Pillow writes one PNG-compressed ICO entry per size, taking each from the
    # frame of that size in append_images, so every layer is its own render
    imgs[0].save(ICO, format="ICO", sizes=[im.size for im in imgs],
                 append_images=imgs[1:], bitmap_format="png")
    print(f"wrote {ICO} ({', '.join(str(s) for s, _ in LAYERS)}) and {PNG512}")


if __name__ == "__main__":
    sys.exit(main())
