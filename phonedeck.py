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
import sys
import threading
import time

from PySide6.QtCore import Qt, QTimer, QThread, Signal, QSettings, QEvent
from PySide6.QtGui import QFont, QColor, QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QFrame, QMenu,
    QDialog, QComboBox, QSpinBox, QSlider, QCheckBox, QDialogButtonBox,
    QFormLayout, QToolButton,
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
SETTING_DEFAULTS = {
    "resolution": "1920x1080",  # virtual display W x H
    "dpi": 180,                 # virtual display density
    "scroll_dist": 260,         # px of swipe per wheel tick
    "scroll_natural": True,     # wheel up scrolls content up
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
            if "_adb-tls-connect" in line:
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
    devices found via mDNS. Returns [(label, target)]."""
    devices, seen = [], set()
    rc, o = run([ADB, "devices"])
    for line in o.splitlines():
        m = re.match(r"^(\S+)\s+device$", line)
        if m and m.group(1) != "List":
            t = m.group(1)
            seen.add(t)
            kind = "wireless" if re.match(r"\d+\.\d+\.\d+\.\d+:", t) else "USB"
            devices.append((f"{t}   ({kind}, connected)", t))
    rc, o = run([ADB, "mdns", "services"], timeout=8)
    for line in o.splitlines():
        if "_adb-tls-connect" in line:
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                devices.append((f"{m.group(1)}   (wireless)", m.group(1)))
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
    """Push dropped files to the phone's Pictures/PhoneDeck folder and media-scan
    each so they show up in Kik's (and any app's) image picker."""
    done = Signal(int)          # number pushed
    DEST = "/sdcard/Pictures/PhoneDeck"

    def __init__(self, target, files):
        super().__init__()
        self.target = target
        self.files = files

    def run(self):
        run([ADB, "-s", self.target, "shell", "mkdir", "-p", self.DEST])
        n = 0
        for f in self.files:
            name = os.path.basename(f)
            rc, _ = run([ADB, "-s", self.target, "push", f,
                         f"{self.DEST}/{name}"], timeout=300)
            if rc == 0:
                run([ADB, "-s", self.target, "shell",
                     "am", "broadcast", "-a",
                     "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                     "-d", f"file://{self.DEST}/{name}"], timeout=20)
                n += 1
        self.done.emit(n)


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
        w, h = cfg_get("resolution").split("x")
        if cfg().value("portrait", False, type=bool):
            w, h = h, w                          # swap for portrait
        res = f"{w}x{h}/{cfg_get('dpi', int)}"
        self.proc = subprocess.Popen(
            [SCRCPY, "-s", self.target,
             f"--new-display={res}",
             "--keyboard=sdk", "--raw-key-events", "--mouse=sdk",
             "--window-borderless", f"--window-title={EMBED_TITLE}",
             "--no-audio"],
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
        # The first fit can land relative to a stale origin (the reparent
        # hasn't fully settled the instant the window appears), leaving the
        # child parked off-screen. Re-fit after layout settles so it can't
        # stick there.
        for ms in (150, 500, 1500):
            QTimer.singleShot(ms, self._fit)
        try:   # drop scrcpy's own file-drop target so drops bubble to Qt
            ole = ctypes.windll.ole32
            ole.RevokeDragDrop.argtypes = [ctypes.c_void_p]
            ole.RevokeDragDrop(hwnd)
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
        fm.addAction("Refresh apps", self.load_apps)
        fm.addAction("Reconnect", self.reconnect)
        fm.addAction("Device search…", self.device_search)
        fm.addSeparator()
        fm.addAction("Settings…", self.open_settings)
        file_btn.setMenu(fm)
        tl.addWidget(file_btn)
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
        row = QHBoxLayout()
        rb = QPushButton("Refresh"); rb.clicked.connect(refresh)
        row.addWidget(rb); row.addStretch(1)
        cb = QPushButton("Connect"); cb.clicked.connect(do_connect)
        xb = QPushButton("Cancel"); xb.clicked.connect(dlg.reject)
        row.addWidget(cb); row.addWidget(xb)
        v.addLayout(row)
        dlg.exec()

    def open_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        form = QFormLayout(dlg)
        res = QComboBox()
        res.addItems(["1280x720", "1600x900", "1920x1080"])
        res.setCurrentText(cfg_get("resolution"))
        dpi = QSpinBox(); dpi.setRange(120, 480); dpi.setSingleStep(20)
        dpi.setValue(cfg_get("dpi", int))
        scroll = QSpinBox(); scroll.setRange(60, 800); scroll.setSingleStep(20)
        scroll.setSuffix(" px"); scroll.setValue(cfg_get("scroll_dist", int))
        natural = QCheckBox("Natural (wheel up scrolls up)")
        natural.setChecked(cfg_get("scroll_natural", bool))
        form.addRow("Resolution", res)
        form.addRow("Density (dpi)", dpi)
        form.addRow("Scroll distance", scroll)
        form.addRow("", natural)
        note = QLabel("Resolution/density changes reconnect the display.")
        note.setStyleSheet("color:#9aa4b2;")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() == QDialog.Accepted:
            old = (cfg_get("resolution"), cfg_get("dpi", int))
            c = cfg()
            c.setValue("resolution", res.currentText())
            c.setValue("dpi", dpi.value())
            c.setValue("scroll_dist", scroll.value())
            c.setValue("scroll_natural", natural.isChecked())
            if self.target and (res.currentText(), dpi.value()) != old:
                self.embed.start(self.target)   # restart display at new size

    def _connected(self, target):
        self.target = target
        if not target:
            self.status.showMessage("No device found — pick one, or plug in / "
                                    "enable Wireless debugging then Refresh")
            QTimer.singleShot(300, self.device_search)   # first-run / no device
            return
        QSettings("PhoneDeck", "PhoneDeck").setValue("last_target", target)
        self.status.showMessage(f"Connected  ({target})")
        self.embed.start(target)
        self.load_apps()

    def _display_ready(self, did):
        self.status.showMessage(f"Connected  ({self.target})")
        self.kb.start(self.target, did)
        self.wheel.install()

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
        self.filter_apps(self.search.text())
        if self.target:
            self.status.showMessage(f"Connected  ({self.target})")

    def _add_header(self, text):
        it = QListWidgetItem(text)
        it.setFlags(Qt.NoItemFlags)          # non-selectable divider
        it.setForeground(QColor("#6b7480"))
        f = it.font(); f.setBold(True); f.setPointSize(max(8, f.pointSize() - 1))
        it.setFont(f)
        self.applist.addItem(it)

    def _add_app(self, label, pkg):
        star = "★ " if pkg in self.favorites else ""
        it = QListWidgetItem(star + label)
        it.setData(Qt.UserRole, pkg)
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
        act = menu.addAction("Remove from Favorites" if fav
                             else "Add to Favorites")
        if menu.exec(self.applist.mapToGlobal(pos)) == act:
            if fav:
                self.favorites.discard(pkg)
            else:
                self.favorites.add(pkg)
            save_favorites(self.favorites)
            self.filter_apps(self.search.text())

    def launch_selected(self, item):
        if not item or not self.target:
            return
        pkg = item.data(Qt.UserRole)
        if not pkg:                          # header row
            return
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

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls()
                 if u.isLocalFile() and os.path.isfile(u.toLocalFile())]
        if not files or not self.target:
            return
        e.acceptProposedAction()
        self.status.showMessage(f"Pushing {len(files)} file(s) to phone…")
        self._pw = PushWorker(self.target, files)
        self._pw.done.connect(self._pushed)
        self._pw.start()

    def _pushed(self, n):
        self.status.showMessage(
            f"Pushed {n} file(s) to Pictures/PhoneDeck — pick them in the app")

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
