"""Web server that puts a map in front of the hold engine.

Default bind is 127.0.0.1 (this Mac only). With explicit ``--lan``, binds to
this Mac's private LAN IP so other devices on the same Wi-Fi can open the
control page. Never binds to 0.0.0.0 / all interfaces.

The Mac still owns the USB tunnel and spoof session; LAN only exposes the
control webpage. No auth — home network trust only.

Endpoints (all tiny):
  GET  /         -> the map page
  GET  /status   -> device info + current spoof state (polled by the page)
  GET  /geocode  -> place search proxy (Nominatim; browser has no CORS there)
  POST /device/confirm | /device/ignore  -> live plug-in confirmation
  POST /spoof    -> {lat, lon}: set/update the held location
  POST /stop     -> clear the spoof, restore real GPS
"""
import asyncio
import contextlib
import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typing import List, Optional, Set

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from pymobiledevice3 import usbmux

from mmaps import __version__, airports, device, flight, is_beta_version, route
from mmaps.errors import humanize_error
from mmaps.session import SpoofSession

logger = logging.getLogger("mmaps.server")

HOST = "127.0.0.1"
PORT = 8765

_WEB_DIR = Path(__file__).parent / "web"

# Capabilities this server build supports. The page reads this from /status and
# only offers features the RUNNING server actually has — so if an old `serve`
# process is still up (it would serve the new HTML from disk but lack the new
# routes in memory), the UI says "restart the server" instead of 404-ing.
FEATURES = ["teleport", "drive", "fly", "geocode", "airports", "trip", "device_confirm"]

# Public GitHub repo used by the UI for optional “new version available” checks.
GITHUB_REPO = "mayoka0/m-maps"

# How often to scan usbmux for a phone that appeared after launch.
DEVICE_POLL_SECONDS = 2.0

# Nominatim requires a descriptive User-Agent. Keep requests sparse (the UI only
# searches on explicit Search/Enter). Personal/local use only.
_NOMINATIM_UA = "M-Maps/1.0 (macOS local app; personal use; no bulk)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_geocode_pool = ThreadPoolExecutor(max_workers=2)

# LAN-mode flag is set in run() before uvicorn starts. Surfaced via /status so
# the page can show a visible "anyone on this Wi-Fi can control this" warning.
_LAN_MODE = False


class SpoofRequest(BaseModel):
    lat: float
    lon: float


class DriveRequest(BaseModel):
    # Route geometry as [[lon, lat], ...] (GeoJSON order, straight from OSRM).
    coordinates: List[List[float]]
    # Speed preset key (field still named ``mode`` for API stability):
    # walk | bicycle | motorcycle | car | fly — see route.MODE_SPEEDS_KMH.
    mode: str


class FlyRequest(BaseModel):
    # Flight path as [[lon, lat], ...]: [current, destination] for click-and-fly.
    waypoints: List[List[float]]
    speed: str = flight.DEFAULT_SPEED  # one of flight.SPEED_PRESETS


class TripLeg(BaseModel):
    """One multi-stop leg — same payloads as /drive or /fly, pre-planned by the UI."""
    kind: str  # "drive" | "fly"
    coordinates: Optional[List[List[float]]] = None  # drive: [[lon,lat],...]
    waypoints: Optional[List[List[float]]] = None    # fly: [[lon,lat],...]


class TripRequest(BaseModel):
    legs: List[TripLeg]


def _norm_udid(udid: Optional[str]) -> str:
    return (udid or "").replace("-", "").strip().lower()


async def _first_usb_serial() -> Optional[str]:
    """usbmux-only scan — never opens lockdown (safe while a hold loop is alive)."""
    devices = await usbmux.list_devices()
    for d in devices:
        if d.is_usb:
            return d.serial
    return None


def _session_lock(app: FastAPI) -> asyncio.Lock:
    lock = getattr(app.state, "session_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.session_lock = lock
    return lock


async def _attach_confirmed_session(app: FastAPI, udid: Optional[str] = None) -> SpoofSession:
    """Open lockdown + SpoofSession for the (already user-confirmed) phone.

    Serialized: never starts a second hold loop beside an existing one. That
    was the root cause of the phone flipping between fixed locations.
    """
    async with _session_lock(app):
        # Already running a live session for this phone — keep it (one writer).
        existing = app.state.session
        if existing is not None and existing.engine_live:
            want = _norm_udid(udid or app.state.confirmed_udid)
            have = _norm_udid(app.state.confirmed_udid)
            if not want or want == have:
                app.state.pending_device = None
                app.state.startup_error = None
                return existing

        # Fully stop any previous hold loop before opening a new tunnel.
        old = app.state.session
        app.state.session = None
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()

        # Prefer the user-confirmed serial so two phones on one Mac attach correctly.
        serial = udid or app.state.confirmed_udid or await _first_usb_serial()
        try:
            # Headless-safe: autopair only if not yet trusted (Trust dialog on phone).
            client = await device.connect(autopair=False, serial=serial)
            client = await device.ensure_trusted(
                client, serial=serial, interactive=False
            )
            status = await device.get_status(client)
            # Do not silently reboot for Developer Mode in the desktop app —
            # surface clear steps instead (CLI `serve` can still prompt).
            if status.get("developer_mode") is False:
                with contextlib.suppress(Exception):
                    await client.close()
                raise RuntimeError(device.DEVELOPER_MODE_OFF_MESSAGE)
        except Exception:
            # Leave no half-open session on attach failure.
            raise

        session = SpoofSession(client)
        # Register session BEFORE start so a concurrent poll sees it and bails.
        app.state.session = session
        app.state.confirmed_udid = (
            serial or getattr(client, "udid", None) or await _first_usb_serial()
        )
        app.state.pending_device = None
        app.state.startup_error = None
        try:
            await session.start()  # one hold loop, target still None until first click
        except Exception:
            app.state.session = None
            with contextlib.suppress(Exception):
                await session.close()
            raise
        logger.info("Device session started (udid=%s).", app.state.confirmed_udid)
        return session


async def _device_poll_once(app: FastAPI) -> None:
    """Scan USB via usbmux only; never open lockdown while a session may hold.

    Opening a second lockdown connection for "probe" while the hold loop uses
    the primary connection was a concurrency hazard (same class of bug as the
    old per-poll Developer Mode read). Detection uses usbmux list only; the
    friendly name is filled in on confirm when we open the real session.
    """
    # Never race an in-progress attach.
    lock = _session_lock(app)
    if lock.locked():
        return

    serial = await _first_usb_serial()
    if not serial:
        # Phone unplugged — clear a pending prompt; keep confirmed session so
        # hold_location can reconnect when the cable returns.
        if app.state.pending_device is not None:
            app.state.pending_device = None
        return

    serial_n = _norm_udid(serial)
    confirmed_n = _norm_udid(app.state.confirmed_udid)
    ignored = {_norm_udid(u) for u in app.state.ignored_udids}

    # Already confirmed this phone.
    if confirmed_n and serial_n == confirmed_n:
        session = app.state.session
        # Also reattach when the session object exists but its hold loop is
        # dead (ERROR). Reconnecting-with-live-task is left alone.
        needs_reattach = session is None or (
            not session.engine_live and getattr(session, "_state", None) == session.ERROR
        )
        if needs_reattach:
            try:
                await _attach_confirmed_session(app, app.state.confirmed_udid or serial)
            except Exception as error:
                app.state.startup_error = humanize_error(error)
                logger.warning("Silent reattach failed: %s", error)
        return

    # User already said "ignore" for this phone this session.
    if serial_n in ignored:
        return

    # Active confirmed session for another identity — do not touch lockdown.
    if app.state.session is not None and confirmed_n:
        return

    # Already showing a confirm for this serial.
    pending = app.state.pending_device
    if pending and _norm_udid(pending.get("udid")) == serial_n:
        return

    # Brand-new phone for this session — usbmux-only pending (no lockdown probe).
    app.state.pending_device = {
        "udid": serial,
        "name": "iPhone",
        "ios_version": None,
        "product_type": None,
        "trusted": None,
        "developer_mode": None,
    }
    logger.info("Pending device confirmation (serial=%s).", serial)


async def _device_poll_loop(app: FastAPI) -> None:
    while True:
        try:
            await _device_poll_once(app)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.debug("Device poll error: %s", error)
        await asyncio.sleep(DEVICE_POLL_SECONDS)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start without requiring a phone; poll USB and attach after user confirms.

    Device sockets still live on uvicorn's loop (session is created here, not
    on a throwaway loop). Confirmed-device reconnect stays silent.
    """
    app.state.session = None
    app.state.startup_error = None
    app.state.confirmed_udid = None
    app.state.pending_device = None
    app.state.ignored_udids = set()  # type: Set[str]
    app.state.session_lock = asyncio.Lock()
    poll_task = asyncio.create_task(_device_poll_loop(app), name="mmaps-device-poll")
    # One immediate scan so a phone already plugged in shows the confirm promptly.
    with contextlib.suppress(Exception):
        await _device_poll_once(app)
    try:
        yield
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await poll_task
        async with _session_lock(app):
            session = app.state.session
            app.state.session = None
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
                logger.info("Session closed; real GPS restored.")


app = FastAPI(title="M Maps", lifespan=_lifespan)
app.state.session = None
app.state.startup_error = None
app.state.desktop_mode = False
app.state.uvicorn_server = None
app.state.confirmed_udid = None
app.state.pending_device = None
app.state.ignored_udids = set()
app.state.session_lock = None


def _require_session(app: FastAPI) -> SpoofSession:
    session = app.state.session
    if session is None:
        if app.state.pending_device:
            name = app.state.pending_device.get("name") or "iPhone"
            raise HTTPException(
                status_code=503,
                detail=f"Confirm the connected device ({name}) before spoofing.",
            )
        raise HTTPException(
            status_code=503,
            detail=app.state.startup_error
            or "No iPhone confirmed yet — plug it in over USB and accept the prompt.",
        )
    return session


def is_private_lan_ip(host: str) -> bool:
    """True for RFC 1918 private IPv4 addresses (home/office LAN).

    Loopback and 0.0.0.0 are never private LAN binds we want for --lan.
    """
    if not host or host in ("0.0.0.0", "127.0.0.1", "localhost"):
        return False
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in nums):
        return False
    a, b = nums[0], nums[1]
    if a == 10:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def detect_lan_ip() -> str:
    """Return this Mac's primary private LAN IPv4 address.

    Preferred method: open a UDP socket toward a public address (no data is
    sent) and read the local address the kernel chose for that route. That
    reliably picks the Wi-Fi/Ethernet interface in use.

    ``socket.gethostbyname(socket.gethostname())`` is a fallback only — on
    many Macs it returns 127.0.0.1 and is not useful.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Destination never receives packets; connect() just selects a route.
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if is_private_lan_ip(ip):
                return ip
    except OSError:
        pass

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if is_private_lan_ip(ip):
            return ip
    except OSError:
        pass

    raise RuntimeError(
        "Could not detect a private LAN IP. Connect this Mac to Wi-Fi (or "
        "Ethernet) and try again."
    )


def _assert_safe_bind(host: str, lan: bool) -> None:
    """Refuse binds that would expose the control page beyond home LAN intent.

    - Never 0.0.0.0 / all-interfaces (or IPv6 equivalents).
    - Without --lan: localhost only.
    - With --lan: a specific private LAN IP only (the one detect_lan_ip found).
    """
    if host in ("0.0.0.0", "::", "[::]"):
        raise ValueError(
            "M Maps refuses to bind to all interfaces (0.0.0.0). "
            "Use --lan to bind to this Mac's private LAN IP only."
        )
    if lan:
        if not is_private_lan_ip(host):
            raise ValueError(
                f"LAN mode must bind to a private LAN IP, not {host!r}."
            )
        return
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError(
            "Without --lan, the M Maps server must bind to localhost only."
        )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # no-store so the browser never runs a cached older page against this server.
    return HTMLResponse(
        content=(_WEB_DIR / "index.html").read_text(),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/status")
async def status() -> dict:
    session = app.state.session
    pending = app.state.pending_device
    if session is None:
        if pending:
            state = "awaiting_confirm"
            err = None
            fatal = False
        elif app.state.startup_error:
            state = "error"
            err = app.state.startup_error
            fatal = True
        else:
            state = "waiting_for_device"
            err = None
            fatal = False
        return {
            "device": None,
            "pending_device": pending,
            "state": state,
            "target": None,
            "error": err,
            "error_fatal": fatal,
            "engine_live": False,
            "spoofing": False,
            "features": FEATURES,
            "lan": _LAN_MODE,
            "version": __version__,
            "beta": is_beta_version(__version__),
            "github_repo": GITHUB_REPO,
        }
    result = await session.status()
    result["features"] = FEATURES
    result["lan"] = _LAN_MODE
    result["pending_device"] = None  # active session owns the phone
    result["spoofing"] = result.get("target") is not None
    result["version"] = __version__
    result["beta"] = is_beta_version(__version__)
    result["github_repo"] = GITHUB_REPO
    return result


@app.post("/device/confirm")
async def device_confirm() -> dict:
    """User accepted the live-detected iPhone — start the spoof session."""
    pending = app.state.pending_device
    if not pending and app.state.session is not None and app.state.session.engine_live:
        # Already attached (e.g. double-click confirm).
        info = await app.state.session.status()
        return {"ok": True, "device": info.get("device"), "udid": app.state.confirmed_udid}
    if not pending:
        raise HTTPException(status_code=400, detail="No device is waiting for confirmation.")
    try:
        session = await _attach_confirmed_session(app, pending.get("udid"))
    except Exception as error:
        app.state.startup_error = humanize_error(error)
        raise HTTPException(status_code=503, detail=app.state.startup_error) from error
    # Refresh friendly name now that we have a real lockdown connection.
    info = await session.status()
    return {"ok": True, "device": info.get("device"), "udid": app.state.confirmed_udid}


@app.post("/device/ignore")
async def device_ignore() -> dict:
    """User declined the live-detected iPhone — leave it alone this session."""
    pending = app.state.pending_device
    if pending and pending.get("udid"):
        app.state.ignored_udids.add(pending["udid"])
    app.state.pending_device = None
    return {"ok": True}


def _nominatim_search(query: str, limit: int) -> list:
    """Blocking Nominatim call (run off the event loop)."""
    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "addressdetails": "0",
        "limit": str(limit),
    })
    req = urllib.request.Request(
        f"{_NOMINATIM_URL}?{params}",
        headers={
            "User-Agent": _NOMINATIM_UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    results = []
    for row in data:
        try:
            lat = float(row["lat"])
            lon = float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        name = row.get("display_name") or query
        # Extra fields help the UI distinguish near-duplicate place names.
        results.append({
            "lat": lat,
            "lon": lon,
            "name": name,
            "type": (row.get("type") or row.get("addresstype") or "").strip(),
            "class": (row.get("class") or "").strip(),
        })
    return results


@app.get("/geocode")
async def geocode(q: str = Query("", min_length=0), limit: int = Query(6, ge=1, le=10)) -> dict:
    """Place search via Nominatim (proxied so the browser isn't blocked by CORS).

    Does not change the phone location — the UI only shows the result until the
    user clicks Move here. Coordinates are parsed client-side and never hit this.
    """
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Type a place name to search.")
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="Search query is too long.")

    import asyncio

    loop = asyncio.get_event_loop()
    try:
        results = await loop.run_in_executor(_geocode_pool, _nominatim_search, query, limit)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise HTTPException(status_code=429, detail="Place search is rate-limited — wait a few seconds.") from e
        logger.warning("Nominatim HTTP error: %s", e)
        raise HTTPException(status_code=502, detail="Place search failed.") from e
    except Exception as e:
        logger.warning("Nominatim error: %s", e)
        raise HTTPException(status_code=502, detail="Couldn’t reach the place search service.") from e

    return {"results": results}


@app.post("/spoof")
async def spoof(request: SpoofRequest) -> dict:
    if not (-90.0 <= request.lat <= 90.0):
        raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90.")
    if not (-180.0 <= request.lon <= 180.0):
        raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180.")
    session = _require_session(app)
    # Teleporting overrides any movement in progress.
    await session.stop_movement()
    await session.set_target(request.lat, request.lon)
    # applied = real DVT success, not merely "task exists".
    applied = await session.wait_for_applied(timeout=4.0)
    return {
        "ok": True,
        "applied": applied,
        "target": {"lat": request.lat, "lon": request.lon},
    }


@app.get("/nearest_airport")
async def nearest_airport(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict:
    """Nearest real passenger airport to a point (offline OurAirports extract).

    Used when speed is Fly: the plane lands at this airport, not the exact
    clicked/searched pin. Returns ``airport: null`` only if the dataset is empty.
    """
    try:
        ap = airports.nearest_airport(lat, lon)
    except Exception as e:
        logger.warning("Airport lookup failed: %s", e)
        raise HTTPException(status_code=500, detail="Airport dataset unavailable.") from e
    if ap is None:
        return {"airport": None}
    return {
        "airport": {
            "name": ap["name"],
            "iata": ap.get("iata") or "",
            "icao": ap.get("icao") or "",
            "lat": ap["lat"],
            "lon": ap["lon"],
            "municipality": ap.get("municipality") or "",
            "country": ap.get("country") or "",
            "type": ap.get("type") or "",
            "label": ap.get("label") or ap["name"],
            "distance_m": ap.get("distance_m"),
        }
    }


@app.post("/drive")
async def drive(request: DriveRequest) -> dict:
    if request.mode == "fly":
        raise HTTPException(
            status_code=400,
            detail="Fly is not a road speed — use POST /fly for a straight great-circle flight.",
        )
    if request.mode not in route.MODE_SPEEDS_KMH:
        raise HTTPException(status_code=400, detail=f"Unknown mode '{request.mode}'.")
    if len(request.coordinates) < 2:
        raise HTTPException(status_code=400, detail="A route needs at least two points.")
    session = _require_session(app)
    speed_kmh = route.MODE_SPEEDS_KMH[request.mode]
    points = route.resample_by_speed(request.coordinates, speed_kmh)
    await session.start_movement(points, route.TICK_SECONDS, "drive")
    # applied = real DVT success, not merely "task exists".
    applied = await session.wait_for_applied(timeout=4.0)
    return {
        "ok": True,
        "applied": applied,
        "mode": request.mode,
        "points": len(points),
        "eta_seconds": round(len(points) * route.TICK_SECONDS),
    }


@app.post("/fly")
async def fly(request: FlyRequest) -> dict:
    """Straight great-circle flight along waypoints (usually current → airport)."""
    if request.speed not in flight.SPEED_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown speed '{request.speed}'.")
    if len(request.waypoints) < 2:
        raise HTTPException(status_code=400, detail="A flight needs at least two points.")
    session = _require_session(app)
    # Snap destination to nearest airport (idempotent if UI already did).
    waypoints = _snap_fly_waypoints(request.waypoints)
    points = flight.resample_flight(waypoints, request.speed)
    await session.start_movement(points, flight.TICK_SECONDS, "fly")
    # applied = real DVT success, not merely "task exists".
    applied = await session.wait_for_applied(timeout=4.0)
    return {
        "ok": True,
        "applied": applied,
        "speed": request.speed,
        "points": len(points),
        "eta_seconds": round(len(points) * flight.TICK_SECONDS),
    }


def _snap_fly_waypoints(waypoints: List[List[float]]) -> List[List[float]]:
    """Force fly leg destination to the nearest real airport (same as standalone Fly).

    ``waypoints`` are ``[[lon, lat], ...]``; the last point is the intended dest
    and is replaced by the nearest passenger airport when one is found.
    """
    if len(waypoints) < 2:
        return waypoints
    start = waypoints[0]
    end = waypoints[-1]
    try:
        dest_lon, dest_lat = float(end[0]), float(end[1])
        ap = airports.nearest_airport(dest_lat, dest_lon)
    except Exception as error:
        logger.warning("Airport snap failed: %s", error)
        ap = None
    if not ap:
        return [start, end]
    return [start, [ap["lon"], ap["lat"]]]


@app.post("/trip")
async def trip(request: TripRequest) -> dict:
    """Run a multi-stop chain: sequential drive/fly legs on one hold loop."""
    if not request.legs:
        raise HTTPException(status_code=400, detail="A trip needs at least one leg.")
    legs = []
    for i, leg in enumerate(request.legs):
        if leg.kind == "drive":
            if not leg.coordinates or len(leg.coordinates) < 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"Trip leg {i + 1}: drive needs at least two coordinates.",
                )
            legs.append({"kind": "drive", "coordinates": leg.coordinates})
        elif leg.kind == "fly":
            if not leg.waypoints or len(leg.waypoints) < 2:
                raise HTTPException(
                    status_code=400,
                    detail=f"Trip leg {i + 1}: fly needs at least two waypoints.",
                )
            # Always land at nearest airport — same rule as standalone Fly.
            legs.append({"kind": "fly", "waypoints": _snap_fly_waypoints(leg.waypoints)})
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Trip leg {i + 1}: kind must be 'drive' or 'fly'.",
            )
    session = _require_session(app)
    await session.start_trip(legs)
    # applied = real DVT success, not merely "task exists".
    applied = await session.wait_for_applied(timeout=4.0)
    return {
        "ok": True,
        "applied": applied,
        "legs": len(legs),
    }


@app.post("/stop_move")
async def stop_move() -> dict:
    """Stop moving (drive, fly, or multi-stop trip) but keep holding the current point."""
    session = _require_session(app)
    await session.stop_movement()
    return {"ok": True}


@app.post("/stop")
async def stop() -> dict:
    session = _require_session(app)
    await session.stop()
    return {"ok": True}


@app.post("/shutdown")
async def shutdown() -> dict:
    """Desktop app only: ask the embedded server process to exit.

    Used by the pywebview front-end process (running as the normal user) to
    stop the elevated server when the window closes. Refused unless the
    desktop entry set ``app.state.desktop_mode``. Localhost-only bind already
    limits who can call this.

    If lifespan teardown hangs (device close), force-exit after a short delay so
    the elevated process never becomes a permanent zombie.
    """
    if not getattr(app.state, "desktop_mode", False):
        raise HTTPException(status_code=404, detail="Not found.")
    server = getattr(app.state, "uvicorn_server", None)
    if server is not None:
        server.should_exit = True

    def _force_exit() -> None:
        # Last resort: process tree should not outlive the desktop window.
        os._exit(0)

    try:
        loop = asyncio.get_running_loop()
        loop.call_later(4.0, _force_exit)
    except RuntimeError:
        pass
    return {"ok": True}


def run(host: str = HOST, port: int = PORT, *, lan: bool = False) -> None:
    """Run the server (blocking). uvicorn owns the event loop the engine uses.

    ``lan=True`` is the explicit opt-in from ``serve --lan``: bind to a
    specific private LAN IP (never 0.0.0.0) so phones on the same Wi-Fi can
    open the map. Default remains localhost-only.
    """
    import uvicorn

    global _LAN_MODE
    _assert_safe_bind(host, lan)
    _LAN_MODE = lan
    uvicorn.run(app, host=host, port=port, log_level="warning")
