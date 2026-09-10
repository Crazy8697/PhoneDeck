"""
PhoneDeck — a desktop shell around scrcpy for the Pixel 8 Pro.

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
import subprocess
import collections
import sys
import threading
import time

from PySide6.QtCore import (Qt, QTimer, QThread, Signal, QSettings,
                            QPointF, QSize)
from PySide6.QtGui import (QFont, QColor, QCursor, QIcon, QPixmap, QPainter,
                           QPen, QBrush, QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QFrame, QMenu,
    QDialog, QComboBox, QSpinBox, QCheckBox, QDialogButtonBox,
    QFormLayout, QToolButton, QMessageBox,
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


def _find_scrcpy_dir():
    for c in (os.path.join(_app_dir(), "scrcpy"),
              os.path.join(_app_dir(), "scrcpy-win64-v4.1"),
              r"C:\Program Files\scrcpy-win64-v4.1"):
        if os.path.exists(os.path.join(c, "scrcpy.exe")):
            return c
    return r"C:\Program Files\scrcpy-win64-v4.1"


def data_dir():
    """Per-user writable dir for favorites and logs."""
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                     "PhoneDeck")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


SCRCPY_DIR = _find_scrcpy_dir()
ADB = os.path.join(SCRCPY_DIR, "adb.exe")
SCRCPY = os.path.join(SCRCPY_DIR, "scrcpy.exe")
EMBED_TITLE = "PhoneDeckDisplay"   # unique scrcpy window title we reparent
ICON_PATH = _resource("phonedeck.ico")
SCRCPY_LOG = os.path.join(data_dir(), "scrcpy.log")
DISPLAY_RES = "1600x900/240"       # virtual external display size/density

NAV_KEYS = {"Back": 4, "Home": 3, "Recents": 187}
FAV_FILE = os.path.join(data_dir(), "favorites.json")

_SCRCPY_VDISP_RE = re.compile(
    r"displayId=(\d+), uniqueId=.virtual:com\.android\.shell,2000,scrcpy,")

CREATE_NO_WINDOW = 0x08000000      # keep adb/scrcpy console windows hidden

# user settings (persisted via QSettings), with defaults
VERSION = "1.3.0"

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
}


def cfg():
    return QSettings("PhoneDeck", "PhoneDeck")


def cfg_get(key, typ=str):
    return cfg().value(key, SETTING_DEFAULTS[key], type=typ)


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
    """IPv4 default gateways — the phone's own IP when the PC is a client on
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
    resets when the phone reboots — rerun then. Returns (target|None, message)."""
    usb, _ = _connected_devices()
    if not usb:
        return None, "No USB device — plug the phone in first."
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
            return tgt, f"Wi-Fi connected: {tgt} — USB can be unplugged."
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
    """A colored rounded tile with the app's initial — a lightweight sidebar
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
# as `adb -s <t> shell <args...>`. Hotspot/USB-tethering aren't here — Android
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
    (`_adb._tcp`, port 5555) — a phone acting as a hotspot advertises the
    legacy service, so matching only the TLS one silently misses it. The
    pairing service (`_adb-tls-pairing`) is NOT connectable, so exclude it."""
    return ("_adb-tls-pairing" not in line
            and ("_adb-tls-connect" in line or "_adb._tcp" in line))


def resolve_target(prefer=None):
    """Find a device to connect to. Order: the preferred (last good) device,
    then any connected USB device, then any connected wireless device, then
    discover any wireless-debugging device via mDNS. Returns a target or None.
    Device-agnostic — not tied to a specific serial."""
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
    scrcpy child), via AttachThreadInput — so scrcpy captures keystrokes."""
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
            devices.append((f"{name}   —   {t}   ({kind}, connected)", t))
    rc, o = run([ADB, "mdns", "services"], timeout=8)
    for line in o.splitlines():
        if _adb_mdns(line):
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                mdns_name = line.split("\t", 1)[0].strip() if "\t" in line else ""
                label = (f"{mdns_name}   —   {m.group(1)}   (wireless)"
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


# ---- keyboard bridge ------------------------------------------------------
class KeyBridge:
    """Forwards PC keystrokes to the phone's virtual display. Each command runs
    as its own direct `adb -s T shell input -d <id> ...` (executed in order by a
    worker thread), because a persistent piped `adb shell` buffers/delays the
    commands — text then commits to whatever field is focused when the backlog
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
    route the keyboard — click the search box -> keys go to search, click
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
                if not self.main._search_hit():
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
                    cx, cy = 800, 450
                    half = max(40, dist // 2)
                    y1, y2 = ((cy + half, cy - half) if down
                              else (cy - half, cy + half))
                    self.kb.swipe(cx, y1, cx, y2)
        except Exception:
            pass
        return self._u.CallNextHookEx(None, nCode, wParam, lParam)


# ---- embedded scrcpy ------------------------------------------------------
class ScrcpyEmbed(QWidget):
    """Hosts an embedded, borderless scrcpy window that shows a *virtual*
    external display (so the phone's own screen is left alone). Emits
    display_ready(id) once Android has created the virtual display, so the
    UI can target app launches / nav at that display."""

    display_ready = Signal(int)

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
        # The keyboard is DISABLED in scrcpy — embedding steals the window focus
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
        # instance's phone-side server is still clearing — retried by _detect.
        self._start_tries += 1
        self._log = open(SCRCPY_LOG, "w", encoding="utf-8", errors="replace")
        if cfg().value("portrait", False, type=bool):
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
                # the focused app's folder — see MainWindow._poll_stage
                f"--push-target={PUSH_STAGE}/",
                "--no-audio"]
        fps = cfg_get("max_fps", int)
        if fps > 0:
            args.append(f"--max-fps={fps}")
        self.proc = subprocess.Popen(
            args,
            stdout=self._log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )
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
        # video init — slightly AFTER the window appears — so re-revoke over
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
        # scrcpy died early (the connection race) — retry a few times
        if self.proc is not None and self.proc.poll() is not None:
            self._disp_poll.stop()
            self._win_poll.stop()
            if self._start_tries < 6:
                QTimer.singleShot(900, self._spawn)
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

    def _fit(self):
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            win32gui.MoveWindow(self.hwnd, 0, 0, self.width(), self.height(), True)

    def resizeEvent(self, e):
        self._fit()
        super().resizeEvent(e)

    def stop(self):
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
        self.proc = None


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
        self._rot_btn.setText("⟳")
        self._rot_btn.setCursor(Qt.PointingHandCursor)
        self._rot_btn.setToolTip("Rotate: portrait / landscape")
        self._rot_btn.clicked.connect(self._toggle_orientation)
        tl.addWidget(self._rot_btn)
        file_btn = QToolButton()
        file_btn.setText("File")
        file_btn.setPopupMode(QToolButton.InstantPopup)
        fm = QMenu(self)
        fm.addAction("Settings…", self.open_settings)
        fm.addAction("About…", self.open_about)
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
            act.triggered.connect(
                lambda checked, o=on_cmd, f=off_cmd: self._control_toggle(o, f))
            self._ctl_actions[key] = (act, getf)
        self._dim_act = cm.addAction("Dim screen (brightness 1)")
        self._dim_act.setCheckable(True)
        self._dim_act.triggered.connect(self._toggle_dim)
        cm.addSeparator()
        cm.addAction("Tethering & hotspot settings…", self._open_tether_settings)
        cm.aboutToShow.connect(self._refresh_controls)
        ctl_btn.setMenu(cm)
        tl.addWidget(ctl_btn)
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

        self.embed = ScrcpyEmbed()
        self.embed.display_ready.connect(self._display_ready)
        self.embed.setFocusPolicy(Qt.StrongFocus)
        body.addWidget(self.embed, 1)

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

    def _display_hot(self):
        # for the global wheel hook: only act when PhoneDeck is active and the
        # cursor is over the display
        return self.isActiveWindow() and self._mouse_over_display()

    def _toggle_sidebar(self):
        self.side.setVisible(not self.side.isVisible())
        QTimer.singleShot(0, self.embed._fit)   # display fills the freed space

    def _toggle_orientation(self):
        c = cfg()
        c.setValue("portrait", not c.value("portrait", False, type=bool))
        if self.target:
            self.embed.start(self.target)       # recreate display, swapped W×H

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
    def connect_phone(self):
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
        m.addAction("Refresh apps", self.load_apps)
        m.addAction("Reconnect", self.reconnect)
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

    def _control_toggle(self, on_cmd, off_cmd):
        if not self.target:
            self.status.showMessage("No device connected", 4000)
            return
        act = self.sender()
        cmd = on_cmd if (act and act.isChecked()) else off_cmd
        rc, out = run([ADB, "-s", self.target, "shell", *cmd], timeout=12)
        msg = out.strip().splitlines()[-1] if out.strip() else ""
        if rc == 0 and "Exception" not in msg and "denied" not in msg.lower():
            self.status.showMessage(f"{' '.join(cmd)} ✓", 4000)
        else:
            self.status.showMessage(f"Failed: {msg or 'see phone'}", 6000)

    def _open_tether_settings(self):
        """Open the phone's Hotspot & tethering screen (softap/USB-tether can't
        be toggled over adb without root — user flips them there)."""
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

    def open_about(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("About PhoneDeck")
        v = QVBoxLayout(dlg)
        title = QLabel("PhoneDeck")
        title.setStyleSheet("font-size:18px; font-weight:600;")
        v.addWidget(title)
        v.addWidget(QLabel(f"Version {VERSION}"))
        body = QLabel(
            "Runs your phone's apps in a window on the PC via scrcpy — a "
            "searchable app launcher on the left and a virtual external "
            "display on the right, so the phone's own screen stays free.\n\n"
            "Connects over USB or Wi-Fi (incl. the phone's hotspot, via "
            "Wi-Fi over USB). No root required.")
        body.setWordWrap(True)
        body.setStyleSheet("color:#c3c9d2;")
        v.addWidget(body)
        link = QLabel('<a href="https://github.com/Crazy8697/PhoneDeck" '
                      'style="color:#6ea8fe;">github.com/Crazy8697/PhoneDeck</a>')
        link.setOpenExternalLinks(True)
        v.addWidget(link)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        v.addWidget(bb)
        dlg.setMinimumWidth(380)
        dlg.exec()

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
                st.setText(msg + "  Paired — open Device search to connect.")

        row = QHBoxLayout()
        pair_btn = QPushButton("Pair"); pair_btn.clicked.connect(do_pair)
        close_btn = QPushButton("Close"); close_btn.clicked.connect(dlg.reject)
        row.addStretch(1); row.addWidget(pair_btn); row.addWidget(close_btn)
        form.addRow(row)
        dlg.setMinimumWidth(360)
        dlg.exec()

    def open_settings(self):
        dlg = QDialog(self)
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
            portrait = cfg().value("portrait", False, type=bool)
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
            self._sync_data_monitor()
            new = (cfg_get(reskey), cfg_get(dpikey, int),
                   cfg_get("max_fps", int), cfg_get("bitrate_mbps", int))
            if self.target and new != old:
                self.embed.start(self.target)   # restart display at new size

    def _connected(self, target):
        self.target = target
        self.poller.target = target
        if not target:
            self.status.showMessage("No device found — pick one, or plug in / "
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

    # -- apps --
    def load_apps(self):
        if not self.target:
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
        try:
            return json.loads(cfg().value("app_profiles", "{}"))
        except Exception:
            return {}

    def _set_profile(self, pkg, orientation):
        """orientation: 'portrait', 'landscape', or None to clear."""
        profs = self._profiles()
        if orientation:
            profs[pkg] = orientation
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
        prof = self._profiles().get(pkg)
        if prof:
            it.setToolTip(f"Launches in {prof}")
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
        fav = pkg in self.favorites
        fav_act = menu.addAction("Remove from Favorites" if fav
                                 else "Add to Favorites")
        menu.addSeparator()
        prof = self._profiles().get(pkg)
        sub = menu.addMenu("Launch orientation")
        a_def = sub.addAction("Default (current)")
        a_land = sub.addAction("Landscape")
        a_port = sub.addAction("Portrait")
        for a, val in ((a_def, None), (a_land, "landscape"), (a_port, "portrait")):
            a.setCheckable(True)
            a.setChecked(prof == val)
        menu.addSeparator()
        info_act = menu.addAction("App info (on phone)")
        clear_act = menu.addAction("Clear dropped images")
        chosen = menu.exec(self.applist.mapToGlobal(pos))
        if chosen == fav_act:
            if fav:
                self.favorites.discard(pkg)
            else:
                self.favorites.add(pkg)
            save_favorites(self.favorites)
            self.filter_apps(self.search.text())
        elif chosen in (a_def, a_land, a_port):
            self._set_profile(pkg, {a_def: None, a_land: "landscape",
                                    a_port: "portrait"}[chosen])
        elif chosen == info_act:
            self._open_app_info(pkg)
        elif chosen == clear_act:
            self._clear_app_images(pkg)

    def launch_selected(self, item):
        if not item or not self.target:
            return
        pkg = item.data(Qt.UserRole)
        if not pkg:                          # header row
            return
        # per-app profile: switch orientation first, then launch once the
        # new display is ready (handled in _display_ready via _pending_launch)
        prof = self._profiles().get(pkg)
        if prof:
            want_portrait = (prof == "portrait")
            if cfg().value("portrait", False, type=bool) != want_portrait:
                cfg().setValue("portrait", want_portrait)
                self._pending_launch = pkg
                self.status.showMessage(f"Switching to {prof}…")
                self.embed.start(self.target)
                return
        self._launch_now(pkg)

    def _launch_now(self, pkg):
        self._current_app = pkg          # fallback for per-app drop folder
        self.poller.current_app = pkg
        did = self.embed.display_id
        if did is None:
            # display not ready yet — fall back to phone screen
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
        self.status.showMessage(f"Pushing {len(files)} file(s) to {folder}…")
        self._pw = PushWorker(self.target, files, dest)
        self._pw.done.connect(self._pushed)
        self._pw.start()

    def _pushed(self, n, dest):
        folder = dest.rsplit("/", 1)[-1]
        self.status.showMessage(
            f"Pushed {n} file(s) to Pictures/Apps/{folder} — pick them in the app")

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


def main():
    try:   # make Windows use our icon in the taskbar, not python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "PhoneDeck.App")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(ICON_PATH))
    app.setFont(QFont("JetBrainsMono NFM", 10))
    w = PhoneDeck()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
