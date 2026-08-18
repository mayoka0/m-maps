"""Web server that puts a map in front of the hold engine.

Default bind is 127.0.0.1 (this Mac only). With explicit ``--lan``, binds to
this Mac's private LAN IP so other devices on the same Wi-Fi can open the
control page. Never binds to 0.0.0.0 / all interfaces.

The Mac still owns the USB tunnel and spoof session; LAN only exposes the
control webpage. No auth - home network trust only.

Endpoints (all tiny):
  GET  /         -> the map page
  GET  /status   -> device info + current spoof state (polled by the page)
  GET  /geocode  -> place search proxy (Nominatim; browser has no CORS there)
  GET  /autocomplete -> debounced search-as-you-type proxy (Photon)
  POST /device/confirm | /device/ignore  -> live plug-in confirmation
  POST /spoof    -> {lat, lon}: set/update the held location
  POST /stop     -> clear the spoof, restore real GPS
"""
import asyncio
import contextlib
import json
import logging
import math
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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pymobiledevice3 import usbmux

from mmaps import __version__, airports, device, flight, is_beta_version, route
from mmaps.android_device import AndroidController
from mmaps.android_session import AndroidSpoofSession
from mmaps.errors import humanize_error
from mmaps.platforms import DeviceCandidate, android_candidate, ios_candidate
from mmaps.session import SpoofSession

logger = logging.getLogger("mmaps.server")

HOST = "127.0.0.1"
PORT = 8765

_WEB_DIR = Path(__file__).parent / "web"

# Capabilities this server build supports. The page reads this from /status and
# only offers features the RUNNING server actually has - so if an old `serve`
# process is still up (it would serve the new HTML from disk but lack the new
# routes in memory), the UI says "restart the server" instead of 404-ing.
FEATURES = [
    "teleport", "drive", "fly", "geocode", "autocomplete",
    "airports", "trip", "device_confirm",
]

# Public GitHub repo used by the UI for optional “new version available” checks.
GITHUB_REPO = "mayoka0/m-maps"

# How often to scan usbmux for a phone that appeared after launch.
DEVICE_POLL_SECONDS = 2.0

# Normal router and flight paths are far smaller than this. The cap prevents
# malformed or hostile API payloads from allocating an enormous movement list.
MAX_PATH_INPUT_POINTS = 100_000
# A trip is one request that can contain many paths, so bound its aggregate
# work separately from the per-path limit above.
MAX_TRIP_LEGS = 100
MAX_TRIP_INPUT_POINTS = 100_000
MAX_TIMING_SECTIONS = 20_000
# A many-hour walk is not a useful walking simulation. Enforce this on the
# server too, so programmatic callers cannot bypass the UI rule.
MAX_WALK_ROAD_DISTANCE_M = 15_000.0

# Nominatim requires a descriptive User-Agent. Keep requests sparse (the UI only
# searches on explicit Search/Enter). Personal/local use only.
_NOMINATIM_UA = "M-Maps/1.0 (macOS local app; personal use; no bulk)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
_geocode_pool = ThreadPoolExecutor(max_workers=2)

# LAN-mode flag is set in run() before uvicorn starts. Surfaced via /status so
# the page can show a visible "anyone on this Wi-Fi can control this" warning.
_LAN_MODE = False
_DEVICE_PLATFORM = "auto"


class SpoofRequest(BaseModel):
    lat: float
    lon: float


class DeviceSelectRequest(BaseModel):
    """Stable discovery key for programmatic device selection compatibility."""

    platform: str
    identifier: str


class TimingSection(BaseModel):
    start_index: int
    end_index: int
    duration_seconds: float


class DriveRequest(BaseModel):
    # Route geometry as [[lon, lat], ...] (GeoJSON order).
    coordinates: List[List[float]]
    # Speed preset key (field still named ``mode`` for API stability):
    # walk | bicycle | motorcycle | car | fly - see route.MODE_SPEEDS_KMH.
    mode: str
    # Optional wall-clock override (seconds). Same path; only pacing changes.
    # None / omit → realistic speed from mode. Min 1 s, max 48 h.
    duration_seconds: Optional[float] = None
    # Optional profile-aware timing returned by the browser route planner. When present,
    # highway/local/turn pacing comes from the router instead of one fixed km/h.
    timing_sections: Optional[List[TimingSection]] = None


class FlyRequest(BaseModel):
    # Flight path as [[lon, lat], ...]: [current, destination] for click-and-fly.
    waypoints: List[List[float]]
    speed: str = flight.DEFAULT_SPEED  # one of flight.SPEED_PRESETS
    # Optional wall-clock override (seconds). Same great-circle; only pacing.
    duration_seconds: Optional[float] = None


class TripLeg(BaseModel):
    """One multi-stop leg - same payloads as /drive or /fly, pre-planned by the UI."""
    kind: str  # "drive" | "fly"
    coordinates: Optional[List[List[float]]] = None  # drive: [[lon,lat],...]
    waypoints: Optional[List[List[float]]] = None    # fly: [[lon,lat],...]
    timing_sections: Optional[List[TimingSection]] = None
    # Hold at this arrival before the next leg. The final stop already holds
    # indefinitely, so its value is accepted but has no effect.
    wait_seconds: float = 0.0


class TripRequest(BaseModel):
    legs: List[TripLeg]


def _norm_udid(udid: Optional[str]) -> str:
    return (udid or "").replace("-", "").strip().lower()


async def _first_usb_serial() -> Optional[str]:
    """usbmux-only scan - never opens lockdown (safe while a hold loop is alive)."""
    serials = await _usb_serials()
    return serials[0] if serials else None


async def _usb_serials() -> List[str]:
    """Return every USB iPhone serial without opening a lockdown session."""
    devices = await usbmux.list_devices()
    return [d.serial for d in devices if d.is_usb and d.serial]


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
        # Already running a live session for this phone - keep it (one writer).
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
            # Do not silently reboot for Developer Mode in the desktop app -
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
        app.state.device_message = None
        app.state.active_platform = "ios"
        app.state.active_device_key = f"ios:{app.state.confirmed_udid}"
        try:
            await session.start()  # one hold loop, target still None until first click
        except Exception:
            app.state.session = None
            with contextlib.suppress(Exception):
                await session.close()
            raise
        logger.info("Device session started (udid=%s).", app.state.confirmed_udid)
        return session


async def _refresh_android_candidates(app: FastAPI) -> List[DeviceCandidate]:
    """Discover Android phones without making Android mandatory for iPhone use."""
    controller = getattr(app.state, "android_controller", None)
    if controller is None:
        try:
            controller = AndroidController()
        except Exception as error:
            app.state.android_discovery_error = str(error)
            return []
        app.state.android_controller = controller

    try:
        devices = await asyncio.to_thread(controller.list_devices)
    except Exception as error:
        app.state.android_discovery_error = str(error)
        return []
    app.state.android_discovery_error = None
    return [android_candidate(item) for item in devices]


def _set_discovered_devices(app: FastAPI, candidates: List[DeviceCandidate]) -> None:
    """Store the latest cheap discovery result for /status and /devices."""
    app.state.discovered_devices = {candidate.key: candidate for candidate in candidates}


async def _close_current_session(app: FastAPI) -> None:
    """Stop and close the active session before switching platforms/devices."""
    async with _session_lock(app):
        session = app.state.session
        if session is None:
            return
        # Stop first so a connected phone returns to real GPS. If Android is
        # unplugged, this raises and the caller must not silently switch away.
        await session.stop()
        await session.close()
        app.state.session = None
        app.state.confirmed_udid = None
        app.state.active_platform = None
        app.state.active_device_key = None


async def _device_poll_once(app: FastAPI) -> None:
    """Refresh both platform inventories and attach only a safe default.

    iPhone discovery uses usbmux only; it never opens a second lockdown probe
    beside the hold loop. Android discovery uses ``adb devices`` and remains
    optional, so an iPhone-only Mac does not need Android Studio installed.
    """
    # Never race an in-progress attach.
    lock = _session_lock(app)
    if lock.locked():
        return

    ios_serials = [] if _DEVICE_PLATFORM == "android" else await _usb_serials()
    android_candidates = [] if _DEVICE_PLATFORM == "ios" else await _refresh_android_candidates(app)
    candidates = [ios_candidate(serial) for serial in ios_serials] + android_candidates
    _set_discovered_devices(app, candidates)
    if app.state.active_platform is None:
        # A previous two-phone warning is no longer relevant once the active
        # session has been cleared and the next scan is evaluating devices.
        app.state.device_message = None

    session = app.state.session
    if app.state.active_platform == "android" and session is not None:
        android_connected = any(item.status == "ready" for item in android_candidates)
        if android_connected:
            with contextlib.suppress(Exception):
                await session.refresh_connection()
            return
        # The Android companion keeps its last mock location after USB loss.
        # Do not silently abandon an active spoof while switching phones.
        if getattr(session, "_target", None) is not None:
            app.state.device_message = (
                "Android disconnected while holding a location. Reconnect it and "
                "restore GPS before switching phones."
            )
            with contextlib.suppress(Exception):
                await session.refresh_connection()
            return
        with contextlib.suppress(Exception):
            await session.close()
        app.state.session = None
        app.state.confirmed_udid = None
        app.state.active_platform = None
        app.state.active_device_key = None

    if app.state.active_platform == "ios" and session is not None:
        active_ios_present = any(
            _norm_udid(serial) == _norm_udid(app.state.confirmed_udid)
            for serial in ios_serials
        )
        replacement_connected = bool(
            ios_serials or any(item.status == "ready" for item in android_candidates)
        )
        if not active_ios_present and replacement_connected:
            # An unplugged iPhone cannot keep its DVT simulation alive, so it
            # is safe to retire the reconnecting session when another phone is
            # waiting. This makes unplug-iPhone/plug-Android work without a
            # manual device picker.
            with contextlib.suppress(Exception):
                await session.close()
            app.state.session = None
            app.state.confirmed_udid = None
            app.state.active_platform = None
            app.state.active_device_key = None
            session = None

    serial = ios_serials[0] if ios_serials else None
    if not serial:
        # Phone unplugged - clear a pending prompt; keep confirmed session so
        # the iPhone engine can reconnect when the cable returns. Android is
        # handled above because its phone-side service may keep holding after
        # USB loss.
        if app.state.pending_device is not None:
            app.state.pending_device = None

        # Auto-attach Android only when it is the sole platform available. If
        # an iPhone is also plugged in, the user should unplug one first.
        ready_android = [c for c in android_candidates if c.status == "ready"]
        if app.state.session is None and len(ready_android) == 1:
            await _attach_android_session(
                app,
                app.state.android_controller,
                ready_android[0].identifier,
            )
        return

    # With no active session, require one physical phone at a time. This keeps
    # device choice automatic instead of guessing when both are connected.
    if app.state.session is None and len(candidates) > 1:
        app.state.pending_device = None
        app.state.device_message = "More than one phone is connected. Unplug one phone to continue."
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

    # Active confirmed session for another identity - do not touch lockdown.
    if app.state.session is not None and app.state.active_platform == "ios" and confirmed_n:
        return

    # Already showing a confirm for this serial.
    pending = app.state.pending_device
    if pending and _norm_udid(pending.get("udid")) == serial_n:
        return

    # Brand-new phone for this session - usbmux-only pending (no lockdown probe).
    app.state.pending_device = {
        "udid": serial,
        "name": "iPhone",
        "ios_version": None,
        "product_type": None,
        "trusted": None,
        "developer_mode": None,
    }
    app.state.device_message = None
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


async def _attach_android_session(
    app: FastAPI,
    controller: AndroidController,
    serial: Optional[str] = None,
) -> AndroidSpoofSession:
    """Attach Android after startup or a later USB connection."""
    if serial:
        phone = await asyncio.to_thread(controller.connect, serial=serial)
    else:
        phone = await asyncio.to_thread(controller.connect)
    session = AndroidSpoofSession(controller, phone)
    await session.start()
    app.state.session = session
    app.state.confirmed_udid = phone.serial
    app.state.pending_device = None
    app.state.active_platform = "android"
    app.state.active_device_key = f"android:{phone.serial}"
    app.state.startup_error = None
    app.state.device_message = None
    logger.info("Android companion connected (%s).", phone.serial)
    return session


async def _android_poll_loop(app: FastAPI, controller: AndroidController) -> None:
    """Detect late USB connections and keep an Android session recovered."""
    while True:
        try:
            session = app.state.session
            if session is None:
                await _attach_android_session(app, controller)
            else:
                await session.refresh_connection()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if app.state.session is None:
                app.state.startup_error = str(error)
            logger.debug("Android poll error: %s", error)
        await asyncio.sleep(1.0)


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
    app.state.active_platform = None
    app.state.active_device_key = None
    app.state.discovered_devices = {}
    app.state.android_controller = None
    app.state.android_discovery_error = None
    app.state.device_message = None

    # Preserve the old explicit --android startup behavior while the default
    # auto mode waits for discovery and attaches a sole connected phone.
    if _DEVICE_PLATFORM == "android":
        try:
            app.state.android_controller = AndroidController()
            await _attach_android_session(app, app.state.android_controller)
        except Exception as error:
            app.state.startup_error = str(error)
            logger.warning("Android startup failed: %s", error)

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
            controller = app.state.android_controller
            if controller is not None and session is None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(controller.close)


app = FastAPI(title="M Maps", lifespan=_lifespan)
app.state.session = None
app.state.startup_error = None
app.state.desktop_mode = False
app.state.uvicorn_server = None
app.state.confirmed_udid = None
app.state.pending_device = None
app.state.ignored_udids = set()
app.state.session_lock = None
app.state.active_platform = None
app.state.active_device_key = None
app.state.discovered_devices = {}
app.state.android_controller = None
app.state.android_discovery_error = None
app.state.device_message = None


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
            or "No iPhone confirmed yet - plug it in over USB and accept the prompt.",
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

    ``socket.gethostbyname(socket.gethostname())`` is a fallback only - on
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


# Modular front-end scripts (keys / provider / chrome / adapter).
# Mount before "/" so /js/* is not swallowed by the SPA index.
_JS_DIR = _WEB_DIR / "js"
if _JS_DIR.is_dir():
    app.mount(
        "/js",
        StaticFiles(directory=str(_JS_DIR)),
        name="mmaps_js",
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # no-store so the browser never runs a cached older page against this server.
    return HTMLResponse(
        content=(_WEB_DIR / "index.html").read_text(),
        headers={"Cache-Control": "no-store"},
    )


def _device_list_payload(app: FastAPI, active_key: Optional[str] = None) -> List[dict]:
    """Serialize discovery records, marking the one active session."""
    records = dict(app.state.discovered_devices)
    if app.state.session is not None:
        key = active_key or app.state.active_device_key
        info = getattr(app.state.session, "_device_info", {})
        if key and key not in records:
            records[key] = DeviceCandidate(
                platform=app.state.active_platform or "ios",
                identifier=app.state.confirmed_udid or "",
                name=info.get("name") or ("Android" if app.state.active_platform == "android" else "iPhone"),
                version=info.get("android_version") or info.get("ios_version"),
                status="ready",
                trusted=info.get("trusted"),
                developer_mode=info.get("developer_mode"),
            )
    return [
        candidate.as_dict(active=candidate.key == (active_key or app.state.active_device_key))
        for candidate in records.values()
    ]


def _status_platform(app: FastAPI) -> str:
    """Expose a useful platform hint while auto mode has no active session."""
    if app.state.active_platform:
        return app.state.active_platform
    if len(app.state.discovered_devices) == 1:
        return next(iter(app.state.discovered_devices.values())).platform
    return _DEVICE_PLATFORM


def _apply_physical_presence(app: FastAPI, result: dict) -> None:
    """Keep cached iPhone facts from masquerading as a live USB connection.

    The iOS session remains in memory after a cable drop so its tunnel can
    reconnect automatically. usbmux discovery is the immediate source of
    truth for whether that phone is physically present.
    """
    if app.state.active_platform != "ios" or not app.state.active_device_key:
        return
    if app.state.active_device_key in app.state.discovered_devices:
        return

    cached = dict(result.get("device") or {})
    cached["connected"] = False
    cached["link_lost"] = True
    result["device"] = cached
    result["applied"] = False
    result["assert_live"] = False
    if result.get("state") != "error":
        result["state"] = "reconnecting"


@app.get("/devices")
async def devices() -> dict:
    """Return all discovered phones and the currently selected device."""
    return {
        "devices": _device_list_payload(app),
        "active": app.state.active_device_key,
        "platform": _status_platform(app),
    }


@app.get("/status")
async def status() -> dict:
    session = app.state.session
    pending = app.state.pending_device
    if session is None:
        if pending:
            state = "awaiting_confirm"
            err = None
            fatal = False
        elif app.state.device_message:
            state = "device_action_required"
            err = app.state.device_message
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
            "devices": _device_list_payload(app),
            "active_device": app.state.active_device_key,
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
            "platform": _status_platform(app),
        }
    result = await session.status()
    _apply_physical_presence(app, result)
    result["features"] = FEATURES
    result["lan"] = _LAN_MODE
    result["pending_device"] = None  # active session owns the phone
    result["devices"] = _device_list_payload(app)
    result["active_device"] = app.state.active_device_key
    result["spoofing"] = result.get("target") is not None
    result["version"] = __version__
    result["beta"] = is_beta_version(__version__)
    result["github_repo"] = GITHUB_REPO
    result["platform"] = _status_platform(app)
    return result


@app.post("/device/select")
async def device_select(request: DeviceSelectRequest) -> dict:
    """Select an iPhone or Android phone from the unified device list.

    iPhone selection still requires the existing explicit Trust/Developer Mode
    confirmation. Android can attach immediately when ADB reports it ready.
    """
    if request.platform not in {"ios", "android"}:
        raise HTTPException(status_code=400, detail="Platform must be ios or android.")
    key = f"{request.platform}:{request.identifier}"
    candidate = app.state.discovered_devices.get(key)
    if candidate is None:
        raise HTTPException(status_code=404, detail="That device is no longer connected.")

    if app.state.active_device_key == key and app.state.session is not None:
        return {"ok": True, "active": key, "device": candidate.as_dict(active=True)}

    if app.state.session is not None:
        try:
            await _close_current_session(app)
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The current device could not be restored. Reconnect it and "
                    "restore GPS before switching devices."
                ),
            ) from error

    if request.platform == "ios":
        if candidate.status not in {"awaiting_confirmation", "ready"}:
            raise HTTPException(status_code=409, detail="Unlock and trust the iPhone first.")
        app.state.pending_device = {
            "udid": request.identifier,
            "name": candidate.name,
            "ios_version": candidate.version,
            "product_type": None,
            "trusted": candidate.trusted,
            "developer_mode": candidate.developer_mode,
        }
        return {
            "ok": True,
            "requires_confirmation": True,
            "pending_device": app.state.pending_device,
        }

    if candidate.status != "ready":
        if candidate.status == "unauthorized":
            raise HTTPException(status_code=409, detail="Approve USB debugging on the Android phone first.")
        raise HTTPException(status_code=409, detail="The Android phone is not ready over USB.")
    controller = app.state.android_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Android platform-tools (adb) are not available.")
    try:
        session = await _attach_android_session(app, controller, request.identifier)
    except Exception as error:
        app.state.startup_error = str(error)
        raise HTTPException(status_code=503, detail=humanize_error(error)) from error
    info = await session.status()
    return {"ok": True, "active": app.state.active_device_key, "device": info.get("device")}


@app.post("/device/confirm")
async def device_confirm() -> dict:
    """User accepted the live-detected iPhone - start the spoof session."""
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
    """User declined the live-detected iPhone - leave it alone this session."""
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


def _photon_search(query: str, limit: int, lat: Optional[float], lon: Optional[float]) -> list:
    """Blocking Photon autocomplete call (run off the event loop)."""
    params = {"q": query, "limit": str(limit), "lang": "en"}
    if lat is not None and lon is not None:
        params.update({"lat": str(lat), "lon": str(lon), "zoom": "8"})
    req = urllib.request.Request(
        f"{_PHOTON_URL}?{urllib.parse.urlencode(params)}",
        headers={
            "User-Agent": "M-Maps/1.0 (local beta autocomplete; reasonable use)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = []
    for feature in data.get("features") or []:
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        properties = feature.get("properties") or {}
        if len(coordinates) < 2:
            continue
        try:
            lon_value = float(coordinates[0])
            lat_value = float(coordinates[1])
        except (TypeError, ValueError):
            continue
        parts = []
        for key in ("name", "street", "city", "county", "state", "country"):
            value = str(properties.get(key) or "").strip()
            if value and value not in parts:
                parts.append(value)
        results.append({
            "lat": lat_value,
            "lon": lon_value,
            "name": ", ".join(parts) or query,
            "type": str(properties.get("type") or properties.get("osm_value") or "").strip(),
            "class": str(properties.get("osm_key") or "").strip(),
        })
    return results


@app.get("/geocode")
async def geocode(q: str = Query("", min_length=0), limit: int = Query(6, ge=1, le=10)) -> dict:
    """Place search via Nominatim (proxied so the browser isn't blocked by CORS).

    Does not change the phone location - the UI only shows the result until the
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
            raise HTTPException(status_code=429, detail="Place search is rate-limited - wait a few seconds.") from e
        logger.warning("Nominatim HTTP error: %s", e)
        raise HTTPException(status_code=502, detail="Place search failed.") from e
    except Exception as e:
        logger.warning("Nominatim error: %s", e)
        raise HTTPException(status_code=502, detail="Couldn’t reach the place search service.") from e

    return {"results": results}


@app.get("/autocomplete")
async def autocomplete(
    q: str = Query("", min_length=0),
    limit: int = Query(6, ge=1, le=8),
    lat: Optional[float] = Query(None, ge=-90, le=90),
    lon: Optional[float] = Query(None, ge=-180, le=180),
) -> dict:
    """Search-as-you-type suggestions via Photon; never moves the phone."""
    query = (q or "").strip()
    if len(query) < 3:
        return {"results": []}
    if len(query) > 200:
        raise HTTPException(status_code=400, detail="Search query is too long.")

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(
            _geocode_pool, _photon_search, query, limit, lat, lon
        )
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise HTTPException(status_code=429, detail="Autocomplete is busy - pause and try again.") from error
        logger.warning("Photon HTTP error: %s", error)
        raise HTTPException(status_code=502, detail="Autocomplete is unavailable.") from error
    except Exception as error:
        logger.warning("Photon error: %s", error)
        raise HTTPException(status_code=502, detail="Couldn’t reach autocomplete.") from error
    return {"results": results}


@app.post("/spoof")
async def spoof(request: SpoofRequest) -> dict:
    if not math.isfinite(request.lat) or not math.isfinite(request.lon):
        raise HTTPException(status_code=400, detail="Coordinates must be finite numbers.")
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


def _validate_geo_points(
    points: Optional[List[List[float]]],
    *,
    label: str,
    minimum: int = 2,
) -> List[List[float]]:
    """Validate GeoJSON-style ``[[lon, lat], ...]`` input at the API edge."""
    if not points or len(points) < minimum:
        raise HTTPException(status_code=400, detail=f"{label} needs at least {minimum} points.")
    if len(points) > MAX_PATH_INPUT_POINTS:
        raise HTTPException(
            status_code=413,
            detail=f"{label} contains too many points (maximum {MAX_PATH_INPUT_POINTS}).",
        )
    for index, point in enumerate(points):
        if len(point) != 2:
            raise HTTPException(
                status_code=400,
                detail=f"{label} point {index + 1} must contain [longitude, latitude].",
            )
        lon, lat = point
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise HTTPException(
                status_code=400,
                detail=f"{label} point {index + 1} must contain finite coordinates.",
            )
        if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
            raise HTTPException(
                status_code=400,
                detail=f"{label} point {index + 1} is outside the valid coordinate range.",
            )
    return points


def _optional_duration_seconds(value: Optional[float]) -> Optional[float]:
    """Validate optional duration override; None means use realistic speed."""
    if value is None:
        return None
    try:
        sec = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="duration_seconds must be a number.") from exc
    if not math.isfinite(sec):
        raise HTTPException(status_code=400, detail="duration_seconds must be finite.")
    if sec <= 0:
        return None  # treat 0 / negative as "use default"
    # Cap absurd values (48 h) so a typo can't allocate millions of points.
    if sec > 48 * 3600:
        raise HTTPException(
            status_code=400,
            detail="duration_seconds max is 48 hours (172800).",
        )
    if sec < 1.0:
        sec = 1.0
    return sec


def _validate_movement_budget(
    estimated_seconds: float,
    tick_seconds: float,
    *,
    label: str,
) -> None:
    """Reject paths that would create too many timed points before sampling.

    The resamplers emit roughly one point per tick. Checking this estimate
    before calling them prevents a malformed duration or very long path from
    allocating a large list and only then discovering the output cap.
    """
    if not math.isfinite(estimated_seconds) or estimated_seconds <= 0:
        return
    estimated_points = max(1, math.ceil(estimated_seconds / tick_seconds)) + 1
    if estimated_points > MAX_PATH_INPUT_POINTS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{label} would produce too many movement points "
                f"(maximum {MAX_PATH_INPUT_POINTS})."
            ),
        )


def _timing_dicts(
    sections: Optional[List[TimingSection]],
    *,
    point_count: Optional[int] = None,
) -> list:
    """Validate and flatten router timing models for the pure route engine."""
    if not sections:
        return []
    if len(sections) > MAX_TIMING_SECTIONS:
        raise HTTPException(
            status_code=413,
            detail=f"Route timing contains too many sections (maximum {MAX_TIMING_SECTIONS}).",
        )
    result = []
    total = 0.0
    for section in sections:
        if section.start_index < 0 or section.end_index <= section.start_index:
            raise HTTPException(status_code=400, detail="Route timing contains invalid shape indexes.")
        if point_count is not None and section.end_index >= point_count:
            raise HTTPException(status_code=400, detail="Route timing does not match its geometry.")
        if not math.isfinite(section.duration_seconds) or section.duration_seconds <= 0:
            raise HTTPException(status_code=400, detail="Route timing must be positive.")
        total += section.duration_seconds
        result.append({
            "start_index": section.start_index,
            "end_index": section.end_index,
            "duration_seconds": section.duration_seconds,
        })
    if total > 48 * 3600:
        raise HTTPException(status_code=400, detail="Route timing exceeds 48 hours.")
    return result


@app.post("/drive")
async def drive(request: DriveRequest) -> dict:
    if request.mode == "fly":
        raise HTTPException(
            status_code=400,
            detail="Fly is not a road speed - use POST /fly for a straight great-circle flight.",
        )
    if request.mode not in route.MODE_SPEEDS_KMH:
        raise HTTPException(status_code=400, detail=f"Unknown mode '{request.mode}'.")
    coordinates = _validate_geo_points(request.coordinates, label="A route")
    road_distance = route.path_length_m([(point[1], point[0]) for point in coordinates])
    if request.mode == "walk" and road_distance > MAX_WALK_ROAD_DISTANCE_M:
        raise HTTPException(
            status_code=400,
            detail="Walk is available only for road routes up to 15 km. Choose Bicycle, Motorcycle, Car, or Fly.",
        )
    session = _require_session(app)
    speed_kmh = route.MODE_SPEEDS_KMH[request.mode]
    duration = _optional_duration_seconds(request.duration_seconds)
    timing = _timing_dicts(request.timing_sections, point_count=len(coordinates))
    if duration is not None:
        estimated_seconds = duration
    elif timing:
        estimated_seconds = sum(section["duration_seconds"] for section in timing)
    else:
        estimated_seconds = route.estimate_eta_seconds(
            road_distance,
            speed_kmh,
        )
    _validate_movement_budget(
        estimated_seconds,
        route.TICK_SECONDS,
        label="The route",
    )
    if timing:
        points = route.resample_by_timing(
            coordinates,
            timing,
            duration_seconds=duration,
        )
        if not points:
            raise HTTPException(status_code=400, detail="Route timing does not match its geometry.")
    else:
        points = route.resample_by_speed(
            coordinates, speed_kmh, duration_seconds=duration
        )
    if not points:
        raise HTTPException(status_code=400, detail="The route did not produce any movement points.")
    if len(points) > MAX_PATH_INPUT_POINTS:
        raise HTTPException(status_code=413, detail="The route produces too many movement points.")
    await session.start_movement(points, route.TICK_SECONDS, "drive")
    # applied = real DVT success, not merely "task exists".
    applied = await session.wait_for_applied(timeout=4.0)
    return {
        "ok": True,
        "applied": applied,
        "mode": request.mode,
        "points": len(points),
        "eta_seconds": round(len(points) * route.TICK_SECONDS),
        "duration_override": duration is not None,
        "profile_timing": bool(timing),
    }


@app.post("/fly")
async def fly(request: FlyRequest) -> dict:
    """Straight great-circle flight along waypoints (usually current → airport)."""
    if request.speed not in flight.SPEED_PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown speed '{request.speed}'.")
    waypoints_input = _validate_geo_points(request.waypoints, label="A flight")
    session = _require_session(app)
    # Snap destination to nearest airport (idempotent if UI already did).
    waypoints = _snap_fly_waypoints(waypoints_input)
    duration = _optional_duration_seconds(request.duration_seconds)
    if duration is not None:
        estimated_seconds = duration
    else:
        flight_distance = route.path_length_m(
            [(point[1], point[0]) for point in waypoints]
        )
        estimated_seconds = flight_distance / max(
            flight.effective_speed_kmh(request.speed) * 1000.0 / 3600.0,
            1e-6,
        )
    _validate_movement_budget(
        estimated_seconds,
        flight.TICK_SECONDS,
        label="The flight",
    )
    points = flight.resample_flight(
        waypoints, request.speed, duration_seconds=duration
    )
    if not points:
        raise HTTPException(status_code=400, detail="The flight did not produce any movement points.")
    if len(points) > MAX_PATH_INPUT_POINTS:
        raise HTTPException(status_code=413, detail="The flight produces too many movement points.")
    await session.start_movement(points, flight.TICK_SECONDS, "fly")
    # applied = real DVT success, not merely "task exists".
    applied = await session.wait_for_applied(timeout=4.0)
    return {
        "ok": True,
        "applied": applied,
        "speed": request.speed,
        "points": len(points),
        "eta_seconds": round(len(points) * flight.TICK_SECONDS),
        "duration_override": duration is not None,
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
        return list(waypoints)
    # Preserve explicit intermediate waypoints. Only the final landing point
    # is replaced by the nearest passenger airport.
    return [*waypoints[:-1], [ap["lon"], ap["lat"]]]


@app.post("/trip")
async def trip(request: TripRequest) -> dict:
    """Run a multi-stop chain: sequential drive/fly legs on one hold loop."""
    if not request.legs:
        raise HTTPException(status_code=400, detail="A trip needs at least one leg.")
    if len(request.legs) > MAX_TRIP_LEGS:
        raise HTTPException(
            status_code=413,
            detail=f"A trip contains too many legs (maximum {MAX_TRIP_LEGS}).",
        )
    legs = []
    total_input_points = 0
    for i, leg in enumerate(request.legs):
        if not math.isfinite(leg.wait_seconds):
            raise HTTPException(status_code=400, detail=f"Trip leg {i + 1}: wait must be finite.")
        if not 0 <= leg.wait_seconds <= 24 * 60 * 60:
            raise HTTPException(
                status_code=400,
                detail=f"Trip leg {i + 1}: wait must be between 0 and 24 hours.",
            )
        if leg.kind == "drive":
            coordinates = _validate_geo_points(
                leg.coordinates,
                label=f"Trip leg {i + 1} drive",
            )
            total_input_points += len(coordinates)
            if total_input_points > MAX_TRIP_INPUT_POINTS:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "A trip contains too many input points "
                        f"(maximum {MAX_TRIP_INPUT_POINTS})."
                    ),
                )
            timing = _timing_dicts(
                leg.timing_sections,
                point_count=len(coordinates),
            )
            if timing:
                estimated_seconds = sum(section["duration_seconds"] for section in timing)
            else:
                estimated_seconds = route.estimate_eta_seconds(
                    route.path_length_m(
                        [(point[1], point[0]) for point in coordinates]
                    ),
                    route.MODE_SPEEDS_KMH["car"],
                )
            _validate_movement_budget(
                estimated_seconds,
                route.TICK_SECONDS,
                label=f"Trip leg {i + 1} drive",
            )
            legs.append({
                "kind": "drive",
                "coordinates": coordinates,
                "timing_sections": timing,
                "wait_seconds": leg.wait_seconds,
            })
        elif leg.kind == "fly":
            waypoints = _validate_geo_points(
                leg.waypoints,
                label=f"Trip leg {i + 1} flight",
            )
            total_input_points += len(waypoints)
            if total_input_points > MAX_TRIP_INPUT_POINTS:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "A trip contains too many input points "
                        f"(maximum {MAX_TRIP_INPUT_POINTS})."
                    ),
                )
            snapped_waypoints = _snap_fly_waypoints(waypoints)
            estimated_seconds = route.path_length_m(
                [(point[1], point[0]) for point in snapped_waypoints]
            ) / max(
                flight.effective_speed_kmh(flight.DEFAULT_SPEED) * 1000.0 / 3600.0,
                1e-6,
            )
            _validate_movement_budget(
                estimated_seconds,
                flight.TICK_SECONDS,
                label=f"Trip leg {i + 1} flight",
            )
            # Always land at nearest airport - same rule as standalone Fly.
            legs.append({
                "kind": "fly",
                "waypoints": snapped_waypoints,
                "wait_seconds": leg.wait_seconds,
            })
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


@app.post("/trip/leave_now")
async def trip_leave_now() -> dict:
    """Skip the current between-stop wait without cancelling the trip."""
    session = _require_session(app)
    skipped = await session.leave_now()
    return {"ok": True, "skipped": skipped}


@app.post("/stop")
async def stop() -> dict:
    session = _require_session(app)
    try:
        await session.stop()
    except Exception as error:
        # Android can keep its last target alive after USB loss. Report the
        # session's recovery guidance instead of leaking an ADB traceback.
        status = await session.status()
        detail = status.get("error") or humanize_error(error)
        raise HTTPException(status_code=503, detail=detail) from error
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


def run(host: str = HOST, port: int = PORT, *, lan: bool = False,
        platform: str = "auto") -> None:
    """Run the server (blocking). uvicorn owns the event loop the engine uses.

    ``lan=True`` is the explicit opt-in from ``serve --lan``: bind to a
    specific private LAN IP (never 0.0.0.0) so phones on the same Wi-Fi can
    open the map. Default remains localhost-only.
    """
    import uvicorn

    global _LAN_MODE, _DEVICE_PLATFORM
    if platform not in ("auto", "ios", "android"):
        raise ValueError("platform must be 'auto', 'ios', or 'android'.")
    _assert_safe_bind(host, lan)
    _LAN_MODE = lan
    _DEVICE_PLATFORM = platform
    uvicorn.run(app, host=host, port=port, log_level="warning")
