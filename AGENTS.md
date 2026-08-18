# M Maps - agent handoff and project source of truth

Read this entire file before changing the project. It describes the code that exists in the
working tree now, including local `cross-platform-develop` work that may not have been committed or pushed yet.
When this file conflicts with an older brief, bug report, README paragraph, or chat transcript,
verify the code and then update this file. `CLAUDE.md` imports this document rather than keeping a
second copy.

## 1. Product, purpose, and boundaries

M Maps is an open-source phone location-simulation tool controlled from macOS. It supports a
trusted Developer-Mode iPhone through `pymobiledevice3`, plus an Android prototype through ADB
and the bundled M Maps Companion app.
It is intended for harmless pranks and device testing only. Do not use it to deceive, defraud, or
evade tracking.

- Runtime: Python 3, `pymobiledevice3`, FastAPI, uvicorn, and pywebview.
- Device support today: iPhone on iOS 17+ over USB, and a tested Android prototype (Samsung A15,
  Android 16) using USB debugging plus the M Maps Companion mock-location app.
- Development machine: Apple Silicon macOS with Xcode Command Line Tools; do not assume Homebrew
  or full Xcode.
- Current release version: `1.0.5` in `mmaps/__init__.py`. Do not bump it automatically.
- Default server exposure: `127.0.0.1:8765`. Optional `--lan` binds only to a specific private
  LAN IPv4 address, never `0.0.0.0`.
- Privacy: M Maps has no telemetry and collects no user data. The Python process talks to the
  phone and proxies Photon autocomplete plus explicit Nominatim place searches. The UI/webview fetches map assets,
  Valhalla profile-aware routes, approximate-IP location, optional GitHub release metadata, and optional
  Google Maps assets. A user-provided Google key stays in browser `localStorage` across normal
  app restarts and updates and is sent to Google, not to the M Maps backend.

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
| Walk / Bicycle / Motorcycle / Car | Valhalla pedestrian/bicycle/motorcycle/auto geometry and timing | Resample, then `start_movement(..., 0.25, "drive")` |
| Fly | Great-circle start-to-destination path, landing at nearest passenger airport | Resample, then `start_movement(..., 0.5, "fly")` |
| Multi-stop | Ordered drive/fly legs planned by the UI | `start_trip()`, including optional dwell waits |

### Android companion architecture

Android uses the same route/flight/trip point schedulers but replaces the iOS DVT target writer:

- `android-companion/` is a small Kotlin app selected once under Developer options → Mock location
  app. Its foreground service accepts newline-delimited JSON on device-local `127.0.0.1:8765`.
- `mmaps/android_device.py` detects one authorized ADB phone, verifies/starts the companion,
  forwards host `127.0.0.1:8766` to device port `8765`, and requires a JSON acknowledgment for
  every command. The acknowledgment prevents a dead ADB forward from reporting false success.
- `mmaps/android_session.py::AndroidSpoofSession` subclasses the shared `SpoofSession` movement
  orchestration. `start_movement()` and `start_trip()` remain shared; only `set_target()` writes
  through ADB instead of DVT.
- The phone service re-asserts its latest target every three seconds. A location therefore stays
  active after USB is disconnected. Reconnect polling runs every second, recreates forwarding,
  restarts the companion when needed, and resends the latest target.
- `CLEAR_LOCATION` disables Android mock mode and restores real GPS. Android does not require
  macOS root/sudo. One-time setup requires USB debugging approval and choosing M Maps Companion
  as the mock-location app.

### Unified automatic device selection

`mmaps/platforms.py::DeviceCandidate` is the common discovery shape for both transports. The
server's default platform is `auto`: it scans usbmux and ADB together and automatically attaches
the only connected phone. If both platforms are connected with no active session, the UI asks the
user to unplug one instead of guessing. Only one phone is controlled at a time in this beta.
An iPhone still keeps the explicit Trust/Developer Mode confirmation; a ready Android phone
attaches immediately. The compatibility `/device/select` endpoint remains available for
programmatic callers, but the normal UI no longer exposes a picker. If Android is unplugged while
holding a target, switching is blocked until it is reconnected and real GPS is restored. An
unplugged iPhone is marked disconnected immediately from usbmux discovery while its session is
retained for same-phone cable recovery; plugging in another phone retires that absent session and
continues automatic detection.
`--android` remains a temporary Android-only compatibility mode.

### Road and flight motion

The browser requests separate Valhalla costing profiles: pedestrian favors sidewalks/footpaths,
bicycle favors cycleways and rejects forbidden access, motorcycle uses its road profile, and car
uses auto. The public demo is for light beta use. `mmaps/route.py` follows the complete returned
polyline and uses router maneuver timing so highway/local sections do not share one fixed speed.
Fallback API speeds remain Walk 5, Bicycle 16, Motorcycle 55, and Car 60 km/h when timing is not
provided. Defaults are `TICK_SECONDS = 0.25` and `JITTER_METERS = 0.0`. Unconstrained
Catmull-Rom smoothing remains available as a legacy opt-in but is off by default because it can
cut corners and place the phone beside the routed road.
Walk routes are limited to 15 km in both the UI and API; longer routes use another road mode or
Teleport. Route, flight, and trip geometry is validated at the API boundary for finite coordinates,
valid geographic ranges, timing indexes, and bounded input size.

`mmaps/flight.py` uses great-circle interpolation at about 875 km/h. Slow/Normal/Fast multipliers
are 0.85/1.0/1.1, and the flight tick is 0.5 seconds. Current flight playback is realistic-time,
not the early 1-3 minute global time-compression design. `duration_seconds` can override playback
duration through the API, but there is not yet a finished trip-duration/arrival-time UI.

Fly has no draw-path mode. Standalone and multi-stop fly legs snap their destination to the
nearest bundled medium/large passenger airport when one exists.
Flight overlays are UI-only drawing paths; device movement still follows the session's generated
flight points, so keep overlay rendering and device motion in sync when changing fly behavior.

For automatic multi-stop planning, `mmaps/trip.py` chooses Fly when Valhalla has no auto route or the
great-circle distance is at least 1,000 km; otherwise it chooses Drive. This threshold applies to
automatic trip leg selection only. There is no current `mmaps/modes.py`: the old destination
mode-availability menu was replaced by explicit Teleport / Route / Multi-stop controls and a
user-selected Route speed.

## 3. Current UI behavior

The frontend is a local, no-build HTML/CSS/JavaScript app. `mmaps/web/index.html` contains the
layout and most feature logic; focused modules under `mmaps/web/js/` isolate provider and chrome
state.

- The map runs edge to edge beneath minimal floating controls. A sidebar button opens one
  collapsible, Apple-Maps-inspired M Maps drawer; the app does not copy Apple assets or labels.
  The native traffic lights and sidebar toggle share the first row. A persistent full-width Search
  field sits directly below, followed by Teleport/Route/Multi-stop, route travel modes, device
  state, map appearance/provider choices, and API configuration. Restore GPS remains independently
  visible as the safety action. There is no duplicate app title, gear icon, or full-width toolbar.
- Device connection and engine details are a drawer section instead of permanently occupying the
  map. The drawer closes with its back button, Escape, or a map/outside click, and restores
  keyboard focus to its opener when closed from the keyboard or close control. A disconnected-
  phone notice appears at the bottom where it cannot be covered by native traffic lights.
- Search remains a full-width field while the drawer is open; closing the drawer hides the entire
  control surface. Three or more characters trigger debounced Photon suggestions after 400 ms;
  Enter uses Nominatim for a full search. `lat, lon` text is parsed locally.
- Teleport clicks do not immediately move the phone. A destination preview asks the user to
  confirm with Move/Place here, preventing accidental map clicks.
- Route requests the selected Valhalla profile in the browser, draws it, and sends geometry plus
  road-aware timing through `POST /drive`. No-route, network, and rate-limit failures produce friendly
  choices rather than a FastAPI 404 or raw exception.
- Multi-stop builds ordered stops, selects a dwell time (presets or custom 1-1,440 minutes), plans
  drive/fly legs, shows live progress/countdown, and provides Leave now during a dwell. Status
  polling uses a render fingerprint so it does not replace an open native `<select>` under the
  mouse-the fix for the wait menu disappearing before a choice could be made.
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
| `GET /devices` | none | `{devices:[...],active,platform}`; all discovered iPhone/Android candidates |
| `POST /device/select` | `{platform:"ios"|"android",identifier}` | Select one candidate; iPhone returns `requires_confirmation:true`, ready Android attaches |
| `POST /device/confirm` | no body | `{ok:true, device, udid}`; attach the pending USB iPhone |
| `POST /device/ignore` | no body | `{ok:true}`; ignore the pending phone for this server run |
| `GET /geocode` | `?q=<text>&limit=1..10` (default 6) | `{results:[{lat,lon,name,type,class}]}` via Nominatim; never moves phone |
| `GET /autocomplete` | `?q=<text>&limit=1..8&lat?&lon?` | `{results:[{lat,lon,name,type,class}]}` via Photon search-as-you-type; never moves phone |
| `GET /nearest_airport` | `?lat=-90..90&lon=-180..180` | `{airport:{name,iata,icao,lat,lon,municipality,country,type,label,distance_m}|null}` from bundled data |
| `POST /spoof` | `{lat, lon}` | `{ok:true, applied, target:{lat,lon}}`; cancel movement, set and hold |
| `POST /drive` | `{coordinates:[[lon,lat],...], mode:"walk"|"bicycle"|"motorcycle"|"car", timing_sections?:[{start_index,end_index,duration_seconds}], duration_seconds?:number}` | `{ok:true, applied, mode, points, eta_seconds, duration_override, profile_timing}` |
| `POST /fly` | `{waypoints:[[lon,lat],...], speed?:"slow"|"normal"|"fast", duration_seconds?:number}` | `{ok:true, applied, speed, points, eta_seconds, duration_override}` |
| `POST /trip` | `{legs:[{kind:"drive",coordinates,wait_seconds?}|{kind:"fly",waypoints,wait_seconds?}]}` | `{ok:true, applied, legs}`; waits must be 0-86,400 seconds |
| `POST /stop_move` | no body | `{ok:true}`; stop motion/trip and hold here |
| `POST /trip/leave_now` | no body | `{ok:true, skipped:boolean}`; skip an active between-leg dwell |
| `POST /stop` | no body | `{ok:true}`; clear simulation and return to real GPS |
| `POST /shutdown` | no body | `{ok:true}` in desktop mode only; otherwise 404 |

`GET /status` includes:

```jsonc
{
  "device": {
    "name": "iPhone or Android model", "ios_version": "...", "android_version": "...",
    "platform": "android", "product_type": "...",
    "trusted": true, "developer_mode": true,
    "connected": true, "link_lost": false
  },
  "pending_device": null,
  "devices": [{"key": "android:serial", "platform": "android", "name": "Samsung A15", "status": "ready", "active": true}],
  "active_device": "android:serial",
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
  "version": "1.0.5",
  "beta": false,
  "github_repo": "mayoka0/m-maps"
}
```

Top-level `platform` is `"ios"`, `"android"`, or `"auto"` while no phone is selected. Android
currently keeps the legacy `ios_version` field populated with a display string (`Android 16`) for
frontend compatibility. Device candidates also expose `status` values such as `ready` and
`unauthorized` so the UI can give setup guidance before an attach attempt.

Fields irrelevant to the current state can be `null` or absent. Device facts are cached at attach
time so the one-second UI poll never races the active tunnel through lockdownd.

The API validates movement work before resampling it. A single route or flight accepts at most
`MAX_PATH_INPUT_POINTS` input points and movement points, timing data is capped by
`MAX_TIMING_SECTIONS`, and a multi-stop request is capped by `MAX_TRIP_LEGS` and
`MAX_TRIP_INPUT_POINTS`. These limits protect local and optional LAN sessions from accidental or
abusive memory use.

## 5. File inventory

### Runtime and core code

- `m_maps.py` - minimal executable entry point into `mmaps.cli.main()`.
- `mmaps/__init__.py` - package metadata and beta-version helper.
- `mmaps/cli.py` - `detect`, `set`, `clear`, `serve`, and `app` commands; root checks, preflight,
  friendly errors, browser opening, and CLI hold-until-Enter behavior.
- `mmaps/device.py` - usbmux/lockdownd connection, trust/pairing cache handling, device facts, and
  Developer Mode guidance/enabling. Pairing paths account for `$SUDO_USER`.
- `mmaps/location.py` - tunnel/DVT lifecycle, keepalive assertion, reconnect classification, and
  standalone clear.
- `mmaps/session.py` - `SpoofSession`, movement/trip orchestration, dwell timers, assertion truth,
  and status snapshots.
- `mmaps/route.py` - distance, turn detection, optional smoothing, easing, and road resampling.
- `mmaps/flight.py` - great-circle interpolation, flight speeds/easing, and resampling.
- `mmaps/trip.py` - pure automatic drive-versus-fly leg choice (`FLY_MIN_STRAIGHT_KM = 1000`).
- `mmaps/airports.py` - loads and searches the offline airport dataset.
- `mmaps/data/airports.csv` - bundled OurAirports-derived medium/large scheduled-service data.
- `mmaps/errors.py` - converts pymobiledevice/device/network exception text to friendly messages.
- `mmaps/android_device.py` - ADB discovery, companion startup, private port forwarding, command
  acknowledgments, set/clear transport, and friendly Android setup failures.
- `mmaps/android_session.py` - Android target writer that reuses `SpoofSession` movement/trip
  scheduling and reports Android connection/application state.
- `mmaps/platforms.py` - platform-neutral device candidate records for unified discovery and
  selection; it performs no device I/O.
- `mmaps/server.py` - FastAPI app, device polling/confirmation, API validation, Nominatim proxy,
  session lifecycle, safe bind enforcement, and uvicorn runner.
- `mmaps/desktop.py` - normal-user pywebview process plus elevated local server helper launched by
  `osascript`.
- `android-companion/` - Gradle/Kotlin Android mock-location companion; source, manifest, wrapper,
  and setup documentation. `.gradle/`, `.idea/`, `local.properties`, and build output are ignored.

### Frontend

- `mmaps/web/index.html` - complete visual interface, inline CSS, feature state, search, Valhalla
  requests, trip planning, settings, status rendering, and API calls.
- `mmaps/web/js/mmaps-keys.js` - sole owner of Google API key test/session/persistence lifecycle.
- `mmaps/web/js/mmaps-map-provider.js` - loads and switches MapLibre/Google, owns Google map type,
  one-world bounds, and provider teardown/retry.
- `mmaps/web/js/mmaps-map-adapter.js` - one provider-neutral marker/polyline API mirrored to both
  map implementations.
- `mmaps/web/js/mmaps-chrome.js` - panel and segmented-control state with one visibility source of
  truth.

### Packaging, assets, and dependencies

- `requirements.txt` - runtime dependencies: pymobiledevice3, FastAPI, uvicorn, pywebview.
- `requirements-dev.txt` - runtime requirements plus pytest and coverage tooling.
- `build_app.py` - creates the self-contained PyInstaller `dist/M Maps.app`, including elevated
  server helper and generated bundle metadata/spec.
- `build_dmg.py` - creates a branded drag-to-Applications `dist/M Maps.dmg` with macOS tools.
- `packaging/desktop_entry.py` - frozen app entry point.
- `packaging/m_maps.spec` - generated/current PyInstaller build specification.
- `packaging/rthook_stub_ipython.py` - stubs pymobiledevice3's unused optional IPython dependency
  inside the frozen app.
- `scripts/make_app_icon.py` - regenerates icon assets.
- `assets/icon/AppIcon-1024.png`, `assets/icon/AppIcon.icns`, and
  `assets/icon/apple_official_icon_shape_mask.png` - source and shipping app icon assets.
- `assets/logo/README.md` and `assets/logo/mayoka-{black,blue,gold,gray,green,orange,purple,red,white}.{svg,png}`
  - Mayoka mark usage notes and color variants; variants are not an in-app theme feature.

### Tests and automation

- `tests/__init__.py` - test package marker.
- `tests/test_errors.py` - friendly error conversion.
- `tests/test_flight.py` - great-circle math, realistic timing, duration override, and easing.
- `tests/test_route.py` - route precision, timing, easing, and no-default-smoothing behavior.
- `tests/test_trip.py` - automatic drive/fly threshold logic.
- `tests/test_session_trip_wait.py` - dwell holding, countdown state, final-wait behavior, and
  Leave now.
- `tests/test_server_bind.py` - localhost/private-LAN bind safety.
- `tests/test_web_ui_contract.py` - static frontend contracts: one world, Teleport confirmation,
  unified command bar, collapsed explicit search, and stable wait selector.
- `tests/test_android_device.py` - ADB detection/setup states, port mapping, payloads, and required
  companion acknowledgments.
- `tests/test_android_session.py` - Android set/clear, shared movement, reconnect, and status.
- `tests/test_android_server.py` - browser HTTP → server → Android integration contract for
  Teleport, Drive, Fly, Stop, and Restore GPS.
- `.github/workflows/ci.yml` - pytest/coverage matrix on macOS, Linux, and Windows; phone/tunnel
  integration cannot run in CI.
- `.github/ISSUE_TEMPLATE/bug_report.yml`, `feature_request.yml`, and `config.yml` - issue forms.
- `.github/PULL_REQUEST_TEMPLATE.md` - PR checklist/template.

### Repository and human documentation

- `README.md` - public product overview, setup, usage, and ethics note.
- `CHANGELOG.md` - public releases and current unreleased beta notes. Some unreleased bullets may
  lag the newest working-tree fixes; update at a release checkpoint, not per tiny edit.
- `CONTRIBUTING.md` - contributor setup and branch/release conventions.
- `CODE_OF_CONDUCT.md` - contributor conduct policy.
- `SECURITY.md` - private vulnerability-reporting process and security scope.
- `LICENSE` - project license.
- `.gitignore` - generated files, venv, local secrets, and local agent notes.
- `AGENTS.md` - this canonical handoff.
- `CLAUDE.md` - ignored local Claude entry file that imports `AGENTS.md`; do not duplicate state.
- `BUGS.md` and `BUGS2.md` - ignored historical inspection reports. They describe older code and
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

# Legacy Android-only compatibility mode; no sudo:
venv/bin/python3 m_maps.py serve --android

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
5. In Route, place a start and choose every profile. Walk should use mapped footways/sidewalks and
   legal shortcuts; Bicycle should favor cycleways and avoid bicycle-forbidden highways. Car and
   Motorcycle should vary pace between local streets and faster roads. The dot must remain on the
   full planned polyline. Stop here should hold the current point.
6. Test Valhalla failure/no-route behavior and ensure the UI offers a calm retry/Teleport path.
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

12. With both an iPhone and Android phone connected before any session is active, verify the UI asks
    you to unplug one rather than guessing. Unplug one phone and confirm the remaining phone is
    detected automatically. If Android was holding a spoof before unplugging, reconnect it and
    restore GPS before testing the other phone.

The iPhone screen may be off while M Maps runs, as long as the phone remains powered, connected,
trusted, and accessible. The Mac and foreground M Maps process must stay awake/running; sleep or
process termination breaks active playback and the DVT connection.

### Real-Android manual regression flow

1. Build/install `android-companion` from Android Studio. Enable USB debugging, approve this Mac,
   and select M Maps Companion under Developer options → Mock location app.
2. Run `sudo venv/bin/python3 m_maps.py serve` for unified iPhone/Android mode. Confirm the device
   panel shows the Android model, system version, and Mock location on. The legacy
   `venv/bin/python3 m_maps.py serve --android` remains available for Android-only testing.
3. Test Teleport, each road speed, Fly, Multi-stop waits, Stop here, and Restore GPS.
4. Hold a static location for at least five minutes; it must not expire after about one minute.
5. Disconnect USB while holding. The phone should keep the last target. Reconnect and choose a new
   target; the server should recognize/recover in about one second without false success. After a
   sustained loss, status should expose reconnecting guidance and Restore GPS should ask for a
   reconnect instead of showing a raw ADB exception.
6. Force-stop the companion, then start Fly. The server should restart the companion and return
   `applied:true` only after a real phone acknowledgment.
7. Restore GPS and verify Google Maps returns to the real position.

## 7. Known issues and deliberate limitations

- Antimeridian overlay: a great-circle flight crossing ±180° can be drawn across the long side of
  a flat map. Device points still take the correct short route; this is cosmetic.
- Route geometry/sidewalk tags can differ among OpenStreetMap, Google, and Apple. The emitted point
  remains on Valhalla's OSM-based route, but another provider can visually place it a few metres beside its
  own road drawing.
- Public Valhalla and Nominatim services are rate-limited and appropriate only for light personal use.
  Distribution at scale needs a hosted/keyed routing/search decision.
- A Google key is user-supplied and stored in browser localStorage. M Maps does not validate
  billing/restriction setup beyond attempting to load Google Maps.
- Long or permanent USB loss can leave movement progression visually frozen while the engine
  retries. Fatal assertion state must never be represented as a successful phone move.
- Android holds its last location intentionally after USB disconnect. Restore GPS requires a
  reconnected ADB link, Force stop in Android app settings, or disabling the selected mock app.
- Android support has been tested on one Samsung A15/Android 16. Other manufacturers may apply
  different battery, foreground-service, or mock-location behavior and need contributor testing.
- Mac sleep interrupts timers, networking, and the USB tunnel. There is no daemon or wake lock.
- Server APIs permit choices the normal UI may not expose; there is no authentication on localhost
  or opt-in LAN mode. `--lan` trusts the private Wi-Fi network.
- Packaging scripts exist, but signing, notarization, universal Intel support, and a polished
  public distribution pipeline are not complete.
- The current UI has had a major command-bar/settings polish pass, but the planned destination
  card, richer active-journey card, deeper settings organization, responsive/mobile refinement,
  and full accessibility/keyboard pass remain future design slices.
- The local native-window redesign uses full-size transparent-titlebar content, restored macOS
  traffic-light controls, an unpainted drag region, and independently floating controls. Keep a
  manual Cocoa regression in the release checklist: drag, close/minimize/zoom, full screen, focus,
  and Dock/menu identity must still work after pywebview or macOS changes.

## 8. Not built / future research

- No Windows or Linux Android host release yet. Android currently uses the same macOS-hosted map
  UI and a separately installed companion APK.
- No simultaneous multi-phone control; the beta intentionally selects one active device at a time.
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
4. When a tested increment is ready, stop and ask: **“Is this good to commit and push to cross-platform-develop
   now?”** A prior approval does not carry forward to later work.
`develop` is the local working line. `main` remains the stable/public line. Current documentation
work must remain local unless MJ separately approves a commit and push.
