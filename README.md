# PhoneDeck

A small desktop shell around [scrcpy](https://github.com/Genymobile/scrcpy) that
turns an Android phone into a windowed "emulator" on your PC.

- **Left sidebar** — a searchable list of your phone's apps. Click one to launch it.
- **Nav bar** — Back / Home / Recents, plus Reconnect and Refresh.
- **Main area** — an embedded scrcpy view that shows a *virtual external display*,
  so apps open in the window **without taking over the phone's own screen**.

Apps are launched and navigated straight over `adb`, so nothing depends on
scrcpy's keyboard shortcuts or on Android's secondary-display quirks.

## How it works

- App names come from `scrcpy --list-apps` (label + package).
- The display is a scrcpy `--new-display` virtual display. The **keyboard** uses
  `uhid` (SDK key injection doesn't reach virtual displays on Android 17); the
  **mouse** stays `sdk` so the pointer isn't captured/locked to the window.
- Apps launch with `am start --display <id> -n <component>` — explicit display
  targeting is what keeps them on the external display instead of the phone.
- Navigation is `input -d <id> keyevent` (Back/Home/Recents).
- The scrcpy window is reparented into the Qt window via pywin32 `SetParent`.
- A startup sweep kills any orphaned embed so virtual displays never accumulate.

## Requirements

- Windows, Python 3.11+
- `pip install PySide6 pywin32`
- [scrcpy](https://github.com/Genymobile/scrcpy) (expects it at
  `C:\Program Files\scrcpy-win64-v4.1`; adjust `SCRCPY_DIR` in `phonedeck.py`)
- A phone with USB debugging / wireless debugging enabled. Set `SERIAL` /
  `PHONE_IP` in `phonedeck.py` for your device.

## Run

```
pythonw phonedeck.py
```

## Notes / ideas

- Tabs for multiple apps/displays at once.
- App icons in the sidebar.
- Persist window size/position.
