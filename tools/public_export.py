# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
"""Export exactly what goes public: the committed tree at HEAD (git archive), into an empty folder,
then refuse if it holds anything that must never be published or if the privacy check finds
anything in it. build.ps1 -PublicSync (and later -Release) runs it before copying the tree into the
public clone:

    python tools/public_export.py <empty folder>      exit 0 and the file list, or exit 1
    python tools/public_export.py --dl-info <README> <version> <installer>
                                                      on a release: the README's download info line
                                                      gets the version and the real installer's size
    python tools/public_export.py --keep-dl-info <README> <previous README>
                                                      on a plain sync: keep the published line

Only committed files can go public (git archive of HEAD): untracked and ignored files (dist, build,
.venv, logs, favorites.json, tools/privacy-local.txt) never reach it. FORBIDDEN is a second fence in
case one of them was ever committed by mistake."""
import fnmatch
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FORBIDDEN = ["tools/privacy-local.txt", "dist/*", "build/*", ".venv/*", "venv/*", "*.log",
             "favorites.json", "*.sha256", "*.exe", "*.pyc", "__pycache__/*", "*/__pycache__/*",
             "*.zip", "*.pem", "*.key", ".env", "*.ini"]


def forbidden(path):
    return [pat for pat in FORBIDDEN if fnmatch.fnmatch(path, pat)]


def export(dest, root=ROOT, ref="HEAD"):
    """Unpack git archive <ref> into dest (must be empty or missing). -> sorted file list."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"public export: {dest} isn't empty")
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["git", "archive", "--format=tar", ref], cwd=root, capture_output=True,
                         check=True).stdout
    files = []
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        for m in t.getmembers():
            if not (m.isfile() or m.isdir()) or m.name.startswith(("/", "..")) or ".." in Path(m.name).parts:
                raise SystemExit(f"public export: refusing archive member {m.name!r}")
            if m.isfile():
                files.append(m.name)
        t.extractall(dest, filter="data")
    return sorted(files)


DL_INFO = re.compile(r"<!-- dl-info -->.*?<!-- /dl-info -->", re.S)
RELEASES = "https://github.com/Crazy8697/PhoneDeck/releases"


def dl_info_line(version, size_bytes):
    """The README's download info, Lazarus PDF style, from the real built installer."""
    mb = round(size_bytes / (1024 * 1024), 1)
    return ("<!-- dl-info -->\n"
            f"<p align=\"center\"><sub>Version {version} &nbsp;&middot;&nbsp; {mb} MB &nbsp;&middot;&nbsp; "
            "Windows 10 / 11, 64-bit &nbsp;&middot;&nbsp; "
            f"<a href=\"{RELEASES}\">all releases</a></sub></p>\n"
            "<!-- /dl-info -->")


def set_dl_info(readme, block):
    """Replace the <!-- dl-info --> block in readme (a file) with block. Refuses if it has none."""
    p = Path(readme)
    text = p.read_text(encoding="utf-8")
    if not DL_INFO.search(text):
        raise SystemExit(f"public export: no <!-- dl-info --> block in {p}")
    p.write_text(DL_INFO.sub(lambda _m: block, text, count=1), encoding="utf-8", newline="")


def get_dl_info(readme):
    """The current <!-- dl-info --> block of readme, or None (missing file or block)."""
    try:
        m = DL_INFO.search(Path(readme).read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(0) if m else None


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--dl-info":          # --dl-info <readme> <version> <installer>: on a release
        readme, version, installer = argv[1:4]
        set_dl_info(readme, dl_info_line(version, Path(installer).stat().st_size))
        print(f"public export: README download info -> version {version}, "
              f"{round(Path(installer).stat().st_size / 1048576, 1)} MB")
        return 0
    if argv[0] == "--keep-dl-info":     # --keep-dl-info <readme> <previous readme>: a plain sync
        block = get_dl_info(argv[2])
        if block:
            set_dl_info(argv[1], block)
            print("public export: README download info carried over from the public README")
        return 0
    dest = Path(argv[0]).resolve()
    files = export(dest)
    bad = [(f, forbidden(f)) for f in files if forbidden(f)]
    for f in files:
        print(f"  {f}")
    for f, why in bad:
        print(f"public export: FORBIDDEN {f} (matches {', '.join(why)})")
    print(f"public export: {len(files)} files at HEAD -> {dest}")
    sys.path.insert(0, str(HERE))
    import privacy_check
    rc = privacy_check.main(["--root", str(dest)])
    return 1 if bad or rc else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
