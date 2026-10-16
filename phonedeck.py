# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DarkRelay
"""
PhoneDeck, a desktop program for Android phones, built around scrcpy.

Left sidebar = your phone's apps (searchable). Click one to launch it.
Top bar     = Back / Home / Recents nav + Reconnect / Refresh.
Main area   = the scrcpy mirror, embedded as a live display + touch surface.

scrcpy is used purely as a display/input transport; app launching and
navigation go straight over adb, so nothing depends on scrcpy's own shortcuts
or on Android's flaky secondary-display behavior.
"""

import ctypes
from ctypes import wintypes
import json
import os
import queue
import re
import shutil
import subprocess
import collections
import sys
import tempfile
import threading
import time
import urllib.request

from PySide6.QtCore import (Qt, QTimer, QThread, Signal, QSettings,
                            QPointF, QSize, QUrl, QRectF)
from PySide6.QtGui import (QFont, QFontDatabase, QColor, QCursor, QIcon,
                           QPixmap, QPainter, QPen, QBrush, QPolygonF,
                           QActionGroup, QDesktopServices, QLinearGradient,
                           QPalette)
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QFrame, QMenu,
    QDialog, QComboBox, QSpinBox, QCheckBox, QDialogButtonBox,
    QFormLayout, QToolButton, QMessageBox, QProgressDialog, QTabWidget,
    QTextBrowser, QStyleFactory,
)

import win32gui
import win32con

# ---- paths / device -------------------------------------------------------
def _app_dir():
    """Directory of the running app (the exe when frozen, else this script)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource(name):
    """A bundled resource (PyInstaller unpacks data to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", _app_dir())
    return os.path.join(base, name)


def scrcpy_search_dirs(app_dir=None, env=None):
    """Folders checked for scrcpy.exe + adb.exe, in order:
    1. PHONEDECK_SCRCPY_DIR, if set (development and tests)
    2. scrcpy\\ next to PhoneDeck (the installed copy's bundled tools)
    3. an unpacked scrcpy-win64-* release next to PhoneDeck (newest name first)
    PATH is tried after these (see find_tools)."""
    app_dir = app_dir or _app_dir()
    env = os.environ if env is None else env
    dirs = []
    if env.get("PHONEDECK_SCRCPY_DIR"):
        dirs.append(env["PHONEDECK_SCRCPY_DIR"])
    dirs.append(os.path.join(app_dir, "scrcpy"))
    try:
        rel = sorted((d for d in os.listdir(app_dir)
                      if d.lower().startswith("scrcpy-win64-")), reverse=True)
    except OSError:
        rel = []
    dirs += [os.path.join(app_dir, d) for d in rel]
    return dirs


def find_tools(app_dir=None, env=None, which=shutil.which):
    """(scrcpy_path, adb_path). The first search folder holding scrcpy.exe
    wins and adb.exe is taken from the same folder (scrcpy's release ships
    them together). Otherwise PATH. Anything not found is reported at its
    bundled location (scrcpy\\ next to PhoneDeck), which is what the
    missing-files dialog then names."""
    app_dir = app_dir or _app_dir()
    for d in scrcpy_search_dirs(app_dir, env):
        sc = os.path.join(d, "scrcpy.exe")
        if os.path.isfile(sc):
            ad = os.path.join(d, "adb.exe")
            return sc, (ad if os.path.isfile(ad) else (which("adb") or ad))
    bundled = os.path.join(app_dir, "scrcpy")
    return (which("scrcpy") or os.path.join(bundled, "scrcpy.exe"),
            which("adb") or os.path.join(bundled, "adb.exe"))


def data_dir():
    """Per-user writable dir for favorites and logs."""
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                     "PhoneDeck")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


SCRCPY, ADB = find_tools()
EMBED_TITLE = "PhoneDeckDisplay"   # unique scrcpy window title we reparent
ICON_PATH = _resource("phonedeck.ico")
SCRCPY_LOG = os.path.join(data_dir(), "scrcpy.log")

NAV_KEYS = {"Back": 4, "Home": 3, "Recents": 187}
FAV_FILE = os.path.join(data_dir(), "favorites.json")

_SCRCPY_VDISP_RE = re.compile(
    r"displayId=(\d+), uniqueId=.virtual:com\.android\.shell,2000,scrcpy,")

CREATE_NO_WINDOW = 0x08000000      # keep adb/scrcpy console windows hidden

# user settings (persisted via QSettings), with defaults
VERSION = "1.5.1"
REPO = "Crazy8697/PhoneDeck"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
SOURCE_URL = f"https://github.com/{REPO}"
SUPPORT_URL = "https://buymeacoffee.com/crazy8697"
BANNER_PATH = _resource(os.path.join("assets", "brand", "darkrelay-banner.jpg"))
# full license texts, if shipped, live in licenses\ next to the program
LICENSES_DIR = os.path.join(_app_dir(), "licenses")

# Help > Licenses: (tab, name, license, files in LICENSES_DIR, official page). The texts are
# shipped in licenses\ next to PhoneDeck.exe; THIRD-PARTY-NOTICES.md sits next to the exe.
LICENSE_ENTRIES = [
    ("PhoneDeck", "PhoneDeck, Copyright 2026 DarkRelay", "Apache License 2.0",
     ["phonedeck.txt"], "https://www.apache.org/licenses/LICENSE-2.0"),
    ("scrcpy", "scrcpy 4.1 by Genymobile (scrcpy.exe, scrcpy-server)", "Apache License 2.0",
     ["scrcpy.txt"], "https://github.com/Genymobile/scrcpy/blob/master/LICENSE"),
    ("adb", "adb 37.0.0, AdbWinApi, AdbWinUsbApi (Android Open Source Project)",
     "Apache License 2.0", ["platform-tools.txt", "platform-tools-notice.txt"],
     "https://android.googlesource.com/platform/packages/modules/adb/"),
    ("FFmpeg", "FFmpeg 8.1.2 (with dav1d 1.5.3 and zlib built in)",
     "GNU LGPL v2.1 or later; dav1d BSD-2-Clause; zlib License",
     ["ffmpeg.txt", "dav1d.txt", "zlib.txt"], "https://ffmpeg.org/legal.html"),
    ("SDL3", "SDL 3.4.12", "zlib License", ["sdl3.txt"],
     "https://github.com/libsdl-org/SDL/blob/main/LICENSE.txt"),
    ("libusb", "libusb 1.0.30", "GNU LGPL v2.1 or later", ["libusb.txt"],
     "https://github.com/libusb/libusb/blob/master/COPYING"),
    ("PySide6", "Qt 6.11.2, PySide6 and Shiboken6 6.11.2", "GNU LGPL v3",
     ["pyside6.txt", "gpl-3.0.txt"], "https://doc.qt.io/qtforpython-6/licenses.html"),
    ("pywin32", "pywin32 312", "BSD-3-Clause style (pywin32 win32/License.txt)",
     ["pywin32.txt"], "https://github.com/mhammond/pywin32/blob/main/win32/License.txt"),
    ("Python", "Python 3.11.9 runtime, with OpenSSL 3.0.13",
     "PSF License 2.0 (and the licenses of code built into it); OpenSSL Apache 2.0",
     ["python.txt", "python-third-party.txt", "openssl.txt"],
     "https://docs.python.org/3.11/license.html"),
    ("PyInstaller", "PyInstaller 6.22.2 bootloader (PhoneDeck.exe's launcher)",
     "GNU GPL v2 or later, with the bootloader exception", ["pyinstaller.txt"],
     "https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt"),
    ("Notices", "All third-party components", "THIRD-PARTY-NOTICES.md",
     [os.path.join(os.pardir, "THIRD-PARTY-NOTICES.md")],
     "https://github.com/Crazy8697/PhoneDeck/blob/main/THIRD-PARTY-NOTICES.md"),
]


def open_url(url):
    """Open a link in the default browser."""
    return QDesktopServices.openUrl(QUrl(url))


# ---- updates (the Lazarus PDF pattern) -------------------------------------
# The ONLY network requests PhoneDeck makes:
#   1. GET https://api.github.com/repos/Crazy8697/PhoneDeck/releases/latest (GitHub's "latest
#      release" endpoint: drafts and prereleases are never returned), a few seconds after startup
#      when Settings > "Check for updates at startup" is on, or from File > Check for updates;
#   2. when the user chooses to update: the release's .sha256 file and its Setup .exe.
# Nothing is sent but the request itself (a plain "PhoneDeck" User-Agent, which GitHub requires).
#
# A release is tag vX.Y.Z with assets PhoneDeck-Setup-X.Y.Z.exe and PhoneDeck-Setup-X.Y.Z.exe.sha256
# (plus PhoneDeck-Setup.exe, the same installer under a constant name for download links). The
# download goes to %TEMP%\PhoneDeck-update (a .part file renamed into place when complete) and is
# used only if its SHA-256 matches the published one; a missing or wrong hash is refused and the
# file deleted. Setup then runs with Inno Setup's switches (UPDATE_SETUP_ARGS) so it upgrades the
# installed copy in place (same AppId), with one UAC prompt, keeping settings and favorites, and
# starts PhoneDeck again (unelevated) when done; PhoneDeck exits itself first.
#
# Dev / test hooks (never set in normal use): PHONEDECK_UPDATE_URL points the check at another URL
# (a local mock server); PHONEDECK_UPDATE_DRYRUN=<file> writes the Setup command line there instead
# of starting it; PHONEDECK_UPDATE_TIMEOUT overrides the network timeout (seconds).
SETUP_ASSET = re.compile(r"^PhoneDeck-Setup-(\d+(?:\.\d+)*)\.exe$", re.I)
UPDATE_TIMEOUT = 10.0
UPDATE_CHUNK = 256 * 1024
UPDATE_STARTUP_DELAY_MS = 4000
UPDATE_SKIP_KEY = "update_skip"         # QSettings: the version the user chose to skip
UPDATE_SETUP_ARGS = ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"]


class UpdateError(Exception):
    """Something the user should be told, in plain words."""


class UpdateCancelled(Exception):
    pass


def parse_version(text):
    """'v1.2.3' / '1.2' / '1.2.3-rc1' -> comparable tuple, or None if it isn't a version.
    A suffix (a prerelease) sorts below the same numbers without one."""
    m = re.fullmatch(r"\s*v?(\d+(?:\.\d+){0,3})(?:[-+.]?([0-9A-Za-z.]+))?\s*", str(text or ""))
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")]
    nums += [0] * (4 - len(nums))
    return tuple(nums) + ((0,) if m.group(2) else (1,))


def is_newer(tag, current=VERSION):
    a, b = parse_version(tag), parse_version(current)
    return a is not None and b is not None and a > b


def can_install():
    """Only an installed (frozen) PhoneDeck updates itself: a source run never installs."""
    return bool(getattr(sys, "frozen", False))


def _update_timeout():
    try:
        return float(os.environ.get("PHONEDECK_UPDATE_TIMEOUT") or UPDATE_TIMEOUT)
    except ValueError:
        return UPDATE_TIMEOUT


def _http_get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "PhoneDeck",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(req, timeout=_update_timeout())


def _net_reason(e):
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.code == 429):
            return "GitHub's rate limit for checks from this network was reached; try again later."
        return f"GitHub answered with an error ({e.code})."
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e).lower():
        return "GitHub didn't answer in time."
    if isinstance(e, urllib.error.URLError):
        return "No connection to GitHub (offline, or blocked)."
    return f"{type(e).__name__}: {e}"


def check_release(current=VERSION):
    """-> dict: status "newer" | "current" | "none" (nothing published yet) | "error";
    version, notes, setup_url, sha_url, setup_name, page; reason (for "error")."""
    import urllib.error
    url = os.environ.get("PHONEDECK_UPDATE_URL") or RELEASES_API
    try:
        with _http_get(url) as r:
            body = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "none"}
        return {"status": "error", "reason": _net_reason(e)}
    except Exception as e:                       # offline, timeout, anything: told, never raised
        return {"status": "error", "reason": _net_reason(e)}
    try:
        rel = json.loads(body.decode("utf-8"))
        if not isinstance(rel, dict):
            raise ValueError("not an object")
    except Exception:
        return {"status": "error", "reason": "GitHub's reply wasn't readable."}
    if rel.get("draft") or rel.get("prerelease"):
        return {"status": "none"}                # (the endpoint never returns these; a mock might)
    tag = str(rel.get("tag_name") or "")
    if parse_version(tag) is None:
        return {"status": "error", "reason": "The latest release has no version number."}
    out = {"version": tag.lstrip("vV"), "notes": str(rel.get("body") or "").strip(),
           "page": rel.get("html_url") or RELEASES_URL, "setup_url": None, "sha_url": None,
           "setup_name": None}
    assets = {a.get("name", ""): a.get("browser_download_url")
              for a in rel.get("assets") or [] if isinstance(a, dict)}
    for name, link in assets.items():
        if SETUP_ASSET.match(name):
            out["setup_name"], out["setup_url"] = name, link
            out["sha_url"] = assets.get(name + ".sha256")
            break
    out["status"] = "newer" if is_newer(tag, current) else "current"
    return out


def expected_hash(sha_url):
    if not sha_url:
        raise UpdateError("The release has no .sha256 checksum, so the download can't be "
                          "verified. Not installing.")
    try:
        with _http_get(sha_url, accept="application/octet-stream") as r:
            text = r.read(4096).decode("ascii", "replace")
    except Exception as e:
        raise UpdateError(f"Couldn't get the release's checksum: {_net_reason(e)}")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("The release's .sha256 file holds no SHA-256 checksum. Not installing.")
    return m.group(1).lower()


def download_update(info, progress=None, stopped=None, folder=None):
    """The release's Setup into %TEMP%\\PhoneDeck-update (or folder), verified. -> its path.
    Raises UpdateError / UpdateCancelled; nothing is left behind then."""
    import hashlib
    if not info.get("setup_url") or not info.get("setup_name"):
        raise UpdateError("The release has no installer (PhoneDeck-Setup-<version>.exe) attached.")
    want = expected_hash(info.get("sha_url"))
    folder = folder or os.path.join(tempfile.gettempdir(), "PhoneDeck-update")
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, os.path.basename(info["setup_name"]))
    part = dest + ".part"
    h = hashlib.sha256()
    try:
        with _http_get(info["setup_url"], accept="application/octet-stream") as r, \
                open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if stopped is not None and stopped():
                    raise UpdateCancelled()
                buf = r.read(UPDATE_CHUNK)
                if not buf:
                    break
                f.write(buf)
                h.update(buf)
                done += len(buf)
                if progress is not None:
                    progress(done, total)
        if h.hexdigest() != want:
            raise UpdateError("The download doesn't match the release's SHA-256 checksum (damaged "
                              "or not the published file). It was deleted; nothing is installed.")
        os.replace(part, dest)
        return dest
    except (UpdateError, UpdateCancelled):
        raise
    except Exception as e:
        raise UpdateError(f"The download failed: {_net_reason(e)}")
    finally:
        if os.path.exists(part):
            os.remove(part)


def setup_command(setup):
    return [str(setup)] + UPDATE_SETUP_ARGS


def launch_setup(setup):
    """Start Setup the way Explorer would (ShellExecute: it asks for elevation itself, one UAC
    prompt). The dry-run hook writes the command line instead."""
    cmd = setup_command(setup)
    dry = os.environ.get("PHONEDECK_UPDATE_DRYRUN")
    if dry:
        with open(dry, "w", encoding="utf-8") as f:
            json.dump(cmd, f)
        return True
    r = ctypes.windll.shell32.ShellExecuteW(None, "open", cmd[0],
                                            subprocess.list2cmdline(cmd[1:]), None, 1)
    return r > 32


SETTING_DEFAULTS = {
    "res_landscape": "1920x1080",  # virtual display W x H, landscape
    "dpi_landscape": 180,
    "res_portrait": "1080x1920",   # virtual display W x H, portrait
    "dpi_portrait": 180,
    "scroll_dist": 260,            # px of swipe per wheel tick
    "scroll_natural": True,        # wheel up scrolls content up
    "show_data_usage": False,      # nerd data: live mobile-data readout
    "show_charge": False,          # nerd data: charge status + watts
    "show_temp": False,            # nerd data: battery temperature
    "max_fps": 0,                  # scrcpy --max-fps (0 = uncapped)
    "bitrate_mbps": 8,             # scrcpy --video-bit-rate, in Mbps
    "update_check": True,          # check GitHub for a newer release at startup
}


def cfg():
    return QSettings("PhoneDeck", "PhoneDeck")


def cfg_get(key, typ=str):
    return cfg().value(key, SETTING_DEFAULTS[key], type=typ)


# orientation dropdown: label -> degrees. The shape (portrait/landscape) is the
# virtual display's own W x H, so apps lay out for it; "Flipped" adds a 180°
# render rotation (scrcpy --orientation=180, touch mapped by scrcpy).
FEED_ORIENTATIONS = [("Portrait", 0), ("Landscape", 90),
                     ("Portrait Flipped", 180), ("Landscape Flipped", 270)]


def orientation_is_portrait():
    return cfg().value("feed_orientation", 0, type=int) in (0, 180)


def active_display_center():
    """(cx, cy) center point of the virtual display at its currently
    configured resolution and orientation, for swipe-based wheel scrolling.
    A 180-degree flip rotates the render but keeps the same W x H, so the
    center point is the same for the flipped and unflipped variant."""
    res = cfg_get("res_portrait") if orientation_is_portrait() else cfg_get("res_landscape")
    try:
        w, h = (int(v) for v in res.lower().split("x", 1))
    except (ValueError, AttributeError):
        w, h = (1080, 1920) if orientation_is_portrait() else (1920, 1080)
    return w // 2, h // 2


def run(args, timeout=20):
    """Run a command, return (rc, stdout+stderr) as text."""
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


# ---- adb helpers ----------------------------------------------------------
def adb(target, *args, timeout=20):
    return run([ADB, "-s", target, *args], timeout=timeout)


def _connected_devices():
    """(usb_serials, wireless_targets) currently in `adb devices`."""
    usb, wl = [], []
    rc, out = run([ADB, "devices"])
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s+device$", line)
        if m and m.group(1) != "List":
            t = m.group(1)
            (wl if re.match(r"\d+\.\d+\.\d+\.\d+:", t) else usb).append(t)
    return usb, wl


def _pc_gateways():
    """IPv4 default gateways: the phone's own IP when the PC is a client on
    the phone's hotspot (mDNS can't cross the hotspot, so this is how we find
    the phone's address there)."""
    ps = ("Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | "
          "ForEach-Object { $_.IPv4DefaultGateway.NextHop }")
    rc, out = run(["powershell", "-NoProfile", "-Command", ps], timeout=10)
    return [ln.strip() for ln in out.splitlines()
            if re.match(r"\d{1,3}(?:\.\d{1,3}){3}$", ln.strip())]


def enable_wifi_over_usb():
    """Phone on USB → put adbd in tcpip mode (listens on 5555 on ALL
    interfaces, including the phone's hotspot AP interface) → connect over the
    network so USB can be unplugged. Wireless debugging only binds the phone's
    wifi-client interface and is 'unavailable' while the phone is a hotspot, so
    this tcpip route is the only cordless option in hotspot mode. tcpip mode
    resets when the phone reboots, so rerun then. Returns (target|None, message)."""
    usb, _ = _connected_devices()
    if not usb:
        return None, "No USB device. Plug the phone in first."
    dev = usb[0]
    run([ADB, "-s", dev, "tcpip", "5555"], timeout=15)
    time.sleep(1.5)
    cands = []
    rc, out = run([ADB, "-s", dev, "shell", "ip", "-o", "-4", "addr",
                   "show", "scope", "global"], timeout=10)
    for m in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})", out):
        if m.group(1) not in cands:
            cands.append(m.group(1))
    for gw in _pc_gateways():
        if gw not in cands:
            cands.append(gw)
    for ip in cands:
        tgt = f"{ip}:5555"
        if "connected" in run([ADB, "connect", tgt], timeout=10)[1]:
            return tgt, f"Wi-Fi connected: {tgt}. USB can be unplugged."
    return None, "Enabled tcpip but couldn't reach the phone over the network."


def pair_device(addr, code):
    """`adb pair <ip:port> <code>` for wireless-debugging pairing. Returns
    (ok, message)."""
    rc, out = run([ADB, "pair", addr, code], timeout=25)
    ok = "Successfully paired" in out
    line = out.strip().splitlines()[-1] if out.strip() else (
        "paired" if ok else "pair failed")
    return ok, line


def find_connect_after_pair(ip, tries=8):
    """After pairing, the device advertises its (different) connect port over
    mDNS. Poll for it and connect. Returns the connected target or None."""
    for _ in range(tries):
        rc, out = run([ADB, "mdns", "services"], timeout=8)
        for line in out.splitlines():
            if _adb_mdns(line) and ip in line:
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
                if m and "connected" in run([ADB, "connect", m.group(1)])[1]:
                    return m.group(1)
        time.sleep(1.5)
    return None


APPS_ROOT = "/sdcard/Pictures/Apps"       # per-app drop folders live here
PUSH_STAGE = APPS_ROOT + "/_incoming"     # scrcpy drops land here, then moved
TAB_CAP = 6                               # max fixed-width tabs across the strip


def safe_folder(name):
    """A filesystem- and shell-safe folder name from an app label (quotes and
    path/wildcard chars removed so it can be single-quoted in adb shell)."""
    name = re.sub(r"[\\/:*?\"'`<>|]+", "", name or "").strip().rstrip(".")
    return name[:48] or "Unknown"


def foreground_pkg(target, display_id):
    """Package of the top/resumed activity on the given display, or None."""
    if display_id is None:
        return None
    rc, out = run([ADB, "-s", target, "shell", "dumpsys", "activity",
                   "activities"], timeout=12)
    in_disp = False
    for line in out.splitlines():
        if re.search(rf"Display #{display_id}\b", line):
            in_disp = True
            continue
        if in_disp:
            if re.search(r"Display #\d+", line):    # reached the next display
                break
            m = re.search(r"ActivityRecord\{[0-9a-f]+ \S+ ([A-Za-z0-9_.]+)/",
                          line)
            if m:
                return m.group(1)
    return None


def _setting(target, scope, key):
    """Read `settings get <scope> <key>` as a stripped string ('' if unset)."""
    rc, out = run([ADB, "-s", target, "shell", "settings", "get", scope, key],
                  timeout=8)
    out = out.strip()
    return "" if out in ("null", "None") else out


def mobile_bytes(target):
    """Total cellular (rmnet*) RX+TX bytes since boot, as (rx, tx). Mobile data
    rides the rmnet interfaces; summing them tracks cellular usage (what gets
    burned while tethering). Returns (None, None) if unreadable."""
    rc, out = run([ADB, "-s", target, "shell", "cat", "/proc/net/dev"], timeout=8)
    rx = tx = 0
    found = False
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("rmnet"):
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        cols = parts[1].split()
        if len(cols) >= 9:
            try:
                rx += int(cols[0]); tx += int(cols[8]); found = True
            except ValueError:
                pass
    return (rx, tx) if found else (None, None)


def tethering_active(target):
    """True if the phone is currently sharing its connection (USB/Wi-Fi/BT):
    a tethered downstream interface (rndis0 usb, ap0/wlan1 softap, bt-pan) has
    an IPv4 address assigned."""
    rc, out = run([ADB, "-s", target, "shell", "ip", "-o", "-4", "addr"], timeout=8)
    return bool(re.search(r"\b(rndis\d+|ap\d+|bt-pan|swlan\d+)\b.*inet ", out))


def fmt_bytes(n):
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def charge_info(target):
    """Battery/charging snapshot: dict(status, level, watts, amps). status is the
    Android battery status int (2=charging, 5=full). watts is instantaneous power
    at the battery (|current| x voltage)."""
    rc, out = run([ADB, "-s", target, "shell", "dumpsys", "battery"], timeout=8)
    d = {}
    for line in out.splitlines():
        m = re.match(r"\s*(status|level|voltage|temperature):\s*(-?\d+)\s*$", line)
        if m:
            d[m.group(1)] = int(m.group(2))
    rc, cur = run([ADB, "-s", target, "shell", "cat",
                   "/sys/class/power_supply/battery/current_now"], timeout=8)
    try:
        amps = int(cur.strip()) / 1e6          # µA -> A
    except ValueError:
        amps = None
    volts = d.get("voltage", 0) / 1000.0       # mV -> V
    watts = abs(amps) * volts if (amps is not None and volts) else None
    temp = d["temperature"] / 10.0 if "temperature" in d else None  # dC -> C
    return {"status": d.get("status"), "level": d.get("level"),
            "watts": watts, "amps": amps, "temp_c": temp}


def _section(text):
    """A small section-header label for form layouts."""
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#8aa0c0; font-weight:600; margin-top:6px;")
    return lbl


_ICON_CACHE = {}
_TILE_COLORS = ["#4f7cff", "#e0568a", "#2fae7a", "#e0913a", "#8a6ff0",
                "#3aa8c0", "#c0553a", "#6a8a2f", "#c04ab0", "#3a7ac0"]


def letter_icon(label, pkg):
    """A colored rounded tile with the app's initial, a lightweight sidebar
    icon (real launcher-icon extraction over adb isn't practical). Color is
    stable per package; cached."""
    ch = next((c for c in label.strip() if c.isalnum()), "?").upper()
    color = _TILE_COLORS[hash(pkg) % len(_TILE_COLORS)]
    key = (ch, color)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    n = 40
    pm = QPixmap(n, n); pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(color))); p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, n - 4, n - 4, 9, 9)
    p.setPen(QColor("#ffffff"))
    f = QFont(); f.setPointSize(15); f.setBold(True); p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def home_icon():
    """A little house tile for the Home tab."""
    key = ("__home__",)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    n = 40
    pm = QPixmap(n, n); pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor("#3a4550"))); p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, n - 4, n - 4, 9, 9)
    p.setPen(QPen(QColor("#e6e9ee"), 2.4)); p.setBrush(Qt.NoBrush)
    p.drawPolyline(QPolygonF([QPointF(12, 21), QPointF(20, 12),
                              QPointF(28, 21)]))          # roof
    p.drawRect(15, 21, 10, 8)                             # body
    p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def make_sparkline(values, w, h, color):
    """A tiny sparkline QPixmap of the recent values (auto-scaled)."""
    pm = QPixmap(w, h); pm.fill(Qt.transparent)
    if len(values) < 2:
        return pm
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 1.4))
    poly = QPolygonF()
    n = len(values)
    for i, v in enumerate(values):
        x = i * (w - 2) / (n - 1) + 1
        y = h - 2 - (v - lo) / span * (h - 4)
        poly.append(QPointF(x, y))
    p.drawPolyline(poly)
    p.end()
    return pm


def dim_active(target):
    """True if the screen is in manual mode pinned to minimum brightness."""
    if _setting(target, "system", "screen_brightness_mode") != "0":
        return False
    b = _setting(target, "system", "screen_brightness")
    return b.isdigit() and int(b) <= 2


def set_dim(target, on):
    """On: manual mode, brightness 1. Off: back to automatic brightness."""
    if on:
        run([ADB, "-s", target, "shell", "settings", "put", "system",
             "screen_brightness_mode", "0"], timeout=8)
        run([ADB, "-s", target, "shell", "settings", "put", "system",
             "screen_brightness", "1"], timeout=8)
    else:
        run([ADB, "-s", target, "shell", "settings", "put", "system",
             "screen_brightness_mode", "1"], timeout=8)


# Phone controls doable over adb WITHOUT root. Each: key -> dict(label, get, on,
# off). `get(target)` returns current bool; `on`/`off` are shell arg lists run
# as `adb -s <t> shell <args...>`. Hotspot/USB-tethering aren't here: Android
# gates softap/tether toggles behind root (shell hits a SecurityException); the
# Controls menu instead opens the phone's Tethering settings for those.
PHONE_CONTROLS = [
    ("wifi", "Wi-Fi",
     lambda t: _setting(t, "global", "wifi_on") == "1",
     ["svc", "wifi", "enable"], ["svc", "wifi", "disable"]),
    ("data", "Mobile data",
     lambda t: _setting(t, "global", "mobile_data") == "1",
     ["svc", "data", "enable"], ["svc", "data", "disable"]),
    ("airplane", "Airplane mode",
     lambda t: _setting(t, "global", "airplane_mode_on") == "1",
     ["cmd", "connectivity", "airplane-mode", "enable"],
     ["cmd", "connectivity", "airplane-mode", "disable"]),
    ("stayawake", "Stay awake while charging",
     lambda t: _setting(t, "global", "stay_on_while_plugged_in") not in ("", "0"),
     ["svc", "power", "stayon", "true"], ["svc", "power", "stayon", "false"]),
    ("taps", "Show taps",
     lambda t: _setting(t, "system", "show_touches") == "1",
     ["settings", "put", "system", "show_touches", "1"],
     ["settings", "put", "system", "show_touches", "0"]),
]


def _adb_mdns(line):
    """True for an adb-over-network mDNS service line we can `adb connect` to.
    Covers wireless-debugging TLS (`_adb-tls-connect._tcp`) AND legacy tcpip
    (`_adb._tcp`, port 5555); a phone acting as a hotspot advertises the
    legacy service, so matching only the TLS one silently misses it. The
    pairing service (`_adb-tls-pairing`) is NOT connectable, so exclude it."""
    return ("_adb-tls-pairing" not in line
            and ("_adb-tls-connect" in line or "_adb._tcp" in line))


def resolve_target(prefer=None):
    """Find a device to connect to. Order: the preferred (last good) device,
    then any connected USB device, then any connected wireless device, then
    discover any wireless-debugging device via mDNS. Returns a target or None.
    Device-agnostic, not tied to a specific serial."""
    run([ADB, "start-server"], timeout=15)
    # 1) preferred / last-good device
    if prefer:
        if re.match(r"\d+\.\d+\.\d+\.\d+:", prefer):
            run([ADB, "connect", prefer], timeout=10)
        usb, wl = _connected_devices()
        if prefer in usb or prefer in wl:
            return prefer
    # 2) any already-connected device (USB first, then wireless)
    usb, wl = _connected_devices()
    if usb:
        return usb[0]
    if wl:
        return wl[0]
    # 3) discover any wireless-debugging device
    run([ADB, "mdns", "check"], timeout=10)
    for _ in range(10):
        rc, out = run([ADB, "mdns", "services"], timeout=10)
        for line in out.splitlines():
            if _adb_mdns(line):
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
                if m and "connected" in run([ADB, "connect", m.group(1)])[1]:
                    return m.group(1)
        time.sleep(2)
    return None


_PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


def load_favorites():
    """Set of favorited package names, persisted in favorites.json."""
    try:
        with open(FAV_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_favorites(favs):
    try:
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(favs), f, indent=2)
    except Exception:
        pass


def force_focus(hwnd):
    """Give Windows keyboard focus to a window in another process (the embedded
    scrcpy child), via AttachThreadInput, so scrcpy captures keystrokes."""
    if not hwnd:
        return
    u = ctypes.windll.user32
    try:
        tgt = u.GetWindowThreadProcessId(hwnd, None)
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = tgt and tgt != cur
        if attached:
            u.AttachThreadInput(cur, tgt, True)
        u.SetFocus(hwnd)
        if attached:
            u.AttachThreadInput(cur, tgt, False)
    except Exception:
        pass


def kill_orphan_embeds():
    """Kill any leftover PhoneDeck scrcpy embeds (e.g. orphaned by a crash),
    identified by our unique window title, so virtual displays never pile up.
    scrcpy-server drops its virtual display when the process dies."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='scrcpy.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{EMBED_TITLE}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    run(["powershell", "-NoProfile", "-Command", ps], timeout=15)


def list_devices():
    """Connectable devices: currently-connected adb devices + wireless-debugging
    devices found via mDNS. Returns [(label, target)]. Labels lead with the
    device name (model) so devices are recognizable when IPs aren't static."""
    devices, seen = [], set()
    rc, o = run([ADB, "devices", "-l"])
    for line in o.splitlines():
        m = re.match(r"^(\S+)\s+device\b(.*)$", line)
        if m and m.group(1) != "List":
            t, rest = m.group(1), m.group(2)
            seen.add(t)
            kind = "wireless" if re.match(r"\d+\.\d+\.\d+\.\d+:", t) else "USB"
            nm = re.search(r"\bmodel:(\S+)", rest) or re.search(r"\bproduct:(\S+)", rest)
            name = nm.group(1).replace("_", " ") if nm else t
            devices.append((f"{name}   ·   {t}   ({kind}, connected)", t))
    rc, o = run([ADB, "mdns", "services"], timeout=8)
    for line in o.splitlines():
        if _adb_mdns(line):
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                mdns_name = line.split("\t", 1)[0].strip() if "\t" in line else ""
                label = (f"{mdns_name}   ·   {m.group(1)}   (wireless)"
                         if mdns_name else f"{m.group(1)}   (wireless)")
                devices.append((label, m.group(1)))
    return devices


def scrcpy_display_ids(target):
    """Set of display ids currently owned by scrcpy virtual displays."""
    rc, out = run([ADB, "-s", target, "shell", "dumpsys", "display"], timeout=15)
    return {int(m.group(1)) for m in _SCRCPY_VDISP_RE.finditer(out)}


def launcher_component(target, pkg):
    """Resolve pkg's main launcher activity as 'pkg/activity', or None."""
    rc, out = run([ADB, "-s", target, "shell", "cmd", "package",
                   "resolve-activity", "--brief", pkg], timeout=15)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(pkg + "/"):
            return line
    return None


def list_apps(target):
    """Return sorted list of (label, package) via `scrcpy --list-apps`."""
    rc, out = run([SCRCPY, "-s", target, "--list-apps"], timeout=90)
    apps, pending_label = [], None
    started = False
    for raw in out.splitlines():
        if "List of apps:" in raw:
            started = True
            continue
        if not started:
            continue
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in "*-":
            body = stripped[1:].strip()
            toks = body.split()
            if toks and _PKG_RE.match(toks[-1]):
                label = " ".join(toks[:-1]).strip() or toks[-1]
                apps.append((label, toks[-1]))
                pending_label = None
            else:
                pending_label = body      # label wrapped; pkg on next line
        elif pending_label is not None and _PKG_RE.match(stripped.split()[-1]):
            apps.append((pending_label, stripped.split()[-1]))
            pending_label = None
    # de-dup, sort case-insensitively by label
    seen, uniq = set(), []
    for label, pkg in apps:
        if pkg in seen:
            continue
        seen.add(pkg)
        uniq.append((label, pkg))
    uniq.sort(key=lambda x: x[0].lower())
    return uniq


# ---- background workers ---------------------------------------------------
class ConnectWorker(QThread):
    done = Signal(object)   # target or None

    def __init__(self, prefer=None):
        super().__init__()
        self.prefer = prefer

    def run(self):
        self.done.emit(resolve_target(self.prefer))


class AppsWorker(QThread):
    done = Signal(list)

    def __init__(self, target):
        super().__init__()
        self.target = target

    def run(self):
        try:
            self.done.emit(list_apps(self.target))
        except Exception:
            self.done.emit([])


class PushWorker(QThread):
    """Push dropped files to a per-app folder under Pictures/Apps and media-scan
    each so they show up in the app's (and any app's) image picker. Grouping by
    app means one app's drops can be cleared without touching the rest."""
    done = Signal(int, str)     # number pushed, dest folder

    def __init__(self, target, files, dest):
        super().__init__()
        self.target = target
        self.files = files
        self.dest = dest

    def run(self):
        # dest may contain spaces (e.g. "Blue Kik X"); single-quote it in every
        # on-device shell command. `adb push` takes the remote path as one arg,
        # so it needs no quoting.
        run([ADB, "-s", self.target, "shell", f"mkdir -p '{self.dest}'"])
        n = 0
        for f in self.files:
            name = os.path.basename(f)
            rc, _ = run([ADB, "-s", self.target, "push", f,
                         f"{self.dest}/{name}"], timeout=300)
            if rc == 0:
                run([ADB, "-s", self.target, "shell",
                     "am broadcast -a "
                     "android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                     f"-d 'file://{self.dest}/{name}'"], timeout=20)
                n += 1
        self.done.emit(n, self.dest)


class Poller(QThread):
    """One background thread for all periodic adb reads, so the UI thread never
    blocks on them. Every ~2s it relocates any scrcpy-grabbed drops and (when
    those readouts are enabled) reads the nerd-data stats, handing results back
    to the main thread via signals. Inputs (target, display_id, apps, flags) are
    set from the main thread; simple attribute assignments are atomic enough."""
    stats = Signal(dict)          # {"batt":..., "data":(rx,tx)|None, "teth":bool}
    moved = Signal(int, str)      # count, destination folder name

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self.target = None
        self.display_id = None
        self.apps = []
        self.current_app = None
        self.want_batt = False
        self.want_data = False

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            t = self.target
            if t:
                try:
                    self._relocate(t)
                    res = {}
                    if self.want_batt:
                        res["batt"] = charge_info(t)
                    if self.want_data:
                        res["data"] = mobile_bytes(t)
                        res["teth"] = tethering_active(t)
                    if res:
                        self.stats.emit(res)
                except Exception:
                    pass
            self._stop.wait(2.0)

    def _folder(self, t):
        pkg = foreground_pkg(t, self.display_id) or self.current_app
        if not pkg:
            return "Unknown"
        label = next((l for l, p in self.apps if p == pkg), pkg)
        return safe_folder(label)

    def _relocate(self, t):
        rc, out = run([ADB, "-s", t, "shell", f"ls -1 '{PUSH_STAGE}'"], timeout=8)
        files = [ln for ln in out.splitlines()
                 if ln.strip() and "No such file" not in ln]
        if not files:
            return
        dest = f"{APPS_ROOT}/{self._folder(t)}"
        run([ADB, "-s", t, "shell", f"mkdir -p '{dest}'"])
        moved = 0
        for name in files:
            rc, _ = run([ADB, "-s", t, "shell",
                         f"mv '{PUSH_STAGE}/{name}' '{dest}/'"], timeout=30)
            if rc == 0:
                run([ADB, "-s", t, "shell",
                     "am broadcast -a "
                     "android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                     f"-d 'file://{dest}/{name}'"], timeout=15)
                moved += 1
        if moved:
            self.moved.emit(moved, dest.rsplit("/", 1)[-1])


class UpdateChecker(QThread):
    """check_release() off the UI thread."""
    result = Signal(dict)

    def run(self):
        self.result.emit(check_release())


class UpdateDownloader(QThread):
    """download_update() off the UI thread, with progress and cancel."""
    progress = Signal(int, int)     # bytes done, total (0 = unknown)
    done = Signal(object, str)      # path or None, error text ("" = ok, "cancelled")

    def __init__(self, info):
        super().__init__()
        self.info = info
        self.cancelled = False

    def run(self):
        try:
            path = download_update(self.info, lambda d, t: self.progress.emit(d, t),
                                   lambda: self.cancelled)
            self.done.emit(path, "")
        except UpdateCancelled:
            self.done.emit(None, "cancelled")
        except UpdateError as e:
            self.done.emit(None, str(e))


# ---- keyboard bridge ------------------------------------------------------
class KeyBridge:
    """Forwards PC keystrokes to the phone's virtual display. Each command runs
    as its own direct `adb -s T shell input -d <id> ...` (executed in order by a
    worker thread), because a persistent piped `adb shell` buffers/delays the
    commands: text then commits to whatever field is focused when the backlog
    finally drains (landing in the wrong chat)."""

    def __init__(self):
        self.did = None
        self.target = None
        self._q = queue.Queue()
        self._worker = None
        self._running = False

    def start(self, target, did):
        self.stop()
        self._q = queue.Queue()   # fresh queue (stop() left a poison None)
        self.target = target
        self.did = did
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self):
        while self._running:
            try:
                cmd = self._q.get(timeout=0.4)
            except queue.Empty:
                continue
            if cmd is None:
                break
            run([ADB, "-s", self.target, "shell", cmd], timeout=15)

    def _enqueue(self, cmd):
        if self.did is not None and self._running:
            self._q.put(cmd)

    def key(self, code):
        self._enqueue(f"input -d {self.did} keyevent {code}")

    def text(self, s):
        # whole phrase in one command; `input text` wants spaces as %s
        esc = s.replace("'", "'\\''").replace(" ", "%s")
        self._enqueue(f"input -d {self.did} text '{esc}'")

    def swipe(self, x1, y1, x2, y2, ms=60):
        self._enqueue(f"input -d {self.did} swipe {x1} {y1} {x2} {y2} {ms}")

    def stop(self):
        self._running = False
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        self.did = None


# ---- wheel bridge ---------------------------------------------------------
_WH_MOUSE_LL = 14
_WM_MOUSEWHEEL = 0x020A
_WM_LBUTTONDOWN = 0x0201


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


_LL_PROC = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                            ctypes.POINTER(_MSLLHOOKSTRUCT))


class WheelBridge:
    """Low-level mouse hook. Two jobs: (1) forward wheel-over-display as adb
    swipes (the reparented scrcpy child ignores the wheel); (2) on each click,
    route the keyboard: click the search box -> keys go to search, click
    anywhere else -> keys go to the phone."""

    def __init__(self, kb, main):
        self.kb = kb
        self.main = main
        self._hot = main._display_hot   # callable -> bool (active + over display)
        self._hook = None
        self._cb = _LL_PROC(self._proc)   # keep the callback ref alive
        self._last = 0.0
        u = ctypes.windll.user32
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, _LL_PROC,
                                        ctypes.c_void_p, wintypes.DWORD]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     wintypes.WPARAM,
                                     ctypes.POINTER(_MSLLHOOKSTRUCT)]
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self._u = u

    def install(self):
        if self._hook:
            return
        k = ctypes.windll.kernel32
        k.GetModuleHandleW.restype = ctypes.c_void_p
        hmod = k.GetModuleHandleW(None)
        self._hook = self._u.SetWindowsHookExW(_WH_MOUSE_LL, self._cb, hmod, 0)

    def remove(self):
        if self._hook:
            self._u.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _proc(self, nCode, wParam, lParam):
        try:
            if (nCode == 0 and wParam == _WM_LBUTTONDOWN
                    and self.main.isActiveWindow()):
                # click the display -> hand keyboard focus to scrcpy (smooth
                # typing straight to the phone). click the search box -> leave
                # Qt focus alone so app-search typing works.
                if self.main._search_hit():
                    QTimer.singleShot(0, self.main._focus_search)
                else:
                    QTimer.singleShot(0, lambda: force_focus(self.main.embed.hwnd))
            if (nCode == 0 and wParam == _WM_MOUSEWHEEL
                    and self.kb.did is not None and self._hot()):
                now = time.monotonic()
                if now - self._last >= 0.04:
                    self._last = now
                    raw = (lParam[0].mouseData >> 16) & 0xFFFF
                    delta = raw - 0x10000 if raw & 0x8000 else raw
                    dist = cfg_get("scroll_dist", int)
                    natural = cfg_get("scroll_natural", bool)
                    down = (delta < 0) == natural     # scroll content down?
                    cx, cy = active_display_center()
                    half = max(40, dist // 2)
                    y1, y2 = ((cy + half, cy - half) if down
                              else (cy - half, cy + half))
                    self.kb.swipe(cx, y1, cx, y2)
        except Exception:
            pass
        return self._u.CallNextHookEx(None, nCode, wParam, lParam)


# ---- About / Licenses (same design as Lazarus PDF's about_ui.py) ----------
# Lazarus PDF runs Fusion + a dark palette + Segoe UI 13px app-wide; PhoneDeck
# doesn't, so those are applied to these two dialogs only.
DIALOG_STYLE = """
QWidget{font-family:'Segoe UI';font-size:13px;color:#e6e6e6}
QDialog{background:#16181d}
QLabel{background:transparent}
QPushButton{padding:5px 12px;border:1px solid #3a4150;border-radius:6px;
            background:#252a33}
QPushButton:hover{background:#2e3440}
QPushButton:pressed{background:#2e3440}
QTextBrowser{background:#1b1f26;border:none;font-family:Consolas}
QTabWidget::pane{border:1px solid #3a4150;background:#1b1f26}
QTabBar::tab{background:#16181d;color:#9aa0a6;border:1px solid #3a4150;
             border-bottom:none;padding:5px 10px;margin-right:2px;
             border-top-left-radius:4px;border-top-right-radius:4px}
QTabBar::tab:selected{background:#2b3039;color:#ffffff}
QTabBar::tab:hover{background:#2e3440}
"""
_FUSION = None


def _family_dialog(dlg):
    """Give a dialog Lazarus PDF's look: Fusion, its dark palette, Segoe UI."""
    global _FUSION
    if _FUSION is None:
        _FUSION = QStyleFactory.create("Fusion")
    pal = QPalette()
    for role, c in [(QPalette.Window, "#16181d"), (QPalette.WindowText, "#e6e6e6"),
                    (QPalette.Base, "#1b1f26"), (QPalette.AlternateBase, "#22262e"),
                    (QPalette.Text, "#e6e6e6"), (QPalette.Button, "#252a33"),
                    (QPalette.ButtonText, "#e6e6e6"), (QPalette.Highlight, "#2d6cdf"),
                    (QPalette.HighlightedText, "#ffffff"),
                    (QPalette.Link, "#7fd3ff"), (QPalette.Light, "#3a4150"),
                    (QPalette.Midlight, "#2e3440"), (QPalette.Mid, "#252a33"),
                    (QPalette.Dark, "#16181d"), (QPalette.Shadow, "#0a0c10")]:
        pal.setColor(role, QColor(c))
    dlg.setPalette(pal)
    dlg.setFont(QFont("Segoe UI", 10))
    dlg.setStyleSheet(DIALOG_STYLE + dlg.styleSheet())
    for wdg in [dlg] + dlg.findChildren(QWidget):
        wdg.setStyle(_FUSION)


def about_html():
    return f"""
<p style="font-size:15px;margin:0 0 10px 0"><b>Run your Android phone's apps in windows on your PC.</b></p>
<p>Pick an app from a searchable list and it opens in its own window on your desktop, while the phone's
own screen stays free. Type with your PC keyboard, click and scroll with the mouse, pin favorite apps, and
drag files onto the window to send them to the phone. Connects over USB or wireless debugging.</p>
<p>Everything runs on this computer. PhoneDeck talks to your phone directly over USB or your local
network. Nothing is uploaded.</p>
<p><b>Updates:</b> the only internet requests are GitHub's latest-release lookup for PhoneDeck (at
startup if Settings &gt; "Check for updates at startup" is on, or File &gt; Check for updates) and, if
you choose to update, the installer download. No telemetry, no IDs, nothing about your phone, apps or
files is sent.</p>
<p>Built on scrcpy by Genymobile and Android Debug Bridge (adb) from the Android SDK Platform-Tools.
Uses Qt for Python (PySide6) and pywin32.</p>
<p style="margin-top:12px">By <b>DarkRelay</b> &nbsp;·&nbsp; Version {VERSION}<br>
Free software under the Apache License 2.0; it comes with NO WARRANTY. See Help &gt; Licenses.<br>
Source code: <a href="{SOURCE_URL}"><span style="color:#7fd3ff">{SOURCE_URL}</span></a></p>
"""


class _Backdrop(QWidget):
    """The DarkRelay banner behind the About content: cropped so the relay
    node sits to the right, and darkened (more on the left, where the text
    is) so everything stays easy to read. Same as Lazarus PDF's."""

    def __init__(self):
        super().__init__()
        self._src = QPixmap(BANNER_PATH)
        self._cache = None

    def paintEvent(self, e):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor("#0a0e14"))
        if not self._src.isNull():
            if self._cache is None or self._cache.size() != r.size():
                sw, sh = self._src.width(), self._src.height()
                # crop the banner's height, with the width the dialog's shape
                # needs, the node at ~78%
                cw = min(sw, int(sh * r.width() / max(1, r.height())))
                x0 = max(0, min(sw - cw, int(sw / 2 - cw * 0.78)))
                self._cache = self._src.copy(x0, 0, cw, sh).scaled(
                    r.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap(0, 0, self._cache)
        g = QLinearGradient(0, 0, r.width(), 0)
        g.setColorAt(0.0, QColor(5, 7, 10, 235))
        g.setColorAt(0.62, QColor(5, 7, 10, 205))
        g.setColorAt(1.0, QColor(5, 7, 10, 120))
        p.fillRect(QRectF(r), g)
        p.end()


# ---- embedded scrcpy ------------------------------------------------------
class ScrcpyEmbed(QWidget):
    """Hosts an embedded, borderless scrcpy window that shows a *virtual*
    external display (so the phone's own screen is left alone). Emits
    display_ready(id) once Android has created the virtual display, so the
    UI can target app launches / nav at that display."""

    display_ready = Signal(int)
    spawn_failed = Signal(str)     # scrcpy could not even be started
    display_timeout = Signal()     # scrcpy is running but no virtual display showed up

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background:#0b0d10;")
        self.proc = None
        self.hwnd = None
        self.target = None
        self.display_id = None
        self._ids_before = set()
        self._win_poll = QTimer(self)
        self._win_poll.timeout.connect(self._find_and_attach)
        self._disp_poll = QTimer(self)
        self._disp_poll.timeout.connect(self._detect_display)
        self._disp_tries = 0
        self._start_tries = 0

    def start(self, target):
        self.stop()
        kill_orphan_embeds()   # sweep any leftover embed from a prior crash
        self.target = target
        self.display_id = None
        # scrcpy handles the MOUSE only (sdk = absolute, cursor not captured).
        # The keyboard is DISABLED in scrcpy: embedding steals the window focus
        # scrcpy's keyboard needs, so PhoneDeck captures keystrokes itself and
        # forwards them over adb (KeyBridge). show_ime_with_hard_keyboard keeps
        # the phone's own soft keyboard behaviour sane.
        run([ADB, "-s", target, "shell", "settings", "put", "secure",
             "show_ime_with_hard_keyboard", "1"], timeout=10)
        self._ids_before = scrcpy_display_ids(target)
        self._start_tries = 0
        self._spawn()

    def _spawn(self):
        # scrcpy sometimes aborts with "Server connection failed" if the prior
        # instance's phone-side server is still clearing, retried by _detect.
        self._start_tries += 1
        self._log = open(SCRCPY_LOG, "w", encoding="utf-8", errors="replace")
        if orientation_is_portrait():
            res_str, dpi = cfg_get("res_portrait"), cfg_get("dpi_portrait", int)
        else:
            res_str, dpi = cfg_get("res_landscape"), cfg_get("dpi_landscape", int)
        res = f"{res_str}/{dpi}"
        args = [SCRCPY, "-s", self.target,
                f"--new-display={res}",
                "--keyboard=sdk", "--raw-key-events", "--mouse=sdk",
                "--display-ime-policy=hide",   # no on-screen keyboard (PC types)
                "--window-borderless", f"--window-title={EMBED_TITLE}",
                f"--video-bit-rate={cfg_get('bitrate_mbps', int)}M",
                # any drop scrcpy grabs itself lands in a PhoneDeck-only staging
                # dir (never /sdcard/Download), which the app then moves into
                # the focused app's folder (see MainWindow._poll_stage)
                f"--push-target={PUSH_STAGE}/",
                "--no-audio"]
        fps = cfg_get("max_fps", int)
        if fps > 0:
            args.append(f"--max-fps={fps}")
        if cfg().value("feed_orientation", 0, type=int) in (180, 270):
            args.append("--orientation=180")   # flipped: render only
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            # scrcpy.exe missing, or otherwise unlaunchable. Not a transient
            # failure like the "Server connection failed" race below, so
            # don't retry, just stop and report it.
            self.proc = None
            self._log.close()
            self._win_poll.stop()
            self._disp_poll.stop()
            self.spawn_failed.emit(f"Could not start scrcpy:\n{SCRCPY}\n\n{e}")
            return
        self._win_poll.start(300)
        self._disp_tries = 0
        self._disp_poll.start(500)

    def _find_and_attach(self):
        hwnd = win32gui.FindWindow(None, EMBED_TITLE)
        if not hwnd:
            return
        self._win_poll.stop()
        self.hwnd = hwnd
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        style = (style & ~win32con.WS_POPUP & ~win32con.WS_CAPTION
                 & ~win32con.WS_THICKFRAME) | win32con.WS_CHILD
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)
        win32gui.SetParent(hwnd, int(self.winId()))
        self._fit()
        self._revoke_drop()
        # The first fit can land relative to a stale origin (the reparent
        # hasn't fully settled the instant the window appears), leaving the
        # child parked off-screen. Re-fit after layout settles so it can't
        # stick there. scrcpy also registers its OWN file-drop target during
        # video init, slightly AFTER the window appears, so re-revoke over
        # the same window; otherwise drops land in /sdcard/Download via scrcpy
        # instead of bubbling to PhoneDeck's per-app-folder handler.
        for ms in (150, 500, 1500, 3000):
            QTimer.singleShot(ms, self._fit)
            QTimer.singleShot(ms, self._revoke_drop)

    def _revoke_drop(self):
        if not self.hwnd:
            return
        try:
            ole = ctypes.windll.ole32
            ole.RevokeDragDrop.argtypes = [ctypes.c_void_p]
            ole.RevokeDragDrop(self.hwnd)
        except Exception:
            pass

    def _detect_display(self):
        self._disp_tries += 1
        if self.display_id is not None:
            return
        # scrcpy died early (the connection race), retry a few times
        if self.proc is not None and self.proc.poll() is not None:
            self._disp_poll.stop()
            self._win_poll.stop()
            if self._start_tries < 6:
                QTimer.singleShot(900, self._spawn)
            else:
                self.spawn_failed.emit(
                    "scrcpy kept exiting before a display was ready, "
                    f"after {self._start_tries} attempts. See:\n{SCRCPY_LOG}")
            return
        if self.target:
            new = scrcpy_display_ids(self.target) - self._ids_before
            if new:
                self.display_id = max(new)
                self._disp_poll.stop()
                self.display_ready.emit(self.display_id)
                return
        if self._disp_tries > 30:      # ~15s
            self._disp_poll.stop()
            self.display_timeout.emit()

    def _fit(self):
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            win32gui.MoveWindow(self.hwnd, 0, 0, self.width(), self.height(), True)

    def resizeEvent(self, e):
        self._fit()
        super().resizeEvent(e)

    def stop(self, wait=False):
        """Close the scrcpy PhoneDeck started (its own child process, nothing else). wait=True
        (before an update) waits for it to exit, killing it after 3 s, so Setup can replace
        the bundled scrcpy files."""
        self._win_poll.stop()
        self._disp_poll.stop()
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        self.hwnd = None
        self.display_id = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            if wait:
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(2)
                    except subprocess.TimeoutExpired:
                        pass
        self.proc = None


# ---- tab strip ------------------------------------------------------------
class TabButton(QFrame):
    """One fixed-width tab: icon + app name + ✕. Clicking the body switches to
    it; clicking ✕ closes it. Emits its index."""
    clicked = Signal(int)
    closed = Signal(int)

    def __init__(self, index, label, icon, active):
        super().__init__()
        self.index = index
        self.setFixedSize(150, 28)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 4, 0)
        lay.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(icon.pixmap(18, 18))
        ic.setStyleSheet("background:transparent;")
        lay.addWidget(ic)
        name = QLabel(label)
        name.setStyleSheet("background:transparent; color:#e6e9ee;")
        lay.addWidget(name, 1)
        x = QToolButton()
        x.setText("✕")
        x.setCursor(Qt.PointingHandCursor)
        x.setStyleSheet("QToolButton{border:none; background:transparent;"
                        "color:#9aa4b2; padding:0 2px;}"
                        "QToolButton:hover{color:#fff;}")
        x.clicked.connect(lambda: self.closed.emit(self.index))
        lay.addWidget(x)
        bg = "#243044" if active else "#171b21"
        self.setStyleSheet(f"TabButton{{background:{bg}; border-radius:6px;}}")

    def mousePressEvent(self, e):
        self.clicked.emit(self.index)
        super().mousePressEvent(e)


# ---- main window ----------------------------------------------------------
class PhoneDeck(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoneDeck")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.setAcceptDrops(True)      # drag files in -> push to phone gallery
        self.resize(1180, 820)
        self.target = None
        self.apps = []
        self.kb = KeyBridge()   # adb bridge, now only for wheel-scroll swipes
        self.wheel = WheelBridge(self.kb, self)
        # background workers; kept here (not just a local var) so a worker
        # already in flight isn't dropped (and GC'd mid-run, which crashes
        # Qt) by a second trigger before it finishes
        self._cw = None    # ConnectWorker
        self._aw = None    # AppsWorker
        self._pw = None    # PushWorker
        self._uc = None    # UpdateChecker
        self._dl = None    # UpdateDownloader
        self._pending_drops = collections.deque()   # (files, dest) queued while _pw runs

        # custom top bar: drawer toggle, rotate, File dropdown
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        topbar = QFrame()
        topbar.setFixedHeight(30)
        topbar.setStyleSheet("background:#15181d;")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(6, 2, 6, 2)
        tl.setSpacing(4)
        self._side_btn = QToolButton()
        self._side_btn.setText("☰")
        self._side_btn.setCursor(Qt.PointingHandCursor)
        self._side_btn.setToolTip("Show/hide app drawer")
        self._side_btn.clicked.connect(self._toggle_sidebar)
        tl.addWidget(self._side_btn)
        self._rot_btn = QToolButton()
        self._rot_btn.setCursor(Qt.PointingHandCursor)
        self._rot_btn.setToolTip("Display orientation")
        self._rot_btn.setPopupMode(QToolButton.InstantPopup)
        rm = QMenu(self)
        self._rot_group = QActionGroup(self)
        self._rot_group.setExclusive(True)
        cur = cfg().value("feed_orientation", 0, type=int)
        for label, deg in FEED_ORIENTATIONS:
            act = rm.addAction(label)
            act.setCheckable(True)
            act.setChecked(deg == cur)
            act.triggered.connect(lambda _=False, d=deg: self._set_orientation(d))
            self._rot_group.addAction(act)
        self._rot_btn.setMenu(rm)
        self._rot_label(cur)
        tl.addWidget(self._rot_btn)
        file_btn = QToolButton()
        file_btn.setText("File")
        file_btn.setPopupMode(QToolButton.InstantPopup)
        fm = QMenu(self)
        fm.addAction("Settings…", self.open_settings)
        fm.addAction("Check for updates…", lambda: self.check_updates(False))
        file_btn.setMenu(fm)
        tl.addWidget(file_btn)
        conn_btn = QToolButton()
        conn_btn.setText("Connections")
        conn_btn.setPopupMode(QToolButton.InstantPopup)
        self._conn_menu = nm = QMenu(self)
        nm.aboutToShow.connect(self._rebuild_connections)
        conn_btn.setMenu(nm)
        tl.addWidget(conn_btn)
        ctl_btn = QToolButton()
        ctl_btn.setText("Controls")
        ctl_btn.setPopupMode(QToolButton.InstantPopup)
        self._ctl_menu = cm = QMenu(self)
        self._ctl_actions = {}
        for key, label, getf, on_cmd, off_cmd in PHONE_CONTROLS:
            act = cm.addAction(label)
            act.setCheckable(True)
            # pass the action and key explicitly; sender() is unreliable
            # through a lambda slot
            act.triggered.connect(
                lambda checked, a=act, k=key, o=on_cmd, f=off_cmd:
                    self._control_toggle(a, k, checked, o, f))
            self._ctl_actions[key] = (act, getf)
        self._dim_act = cm.addAction("Dim screen (brightness 1)")
        self._dim_act.setCheckable(True)
        self._dim_act.triggered.connect(self._toggle_dim)
        cm.addSeparator()
        cm.addAction("Tethering & hotspot settings…", self._open_tether_settings)
        cm.aboutToShow.connect(self._refresh_controls)
        ctl_btn.setMenu(cm)
        tl.addWidget(ctl_btn)
        help_btn = QToolButton()
        help_btn.setText("Help")
        help_btn.setPopupMode(QToolButton.InstantPopup)
        self._help_menu = hm = QMenu(self)
        hm.addAction("About PhoneDeck…", self.open_about)
        hm.addAction("Licenses…", self.open_licenses)
        hm.addSeparator()
        hm.addAction("Support DarkRelay", self.open_support)
        help_btn.setMenu(hm)
        tl.addWidget(help_btn)
        tl.addStretch(1)
        outer.addWidget(topbar)

        # body: sidebar + display
        bodyw = QWidget()
        body = QHBoxLayout(bodyw)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addWidget(bodyw, 1)

        self.side = side = QFrame()
        side.setFixedWidth(260)
        side.setStyleSheet("background:#101317;")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 8, 8, 8)
        sl.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search apps…")
        self.search.textChanged.connect(self.filter_apps)
        sl.addWidget(self.search)
        self.favorites = load_favorites()
        self.applist = QListWidget()
        self.applist.setIconSize(QSize(24, 24))
        self.applist.itemActivated.connect(self.launch_selected)
        self.applist.itemClicked.connect(self.launch_selected)
        self.applist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.applist.customContextMenuRequested.connect(self._app_menu)
        self.applist.setFocusPolicy(Qt.NoFocus)   # don't swallow keystrokes
        sl.addWidget(self.applist, 1)

        # nav buttons under the app list
        navrow = QHBoxLayout()
        navrow.setSpacing(6)
        for name in ("Back", "Home", "Recents"):
            b = QPushButton(name)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, n=name: self.nav(n))
            navrow.addWidget(b)
        sl.addLayout(navrow)
        body.addWidget(side)

        # right column: tab strip (aligned with the search box) over the display
        rightw = QWidget()
        rightcol = QVBoxLayout(rightw)
        rightcol.setContentsMargins(0, 8, 0, 0)
        rightcol.setSpacing(6)
        self.tabbar = QFrame()
        self.tabbar.setFixedHeight(30)
        self.tabbar.setStyleSheet("background:transparent;")
        self._tabrow = QHBoxLayout(self.tabbar)
        self._tabrow.setContentsMargins(2, 0, 2, 0)
        self._tabrow.setSpacing(4)
        rightcol.addWidget(self.tabbar)
        self.embed = ScrcpyEmbed()
        self.embed.display_ready.connect(self._display_ready)
        self.embed.spawn_failed.connect(self._on_spawn_failed)
        self.embed.display_timeout.connect(self._on_display_timeout)
        self.embed.setFocusPolicy(Qt.StrongFocus)
        rightcol.addWidget(self.embed, 1)
        body.addWidget(rightw, 1)

        # tab model: always at least a Home tab (pkg None = phone home screen)
        self.tabs = [{"pkg": None, "label": "Home"}]
        self.active_tab = 0
        self._render_tabs()

        self.status = self.statusBar()
        self.status.showMessage("Connecting…")
        self._temp_lbl = QLabel("")
        self._temp_lbl.setStyleSheet("color:#9aa4b2; padding:0 6px;")
        self.status.addPermanentWidget(self._temp_lbl)
        self._charge_lbl = QLabel("")
        self._charge_lbl.setStyleSheet("color:#9aa4b2; padding:0 6px;")
        self.status.addPermanentWidget(self._charge_lbl)
        self._charge_spark = QLabel("")
        self.status.addPermanentWidget(self._charge_spark)
        self._data_lbl = QLabel("")
        self._data_lbl.setStyleSheet("color:#9aa4b2; padding:0 6px;")
        self.status.addPermanentWidget(self._data_lbl)
        self._data_spark = QLabel("")
        self.status.addPermanentWidget(self._data_spark)
        self._update_info = None
        self._update_last = None
        # a newer release: one compact status-bar button whose menu holds Lazarus PDF's
        # update-bar choices (never a popup, never takes the focus)
        self._update_btn = QToolButton()
        self._update_btn.setObjectName("update_btn")
        self._update_btn.setText("Update")
        self._update_btn.setFocusPolicy(Qt.NoFocus)
        self._update_btn.setPopupMode(QToolButton.InstantPopup)
        self._update_btn.setStyleSheet(
            "QToolButton#update_btn{background:#1f3352;border:1px solid #2d6cdf;"
            "border-radius:5px;padding:1px 10px;color:#e6eefc;font-size:12px}"
            "QToolButton#update_btn:hover{background:#2a4470}")
        um = self._update_menu = QMenu(self)
        self._act_notes = um.addAction("Release notes…", self._show_release_notes)
        self._act_install = um.addAction("Download and install",
                                         lambda: self._start_update_download())
        um.addSeparator()
        self._act_skip = um.addAction("Skip this version", self._skip_update)
        self._act_later = um.addAction("Later", self._later_update)
        self._update_btn.setMenu(um)
        self._update_btn.hide()
        self.status.addPermanentWidget(self._update_btn)
        self._charge_hist = collections.deque(maxlen=40)
        self._rate_hist = collections.deque(maxlen=40)
        self._data_base = None          # (rx, tx) baseline for this session
        self._data_last = None          # (rx, tx, monotonic) for rate calc
        self.poller = Poller()          # all periodic adb reads, off the UI thread
        self.poller.stats.connect(self._on_stats)
        self.poller.moved.connect(self._on_moved)
        self.poller.start()

        self._apply_theme()
        self._restore_geometry()
        QTimer.singleShot(200, self.connect_phone)
        if cfg_get("update_check", bool):
            QTimer.singleShot(UPDATE_STARTUP_DELAY_MS, lambda: self.check_updates(silent=True))

    # Typing goes through scrcpy's own keyboard (instant) once the display
    # holds Windows focus; WheelBridge hands it focus on a display click.
    def _mouse_over_display(self):
        gp = QCursor.pos()
        tl = self.embed.mapToGlobal(self.embed.rect().topLeft())
        return (tl.x() <= gp.x() <= tl.x() + self.embed.width() and
                tl.y() <= gp.y() <= tl.y() + self.embed.height())

    def _search_hit(self):
        # is the cursor over the app-search box right now?
        gp = QCursor.pos()
        tl = self.search.mapToGlobal(self.search.rect().topLeft())
        return (tl.x() <= gp.x() <= tl.x() + self.search.width() and
                tl.y() <= gp.y() <= tl.y() + self.search.height())

    def _focus_search(self):
        # Reclaim WINDOWS keyboard focus for the Qt window (the scrcpy child may
        # be holding it after a display click), then focus the search box, else
        # keystrokes like Backspace leak to scrcpy/the phone instead of the box.
        force_focus(int(self.winId()))
        self.search.setFocus()

    def _display_hot(self):
        # for the global wheel hook: only act when PhoneDeck is active and the
        # cursor is over the display
        return self.isActiveWindow() and self._mouse_over_display()

    def _toggle_sidebar(self):
        self.side.setVisible(not self.side.isVisible())
        QTimer.singleShot(0, self.embed._fit)   # display fills the freed space

    @staticmethod
    def _orient_name(deg):
        return dict((d, l) for l, d in FEED_ORIENTATIONS).get(deg, "Portrait")

    def _rot_label(self, deg):
        self._rot_btn.setText(f"⟳ {self._orient_name(deg)}")

    def _set_orientation(self, deg):
        # recreates the virtual display at the new W x H (+ 180° if flipped)
        if deg == cfg().value("feed_orientation", 0, type=int):
            return
        cfg().setValue("feed_orientation", deg)
        self._rot_label(deg)
        for act in self._rot_group.actions():   # keep the dropdown tick in sync
            act.setChecked(act.text() == self._orient_name(deg))
        if self.target:
            # the old display dies with the embed; reopen the active tab's app
            # on the new one once it's ready
            self._pending_launch = self.tabs[self.active_tab]["pkg"]
            self.embed.start(self.target)

    # -- window placement (persist; default to the right-most monitor) --
    def _restore_geometry(self):
        s = QSettings("PhoneDeck", "PhoneDeck")
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
            return
        screens = QApplication.screens()
        target = max(screens, key=lambda sc: sc.geometry().x())
        g = target.availableGeometry()
        self.resize(min(1400, g.width() - 80), min(900, g.height() - 80))
        self.move(g.x() + (g.width() - self.width()) // 2,
                  g.y() + (g.height() - self.height()) // 2)

    # -- theme --
    def _apply_theme(self):
        self.setStyleSheet(self.styleSheet() + """
            QWidget { color:#e6e9ee; font-size:13px; }
            QMainWindow, QFrame { background:#101317; }
            QLineEdit { background:#1b1f26; border:1px solid #2a2f38;
                        border-radius:6px; padding:6px; }
            QListWidget { background:#101317; border:none; outline:none; }
            QListWidget::item { padding:8px 6px; border-radius:6px; }
            QListWidget::item:selected { background:#243044; }
            QListWidget::item:hover { background:#1a2028; }
            QPushButton { background:#232a34; border:1px solid #2f3742;
                          border-radius:6px; padding:6px 12px; }
            QPushButton:hover { background:#2c3540; }
            QPushButton:pressed { background:#3a4550; }
            QPushButton:disabled { color:#5b6472; background:#191d23; }
            QMenuBar { background:#15181d; color:#e6e9ee; }
            QMenuBar::item:selected { background:#243044; }
            QMenu { background:#15181d; color:#e6e9ee; border:1px solid #2a2f38; }
            QMenu::item:selected { background:#243044; }
            QStatusBar { background:#15181d; color:#9aa4b2; }
            QToolButton { background:transparent; color:#d7dbe2; border:none;
                          border-radius:5px; padding:2px 9px; font-size:14px; }
            QToolButton:hover { background:#243044; }
            QToolButton:pressed { background:#2c3540; }
            QToolButton::menu-indicator { image:none; width:0; }
            QComboBox { background:#1b1f26; border:1px solid #2a2f38;
                        border-radius:6px; padding:4px; }
            QSpinBox { background:#1b1f26; border:1px solid #2a2f38;
                       border-radius:6px; padding:4px 22px 4px 6px; }
            QSpinBox::up-button { subcontrol-origin:border;
                subcontrol-position:top right; width:20px; background:#232a34;
                border-left:1px solid #2a2f38; border-top-right-radius:6px; }
            QSpinBox::down-button { subcontrol-origin:border;
                subcontrol-position:bottom right; width:20px; background:#232a34;
                border-left:1px solid #2a2f38; border-bottom-right-radius:6px; }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background:#2c3540; }
            QDialog { background:#101317; }
        """)

    # -- connection --
    def _check_binaries(self):
        """PhoneDeck's own adb.exe/scrcpy.exe must exist before connecting.
        Shows a plain dialog naming whichever is missing and the path it
        looked in, instead of letting a missing binary present itself as
        "no device found". Returns True if both are present."""
        missing = [(name, path) for name, path in
                   (("adb.exe", ADB), ("scrcpy.exe", SCRCPY))
                   if not os.path.exists(path)]
        if not missing:
            return True
        lines = "\n\n".join(f"{name} not found at:\n{path}"
                            for name, path in missing)
        QMessageBox.critical(
            self, "PhoneDeck files missing",
            "PhoneDeck can't find its own tools.\n\n" + lines +
            "\n\nPhoneDeck looks in the scrcpy folder next to "
            "PhoneDeck.exe, then on PATH. Reinstall PhoneDeck to put them "
            "back.")
        self.status.showMessage("scrcpy/adb not found, see the error dialog")
        return False

    def connect_phone(self):
        if self._cw is not None and self._cw.isRunning():
            self.status.showMessage("Already connecting…", 3000)
            return
        if not self._check_binaries():
            return
        # prefer the last good device, else auto-discover any device
        last = cfg().value("last_target")
        self.status.showMessage("Connecting…")
        self._cw = ConnectWorker(last)
        self._cw.done.connect(self._connected)
        self._cw.start()

    def reconnect(self):
        # same as startup: prefer last good device, else discover
        self.connect_phone()

    def connect_to(self, target):
        # connect to a specific device chosen from Device search
        run([ADB, "connect", target], timeout=10)
        self._connected(target)

    # -- recent devices (quick switcher) --
    def _recent_list(self):
        try:
            return json.loads(cfg().value("recent_devices", "[]"))
        except Exception:
            return []

    def _recent_add(self, target):
        """Record target (with its model name) at the top of the recents list."""
        rc, out = run([ADB, "-s", target, "shell", "getprop", "ro.product.model"],
                      timeout=8)
        name = out.strip() or target
        recents = [d for d in self._recent_list() if d.get("target") != target]
        recents.insert(0, {"name": name, "target": target})
        cfg().setValue("recent_devices", json.dumps(recents[:6]))

    def _rebuild_connections(self):
        """Populate the Connections menu: actions + recent devices to switch to."""
        m = self._conn_menu
        m.clear()
        refresh_act = m.addAction("Refresh apps", self.load_apps)
        refresh_act.setEnabled(not (self._aw is not None and self._aw.isRunning()))
        reconnect_act = m.addAction("Reconnect", self.reconnect)
        reconnect_act.setEnabled(not (self._cw is not None and self._cw.isRunning()))
        m.addAction("Device search…", self.device_search)
        recents = self._recent_list()
        if recents:
            m.addSeparator()
            hdr = m.addAction("Recent devices")
            hdr.setEnabled(False)
            for d in recents:
                tgt, name = d.get("target"), d.get("name", "")
                mark = "● " if tgt == self.target else "   "
                act = m.addAction(f"{mark}{name}   ({tgt})")
                act.triggered.connect(lambda _=False, t=tgt: self.connect_to(t))

    def _refresh_controls(self):
        """Tick each control to the phone's current state before the menu shows."""
        if not self.target:
            for act, _ in self._ctl_actions.values():
                act.setEnabled(False)
            return
        for act, getf in self._ctl_actions.values():
            act.setEnabled(True)
            try:
                act.setChecked(bool(getf(self.target)))
            except Exception:
                pass
        self._dim_act.setEnabled(True)
        try:
            self._dim_act.setChecked(dim_active(self.target))
        except Exception:
            pass

    def _toggle_dim(self):
        if not self.target:
            self.status.showMessage("No device connected", 4000)
            return
        set_dim(self.target, self._dim_act.isChecked())

    def _is_wireless_kill(self, key, checked):
        """True if this toggle would cut the adb link PhoneDeck is using:
        connected over Wi-Fi (target is IP:port) and turning Wi-Fi off or
        Airplane mode on. USB targets never qualify."""
        if not (self.target and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}:\d+",
                                             self.target)):
            return False
        return (key == "wifi" and not checked) or (key == "airplane" and checked)

    def _control_toggle(self, act, key, checked, on_cmd, off_cmd):
        if not self.target:
            self.status.showMessage("No device connected", 4000)
            return
        if self._is_wireless_kill(key, checked):
            what = "Turning Wi-Fi off" if key == "wifi" else \
                   "Turning Airplane mode on"
            ans = QMessageBox.warning(
                self, "This will disconnect PhoneDeck",
                f"PhoneDeck is connected to {self.target} over Wi-Fi.\n\n"
                f"{what} cuts that connection, and PhoneDeck can't turn it "
                "back on. You'd have to undo it on the phone itself.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if ans != QMessageBox.Yes:
                act.setChecked(not checked)
                return
        cmd = on_cmd if checked else off_cmd
        rc, out = run([ADB, "-s", self.target, "shell", *cmd], timeout=12)
        msg = out.strip().splitlines()[-1] if out.strip() else ""
        if rc == 0 and "Exception" not in msg and "denied" not in msg.lower():
            self.status.showMessage(f"{' '.join(cmd)} ✓", 4000)
        else:
            self.status.showMessage(f"Failed: {msg or 'see phone'}", 6000)

    def _open_tether_settings(self):
        """Open the phone's Hotspot & tethering screen (softap/USB-tether can't
        be toggled over adb without root; user flips them there)."""
        if not self.target:
            self.status.showMessage("No device connected", 4000)
            return
        run([ADB, "-s", self.target, "shell", "am", "start", "-a",
             "android.settings.TETHER_SETTINGS"], timeout=10)
        self.status.showMessage("Opened Tethering settings on the phone", 5000)

    # -- nerd-data readouts (driven by the background Poller) --
    def _sync_data_monitor(self):
        """Point the poller at which stats to read, and clear any now-off labels.
        The poller thread itself always runs (it also relocates dropped files)."""
        self.poller.want_batt = (cfg_get("show_charge", bool)
                                 or cfg_get("show_temp", bool))
        self.poller.want_data = cfg_get("show_data_usage", bool)
        if not cfg_get("show_temp", bool):
            self._temp_lbl.clear()
        if not cfg_get("show_charge", bool):
            self._charge_lbl.clear(); self._charge_spark.clear()
        if not cfg_get("show_data_usage", bool):
            self._data_lbl.clear(); self._data_spark.clear()

    def _clear_nerd_labels(self):
        for w in (self._data_lbl, self._charge_lbl, self._temp_lbl,
                  self._data_spark, self._charge_spark):
            w.clear()

    def _on_stats(self, res):
        """Render a batch of readings from the poller (runs on the UI thread)."""
        c = res.get("batt")
        if c is not None:
            if cfg_get("show_temp", bool):
                self._render_temp(c)
            if cfg_get("show_charge", bool):
                self._render_charge(c)
        if "data" in res and cfg_get("show_data_usage", bool):
            self._render_data(res["data"], res.get("teth", False))

    def _on_moved(self, n, folder):
        self.status.showMessage(
            f"Moved {n} dropped file(s) to Pictures/Apps/{folder}", 5000)

    def _render_temp(self, c):
        t = c.get("temp_c")
        self._temp_lbl.setText(f"🌡 {t:.1f}°C" if t is not None else "")

    def _render_charge(self, c):
        lvl = c["level"]
        lvl_s = f"{lvl}%" if lvl is not None else "?"
        if c["status"] == 5:
            self._charge_lbl.setText(f"⚡ full  {lvl_s}")
        elif c["status"] == 2 and c["watts"] is not None:
            self._charge_lbl.setText(f"⚡ {c['watts']:.1f} W  {lvl_s}")
        elif c["watts"] is not None and c["amps"] is not None and c["amps"] < 0:
            self._charge_lbl.setText(f"🔋 -{c['watts']:.1f} W  {lvl_s}")
        else:
            self._charge_lbl.setText(f"🔋 {lvl_s}")
        if c["watts"] is not None:
            self._charge_hist.append(c["watts"])
            self._charge_spark.setPixmap(
                make_sparkline(list(self._charge_hist), 48, 16, "#e0913a"))

    def _render_data(self, data, teth_active):
        rx, tx = data
        if rx is None:
            self._data_lbl.setText("")
            return
        now = time.monotonic()
        if self._data_base is None:
            self._data_base = (rx, tx)
        rate = ""
        if self._data_last:
            prx, ptx, pt = self._data_last
            dt = now - pt
            if dt > 0:
                dn, up = max(0, rx - prx) / dt, max(0, tx - ptx) / dt
                rate = f"  ↓{fmt_bytes(dn)}/s ↑{fmt_bytes(up)}/s"
                self._rate_hist.append(dn + up)
                self._data_spark.setPixmap(
                    make_sparkline(list(self._rate_hist), 48, 16, "#3aa8c0"))
        self._data_last = (rx, tx, now)
        teth = "📡 " if teth_active else "📶 "
        self._data_lbl.setText(
            f"{teth}mobile  ↓{fmt_bytes(max(0, rx - self._data_base[0]))}"
            f" ↑{fmt_bytes(max(0, tx - self._data_base[1]))}{rate}")

    # -- updates --
    def check_updates(self, silent=False):
        """Ask GitHub for the latest release. silent=True is the startup check: it says
        nothing unless a newer version is out, and then only in the status bar."""
        if self._uc is not None and self._uc.isRunning():
            if not silent:
                self.status.showMessage("Already checking for updates…", 3000)
            return False
        self._uc = UpdateChecker()
        self._uc.result.connect(lambda d, silent=silent: self._on_update_result(d, silent))
        self._uc.start()
        if not silent:
            self.status.showMessage("Checking GitHub for a newer PhoneDeck…", 3000)
        return True

    def _on_update_result(self, res, silent):
        self._update_last = res
        st = res.get("status")
        if st == "newer":
            self._update_info = res
            if silent and res["version"] == cfg().value(UPDATE_SKIP_KEY, "", type=str):
                return                   # skipped: quiet (a newer one still shows)
            self._update_btn.setText(f"Update to {res['version']}")
            self._update_btn.setToolTip(
                f"PhoneDeck {res['version']} is available (this is {VERSION}): release notes, "
                "download and install, skip this version, or later.")
            self._update_btn.show()
            if silent:
                self.status.showMessage(
                    f"PhoneDeck {res['version']} is available: Update is at the bottom right.",
                    8000)
                return
            self._start_update_download(res)
            return
        if silent:
            return                       # startup: quiet unless there's news
        if st == "current":
            QMessageBox.information(self, "Updates", f"PhoneDeck is up to date ({VERSION}).")
        elif st == "none":
            QMessageBox.information(self, "Updates", "No PhoneDeck release is published yet. "
                                    f"This is {VERSION}.")
        else:
            QMessageBox.warning(self, "Updates", "Couldn't check for updates: "
                                f"{res.get('reason', 'unknown error')}")

    def _show_release_notes(self):
        """The release's notes (Markdown, links open in the browser), with Download and install."""
        info = getattr(self, "_update_info", None)
        if not info:
            return
        self._notes_dlg = dlg = QDialog(self)
        dlg.setWindowTitle(f"PhoneDeck {info['version']}: release notes")
        dlg.resize(620, 460)
        v = QVBoxLayout(dlg)
        head = QLabel(f"<b>PhoneDeck {info['version']}</b>&nbsp;&nbsp;"
                      f"<span style='color:#9fb4c8'>this is {VERSION}</span>")
        v.addWidget(head)
        notes = (info.get("notes") or "").strip()
        page = info.get("page") or RELEASES_URL
        if notes:
            tb = QTextBrowser()
            tb.setObjectName("release_notes")
            tb.setOpenExternalLinks(True)
            tb.setMarkdown(notes)
            v.addWidget(tb, 1)
        else:
            lbl = QLabel("This release has no notes. See the release page:<br>"
                         f"<a href='{page}'><span style='color:#7fd3ff'>{page}</span></a>")
            lbl.setObjectName("release_notes_link")
            lbl.setTextFormat(Qt.RichText)
            lbl.setOpenExternalLinks(True)
            lbl.setWordWrap(True)
            v.addWidget(lbl)
            v.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        inst = QPushButton("Download and install")
        inst.clicked.connect(lambda: (dlg.accept(), self._start_update_download()))
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(dlg.reject)
        row.addWidget(inst)
        row.addWidget(close)
        v.addLayout(row)
        _family_dialog(dlg)
        dlg.exec()

    def _skip_update(self):
        info = getattr(self, "_update_info", None)
        if info:
            cfg().setValue(UPDATE_SKIP_KEY, info["version"])
            self.status.showMessage(f"PhoneDeck {info['version']} skipped. A newer version will "
                                    "still show.", 5000)
        self._update_btn.hide()

    def _later_update(self):
        """Hide the button for this session; the next startup check offers it again."""
        self._update_btn.hide()

    def _start_update_download(self, info=None):
        if self._dl is not None and self._dl.isRunning():
            self.status.showMessage("Already downloading an update…", 3000)
            return False
        info = info if isinstance(info, dict) else getattr(self, "_update_info", None)
        if not info:
            return False
        if not can_install():
            self._update_last = {"status": "refused", "reason": "source run"}
            QMessageBox.information(
                self, "Updates", f"PhoneDeck {info['version']} is available, but this is a "
                "source run: it doesn't install updates over itself. Use the installer from "
                f"the Releases page:\n{info.get('page') or RELEASES_URL}")
            return False
        dlg = self._up_dlg = QProgressDialog(f"Downloading PhoneDeck {info['version']}…",
                                             "Cancel", 0, 100, self)
        dlg.setWindowTitle("Updating PhoneDeck")
        dlg.setMinimumWidth(320)
        dlg.setWindowModality(Qt.NonModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        d = self._dl = UpdateDownloader(info)
        dlg.canceled.connect(lambda: setattr(d, "cancelled", True))
        d.progress.connect(lambda done, total: (dlg.setMaximum(max(1, total)),
                                                dlg.setValue(min(done, max(1, total))))
                           if self._up_dlg is dlg else None)
        d.done.connect(lambda path, err, info=info: self._on_update_downloaded(path, err, info))
        d.start()
        return True

    def _on_update_downloaded(self, path, err, info):
        d = self._dl
        dlg, self._up_dlg = self._up_dlg, None
        if dlg is not None:
            dlg.close()
            dlg.deleteLater()
        if d is not None:
            d.wait()
        if err == "cancelled":
            self._update_last = {"status": "cancelled"}
            self.status.showMessage("Update download cancelled; nothing was kept.", 5000)
            return
        if err:
            self._update_last = {"status": "download-error", "reason": err}
            QMessageBox.warning(self, "Updates", err)
            return
        if QMessageBox.question(
                self, "Update PhoneDeck",
                f"Update to {info['version']} now? PhoneDeck will close and reopen.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            self._update_last = {"status": "postponed", "setup": path}
            self.status.showMessage("Update postponed. Use Update at the bottom right "
                                    "when you're ready.", 6000)
            return
        self._install_update(path)

    def _install_update(self, setup):
        """Close PhoneDeck's own scrcpy, start Setup silently, quit. Setup replaces the files
        and starts PhoneDeck again (/RELAUNCH=1)."""
        self.embed.stop(wait=True)          # only the scrcpy PhoneDeck started itself
        if not launch_setup(setup):
            self._update_last = {"status": "launch-failed"}
            QMessageBox.warning(self, "Updates",
                                "Setup couldn't be started (was the Windows prompt declined?).")
            return False
        self._update_last = {"status": "installing", "setup": str(setup)}
        self.close()
        QApplication.instance().quit()
        return True

    def open_about(self):
        self._about_dlg = dlg = QDialog(self)
        dlg.setWindowTitle("About PhoneDeck")
        dlg.setMinimumSize(680, 600)
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)
        backdrop = _Backdrop()
        backdrop.setObjectName("about_backdrop")
        outer.addWidget(backdrop)
        v = QVBoxLayout(backdrop)
        v.setContentsMargins(26, 22, 26, 16)
        head = QHBoxLayout()
        icon = QLabel()
        icon.setObjectName("about_icon")
        if os.path.exists(ICON_PATH):
            icon.setPixmap(QIcon(ICON_PATH).pixmap(64, 64))
        head.addWidget(icon, 0, Qt.AlignTop)
        title = QLabel(
            "<span style='font-size:22px;font-weight:700;color:#ffffff'>"
            "PhoneDeck</span><br>"
            f"<span style='color:#9fb4c8'>Version {VERSION}</span>")
        head.addSpacing(10)
        head.addWidget(title, 1, Qt.AlignVCenter)
        v.addLayout(head)
        v.addSpacing(8)
        body = QLabel(about_html())
        body.setObjectName("about_body")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setOpenExternalLinks(True)
        body.setStyleSheet("color:#e6eef6;font-size:13px;background:transparent")
        body.setMaximumWidth(560)
        v.addWidget(body)
        v.addStretch(1)
        row = QHBoxLayout()
        lic = QPushButton("Licenses…")
        lic.clicked.connect(self.open_licenses)
        sup = QPushButton("Support DarkRelay")
        sup.clicked.connect(self.open_support)
        row.addWidget(lic)
        row.addWidget(sup)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(dlg.accept)
        row.addWidget(bb)
        v.addLayout(row)
        _family_dialog(dlg)
        body.setPalette(dlg.palette())
        dlg.exec()

    def open_licenses(self):
        """Help > Licenses: one tab per component. Shows the full text when
        its files ship in licenses\\ (all of them, one after another), else a link
        to the official license page."""
        self._lic_dlg = dlg = QDialog(self)
        dlg.setWindowTitle("PhoneDeck: licenses")
        dlg.resize(900, 640)
        v = QVBoxLayout(dlg)
        intro = QLabel("PhoneDeck is free software under the Apache License 2.0. Source "
                       f"code: <a href='{SOURCE_URL}'><span style='color:#7fd3ff'>"
                       f"{SOURCE_URL}</span></a>. It includes the third-party components "
                       "below, each under its own license.")
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        v.addWidget(intro)
        tabs = QTabWidget()
        tabs.setObjectName("licenses_tabs")
        for tab, name, lic, fnames, url in LICENSE_ENTRIES:
            page = QWidget()
            pv = QVBoxLayout(page)
            head = QLabel(f"<b>{name}</b>&nbsp;&nbsp;"
                          f"<span style='color:#9fb4c8'>{lic}</span>")
            pv.addWidget(head)
            parts = []
            for fname in fnames:
                path = os.path.normpath(os.path.join(LICENSES_DIR, fname))
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        parts.append((os.path.basename(path), f.read()))
                except OSError:
                    pass
            text = None
            if len(parts) == 1:
                text = parts[0][1]
            elif parts:
                rule = "=" * 78
                text = "\n\n".join(f"{rule}\n{n}\n{rule}\n\n{t}" for n, t in parts)
            if text is not None:
                tb = QTextBrowser()
                tb.setObjectName("license_text")
                tb.setLineWrapMode(QTextBrowser.NoWrap)
                f = tb.font()
                f.setFamily("Consolas")
                tb.setFont(f)
                tb.setPlainText(text)
                pv.addWidget(tb, 1)
            else:
                if url:
                    msg = ("The full license text isn't included with this "
                           "copy. Read it here:<br><a href='" + url + "'>"
                           "<span style='color:#7fd3ff'>" + url + "</span></a>")
                else:
                    msg = "License to be added."
                note = QLabel(msg)
                note.setObjectName("license_link")
                note.setTextFormat(Qt.RichText)
                note.setOpenExternalLinks(True)
                note.setWordWrap(True)
                pv.addWidget(note)
                pv.addStretch(1)
            tabs.addTab(page, tab)
        v.addWidget(tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        _family_dialog(dlg)
        dlg.exec()

    def open_support(self):
        """Help > Support DarkRelay: Buy Me a Coffee in the default browser."""
        open_url(SUPPORT_URL)

    def device_search(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Device search")
        dlg.resize(440, 340)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Devices (USB + wireless debugging):"))
        lst = QListWidget()
        v.addWidget(lst, 1)

        def refresh():
            lst.clear()
            for label, target in list_devices():
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, target)
                lst.addItem(it)
            if lst.count():
                lst.setCurrentRow(0)
        refresh()

        def do_connect():
            it = lst.currentItem()
            if it:
                dlg.accept()
                self.connect_to(it.data(Qt.UserRole))

        lst.itemActivated.connect(lambda _: do_connect())

        def wifi_over_usb():
            self.status.showMessage("Enabling Wi-Fi over USB…")
            QApplication.processEvents()
            tgt, msg = enable_wifi_over_usb()
            self.status.showMessage(msg, 8000)
            if tgt:
                dlg.accept()
                self.connect_to(tgt)
            else:
                refresh()

        note = QLabel("Wi-Fi over USB: enables cordless adb (works on a "
                      "hotspot); redo after a phone reboot.")
        note.setStyleSheet("color:#9aa4b2;")
        note.setWordWrap(True)
        v.addWidget(note)
        row = QHBoxLayout()
        rb = QPushButton("Refresh"); rb.clicked.connect(refresh)
        wb = QPushButton("Wi-Fi over USB"); wb.clicked.connect(wifi_over_usb)
        pb = QPushButton("Pair new…")
        pb.clicked.connect(lambda: (dlg.accept(), self.pair_dialog()))
        row.addWidget(rb); row.addWidget(wb); row.addWidget(pb); row.addStretch(1)
        cb = QPushButton("Connect"); cb.clicked.connect(do_connect)
        xb = QPushButton("Cancel"); xb.clicked.connect(dlg.reject)
        row.addWidget(cb); row.addWidget(xb)
        v.addLayout(row)
        dlg.exec()

    def pair_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Pair wireless device")
        form = QFormLayout(dlg)
        info = QLabel("On the phone: Developer options → Wireless debugging → "
                      "Pair device with pairing code. Enter the address and "
                      "code it shows.")
        info.setWordWrap(True); form.addRow(info)
        addr = QLineEdit(); addr.setPlaceholderText("192.168.x.x:37xxx")
        code = QLineEdit(); code.setPlaceholderText("6-digit code")
        form.addRow("Pairing IP:port", addr)
        form.addRow("Code", code)
        st = QLabel(""); st.setStyleSheet("color:#9aa4b2;"); st.setWordWrap(True)
        form.addRow(st)

        def do_pair():
            a, c = addr.text().strip(), code.text().strip()
            if not a or not c:
                st.setText("Enter both the address and the code."); return
            st.setText("Pairing…"); QApplication.processEvents()
            ok, msg = pair_device(a, c)
            if not ok:
                st.setText(msg); return
            st.setText(msg + "  Finding device…"); QApplication.processEvents()
            tgt = find_connect_after_pair(a.split(":")[0])
            if tgt:
                dlg.accept(); self.connect_to(tgt)
            else:
                st.setText(msg + "  Paired. Open Device search to connect.")

        row = QHBoxLayout()
        pair_btn = QPushButton("Pair"); pair_btn.clicked.connect(do_pair)
        close_btn = QPushButton("Close"); close_btn.clicked.connect(dlg.reject)
        row.addStretch(1); row.addWidget(pair_btn); row.addWidget(close_btn)
        form.addRow(row)
        dlg.setMinimumWidth(360)
        dlg.exec()

    def open_settings(self):
        if self._cw is not None and self._cw.isRunning():
            self.status.showMessage(
                "Still connecting, try Settings again in a moment", 4000)
            return
        self._settings_dlg = dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        form = QFormLayout(dlg)

        def _res_combo(items, current):
            c = QComboBox(); c.setEditable(True); c.addItems(items)
            c.setCurrentText(current); return c

        def _dpi_spin(val):
            s = QSpinBox(); s.setRange(120, 480); s.setSingleStep(20)
            s.setValue(val); return s

        ls_res = _res_combo(["1280x720", "1600x900", "1920x1080", "2560x1440"],
                            cfg_get("res_landscape"))
        ls_dpi = _dpi_spin(cfg_get("dpi_landscape", int))
        pt_res = _res_combo(["720x1280", "900x1600", "1080x1920", "1440x2560"],
                            cfg_get("res_portrait"))
        pt_dpi = _dpi_spin(cfg_get("dpi_portrait", int))
        form.addRow(_section("Landscape"))
        form.addRow("Resolution", ls_res)
        form.addRow("Density (dpi)", ls_dpi)
        form.addRow(_section("Portrait"))
        form.addRow("Resolution", pt_res)
        form.addRow("Density (dpi)", pt_dpi)

        fps = QSpinBox(); fps.setRange(0, 120); fps.setSingleStep(5)
        fps.setSpecialValueText("Uncapped"); fps.setValue(cfg_get("max_fps", int))
        bitrate = QSpinBox(); bitrate.setRange(1, 50); bitrate.setSuffix(" Mbps")
        bitrate.setValue(cfg_get("bitrate_mbps", int))
        form.addRow(_section("Streaming"))
        form.addRow("Max FPS", fps)
        form.addRow("Bitrate", bitrate)

        scroll = QSpinBox(); scroll.setRange(60, 800); scroll.setSingleStep(20)
        scroll.setSuffix(" px"); scroll.setValue(cfg_get("scroll_dist", int))
        natural = QCheckBox("Natural (wheel up scrolls up)")
        natural.setChecked(cfg_get("scroll_natural", bool))
        form.addRow(_section("Other"))
        form.addRow("Scroll distance", scroll)
        form.addRow("", natural)

        data_usage = QCheckBox("Show mobile data usage (for tethering)")
        data_usage.setChecked(cfg_get("show_data_usage", bool))
        charge = QCheckBox("Show charge rate (watts + battery %)")
        charge.setChecked(cfg_get("show_charge", bool))
        temp = QCheckBox("Show battery temperature")
        temp.setChecked(cfg_get("show_temp", bool))
        upd = QCheckBox("Check for updates at startup")
        upd.setObjectName("set_update_check")
        upd.setToolTip("Asks GitHub for the latest PhoneDeck release in the background a few "
                       "seconds after startup. Nothing else is sent.")
        upd.setChecked(cfg_get("update_check", bool))
        form.addRow(_section("Updates"))
        form.addRow("", upd)
        form.addRow(_section("Nerd data"))
        form.addRow("", data_usage)
        form.addRow("", charge)
        form.addRow("", temp)
        note = QLabel("Resolution/density applies to the matching orientation "
                      "and reconnects the display when it changes.")
        note.setStyleSheet("color:#9aa4b2;")
        note.setWordWrap(True)
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() == QDialog.Accepted:
            portrait = orientation_is_portrait()
            reskey = "res_portrait" if portrait else "res_landscape"
            dpikey = "dpi_portrait" if portrait else "dpi_landscape"
            old = (cfg_get(reskey), cfg_get(dpikey, int),
                   cfg_get("max_fps", int), cfg_get("bitrate_mbps", int))
            c = cfg()
            c.setValue("res_landscape", ls_res.currentText())
            c.setValue("dpi_landscape", ls_dpi.value())
            c.setValue("res_portrait", pt_res.currentText())
            c.setValue("dpi_portrait", pt_dpi.value())
            c.setValue("max_fps", fps.value())
            c.setValue("bitrate_mbps", bitrate.value())
            c.setValue("scroll_dist", scroll.value())
            c.setValue("scroll_natural", natural.isChecked())
            c.setValue("show_data_usage", data_usage.isChecked())
            c.setValue("show_charge", charge.isChecked())
            c.setValue("show_temp", temp.isChecked())
            c.setValue("update_check", upd.isChecked())
            self._sync_data_monitor()
            new = (cfg_get(reskey), cfg_get(dpikey, int),
                   cfg_get("max_fps", int), cfg_get("bitrate_mbps", int))
            if self.target and new != old:
                self.embed.start(self.target)   # restart display at new size

    def _connected(self, target):
        self.target = target
        self.poller.target = target
        if not target:
            self.status.showMessage("No device found. Pick one, or plug in / "
                                    "enable Wireless debugging then Refresh")
            QTimer.singleShot(300, self.device_search)   # first-run / no device
            return
        QSettings("PhoneDeck", "PhoneDeck").setValue("last_target", target)
        self._recent_add(target)
        self.status.showMessage(f"Connected  ({target})")
        self.embed.start(target)
        self.load_apps()
        self._data_base = None
        run([ADB, "-s", target, "shell", f"mkdir -p '{PUSH_STAGE}'"])
        self.poller.target = target
        self._sync_data_monitor()

    def _display_ready(self, did):
        self.status.showMessage(f"Connected  ({self.target})")
        self.kb.start(self.target, did)
        self.wheel.install()
        self.poller.display_id = did
        pend = getattr(self, "_pending_launch", None)
        if pend:
            self._pending_launch = None
            QTimer.singleShot(150, lambda p=pend: self._launch_now(p))

    def _on_spawn_failed(self, msg):
        self.status.showMessage("scrcpy failed to start", 6000)
        QMessageBox.critical(self, "scrcpy error", msg)

    def _on_display_timeout(self):
        self.status.showMessage(
            "Virtual display not detected (scrcpy version may not match)",
            8000)

    # -- apps --
    def load_apps(self):
        if not self.target:
            return
        if self._aw is not None and self._aw.isRunning():
            self.status.showMessage("Already loading apps…", 3000)
            return
        self.status.showMessage("Loading apps…")
        self._aw = AppsWorker(self.target)
        self._aw.done.connect(self._apps_loaded)
        self._aw.start()

    def _apps_loaded(self, apps):
        self.apps = apps
        self.poller.apps = apps
        self.filter_apps(self.search.text())
        if self.target:
            self.status.showMessage(f"Connected  ({self.target})")

    # -- per-app launch profiles (preferred orientation) --
    def _profiles(self):
        """pkg -> launch orientation in degrees (0/90/180/270). Older
        'portrait'/'landscape' strings are read as 0/90."""
        try:
            raw = json.loads(cfg().value("app_profiles", "{}"))
        except Exception:
            return {}
        legacy = {"portrait": 0, "landscape": 90}
        out = {}
        for pkg, v in raw.items():
            v = legacy.get(v, v)
            if v in (0, 90, 180, 270):
                out[pkg] = v
        return out

    def _profile(self, pkg):
        return self._profiles().get(pkg)

    def _set_profile(self, pkg, deg):
        """deg: 0/90/180/270, or None to clear (back to Default)."""
        profs = self._profiles()
        if deg is not None:          # 0 (Portrait) is falsy, check None
            profs[pkg] = deg
        else:
            profs.pop(pkg, None)
        cfg().setValue("app_profiles", json.dumps(profs))
        self.filter_apps(self.search.text())

    def _open_app_info(self, pkg):
        """Open the phone's App info (settings details) screen for a package,
        on the virtual display if there is one."""
        if not self.target:
            self.status.showMessage("No device connected", 4000)
            return
        args = ["am", "start"]
        if self.embed.display_id is not None:
            args += ["--display", str(self.embed.display_id)]
        args += ["-a", "android.settings.APPLICATION_DETAILS_SETTINGS",
                 "-d", f"package:{pkg}"]
        run([ADB, "-s", self.target, "shell", *args], timeout=10)
        self.status.showMessage(f"Opened App info for {self._app_label(pkg)}", 4000)

    def _clear_app_images(self, pkg):
        """Delete the files PhoneDeck dropped into this app's folder."""
        if not self.target:
            self.status.showMessage("No device connected", 4000)
            return
        folder = safe_folder(self._app_label(pkg))
        dest = f"{APPS_ROOT}/{folder}"
        rc, out = run([ADB, "-s", self.target, "shell", f"ls -1 '{dest}'"],
                      timeout=10)
        files = [ln for ln in out.splitlines()
                 if ln.strip() and "No such file" not in ln]
        if not files:
            self.status.showMessage(f"No dropped images for {folder}", 4000)
            return
        if QMessageBox.question(
                self, "Clear images",
                f"Delete {len(files)} image(s) from Pictures/Apps/{folder}?"
                ) != QMessageBox.Yes:
            return
        run([ADB, "-s", self.target, "shell", f"rm -f '{dest}'/*"], timeout=30)
        run([ADB, "-s", self.target, "shell",
             "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
             f"-d 'file://{dest}'"], timeout=20)
        self.status.showMessage(
            f"Cleared {len(files)} image(s) from {folder}", 5000)

    def _add_header(self, text):
        it = QListWidgetItem(text)
        it.setFlags(Qt.NoItemFlags)          # non-selectable divider
        it.setForeground(QColor("#6b7480"))
        f = it.font(); f.setBold(True); f.setPointSize(max(8, f.pointSize() - 1))
        it.setFont(f)
        self.applist.addItem(it)

    def _add_app(self, label, pkg):
        star = "★ " if pkg in self.favorites else ""
        it = QListWidgetItem(letter_icon(label, pkg), star + label)
        it.setData(Qt.UserRole, pkg)
        prof = self._profile(pkg)
        if prof is not None:
            it.setToolTip(f"Launches in {self._orient_name(prof)}")
        self.applist.addItem(it)

    def filter_apps(self, text):
        text = (text or "").lower()
        self.applist.clear()
        match = lambda l, p: text in l.lower() or text in p.lower()
        favs = [(l, p) for (l, p) in self.apps
                if p in self.favorites and match(l, p)]
        if favs:
            self._add_header("★  FAVORITES")
            for l, p in favs:
                self._add_app(l, p)
            self._add_header("ALL APPS")
        for l, p in self.apps:
            if match(l, p):
                self._add_app(l, p)

    def _app_menu(self, pos):
        item = self.applist.itemAt(pos)
        if not item:
            return
        pkg = item.data(Qt.UserRole)
        if not pkg:
            return
        menu = QMenu(self)
        new_act = menu.addAction("Open in new tab")
        new_act.setEnabled(len(self.tabs) < TAB_CAP)
        replace_act = menu.addAction("Replace current tab")
        menu.addSeparator()
        fav = pkg in self.favorites
        fav_act = menu.addAction("Remove from Favorites" if fav
                                 else "Add to Favorites")
        om = menu.addMenu("Launch orientation")
        og = QActionGroup(om)
        og.setExclusive(True)
        cur_prof = self._profile(pkg)
        orient_acts = {}
        for label, deg in [("Default", None)] + FEED_ORIENTATIONS:
            a = om.addAction(label)
            a.setCheckable(True)
            a.setChecked(deg == cur_prof)
            og.addAction(a)
            orient_acts[a] = deg
        info_act = menu.addAction("App info (on phone)")
        clear_act = menu.addAction("Clear dropped images")
        chosen = menu.exec(self.applist.mapToGlobal(pos))
        if chosen in orient_acts:
            self._set_profile(pkg, orient_acts[chosen])
        elif chosen == new_act:
            self._open_in_tab(pkg, new=True)
        elif chosen == replace_act:
            self._open_in_tab(pkg, new=False)
        elif chosen == fav_act:
            if fav:
                self.favorites.discard(pkg)
            else:
                self.favorites.add(pkg)
            save_favorites(self.favorites)
            self.filter_apps(self.search.text())
        elif chosen == info_act:
            self._open_app_info(pkg)
        elif chosen == clear_act:
            self._clear_app_images(pkg)

    def launch_selected(self, item):
        # left-click a sidebar app -> launch it in the current tab (replace)
        if not item or not self.target:
            return
        pkg = item.data(Qt.UserRole)
        if not pkg:                          # header row
            return
        self._open_in_tab(pkg, new=False)

    def _launch_now(self, pkg):
        prof = self._profile(pkg)
        if (prof is not None and self.target and
                prof != cfg().value("feed_orientation", 0, type=int)):
            # app has its own orientation: rebuild the display at that shape;
            # _display_ready relaunches pkg once the new display is up
            self._set_orientation(prof)
            self._pending_launch = pkg
            return
        self._current_app = pkg          # fallback for per-app drop folder
        self.poller.current_app = pkg
        did = self.embed.display_id
        if did is None:
            # display not ready yet, fall back to phone screen
            adb(self.target, "shell", "monkey", "-p", pkg,
                "-c", "android.intent.category.LAUNCHER", "1")
            return
        comp = launcher_component(self.target, pkg)
        if comp:
            adb(self.target, "shell", "am", "start", "--display", str(did),
                "-n", comp)
        else:
            adb(self.target, "shell", "am", "start", "--display", str(did),
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", pkg)

    # -- tabs (one display; a tab = a running app brought to the front) --
    def _render_tabs(self):
        while self._tabrow.count():
            it = self._tabrow.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        for i, t in enumerate(self.tabs):
            icon = home_icon() if t["pkg"] is None else letter_icon(t["label"],
                                                                    t["pkg"])
            tb = TabButton(i, t["label"], icon, i == self.active_tab)
            tb.clicked.connect(self._switch_tab)
            tb.closed.connect(self._close_tab)
            self._tabrow.addWidget(tb)
        self._tabrow.addStretch(1)

    def _show_tab(self, t):
        """Bring a tab's app (or the home screen) to the front of the display."""
        if not self.target:
            return
        if t["pkg"] is None:
            self._go_home()
        else:
            self._launch_now(t["pkg"])

    def _go_home(self):
        self._current_app = None
        self.poller.current_app = None
        did = self.embed.display_id
        if self.target and did is not None:
            adb(self.target, "shell", "input", "-d", str(did), "keyevent", "3")

    def _open_in_tab(self, pkg, new):
        label = self._app_label(pkg)
        if new:
            if len(self.tabs) >= TAB_CAP:
                self.status.showMessage(f"Tab limit reached ({TAB_CAP})", 4000)
                return
            self.tabs.append({"pkg": pkg, "label": label})
            self.active_tab = len(self.tabs) - 1
        else:
            cur = self.tabs[self.active_tab]
            if cur["pkg"] and cur["pkg"] != pkg and self.target:
                run([ADB, "-s", self.target, "shell", "am", "force-stop",
                     cur["pkg"]])       # replace closes the old app
            cur["pkg"] = pkg
            cur["label"] = label
        self._launch_now(pkg)
        self._render_tabs()

    def _switch_tab(self, i):
        if not (0 <= i < len(self.tabs)):
            return
        self.active_tab = i
        self._show_tab(self.tabs[i])
        self._render_tabs()

    def _close_tab(self, i):
        if not (0 <= i < len(self.tabs)):
            return
        t = self.tabs[i]
        if t["pkg"] and self.target:
            run([ADB, "-s", self.target, "shell", "am", "force-stop", t["pkg"]])
        del self.tabs[i]
        if not self.tabs:                       # never empty -> fall back to Home
            self.tabs = [{"pkg": None, "label": "Home"}]
            self.active_tab = 0
        else:
            self.active_tab = min(i, len(self.tabs) - 1)
        self._show_tab(self.tabs[self.active_tab])
        self._render_tabs()

    # -- drag & drop files -> phone gallery --
    def dragEnterEvent(self, e):
        if self.target and e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if self.target and e.mimeData().hasUrls():
            e.acceptProposedAction()

    def _app_label(self, pkg):
        """Human label for a package (from the loaded app list), else the pkg."""
        for label, p in self.apps:
            if p == pkg:
                return label
        return pkg or "Unknown"

    def _current_folder_name(self):
        """Folder name for the app currently in front (for per-app drops)."""
        pkg = (foreground_pkg(self.target, self.embed.display_id)
               or getattr(self, "_current_app", None))
        return safe_folder(self._app_label(pkg)) if pkg else "Unknown"

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
        if not files or not self.target:
            return
        e.acceptProposedAction()
        folder = self._current_folder_name()
        dest = f"{APPS_ROOT}/{folder}"
        if self._pw is not None and self._pw.isRunning():
            # a drop is already in flight; queue this one rather than drop
            # it, and send it once the current push finishes (see _pushed)
            self._pending_drops.append((files, dest, folder))
            self.status.showMessage(
                f"Queued {len(files)} file(s) for {folder}, sending after "
                "the current drop finishes")
            return
        self._start_push(files, dest, folder)

    def _start_push(self, files, dest, folder):
        self.status.showMessage(f"Pushing {len(files)} file(s) to {folder}…")
        self._pw = PushWorker(self.target, files, dest)
        self._pw.done.connect(self._pushed)
        self._pw.start()

    def _pushed(self, n, dest):
        folder = dest.rsplit("/", 1)[-1]
        self.status.showMessage(
            f"Pushed {n} file(s) to Pictures/Apps/{folder} . Pick them in the app.")
        if self._pending_drops and self.target:
            next_files, next_dest, next_folder = self._pending_drops.popleft()
            self._start_push(next_files, next_dest, next_folder)
        else:
            self._pending_drops.clear()

    # -- nav --
    def nav(self, name):
        if not self.target:
            return
        did = self.embed.display_id
        if did is not None:
            adb(self.target, "shell", "input", "-d", str(did),
                "keyevent", str(NAV_KEYS[name]))
        else:
            adb(self.target, "shell", "input", "keyevent", str(NAV_KEYS[name]))

    # -- lifecycle --
    def closeEvent(self, e):
        QSettings("PhoneDeck", "PhoneDeck").setValue("geometry",
                                                     self.saveGeometry())
        self.poller.stop()
        self.poller.wait(2000)
        self.wheel.remove()
        self.kb.stop()
        self.embed.stop()
        super().closeEvent(e)


def _pick_app_font():
    """JetBrains Mono Nerd Font if it happens to be installed (not bundled,
    not required), else the first clean monospace PhoneDeck can find, else
    just leave the system's own default UI font alone. Needs a QApplication
    to already exist, since QFontDatabase reads the live font database."""
    families = QFontDatabase.families()
    for name in ("JetBrainsMono NFM", "Consolas", "Cascadia Mono", "Courier New"):
        if name in families:
            return QFont(name, 10)
    return QFont()


def main():
    try:   # make Windows use our icon in the taskbar, not python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PhoneDeck.App")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(ICON_PATH))
    app.setFont(_pick_app_font())
    w = PhoneDeck()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
