# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
"""Export exactly what goes public: the committed tree at HEAD (git archive), into an empty folder,
then refuse if it holds anything that must never be published or if the privacy check finds
anything in it. build.ps1 -PublicSync (and later -Release) runs it before copying the tree into the
public clone:

    python tools/public_export.py <empty folder>      exit 0 and the file list, or exit 1

Only committed files can go public (git archive of HEAD): untracked and ignored files (dist, build,
.venv, logs, favorites.json, tools/privacy-local.txt) never reach it. FORBIDDEN is a second fence in
case one of them was ever committed by mistake."""
import fnmatch
import io
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


def main(argv):
    if not argv:
        print(__doc__)
        return 2
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
