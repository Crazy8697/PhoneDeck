<p align="center">
  <img src="assets/brand/darkrelay-banner.jpg" alt="DarkRelay" width="100%">
</p>

<h1 align="center">PhoneDeck</h1>

<p align="center"><b>Run your Android phone's apps in windows on your PC.</b><br>
Free, local, built on scrcpy. Your phone's own screen stays free.</p>

## Download

<p align="center">
  <a href="https://github.com/Crazy8697/PhoneDeck/releases/latest/download/PhoneDeck-Setup.exe">
    <img src="https://img.shields.io/badge/Download%20PhoneDeck%20for%20Windows-19C2E6?style=for-the-badge&logo=windows&logoColor=white" alt="Download PhoneDeck for Windows" height="56">
  </a>
</p>

<!-- dl-info -->
<p align="center"><sub>Version 1.5.1 &nbsp;&middot;&nbsp; 28.8 MB &nbsp;&middot;&nbsp; Windows 10 / 11, 64-bit &nbsp;&middot;&nbsp; <a href="https://github.com/Crazy8697/PhoneDeck/releases">all releases</a></sub></p>
<!-- /dl-info -->

<p align="center"><sub>The installer isn't code-signed, so Windows may show "Windows protected your PC": click <b>More info</b>, then <b>Run anyway</b>.</sub></p>

Run the downloaded `PhoneDeck-Setup.exe`: it installs PhoneDeck to Program Files with Start-menu (and
optionally desktop) shortcuts. It bundles scrcpy and adb, so there's nothing else to install on the
PC. Each release also lists the versioned installer and its `.sha256` checksum. Uninstall from
Windows Settings > Apps; your settings and favorites stay.

## What PhoneDeck does

Pick an app from a searchable list and it opens in its own window on your desktop, on a virtual
display, while the phone's own screen stays free. Type with your PC keyboard, click and scroll with
the mouse.

- **App list:** every app on the phone, searchable, with pinned **Favorites** at the top. Click to
  open; right-click for more (open in a new tab, app info, clear dropped images, launch orientation).
- **Tabs:** keep several apps open and switch between them, plus a Home tab.
- **Orientation:** Portrait, Landscape and both flipped variants from the top bar, and a per-app
  **launch orientation** so an app always opens the way it's meant to be used.
- **Drag and drop:** drop files onto the window to send them to the phone, into a folder per app
  under Pictures/Apps, ready in that app's picker.
- **Connections:** USB, or cordless over wireless debugging. **Device search** lists USB and
  wireless devices by model name; **Wi-Fi over USB** switches a plugged-in phone to cordless;
  **Pair new…** (in Device search) handles Android 11+ pairing codes. PhoneDeck reconnects to the last device on launch.
- **Controls menu** (over adb, no root): Wi-Fi, Mobile data, Airplane mode, Stay awake while
  charging, Show taps, Dim screen, and a shortcut to the phone's tethering settings. Turning off
  the connection PhoneDeck is using asks first.
- **Status readouts** (optional): charge rate, battery temperature and mobile data use.
- **Settings** (File menu): display resolution and density per orientation, frame rate, bitrate, scroll
  distance and direction, and the startup update check.

## Requirements

- Windows 10 or 11, 64-bit.
- An Android phone with **USB debugging** (or **wireless debugging**) turned on. No root, no app to
  install on the phone.

## First run

On the phone, once:

1. **Enable Developer options:** Settings > About phone > tap **Build number** seven times.
2. **Turn on USB debugging:** Settings > System > Developer options > **USB debugging**.
3. **Plug in and allow the PC:** the phone asks **"Allow USB debugging?"**; tick *Always allow from
   this computer* and tap **Allow**.

Then open PhoneDeck. The phone must be **unlocked with the screen on** for typing to reach it.

**Cordless:** turn on **Wireless debugging** in Developer options and use **Connections > Device
search**, or **Pair new…** there with the pairing code the phone shows (Android 11+). Or plug in once and use
**Wi-Fi over USB**, then unplug. While the phone is a hotspot, Wireless debugging is unavailable (an
Android limitation): use Wi-Fi over USB instead, and again after a phone reboot.

## Privacy

PhoneDeck runs entirely on your computer and talks to your phone directly, over USB or your local
network. Nothing is uploaded. Its only internet requests are:

- one lookup of the latest release on this GitHub repository, at startup when **Settings >
  "Check for updates at startup"** is on (it is by default), or from **File > Check for updates**, and
- if you choose to update, the download of the new installer and its checksum.

No telemetry, no IDs, nothing about your phone, apps or files is sent anywhere.

## Updates

When a newer version is out, an **Update** button appears in the status bar (never a pop-up):
**Release notes**, **Download and install**, **Skip this version** or **Later**. The download is
checked against the published SHA-256 before it runs; PhoneDeck then closes, updates and reopens.

## Build from source

Requirements: Windows 10 / 11 64-bit, Python 3.11, an unpacked
[scrcpy Windows release](https://github.com/Genymobile/scrcpy/releases), and (for the installer)
Inno Setup 6.

```powershell
git clone https://github.com/Crazy8697/PhoneDeck.git
cd PhoneDeck
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PHONEDECK_SCRCPY_DIR = "<folder of the unpacked scrcpy release>"
.venv\Scripts\pythonw.exe phonedeck.py           # run from source
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
$env:SCRCPY_DIR = $env:PHONEDECK_SCRCPY_DIR
.\build.ps1 -Setup                               # dist\PhoneDeck + dist\installer\PhoneDeck-Setup-<version>.exe
```

PhoneDeck looks for scrcpy.exe and adb.exe in `PHONEDECK_SCRCPY_DIR`, then in a `scrcpy` folder
next to it (where the installer puts them), then in an unpacked `scrcpy-win64-*` folder next to it,
then on PATH. The build copies the scrcpy release from `SCRCPY_DIR` into the program folder.

## License

PhoneDeck is free software under the [Apache License 2.0](LICENSE) (see also [NOTICE](NOTICE)). It
comes with NO WARRANTY. It includes third-party components under their own licenses, among them
[scrcpy](https://github.com/Genymobile/scrcpy) by Genymobile and adb from the Android Open Source
Project: see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) (also in the program under
**Help > Licenses**).

## Support

PhoneDeck is made by DarkRelay, on top of [scrcpy](https://github.com/Genymobile/scrcpy) by
Genymobile. If it's useful to you, you can
[buy DarkRelay some cheese 🧀](https://buymeacoffee.com/crazy8697).
