"""
PhoneDeck — a desktop shell around scrcpy for the Pixel 8 Pro.

Left sidebar = your phone's apps (searchable). Click one to launch it.
Top bar     = Back / Home / Recents nav + Reconnect / Refresh.
Main area   = the scrcpy mirror, embedded as a live display + touch surface.

scrcpy is used purely as a display/input transport; app launching and
navigation go straight over adb, so nothing depends on scrcpy's own shortcuts
or on Android's flaky secondary-display behavior.
"""

import json
import os
import re
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QHBoxLayout, QVBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QFrame, QMenu,
)

import win32gui
import win32con

# ---- paths / device -------------------------------------------------------
SCRCPY_DIR = r"C:\Program Files\scrcpy-win64-v4.1"
ADB = SCRCPY_DIR + r"\adb.exe"
SCRCPY = SCRCPY_DIR + r"\scrcpy.exe"
SERIAL = "3C210DLJG002RN"          # Pixel 8 Pro USB serial
PHONE_IP = "10.42.69.170"          # DHCP-reserved; used for the 5555 fallback
EMBED_TITLE = "PhoneDeckDisplay"   # unique scrcpy window title we reparent
DISPLAY_RES = "1600x900/240"       # virtual external display size/density

NAV_KEYS = {"Back": 4, "Home": 3, "Recents": 187}
FAV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "favorites.json")

_SCRCPY_VDISP_RE = re.compile(
    r"displayId=(\d+), uniqueId=.virtual:com\.android\.shell,2000,scrcpy,")

CREATE_NO_WINDOW = 0x08000000      # keep adb/scrcpy console windows hidden


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


def resolve_target():
    """Find the phone: USB serial first, then wireless debugging via mDNS,
    then the classic 5555. Returns an adb target string or None."""
    run([ADB, "start-server"], timeout=15)
    rc, out = run([ADB, "devices"])
    for line in out.splitlines():
        if line.startswith(SERIAL) and "device" in line:
            return SERIAL
    # already-connected wireless device?
    for line in out.splitlines():
        m = re.match(r"^(\d{1,3}(?:\.\d{1,3}){3}:\d+)\s+device", line)
        if m:
            return m.group(1)
    # discover wireless debugging (random TLS port)
    run([ADB, "mdns", "check"], timeout=10)
    for _ in range(10):
        rc, out = run([ADB, "mdns", "services"], timeout=10)
        for line in out.splitlines():
            if SERIAL in line and "_adb-tls-connect" in line:
                m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d+)", line)
                if m:
                    tgt = m.group(1)
                    rc, o = run([ADB, "connect", tgt], timeout=10)
                    if "connected" in o:
                        return tgt
        time.sleep(2)
    # last resort
    rc, out = run([ADB, "connect", f"{PHONE_IP}:5555"], timeout=10)
    if "connected" in out:
        return f"{PHONE_IP}:5555"
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


def kill_orphan_embeds():
    """Kill any leftover PhoneDeck scrcpy embeds (e.g. orphaned by a crash),
    identified by our unique window title, so virtual displays never pile up.
    scrcpy-server drops its virtual display when the process dies."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='scrcpy.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{EMBED_TITLE}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    run(["powershell", "-NoProfile", "-Command", ps], timeout=15)


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

    def run(self):
        self.done.emit(resolve_target())


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

    def start(self, target):
        self.stop()
        kill_orphan_embeds()   # sweep any leftover embed from a prior crash
        self.target = target
        self.display_id = None
        # Keyboard must be UHID to reach the virtual display; mouse stays SDK
        # so the pointer isn't captured/locked to the window (absolute, click
        # where you point). show_ime_with_hard_keyboard keeps the phone's own
        # soft keyboard working despite the UHID keyboard being attached.
        run([ADB, "-s", target, "shell", "settings", "put", "secure",
             "show_ime_with_hard_keyboard", "1"], timeout=10)
        self._ids_before = scrcpy_display_ids(target)
        self._log = open(r"C:\Users\Adam\PhoneDeck\scrcpy.log", "w",
                         encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [SCRCPY, "-s", target,
             f"--new-display={DISPLAY_RES}",
             "--keyboard=uhid", "--mouse=sdk",
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

    def _detect_display(self):
        self._disp_tries += 1
        if self.display_id is None and self.target:
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
        self.resize(1180, 820)
        self.target = None
        self.apps = []

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # top nav bar
        bar = QFrame()
        bar.setFixedHeight(48)
        bar.setStyleSheet("background:#15181d;")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 6, 8, 6)
        bl.setSpacing(6)
        self.btn_refresh = QPushButton("Refresh apps")
        self.btn_refresh.clicked.connect(self.load_apps)
        bl.addWidget(self.btn_refresh)
        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.clicked.connect(self.connect_phone)
        bl.addWidget(self.btn_reconnect)
        bl.addStretch(1)
        self.status = QLabel("Connecting…")
        self.status.setStyleSheet("color:#9aa4b2;")
        bl.addWidget(self.status)
        bl.addStretch(1)
        for name in ("Back", "Home", "Recents"):
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: self.nav(n))
            bl.addWidget(b)
        outer.addWidget(bar)

        # body: sidebar + display
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        side = QFrame()
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
        sl.addWidget(self.applist, 1)
        body.addWidget(side)

        self.embed = ScrcpyEmbed()
        self.embed.display_ready.connect(self._display_ready)
        body.addWidget(self.embed, 1)

        self._apply_theme()
        QTimer.singleShot(200, self.connect_phone)

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
        """)

    # -- connection --
    def connect_phone(self):
        self.status.setText("Connecting…")
        self.btn_reconnect.setEnabled(False)
        self._cw = ConnectWorker()
        self._cw.done.connect(self._connected)
        self._cw.start()

    def _connected(self, target):
        self.btn_reconnect.setEnabled(True)
        self.target = target
        if not target:
            self.status.setText("Phone not found — USB or Wireless debugging?")
            return
        self.status.setText(f"Connected  ({target})")
        self.embed.start(target)
        self.load_apps()

    def _display_ready(self, did):
        self.status.setText(f"Connected  ({self.target})")

    # -- apps --
    def load_apps(self):
        if not self.target:
            return
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("Loading…")
        self._aw = AppsWorker(self.target)
        self._aw.done.connect(self._apps_loaded)
        self._aw.start()

    def _apps_loaded(self, apps):
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("Refresh apps")
        self.apps = apps
        self.filter_apps(self.search.text())

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
        self.embed.stop()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("JetBrainsMono NFM", 10))
    w = PhoneDeck()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
