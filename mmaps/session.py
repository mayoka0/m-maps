"""A long-lived spoof session for the map server.

This is the thin stateful layer between the HTTP API and the hold engine. It
owns exactly one tunnel + one `hold_location` loop for the whole session
(started eagerly so the tunnel is up before the first click), plus a single
mutable "current target". The API just calls `set_target()` / `stop()`; the
already-running loop reads the target on its next tick (or immediately, via
the wake event) — no tunnel restart per click.

Drive and fly reuse this exactly: a background task walks a resampled path and
calls `set_target()` with the next point every tick. One tunnel, one hold loop
— the target just moves on its own.
"""
import asyncio
import contextlib
import time
from typing import Any, Dict, List, Optional, Tuple

from mmaps import device, flight, location, route
from mmaps.errors import humanize_error

# Continuous RECONNECTING longer than this escalates the UI message.
RECONNECT_ESCALATE_SECONDS = 20.0


class SpoofSession:
    # High-level states surfaced to the UI via /status.
    IDLE = "idle"                 # loop not started yet
    CONNECTING = "connecting"     # opening the tunnel
    READY = "ready"              # tunnel up, no location asserted yet
    HOLDING = "holding"          # actively holding a spoofed point
    RECONNECTING = "reconnecting"  # tunnel dropped, rebuilding
    STOPPED = "stopped"          # cleared, loop ended
    ERROR = "error"              # loop died unexpectedly

    def __init__(self, lockdown_client):
        self._client = lockdown_client
        self._target: Optional[Tuple[float, float]] = None
        # Device facts (name/iOS/trust/Developer Mode), read once and cached.
        # These don't change during a serve session, and reading Developer Mode
        # hits lockdownd — which the hold loop also uses to build the tunnel, on
        # the SAME connection. Polling it every second could interleave with a
        # reconnect and corrupt that connection, so we snapshot it up front.
        self._device_info: Optional[dict] = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._state = self.IDLE
        self._error: Optional[str] = None
        # True only after a successful DVT location_simulation.set() for the
        # current connection. Cleared on connection_lost. Used for API
        # ``applied`` — not engine_live (task existence alone is not enough).
        self._assert_live = False
        # monotonic timestamp when the current reconnect streak began, or None.
        self._reconnect_since: Optional[float] = None
        # Sticky: once a long reconnect proves the link is dead, keep the chip
        # looking disconnected even after Stop.
        self._link_lost_sticky = False
        # Movement (drive / fly): a task that walks the target along a
        # precomputed path of points. Same machinery for both; only the path
        # source differs (OSRM route vs. great-circle flight).
        self._move_task: Optional[asyncio.Task] = None
        self._move_kind: Optional[str] = None   # "drive" | "fly"
        self._move_total = 0
        self._move_index = 0
        # Multi-stop trip: orchestrates sequential drive/fly legs.
        self._trip_task: Optional[asyncio.Task] = None
        self._trip_leg: int = 0       # 1-based current leg (0 = not on a trip)
        self._trip_legs: int = 0      # total legs

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the hold loop with no target yet (tunnel comes up idle).

        Guarantees at most ONE hold task: never starts a second loop alongside
        a running one (duplicate writers flip the phone between locations).
        """
        if self._task is not None and not self._task.done():
            return
        # Drop a finished task handle so we don't confuse later checks.
        self._task = None
        # Snapshot device info while lockdownd is idle, before the hold loop
        # starts using that connection for the tunnel (see __init__).
        if self._device_info is None:
            self._device_info = await device.get_status(self._client)
        # Fresh events for this generation (don't reuse a previously-set stop).
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._error = None
        self._assert_live = False
        self._reconnect_since = None
        self._state = self.CONNECTING
        self._task = asyncio.create_task(self._run(), name="mmaps-hold")

    async def _run(self) -> None:
        try:
            await location.hold_location(
                self._client,
                self._coordinate_source,
                self._stop_event,
                on_event=self._on_event,
                wake_event=self._wake_event,
            )
            # Clean exit (stop requested) — leave state as set by stop().
            if self._state != self.STOPPED:
                self._state = self.STOPPED
                self._error = None
        except asyncio.CancelledError:
            self._state = self.STOPPED
            self._error = None
            raise
        except location.RootRequiredError as error:
            self._assert_live = False
            self._link_lost_sticky = True
            self._state = self.ERROR
            self._error = humanize_error(error)
        except Exception as error:  # keep the server alive; report via /status
            self._assert_live = False
            self._link_lost_sticky = True
            self._state = self.ERROR
            self._error = humanize_error(error)

    async def set_target(self, latitude: float, longitude: float) -> None:
        """Point the phone at (latitude, longitude), starting the loop if needed."""
        self._target = (latitude, longitude)
        if self._task is None or self._task.done():
            await self.start()
        else:
            # Nudge the running loop to re-assert immediately.
            self._wake_event.set()

    async def stop(self) -> None:
        """Clear the spoof and stop the loop, restoring the phone's real GPS."""
        await self.stop_movement()
        self._target = None
        task = self._task
        if task is not None and not task.done():
            self._stop_event.set()
            self._wake_event.set()  # break any wait so it notices the stop now
            try:
                await asyncio.wait_for(task, timeout=20.0)
            except asyncio.TimeoutError:
                # Tunnel rebuild can hang; force-cancel so we never leave a
                # second writer alive when a new session starts.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._assert_live = False
        self._reconnect_since = None
        self._state = self.STOPPED
        self._error = None  # never leave a stale error after a clean stop

    async def close(self) -> None:
        """Stop the loop (if running) and close the device connection."""
        try:
            await self.stop()
        finally:
            with contextlib.suppress(Exception):
                await self._client.close()

    # -- movement: drive and fly -------------------------------------------

    async def start_movement(self, points: List[Tuple[float, float]], tick_seconds: float,
                             kind: str) -> None:
        """Walk the target along ``points`` (lat, lon), one per ``tick_seconds``.

        ``kind`` is just a label for the UI ("drive" or "fly"). Cancels any
        movement already in progress (including a multi-stop trip). The hold
        loop/tunnel is reused: moving is just repeated ``set_target`` calls.
        When the points run out the target stays on the last one.
        """
        await self.stop_movement()
        await self._begin_leg(points, tick_seconds, kind)

    async def _begin_leg(self, points: List[Tuple[float, float]], tick_seconds: float,
                         kind: str) -> None:
        """Start one leg without cancelling an outer multi-stop trip task."""
        # Cancel only the current leg walker, not the trip orchestrator.
        task = self._move_task
        self._move_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if not points:
            return
        self._move_kind = kind
        self._move_total = len(points)
        self._move_index = 0
        self._move_task = asyncio.create_task(self._move(points, tick_seconds))

    async def stop_movement(self) -> None:
        """Stop moving (and any multi-stop trip) but keep holding the current point."""
        await self._cancel_trip()
        task = self._move_task
        self._move_task = None
        self._move_kind = None
        self._move_total = 0
        self._move_index = 0
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _cancel_trip(self) -> None:
        task = self._trip_task
        self._trip_task = None
        self._trip_leg = 0
        self._trip_legs = 0
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def start_trip(self, legs: List[Dict[str, Any]]) -> None:
        """Run pre-planned drive/fly legs in order (multi-stop).

        Each leg is ``{"kind": "drive"|"fly", "coordinates"|"waypoints": ...}``
        in the same shape as POST /drive and POST /fly. One tunnel, one hold
        loop — only the movement task is swapped per leg.
        """
        await self.stop_movement()
        if not legs:
            return
        self._trip_legs = len(legs)
        self._trip_leg = 0
        self._trip_task = asyncio.create_task(self._run_trip(legs))

    async def _run_trip(self, legs: List[Dict[str, Any]]) -> None:
        try:
            for i, leg in enumerate(legs, start=1):
                self._trip_leg = i
                kind = leg.get("kind")
                if kind == "drive":
                    coords = leg.get("coordinates") or []
                    if len(coords) < 2:
                        continue
                    points = route.resample_by_speed(coords, route.MODE_SPEEDS_KMH["car"])
                    tick = route.TICK_SECONDS
                elif kind == "fly":
                    waypoints = leg.get("waypoints") or []
                    if len(waypoints) < 2:
                        continue
                    points = flight.resample_flight(waypoints, flight.DEFAULT_SPEED)
                    tick = flight.TICK_SECONDS
                else:
                    continue
                if not points:
                    continue
                await self._begin_leg(points, tick, kind)
                move = self._move_task
                if move is not None:
                    await move
        except asyncio.CancelledError:
            raise
        finally:
            # Trip finished or cancelled — clear trip fields; hold stays on last point.
            if self._trip_task is asyncio.current_task():
                self._trip_task = None
                self._trip_leg = 0
                self._trip_legs = 0

    async def _move(self, points, tick_seconds) -> None:
        for index, (latitude, longitude) in enumerate(points, start=1):
            await self.set_target(latitude, longitude)
            self._move_index = index
            await asyncio.sleep(tick_seconds)

    @property
    def _is_moving(self) -> bool:
        return self._move_task is not None and not self._move_task.done()

    @property
    def _is_on_trip(self) -> bool:
        return self._trip_task is not None and not self._trip_task.done()

    @property
    def engine_live(self) -> bool:
        """True while the hold loop task is running (including reconnecting)."""
        return self._task is not None and not self._task.done()

    @property
    def applied(self) -> bool:
        """True only after a real successful DVT set() on the current link.

        Do not treat "task exists" as success — that is true while RECONNECTING
        with no phone attached.
        """
        return bool(self._assert_live and self.engine_live)

    @property
    def reconnect_stale(self) -> bool:
        """True when RECONNECTING has lasted longer than the escalate threshold."""
        if self._state != self.RECONNECTING or self._reconnect_since is None:
            return False
        return (time.monotonic() - self._reconnect_since) >= RECONNECT_ESCALATE_SECONDS

    async def wait_for_applied(self, timeout: float = 4.0) -> bool:
        """Wait briefly for a successful DVT set after set_target / start_movement.

        Returns False early if we are already in a long reconnect, so API
        callers do not claim success while the phone is unreachable.
        """
        if self._assert_live and self.engine_live:
            return True
        if self._state == self.ERROR:
            return False
        # Already been reconnecting a while — do not pretend success.
        if self._state == self.RECONNECTING and self._reconnect_since is not None:
            if (time.monotonic() - self._reconnect_since) >= 1.0:
                return False
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            if self._assert_live and self.engine_live:
                return True
            if self._state == self.ERROR:
                return False
            if self.reconnect_stale:
                return False
            await asyncio.sleep(0.1)
        return bool(self._assert_live and self.engine_live)

    # -- engine callbacks --------------------------------------------------

    def _coordinate_source(self):
        return self._target

    def _on_event(self, kind, info):
        if kind == "connected" or kind == "reconnected":
            self._error = None  # recovered from a transient drop
            self._reconnect_since = None
            # Tunnel is up, but we have not successfully set() yet this link.
            self._assert_live = False
            self._state = self.HOLDING if self._target is not None else self.READY
        elif kind == "asserting":
            # location_simulation.set() returned successfully.
            self._assert_live = True
            self._reconnect_since = None
            self._link_lost_sticky = False
            self._state = self.HOLDING
        elif kind == "connection_lost":
            self._assert_live = False
            if self._reconnect_since is None:
                self._reconnect_since = time.monotonic()
            self._state = self.RECONNECTING

    # -- status ------------------------------------------------------------

    async def status(self) -> dict:
        """A JSON-serializable snapshot for the /status endpoint.

        Uses the cached device snapshot (see __init__) — no lockdown I/O here,
        so polling never races with the hold loop's use of that connection.
        When the engine is dead (error) or reconnect has gone stale, device
        chips should not look "ok".
        """
        device_status = self._device_info
        fatal = self._state == self.ERROR
        stale_reconnect = self.reconnect_stale
        if stale_reconnect:
            self._link_lost_sticky = True
        link_lost = fatal or stale_reconnect or self._link_lost_sticky
        # Don't keep a green "connected" chip from a stale start-of-session
        # snapshot when we know the link is gone.
        if device_status is not None:
            if link_lost:
                device_status = {
                    **device_status,
                    "connected": False,
                    "link_lost": True,
                }
            else:
                device_status = {
                    **device_status,
                    "connected": True,
                    "link_lost": False,
                }

        target = None
        if self._target is not None:
            target = {"lat": self._target[0], "lon": self._target[1]}
        moving = self._is_moving
        on_trip = self._is_on_trip
        if on_trip:
            state = "trip"
        elif moving:
            state = self._move_kind or self._state
        else:
            state = self._state
        trip = None
        if on_trip or (self._trip_legs and moving):
            trip = {
                "leg": self._trip_leg,
                "legs": self._trip_legs,
                "kind": self._move_kind,
            }

        reconnect_seconds = None
        if self._state == self.RECONNECTING and self._reconnect_since is not None:
            reconnect_seconds = round(time.monotonic() - self._reconnect_since, 1)

        return {
            "device": device_status,
            "state": state,
            "target": target,
            "error": self._error,
            "error_fatal": fatal,  # UI must not hide this while "moving"
            "engine_live": self.engine_live,
            "applied": self.applied,  # real DVT success only
            "assert_live": self._assert_live,
            "reconnect_stale": stale_reconnect,
            "reconnect_seconds": reconnect_seconds,
            "moving": moving or on_trip,
            "movement": {"kind": self._move_kind, "index": self._move_index,
                         "total": self._move_total} if moving else None,
            "trip": trip,
        }
