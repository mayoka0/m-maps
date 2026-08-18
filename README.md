# M Maps

M Maps is an open-source desktop tool for setting a connected phone's simulated
GPS location over USB. It supports iPhone through `pymobiledevice3` and Android
through the M Maps Companion app and ADB.

Use M Maps for educational purposes only.

## What it does

- Teleport to a confirmed point and hold that location.
- Follow a road route by Walk, Bicycle, Motorcycle, or Car.
- Fly along a great-circle path to the nearest passenger airport.
- Build multi-stop trips with optional waits between stops.
- Stop movement while holding the current point, or restore real GPS.
- Search for places, enter coordinates, and choose OpenFreeMap or Google Maps.

M Maps has no telemetry and does not collect user data. The phone connection is
local and USB-based. Search, map, routing, approximate-IP location, optional
release checks, and Google Maps are separate network services used by the UI.

## Current support

| Target | Current support |
| --- | --- |
| iPhone | iOS 17 or later, USB, Trust This Computer, Developer Mode |
| Android | Tested on Samsung A15 with Android 16, USB debugging, M Maps Companion |
| Desktop host | macOS is the supported host in this release |
| Windows and Linux | No host release yet. Android support on those hosts is future work |
| Mac hardware | Apple Silicon is tested. Intel and universal release artifacts are not finished |

Only one phone is controlled at a time. If both an iPhone and Android phone are
connected before a session starts, M Maps asks you to unplug one instead of
guessing.

## Requirements

- macOS with Python 3.9 or later. CI currently checks Python 3.12 through 3.14.
- A USB-connected phone.
- Administrator access for an iPhone tunnel. Android does not need `sudo`.
- Android Studio or Android platform-tools when using the Android companion.

## Setup

```sh
cd /Users/mj/Developer/m-maps
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

For local development tools:

```sh
venv/bin/pip install -r requirements-dev.txt
```

## Run M Maps

### Native macOS window

This is the recommended development command. macOS opens an administrator
approval dialog when the iPhone tunnel is needed.

```sh
venv/bin/python3 m_maps.py app
```

### Local browser window

The default server listens only on this Mac at `127.0.0.1:8765`.

```sh
sudo venv/bin/python3 m_maps.py serve
```

Use `--no-browser` when you want to open the address yourself. Use `--port`
to choose another local port.

The optional LAN mode binds to this Mac's private LAN address, never to all
interfaces:

```sh
sudo venv/bin/python3 m_maps.py serve --lan
```

LAN mode has no login. Anyone on that private Wi-Fi can control the connected
phone, so stop the server with Ctrl+C when finished.

## iPhone setup and test

1. Connect and unlock the iPhone.
2. Tap Trust This Computer and enter the passcode if asked.
3. Enable Settings > Privacy & Security > Developer Mode, then restart if the
   phone asks.
4. Start M Maps with the native or browser command above.
5. Choose Teleport, click a destination, and confirm the move.
6. Verify the point in Maps or Find My.
7. Try Route, Fly, and Multi-stop. Stop here keeps the current point.
8. Use Restore GPS to clear the simulation and return to the real location.

The iPhone can have its screen off while it remains powered, trusted, connected,
and accessible. The Mac process must keep running. Mac sleep or process exit
breaks active playback.

## Android setup and test

Build and install the companion from Android Studio in
[`android-companion/`](android-companion/). On the phone:

1. Enable Developer options and USB debugging.
2. Approve this Mac when Android asks to allow USB debugging.
3. Select M Maps Companion under Developer options > Mock location app.
4. Connect the phone and run the unified server command:

```sh
sudo venv/bin/python3 m_maps.py serve
```

The server detects the only connected phone automatically. Test Teleport, each
route mode, Fly, Multi-stop, Stop here, and Restore GPS. Android keeps its last
simulated point after a temporary USB disconnect and reconnects when the cable
returns. Restoring real GPS requires the Android link to be available again.

The legacy Android-only command remains available for troubleshooting:

```sh
venv/bin/python3 m_maps.py serve --android
```

See [`android-companion/README.md`](android-companion/README.md) for the
companion protocol and Android-specific setup details.

## Command line

Read device status without root:

```sh
venv/bin/python3 m_maps.py detect
```

Set and hold a point until Enter is pressed:

```sh
sudo venv/bin/python3 m_maps.py set 48.8584 2.2945
```

Clear a simulation after an unclean exit:

```sh
sudo venv/bin/python3 m_maps.py clear
```

Run `venv/bin/python3 m_maps.py <command> --help` for command details.

## How the engine works

`SpoofSession` owns one phone connection, one keepalive loop, and one mutable
target. Teleport changes the target directly. Route and Fly feed timed points
to the same session. Multi-stop uses the same movement scheduler and adds wait
periods between legs. A dropped connection is retried and the latest target is
reasserted. A transient recovered error is not shown as a fatal movement error.

The FastAPI server is local by default and serves the MapLibre web interface.
The native macOS window embeds the same interface through pywebview. Android
uses the shared movement scheduler but sends target updates to the companion
over an ADB port-forwarded localhost socket.

## Main API endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Serve the map interface |
| `GET` | `/status` | Device, connection, spoof, movement, and trip state |
| `GET` | `/devices` | List discovered phones |
| `GET` | `/autocomplete` | Search-as-you-type suggestions |
| `GET` | `/geocode` | Explicit place search |
| `GET` | `/nearest_airport` | Find the nearest bundled passenger airport |
| `POST` | `/device/confirm` | Confirm a pending iPhone |
| `POST` | `/device/ignore` | Ignore a pending phone for this run |
| `POST` | `/spoof` | Set and hold one coordinate |
| `POST` | `/drive` | Start Walk, Bicycle, Motorcycle, or Car movement |
| `POST` | `/fly` | Start great-circle flight movement |
| `POST` | `/trip` | Start ordered drive and flight legs |
| `POST` | `/stop_move` | Stop moving and keep holding the current point |
| `POST` | `/trip/leave_now` | Skip an active multi-stop wait |
| `POST` | `/stop` | Clear the simulation and restore real GPS |

Coordinates in route arrays use GeoJSON order: `[longitude, latitude]`.
The server rejects oversized route, flight, timing, and multi-stop requests
before movement points are generated. This protects local and optional LAN
sessions from accidental or abusive memory use.

## Testing and packaging

Run the offline test suite and syntax check:

```sh
venv/bin/python3 -m pytest -q
venv/bin/python3 -m compileall -q mmaps m_maps.py
```

The suite checks routing and flight math, session waits, API validation, device
selection, Android transport behavior, server contracts, and frontend contracts.
It cannot replace testing with a real phone.

Local macOS packaging commands are available, but signed and notarized public
distribution is not complete:

```sh
venv/bin/python3 build_app.py
venv/bin/python3 build_dmg.py
```

## Network and privacy notes

- OpenFreeMap is the default map provider and needs no key.
- Google Maps is optional and uses a key supplied by the user. The key remains
  in browser local storage and is sent to Google, not to the M Maps backend.
- Valhalla provides public route geometry and timing for light personal use.
- Photon provides autocomplete and Nominatim provides explicit place search.
- These public services can rate-limit requests. A distributed release will
  need a deliberate routing and geocoding strategy.
- The optional LAN mode has no login or authentication. Use it only on a
  trusted private network and stop the server when finished.

## Contributing

Please keep changes small, readable, tested, and focused. Never add telemetry,
hidden behavior, credentials, or network calls that are not documented. Do not
replace the shared `SpoofSession` engine with separate tunnel owners.

The stable branch is `main`. Development work is kept on preview branches until
it is tested and explicitly approved for release. See
[`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Current limitations

- A flight crossing the antimeridian can draw across the long side of the flat
  map. The phone points still follow the short great-circle path.
- OpenStreetMap-derived route geometry can differ by a few metres from Apple or
  Google map drawings.
- No Windows or Linux host release, iPhone tunnel, public remote control,
  accounts, persistence daemon, automatic updater, Google routing, custom drawn
  flight path, or altitude simulation is included.
- Trip waits work, but a general departure-time and arrival-time editor is not
  finished.

## License

See [`LICENSE`](LICENSE).
