# M Maps


[![codecov](https://codecov.io/gh/mayoka0/m-maps/graph/badge.svg)](https://codecov.io/gh/mayoka0/m-maps)

A macOS tool that spoofs the GPS location an iPhone reports, over a USB connection.

**An open-source project by [Mayoka Labs](https://github.com/Mayoka-Labs)**

**For harmless pranks only — do not use to deceive, defraud, or evade tracking.**

It's open source specifically so anyone can read exactly what it does to their phone: it only ever sets (or clears) a simulated location via Apple's own developer/instruments protocol. **No telemetry or data collection.** The Python backend talks to the phone over USB; place search is an optional proxy to OpenStreetMap Nominatim. Map tiles and road routing are fetched by the browser (or the embedded webview), not for analytics.

## Features

- **Native desktop app** (recommended) or browser-based map GUI
- **Teleport** — jump the phone to any point on the map
- **Route** — travel real roads at Walk / Bicycle / Motorcycle / Car speed (OSRM)
- **Fly** — straight great-circle flight to the nearest real passenger airport
- **Multi-stop trips** — ordered stops with automatic fly/drive legs, optional wait times, and
  a live “Leave now” control
- **Place search** — OpenStreetMap Nominatim (place names) or raw coordinates
- **Map providers** — free OpenFreeMap styles plus optional Google Maps with your own API key
- **Map views** — Liberty, Bright, Positron, Dark, Fiord, Normal/3D, and Google road/satellite/
  hybrid/terrain views
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

`build_app.py` uses **PyInstaller** (windowed onedir) to produce a self-contained
`.app`: the bootloader at `Contents/MacOS/M Maps` loads Python in-process (no
nested Python.app identity for Dock / Cmd+Tab). App code and dependencies are
collected into the bundle. Build with the project venv after
`pip install -r requirements.txt` and `pip install 'pyinstaller>=6.0'`.

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
collapsible **search** control to pick a place. Teleport asks for confirmation
before moving. Route follows roads, Fly travels a great-circle path, and
Multi-stop can hold at intermediate stops for a chosen amount of time.
**Stop here** holds the current point; **Stop & restore GPS** clears the spoof
entirely.

Road routes come from the free public OSRM demo (no key; personal use). Place
search uses Nominatim (no key; be polite with rate limits). OpenFreeMap is the
default map; Google Maps is optional and requires your own key, stored only in
browser local storage. The browser/webview fetches map assets, OSRM routes,
approximate-IP location, optional update metadata, and Google assets. The
Python backend talks to the phone and proxies explicit Nominatim searches.
M Maps has no telemetry or data collection.

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

## Code Coverage


Coverage is uploaded automatically by the [CI workflow](.github/workflows/ci.yml).


For public repositories, Codecov can upload coverage without a token.
Protected-branch uploads or private forks may require a `CODECOV_TOKEN`
secret; token authentication can be disabled if appropriate.
