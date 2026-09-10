# PhoneDeck

A small desktop shell around [scrcpy](https://github.com/Genymobile/scrcpy) that
turns an Android phone into a windowed "emulator" on your PC — a searchable app
launcher on the left, your phone's apps running in a virtual external display on
the right, **without taking over the phone's own screen**.

- **Left sidebar** — searchable list of your phone's apps (with a Steam-style
  ★ Favorites section). Click one to launch it onto the display.
- **Top bar** — app-drawer toggle, screen rotate, **File** menu (Refresh /
  Reconnect / Device search / Settings) and a **Controls** menu of phone toggles.
- **Main area** — an embedded scrcpy view on a *virtual external display*, so
  apps open in the window and the phone's real screen stays free.
- **Drag & drop** a file onto the window to push it to the phone (shows up in
  pickers, e.g. sending a photo in a chat app).

## Setup

### On the PC — just download and install

Grab the installer from [Releases](../../releases), run it, done. It bundles
**scrcpy and adb**, so there's nothing else to install — no PATH setup, no
drivers to hunt down (Windows pulls the phone's adb driver on its own).

### On the phone — three one-time steps

1. **Enable Developer Options** — Settings → About phone → tap **Build number**
   seven times.
2. **Turn on USB debugging** — Settings → System → Developer options → **USB
   debugging**.
3. **Authorize the PC** — plug in over USB the first time; the phone shows
   **"Allow USB debugging?"** → tap **Allow** (tick *Always allow from this
   computer* so it sticks).

That's the whole setup. **No root, no sideloaded app, nothing else.** PhoneDeck
applies the one system setting it needs by itself once connected.

> **Per-use:** the phone must be **unlocked with the screen on** for typing to
> register.

### Connecting

- **USB** — nothing beyond the steps above; just plug in.
- **Cordless on normal Wi-Fi** — either turn on **Wireless debugging**
  (Developer options), or plug in once and hit **Device search → Wi-Fi over
  USB**, then unplug.
- **Cordless while the phone is a hotspot** — Wireless debugging is unavailable
  in hotspot mode (an Android limitation), so plug in USB once, hit **Wi-Fi over
  USB**, then unplug. Re-do it after a phone reboot.

PhoneDeck remembers the last device it connected to and reconnects to it on
launch, so day-to-day it's just: open the app.

## Features

- **Device-agnostic** — connects to the last good device, or pick one from
  **Device search** (lists USB + wireless devices by name, e.g. *Pixel 8 Pro*).
- **Controls menu** (all over adb, no root): Wi-Fi, Mobile data, Airplane mode,
  Stay awake while charging, Show taps, Dim screen. Hotspot and USB tethering
  need root to toggle directly, so that entry opens the phone's Tethering
  settings instead.
- **Favorites**, **screen rotation**, collapsible app drawer, drag-and-drop file
  push, and a **Settings** dialog (resolution, density/DPI, scroll speed).

## How it works

- App names come from `scrcpy --list-apps` (label + package).
- The display is a scrcpy `--new-display` virtual display. The **keyboard** uses
  scrcpy's own `--keyboard=sdk --raw-key-events` (instant, and `--raw-key-events`
  is what makes punctuation reach the virtual display); `--display-ime-policy=hide`
  keeps the phone's soft keyboard from popping up. The **mouse** stays `sdk` so
  the pointer isn't captured/locked to the window. Scrolling is forwarded as an
  adb swipe via a low-level mouse hook.
- Apps launch with `am start --display <id> -n <component>` — explicit display
  targeting is what keeps them on the external display instead of the phone.
- Navigation is `input -d <id> keyevent` (Back/Home/Recents).
- The scrcpy window is reparented into the Qt window via pywin32 `SetParent`.
- A startup sweep kills any orphaned embed so virtual displays never accumulate.

## Running from source

```
pip install PySide6 pywin32 Pillow
pythonw phonedeck.py
```

Expects scrcpy at `C:\Program Files\scrcpy-win64-v4.1` (or bundled under the
install dir). Building the installer: PyInstaller (`--windowed --name PhoneDeck
--icon phonedeck.ico`) then Inno Setup on `installer/phonedeck.iss`.

## Ideas

- Tabs for multiple apps/displays at once.
- App icons in the sidebar.
