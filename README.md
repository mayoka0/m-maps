# M Maps

A macOS tool that spoofs the GPS location an iPhone reports, over a USB connection.

**An open-source project by [Mayoka Labs](https://www.mayoka.dev).**

**For harmless pranks only — do not use to deceive, defraud, or evade tracking.**

It's open source specifically so anyone can read exactly what it does to their phone: it only ever sets (or clears) a simulated location via Apple's own developer/instruments protocol. No network calls, no telemetry, no data collection.

## Features

- **Native desktop app** (recommended) or browser-based map GUI
- **Teleport** — jump the phone to any point on the map
- **Route** — travel real roads at Walk / Bicycle / Motorcycle / Car speed (OSRM)
- **Fly** — straight great-circle flight to the nearest real passenger airport
- **Multi-stop trips** — ordered stops with automatic fly/drive legs
- **Place search** — OpenStreetMap Nominatim (place names) or raw coordinates
- **Map styles** — OpenFreeMap basemaps (Liberty, Bright, Positron, Dark, Fiord) plus a Normal / 3D camera toggle
- Live USB device detection with a confirm prompt before spoofing starts

## Requirements

- macOS on Apple Silicon, with Xcode Command Line Tools (`python3`, `git`, `clang`).
- An iPhone on iOS 17 or later, connected by USB cable.
- Administrator access when spoofing (macOS password dialog, or `sudo` for the CLI/browser server). The location tunnel needs root because it creates a network interface (`utun`).

## Setup

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Usage

### Desktop app (recommended)

Build the double-clickable app (and optional installer disk image):

```sh
venv/bin/python3 build_app.py          # → dist/M Maps.app (self-contained)
venv/bin/python3 build_dmg.py          # → dist/M Maps.dmg  (optional)
```

`build_app.py` bundles the `mmaps` package and all pip dependencies **inside**
the `.app` (`Contents/Resources/app` + `vendor`). The launcher resolves paths
from the bundle at runtime, so a DMG you build can be installed on another
Mac (same architecture, with Xcode Command Line Tools) via drag-to-Applications.

Then either:

- open **`dist/M Maps.dmg`**, drag **M Maps** onto **Applications**, and launch from there, or
- double-click **`dist/M Maps.app`**, or
- run from a terminal for development (uses the project venv, not the bundle):

```sh
venv/bin/python3 m_maps.py app
```

A native window opens with the map inside (not a browser). The app starts the
local server with administrator rights so it can talk to the iPhone over USB.

### Browser map GUI

```sh
sudo venv/bin/python3 m_maps.py serve
```

Opens a local map at `http://127.0.0.1:8765` (this Mac only by default).

To open the same map from another device on your home Wi-Fi (e.g. Safari on the
iPhone that is USB-connected to the Mac for spoofing):

```sh
sudo venv/bin/python3 m_maps.py serve --lan
```

That binds to this Mac's private LAN IP (never `0.0.0.0`) and prints the exact
URL. **Anyone on that Wi-Fi can control the page** — there is no password; stop
the server (Ctrl+C) when you are done.

On the map: pick a **Mode** (Teleport, Route, or Multi-stop) and, for Route, a
**Speed** (Walk / Bicycle / Motorcycle / Car / Fly). Click the map or use the
**search box** to pick a place. First point places you; then teleport, follow
roads, fly, or run a multi-stop trip. **Stop here** holds the current point;
**Stop & restore GPS** clears the spoof entirely.

Road routes come from the free public OSRM demo (no key; personal use). Place
search uses Nominatim (no key; be polite with rate limits). Map tiles are
OpenFreeMap. Map/routing/search network calls happen in the UI — the Python
backend only ever talks to your phone.

### Command line

Check the phone's status (no root needed):

```sh
venv/bin/python3 m_maps.py detect
```

Set a location and hold it until you press Enter (needs sudo — the USB location
tunnel creates a `utun` interface, which requires administrator privileges):

```sh
sudo venv/bin/python3 m_maps.py set 48.8584 2.2945
```

Force-clear a simulated location (e.g. after an unclean exit):

```sh
sudo venv/bin/python3 m_maps.py clear
```

Run `venv/bin/python3 m_maps.py <command> --help` for details.

## Logo assets

Color variants of the app mark live under `assets/logo/` (SVG + PNG). The
shipping app icon is built from `mayoka-black.svg` into `assets/icon/AppIcon.icns`
(`scripts/make_app_icon.py`). Other colors are reserved for a future in-app
picker and are not wired into the `.app` yet.
