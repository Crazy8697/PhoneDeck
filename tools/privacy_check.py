# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
"""Refuse to publish if PhoneDeck carries something private: a personal Windows user path, a
home-network address or hostname, an e-mail address, a token or key, or an em dash. build.ps1
-Release runs it before anything goes public; run it by hand any time:

    python tools/privacy_check.py                 the tracked files
    python tools/privacy_check.py --dist          also the frozen build in dist\\PhoneDeck (its
                                                  PyInstaller archives are unpacked and scanned too)
    python tools/privacy_check.py --root <dir>    check another copy of the tree (tests)

Exit 1 and a list of file:line (or file: for the build) if anything is found.

The patterns are generic, so nothing private is written here. Exact values that no generic
pattern catches (a device serial, a real name) go one per line in tools/privacy-local.txt,
which is gitignored and never leaves this PC. Binary files in the tree are skipped; license texts
quote their authors' addresses, so those are skipped too."""
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOCAL = HERE / "privacy-local.txt"

USER_PATH = ("a personal user path",
             re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|<|%|\$|\{)[A-Za-z0-9_.-]+", re.I))
PRIVATE_IP = ("a private network address",
              re.compile(r"(?<!Version: )(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
                         r"\.\d{1,3}\.\d{1,3}(?![\d.])"))
TAILNET = ("a tailnet name", re.compile(r"\btailscale\b|\.ts\.net\b|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", re.I))
HOMELAB = ("a home-lab hostname",
           re.compile(r"\bnexus(?:-config|-wire|\.)|\bhal9000\b|\bhal69000\b|[a-z0-9-]+\.darkrelay\.net", re.I))
TOKEN = ("a token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|\beyJ[A-Za-z0-9_-]{20,}\.eyJ"))
KEY = ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"))
EMAIL = ("an e-mail address",
         re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\.|users\.noreply\.github\.com\b|github\.com\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"))
EMDASH = ("an em dash", re.compile(chr(0x2014)))

TREE_PATTERNS = [USER_PATH, PRIVATE_IP, TAILNET, HOMELAB, TOKEN, KEY, EMAIL, EMDASH]
# the frozen build is mostly third-party binaries (Qt, Python, scrcpy) whose strings carry their
# own authors' addresses and sample IPs; there, look only for what could only be ours
# (Qt's and other vendors' DLLs carry PDB paths from their CI machines' user folders: the "qt"
# user and the usual CI accounts are not ours)
DIST_USER_PATH = ("a personal user path",
                  re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|qt\b|runneradmin\b|"
                             r"VssAdministrator\b|appveyor\b|<|%|\$|\{)[A-Za-z0-9_.-]+", re.I))
DIST_PATTERNS = [DIST_USER_PATH, HOMELAB, TOKEN, KEY]
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".ico", ".ttf", ".pdf"}
SKIP_PREFIX = ("licenses/", "LICENSE")   # verbatim official license texts


def local_values():
    """Exact private strings from the gitignored tools/privacy-local.txt (# comments allowed)."""
    if not LOCAL.is_file():
        return []
    vals = [ln.strip() for ln in LOCAL.read_text(encoding="utf-8").splitlines()]
    return [("a private value from privacy-local.txt", re.compile(re.escape(v), re.I))
            for v in vals if v and not v.startswith("#")]


def tracked(root):
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "."], cwd=root, capture_output=True,
                             check=True).stdout
        return [p for p in out.decode("utf-8").split("\0") if p]
    except (subprocess.CalledProcessError, OSError):
        # not a git checkout (a test copy): every file except build output and venvs
        skip = {".git", ".venv", "build", "dist", "__pycache__"}
        files = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in skip]
            files += [os.path.relpath(os.path.join(d, n), root).replace("\\", "/") for n in names]
        return files


def scan_text(label, text, patterns, found, per_line=True):
    if per_line:
        for n, line in enumerate(text.splitlines(), 1):
            for what, rx in patterns:
                if rx.search(line):
                    found.append(f"{label}:{n}: {what}")
    else:
        for what, rx in patterns:
            if rx.search(text):
                found.append(f"{label}: {what}")


def check_tree(root, extra):
    found = []
    files = tracked(root)
    for rel in files:
        if Path(rel).suffix.lower() in SKIP_SUFFIX or rel.startswith(SKIP_PREFIX):
            continue
        if rel == "tools/privacy-local.txt":
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scan_text(rel, text, TREE_PATTERNS + extra, found)
    return found, len(files)


def _pyinstaller_members(path):
    """(name, bytes) of every entry inside a PyInstaller exe (its CArchive, and the PYZ of Python
    modules embedded in it), decompressed. Code objects come back marshalled, which keeps their
    file names and string constants readable. Needs PyInstaller (the build venv has it)."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError:
        print("privacy check: PyInstaller not importable, embedded archives not unpacked")
        return
    try:
        arc = CArchiveReader(str(path))
    except Exception:
        return
    for name in arc.toc:
        if name.lower().endswith(".pyz"):
            try:
                pyz = arc.open_embedded_archive(name)
            except Exception:
                continue
            for mod in pyz.toc:
                try:
                    yield f"{name}!{mod}", pyz.extract(mod, raw=True)
                except Exception:
                    pass
            continue
        try:
            yield name, arc.extract(name)
        except Exception:
            pass


def _zip_members(path):
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            yield info.filename, z.read(info)


def check_dist(dist, extra):
    found = []
    pats = DIST_PATTERNS + extra
    n = 0
    for p in sorted(dist.rglob("*")):
        if not p.is_file():
            continue
        n += 1
        rel = p.relative_to(dist.parent).as_posix()
        data = p.read_bytes()
        # ASCII/UTF-8 strings, and UTF-16LE (Windows resources, Qt) folded to ASCII
        scan_text(rel, data.decode("latin-1"), pats, found, per_line=False)
        scan_text(rel + " (utf-16)", data.replace(b"\x00", b"").decode("latin-1"), pats, found,
                  per_line=False)
        members = (_pyinstaller_members(p) if p.name == "PhoneDeck.exe"
                   else _zip_members(p) if p.suffix.lower() == ".zip" else ())
        for name, blob in members:
            if isinstance(blob, (bytes, bytearray)):
                scan_text(f"{rel}!{name}", blob.decode("latin-1"), pats, found, per_line=False)
    # de-duplicate (the utf-16 fold often repeats the plain hit)
    seen, out = set(), []
    for f in found:
        k = f.replace(" (utf-16)", "")
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out, n


def main(argv):
    root = ROOT
    if "--root" in argv:
        root = Path(argv[argv.index("--root") + 1]).resolve()
    extra = local_values()
    found, nfiles = check_tree(root, extra)
    for f in found:
        print(f)
    print(f"privacy check: {len(found)} finding(s) in {nfiles} files (tracked + new)"
          + (f" (+{len(extra)} local values)" if extra else ""))
    bad = bool(found)
    if "--dist" in argv:
        dist = root / "dist" / "PhoneDeck"
        if not dist.is_dir():
            print(f"privacy check: no build at {dist}")
            return 1
        dfound, dn = check_dist(dist, extra)
        for f in dfound:
            print(f)
        print(f"privacy check: {len(dfound)} finding(s) in {dn} files of the build")
        bad = bad or bool(dfound)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
