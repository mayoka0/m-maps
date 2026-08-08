# M Maps — agent handoff and project source of truth

Read this entire file before changing the project. It describes the code that exists in the
working tree now, including local `develop` work that may not have been committed or pushed yet.
When this file conflicts with an older brief, bug report, README paragraph, or chat transcript,
verify the code and then update this file. `CLAUDE.md` imports this document rather than keeping a
second copy.

## 1. Product, purpose, and boundaries

M Maps is an open-source iPhone location-simulation tool for macOS. It connects to a trusted,
Developer-Mode iPhone over USB and uses `pymobiledevice3` to set the location that iOS reports.
It is intended for harmless pranks and device testing only. Do not use it to deceive, defraud, or
evade tracking.

- Runtime: Python 3, `pymobiledevice3`, FastAPI, uvicorn, and pywebview.
- Device support today: iPhone on iOS 17+ connected to a Mac by USB.
- Development machine: Apple Silicon macOS with Xcode Command Line Tools; do not assume Homebrew
  or full Xcode.
- Current code version: `1.0.4-beta.2` in `mmaps/__init__.py`. Do not bump it automatically.
- Default server exposure: `127.0.0.1:8765`. Optional `--lan` binds only to a specific private
  LAN IPv4 address, never `0.0.0.0`.
- Privacy: M Maps has no telemetry and collects no user data. The Python process talks to the
  phone and proxies explicit place searches to Nominatim. The UI/webview fetches map assets,
  OSRM road routes, approximate-IP location, optional GitHub release metadata, and optional
  Google Maps assets. A user-provided Google key stays in browser `localStorage` and is sent to
  Google, not to the M Maps backend.

The iOS 17+ RemoteXPC tunnel creates a `utun` interface, so setting, clearing, or serving a spoof
session requires root. Detection does not. The native app asks for administrator access through a
macOS system dialog and keeps the visible pywebview process unprivileged.

## 2. Core architecture

### One tunnel, one hold loop, one mutable target

`mmaps/location.py::hold_location()` is the low-level engine. It opens one Remote Service
Discovery tunnel and one DVT `LocationSimulation` channel, reads a coordinate from a callable,
and re-asserts that coordinate approximately every 2.5 seconds. The DVT connection must remain
open or iOS returns to real GPS. A wake event makes retargets immediate instead of waiting for the
next keepalive tick.

Recoverable USB, tunnel, and transient lock errors are caught inside the loop. It reports a calm
reconnecting state, backs off, rebuilds the tunnel, and resumes the latest coordinate. A recovered
per-tick error is not displayed as a fatal UI error. Unexpected failures terminate the engine and
are humanized by `mmaps/errors.py`.

`mmaps/session.py::SpoofSession` is the only stateful owner of that engine:

- `start()` caches device facts and starts one hold task with no target.
- `set_target(lat, lon)` changes the single mutable target and wakes the existing loop.
- `start_movement(points, tick_seconds, kind)` cancels earlier movement and walks `(lat, lon)`
  points by repeatedly calling `set_target()`. It does not rebuild the tunnel.
- `start_trip(legs)` runs drive/fly legs in order through the same movement mechanism.
- A trip leg may include `wait_seconds`. Between non-final legs, the session holds the arrival
  point until the wait expires or `leave_now()` is called. The final point already holds
  indefinitely, so a final-leg wait is intentionally ignored.
- `stop_movement()` cancels movement/trip but continues holding the current point.
- `stop()` cancels movement, clears the simulated location, and ends the hold loop.
- `status()` returns cached device facts and live engine, assertion, movement, reconnect, and trip
  state without performing lockdownd I/O during polling.
- `applied` means a real DVT `set()` succeeded on the current connection; merely having a live
  asyncio task is not treated as success.

All movement modes reuse this exact engine:

| User action | Path source | Engine operation |
|---|---|---|
| Teleport | One confirmed clicked/searched coordinate | `set_target()` once, then hold |
| Walk / Bicycle / Motorcycle / Car | Full OSRM road geometry | Resample, then `start_movement(..., 0.25, "drive")` |
| Fly | Great-circle start-to-destination path, landing at nearest passenger airport | Resample, then `start_movement(..., 0.5, "fly")` |
| Multi-stop | Ordered drive/fly legs planned by the UI | `start_trip()`, including optional dwell waits |

### Road and flight motion

`mmaps/route.py` follows the complete OSRM polyline exactly in production. Defaults are
`TICK_SECONDS = 0.25`, `JITTER_METERS = 0.0`, and speeds of Walk 5, Bicycle 16, Motorcycle 55,
and Car 60 km/h. Cosine speed easing slows starts, finishes, and sharp turns. Unconstrained
Catmull–Rom smoothing remains available as a legacy opt-in but is off by default because it can
cut corners and place the phone beside the routed road.

`mmaps/flight.py` uses great-circle interpolation at about 875 km/h. Slow/Normal/Fast multipliers
are 0.85/1.0/1.1, and the flight tick is 0.5 seconds. Current flight playback is realistic-time,
not the early 1–3 minute global time-compression design. `duration_seconds` can override playback
duration through the API, but there is not yet a finished trip-duration/arrival-time UI.

Fly has no draw-path mode. Standalone and multi-stop fly legs snap their destination to the
nearest bundled medium/large passenger airport when one exists.

For automatic multi-stop planning, `mmaps/trip.py` chooses Fly when OSRM has no route or the
great-circle distance is at least 1,000 km; otherwise it chooses Drive. This threshold applies to
automatic trip leg selection only. There is no current `mmaps/modes.py`: the old destination
mode-availability menu was replaced by explicit Teleport / Route / Multi-stop controls and a
user-selected Route speed.

## 3. Current UI behavior

The frontend is a local, no-build HTML/CSS/JavaScript app. `mmaps/web/index.html` contains the
layout and most feature logic; focused modules under `mmaps/web/js/` isolate provider and chrome
state.

- A single floating translucent command bar sits over the map. It contains M Maps identity,
  compact iPhone status/details, collapsed Search, Teleport/Route/Multi-stop mode controls,
  Route speeds, Settings, and Restore GPS.
- Search is an icon while idle. It expands into an input when used and collapses on Escape or an
  outside click. Place-name lookup occurs only on explicit Search/Enter to respect Nominatim;
  `lat, lon` text is parsed locally.
- Teleport clicks do not immediately move the phone. A destination preview asks the user to
  confirm with Move/Place here, preventing accidental map clicks.
- Route requests the public OSRM demo route in the browser, draws it, and starts the selected road
  speed through `POST /drive`. OSRM no-route, network, and rate-limit failures produce friendly
  choices rather than a FastAPI 404 or raw exception.
- Multi-stop builds ordered stops, selects a dwell time (presets or custom 1–1,440 minutes), plans
  drive/fly legs, shows live progress/countdown, and provides Leave now during a dwell. Status
  polling uses a render fingerprint so it does not replace an open native `<select>` under the
  mouse—the fix for the wait menu disappearing before a choice could be made.
- Stop here cancels movement but holds the current location. Stop & restore GPS clears the spoof.
- No emoji are used in the application UI.

### Map providers and zoom behavior

OpenFreeMap/MapLibre is the no-key default. Styles include Liberty, Bright, Positron, Dark, Fiord,
and a pitched 3D view. The optional Google provider accepts the user's own Maps JavaScript API
key and supports roadmap, satellite, hybrid, and terrain. Provider/style/key choices are browser
local state.

MapLibre has repeated-world rendering disabled and a minimum zoom. Google dynamically computes a
minimum zoom from viewport width and uses strict longitude bounds so zooming out cannot display
many duplicate worlds. Provider switching and overlay updates are separated to reduce Google zoom
lag. Backdrop blur is disabled over an active Google map because animating Google tiles behind
blur was disproportionately expensive; the bar remains translucent.

## 4. HTTP API

Coordinates in JSON road/flight arrays use GeoJSON order `[longitude, latitude]`. The engine uses
`(latitude, longitude)` tuples internally. Device-dependent endpoints return HTTP 503 when no
phone session is confirmed. Validation and service failures use FastAPI `{ "detail": "..." }`.

| Method and path | Request | Successful response and purpose |
|---|---|---|
| `GET /` | none | HTML map page, `Cache-Control: no-store` |
| `GET /status` | none | Current device/session/UI capability snapshot described below |
| `POST /device/confirm` | no body | `{ok:true, device, udid}`; attach the pending USB iPhone |
| `POST /device/ignore` | no body | `{ok:true}`; ignore the pending phone for this server run |
| `GET /geocode` | `?q=<text>&limit=1..10` (default 6) | `{results:[{lat,lon,name,type,class}]}` via Nominatim; never moves phone |
| `GET /nearest_airport` | `?lat=-90..90&lon=-180..180` | `{airport:{name,iata,icao,lat,lon,municipality,country,type,label,distance_m}|null}` from bundled data |
| `POST /spoof` | `{lat, lon}` | `{ok:true, applied, target:{lat,lon}}`; cancel movement, set and hold |
| `POST /drive` | `{coordinates:[[lon,lat],...], mode:"walk"|"bicycle"|"motorcycle"|"car", duration_seconds?:number}` | `{ok:true, applied, mode, points, eta_seconds, duration_override}` |
| `POST /fly` | `{waypoints:[[lon,lat],...], speed?:"slow"|"normal"|"fast", duration_seconds?:number}` | `{ok:true, applied, speed, points, eta_seconds, duration_override}` |
| `POST /trip` | `{legs:[{kind:"drive",coordinates,wait_seconds?}|{kind:"fly",waypoints,wait_seconds?}]}` | `{ok:true, applied, legs}`; waits must be 0–86,400 seconds |
| `POST /stop_move` | no body | `{ok:true}`; stop motion/trip and hold here |
| `POST /trip/leave_now` | no body | `{ok:true, skipped:boolean}`; skip an active between-leg dwell |
| `POST /stop` | no body | `{ok:true}`; clear simulation and return to real GPS |
| `POST /shutdown` | no body | `{ok:true}` in desktop mode only; otherwise 404 |

`GET /status` includes:

```jsonc
{
  "device": {
    "name": "iPhone", "ios_version": "...", "product_type": "...",
    "trusted": true, "developer_mode": true,
    "connected": true, "link_lost": false
  },
  "pending_device": null,
  "state": "waiting_for_device|awaiting_confirm|idle|connecting|ready|holding|reconnecting|stopped|error|drive|fly|trip",
  "target": {"lat": 0.0, "lon": 0.0},
  "error": null,
  "error_fatal": false,
  "engine_live": true,
  "applied": true,
  "assert_live": true,
  "reconnect_stale": false,
  "reconnect_seconds": null,
  "moving": false,
  "movement": {"kind": "drive|fly", "index": 0, "total": 0},
  "trip": {"leg": 1, "legs": 3, "kind": "drive|fly", "phase": "moving|waiting", "wait_remaining_seconds": 120},
  "spoofing": true,
  "features": ["teleport", "drive", "fly", "geocode", "airports", "trip", "device_confirm"],
  "lan": false,
  "version": "1.0.4-beta.2",
  "beta": true,
  "github_repo": "mayoka0/m-maps"
}
```

Fields irrelevant to the current state can be `null` or absent. Device facts are cached at attach
time so the one-second UI poll never races the active tunnel through lockdownd.

## 5. File inventory

### Runtime and core code

- `m_maps.py` — minimal executable entry point into `mmaps.cli.main()`.
- `mmaps/__init__.py` — package metadata and beta-version helper.
- `mmaps/cli.py` — `detect`, `set`, `clear`, `serve`, and `app` commands; root checks, preflight,
  friendly errors, browser opening, and CLI hold-until-Enter behavior.
- `mmaps/device.py` — usbmux/lockdownd connection, trust/pairing cache handling, device facts, and
  Developer Mode guidance/enabling. Pairing paths account for `$SUDO_USER`.
- `mmaps/location.py` — tunnel/DVT lifecycle, keepalive assertion, reconnect classification, and
  standalone clear.
- `mmaps/session.py` — `SpoofSession`, movement/trip orchestration, dwell timers, assertion truth,
  and status snapshots.
- `mmaps/route.py` — distance, turn detection, optional smoothing, easing, and road resampling.
- `mmaps/flight.py` — great-circle interpolation, flight speeds/easing, and resampling.
- `mmaps/trip.py` — pure automatic drive-versus-fly leg choice (`FLY_MIN_STRAIGHT_KM = 1000`).
- `mmaps/airports.py` — loads and searches the offline airport dataset.
- `mmaps/data/airports.csv` — bundled OurAirports-derived medium/large scheduled-service data.
- `mmaps/errors.py` — converts pymobiledevice/device/network exception text to friendly messages.
- `mmaps/server.py` — FastAPI app, device polling/confirmation, API validation, Nominatim proxy,
  session lifecycle, safe bind enforcement, and uvicorn runner.
- `mmaps/desktop.py` — normal-user pywebview process plus elevated local server helper launched by
  `osascript`.

### Frontend

- `mmaps/web/index.html` — complete visual interface, inline CSS, feature state, search, OSRM
  requests, trip planning, settings, status rendering, and API calls.
- `mmaps/web/js/mmaps-keys.js` — sole owner of Google API key test/session/persistence lifecycle.
- `mmaps/web/js/mmaps-map-provider.js` — loads and switches MapLibre/Google, owns Google map type,
  one-world bounds, and provider teardown/retry.
- `mmaps/web/js/mmaps-map-adapter.js` — one provider-neutral marker/polyline API mirrored to both
  map implementations.
- `mmaps/web/js/mmaps-chrome.js` — panel and segmented-control state with one visibility source of
  truth.

### Packaging, assets, and dependencies

- `requirements.txt` — runtime dependencies: pymobiledevice3, FastAPI, uvicorn, pywebview.
- `requirements-dev.txt` — runtime requirements plus pytest and coverage tooling.
- `build_app.py` — creates the self-contained PyInstaller `dist/M Maps.app`, including elevated
  server helper and generated bundle metadata/spec.
- `build_dmg.py` — creates a branded drag-to-Applications `dist/M Maps.dmg` with macOS tools.
- `packaging/desktop_entry.py` — frozen app entry point.
- `packaging/m_maps.spec` — generated/current PyInstaller build specification.
- `packaging/rthook_stub_ipython.py` — stubs pymobiledevice3's unused optional IPython dependency
  inside the frozen app.
- `scripts/make_app_icon.py` — regenerates icon assets.
- `assets/icon/AppIcon-1024.png`, `assets/icon/AppIcon.icns`, and
  `assets/icon/apple_official_icon_shape_mask.png` — source and shipping app icon assets.
- `assets/logo/README.md` and `assets/logo/mayoka-{black,blue,gold,gray,green,orange,purple,red,white}.{svg,png}`
  — Mayoka mark usage notes and color variants; variants are not an in-app theme feature.

### Tests and automation

- `tests/__init__.py` — test package marker.
- `tests/test_errors.py` — friendly error conversion.
- `tests/test_flight.py` — great-circle math, realistic timing, duration override, and easing.
- `tests/test_route.py` — route precision, timing, easing, and no-default-smoothing behavior.
- `tests/test_trip.py` — automatic drive/fly threshold logic.
- `tests/test_session_trip_wait.py` — dwell holding, countdown state, final-wait behavior, and
  Leave now.
- `tests/test_server_bind.py` — localhost/private-LAN bind safety.
- `tests/test_web_ui_contract.py` — static frontend contracts: one world, Teleport confirmation,
  unified command bar, collapsed explicit search, and stable wait selector.
- `.github/workflows/ci.yml` — pytest/coverage matrix on macOS, Linux, and Windows; phone/tunnel
  integration cannot run in CI.
- `.github/ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml`, and `config.yml` — issue forms.
- `.github/PULL_REQUEST_TEMPLATE.md` — PR checklist/template.

### Repository and human documentation

- `README.md` — public product overview, setup, usage, and ethics note.
- `CHANGELOG.md` — public releases and current unreleased beta notes. Some unreleased bullets may
  lag the newest working-tree fixes; update at a release checkpoint, not per tiny edit.
- `CONTRIBUTING.md` — contributor setup and branch/release conventions.
- `CODE_OF_CONDUCT.md` — contributor conduct policy.
- `SECURITY.md` — private vulnerability-reporting process and security scope.
- `LICENSE` — project license.
- `.gitignore` — generated files, venv, local secrets, and local agent notes.
- `AGENTS.md` — this canonical handoff.
- `CLAUDE.md` — ignored local Claude entry file that imports `AGENTS.md`; do not duplicate state.
- `BUGS.md` and `BUGS2.md` — ignored historical inspection reports. They describe older code and
  are evidence/history, not current truth.

Generated/local directories such as `venv/`, `build/`, `dist/`, `.pytest_cache/`,
`.layout-verify/`, `.git/`, coverage output, and `.DS_Store` are not source files.

## 6. How to set up, run, and test

### One-time environment

```sh
cd /Users/mj/Developer/m-maps
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install -r requirements-dev.txt
```

Do not reinstall the environment unless it is missing or broken. Under sudo, always invoke the
venv interpreter by path; sudo does not preserve an activated venv's PATH.

### Commands

```sh
# Read-only device status; no root:
venv/bin/python3 m_maps.py detect

# Recommended development/native window; macOS asks for an admin password:
venv/bin/python3 m_maps.py app
venv/bin/python3 m_maps.py app --port 8765

# Browser UI, Mac-local by default:
sudo venv/bin/python3 m_maps.py serve
sudo venv/bin/python3 m_maps.py serve --port 8765 --no-browser

# Explicit home-LAN control page; no auth, so stop it when done:
sudo venv/bin/python3 m_maps.py serve --lan

# CLI set-and-hold, then force-clear if needed:
sudo venv/bin/python3 m_maps.py set 48.8584 2.2945
sudo venv/bin/python3 m_maps.py clear

# Offline suite:
venv/bin/python3 -m pytest -q

# Packaging (local artifacts only):
venv/bin/python3 build_app.py
venv/bin/python3 build_dmg.py
```

If server routes or Python state change, restart `serve`/`app`. The HTML is read from disk on each
request, but an already-running process still has the old Python routes in memory.

### Real-iPhone manual regression flow

1. Plug the iPhone into USB, unlock it, accept Trust, and ensure Developer Mode is enabled. Enabling
   it may reboot the phone; leave it connected and unlock again.
2. Start `venv/bin/python3 m_maps.py app` or `sudo venv/bin/python3 m_maps.py serve`.
3. Confirm the detected device. Verify name, iOS, trust, and Developer Mode in the device panel.
4. In Teleport, click a point. Confirm that nothing moves until Move/Place here is pressed. Verify
   the new location in iPhone Maps and Find My, then intentionally click elsewhere and cancel.
5. In Route, place a start, choose each road speed, select a nearby destination, inspect the OSRM
   line, and start. The dot should remain on the full road polyline with no corner-cutting or
   synthetic sidewalk jitter. Stop here should hold the current point.
6. Test OSRM failure/no-route behavior and ensure the UI offers a calm retry/Teleport path.
7. Test Fly. It should draw/follow a great-circle path and land at the nearest passenger airport.
8. Build a multi-stop trip with at least three stops. Give the first two different waits, verify
   the countdown persists and the wait selector stays open, use Leave now on one stop, and verify
   the final destination holds indefinitely.
9. Switch among OpenFreeMap styles and Google map types. Zoom fully out: only one world should be
   visible. Check Google zoom responsiveness and settings/key failure recovery.
10. Lock/unlock the phone briefly and jostle/reconnect USB during movement. A transient event may
    show reconnecting, should clear after recovery, and must not expose a raw exception.
11. Press Stop & restore GPS and verify real GPS returns. Also verify Ctrl+C/window close restores
    GPS. If not, reconnect USB and run the `clear` command.

The iPhone screen may be off while M Maps runs, as long as the phone remains powered, connected,
trusted, and accessible. The Mac and foreground M Maps process must stay awake/running; sleep or
process termination breaks active playback and the DVT connection.

## 7. Known issues and deliberate limitations

- Antimeridian overlay: a great-circle flight crossing ±180° can be drawn across the long side of
  a flat map. Device points still take the correct short route; this is cosmetic.
- Road centerlines differ among OSRM/OpenStreetMap, Google, and Apple. The emitted point remains on
  OSRM's route, but another provider can visually place that coordinate a few metres beside its
  own road drawing.
- Public OSRM and Nominatim services are rate-limited and appropriate only for light personal use.
  Distribution at scale needs a hosted/keyed routing/search decision.
- A Google key is user-supplied and stored in browser localStorage. M Maps does not validate
  billing/restriction setup beyond attempting to load Google Maps.
- Long or permanent USB loss can leave movement progression visually frozen while the engine
  retries. Fatal assertion state must never be represented as a successful phone move.
- Mac sleep interrupts timers, networking, and the USB tunnel. There is no daemon or wake lock.
- Server APIs permit choices the normal UI may not expose; there is no authentication on localhost
  or opt-in LAN mode. `--lan` trusts the private Wi-Fi network.
- Packaging scripts exist, but signing, notarization, universal Intel support, and a polished
  public distribution pipeline are not complete.
- The current UI has had a major command-bar/settings polish pass, but the planned destination
  card, richer active-journey card, deeper settings organization, responsive/mobile refinement,
  and full accessibility/keyboard pass remain future design slices.

## 8. Not built / future research

- No Android device engine. Android mock-location behavior, permissions, OEM differences, root/no-
  root feasibility, and cross-platform ADB architecture need a separate research/prototype phase.
- No Windows or Linux iPhone tunnel implementation, and no Intel/universal macOS release artifact.
- No automatic update installation. The UI can perform an optional notify-only GitHub release
  check; it does not download or install updates.
- No public-internet remote control, accounts, authentication, Tailscale/UPnP/port forwarding, or
  persistent server/daemon. `--lan` exposes only the webpage on a private LAN; the phone remains
  USB-connected to the Mac.
- No route schedule/arrival-time editor yet. Per-stop waits work; generalized trip-duration and
  departure scheduling remain future features.
- No joystick, custom drawn flight path, altitude simulation, Google routing, or self-hosted
  OSRM/Valhalla.
- No automatic airport-availability intelligence beyond nearest bundled airport plus the 1,000 km
  multi-stop heuristic.

## 9. Working rules for agents

Work in thin, testable, reversible increments. Preserve readable comments and the one-engine
architecture. Do not fork Teleport/Drive/Fly into separate tunnel owners. After each meaningful
change, report exact commands and a real-phone test flow, then wait for MJ's result.

The repository may have intentional uncommitted work. Inspect `git status` and diffs first; never
discard or overwrite unrelated changes. Keep UI changes in separate logical increments so an
unwanted design decision can be reverted independently.

### Nothing ships without MJ's explicit approval each time

1. Build and test locally. Do not `git commit` or `git push` to any branch, including `develop`,
   unless MJ explicitly authorizes that action for the current batch.
2. Do not touch `main`, create tags, publish a GitHub Release, or merge branches unless MJ
   explicitly requests that exact action.
3. Do not bump `__version__` automatically. Related fixes can accumulate and be tested under the
   same beta version until MJ chooses a checkpoint.
4. When a tested increment is ready, stop and ask: **“Is this good to commit and push to develop
   now?”** A prior approval does not carry forward to later work.

`develop` is the local working line. `main` remains the stable/public line. Current documentation
work must remain local unless MJ separately approves a commit and push.
