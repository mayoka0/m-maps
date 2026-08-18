"""Spoofing the phone's reported GPS location, iOS 17+.

iOS 17 moved the developer services (including location simulation) behind a
RemoteXPC tunnel over USB. Getting there means:

  1. Open a `CoreDeviceTunnelProxy` service over the existing lockdown
     connection and start a TCP tunnel through it. This creates a virtual
     network interface (utun) on the Mac, which is why it needs root.
  2. Connect to the RemoteServiceDiscovery (RSD) endpoint the tunnel hands
     back - an (address, port) pair.
  3. Open a DVT (Instruments) connection over RSD and use its
     LocationSimulation channel to set/clear the simulated coordinates.

The simulated location only sticks around while the DVT connection from step
3 stays open - closing it (even cleanly) lets the phone fall back to its real
GPS. That's why `hold_location` keeps the connection alive and re-asserts the
coordinate on a timer, rather than setting once and disconnecting.
"""
import asyncio
import os
from contextlib import asynccontextmanager

from pymobiledevice3.exceptions import (
    ChannelClosedError,
    ConnectionFailedError,
    ConnectionTerminatedError,
    DeviceNotFoundError,
    InvalidConnectionError,
    NoDeviceConnectedError,
    PasscodeRequiredError,
    PasswordRequiredError,
)
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

# Errors that mean "the tunnel/DVT link dropped" rather than "something is
# fundamentally wrong". When we see one of these mid-hold we rebuild the
# tunnel and carry on instead of dying. ConnectionError covers the built-in
# BrokenPipe/Reset/Aborted; OSError covers the raw socket going away.
CONNECTION_LOST_ERRORS = (
    ConnectionTerminatedError,  # includes StreamClosedError
    ChannelClosedError,
    ConnectionFailedError,
    InvalidConnectionError,
    ConnectionError,
    asyncio.IncompleteReadError,
    OSError,
)

# Phone gone from USB mid-session (cable jostled, brief disconnect). These are
# PyMobileDevice3Exception subclasses - *not* OSError/ConnectionError - so they
# must be listed explicitly or the hold loop dies instead of reconnecting.
DEVICE_LOST_ERRORS = (
    DeviceNotFoundError,
    NoDeviceConnectedError,
)

# "Device momentarily locked/busy" (lockdown's PasswordProtected). During a
# drive the phone can briefly refuse a lockdown/DVT request and then accept the
# next one - a transient blip, not a real failure. We treat these exactly like
# a dropped tunnel: back off and retry on the next tick rather than surfacing a
# scary error the drive has already recovered from.
TRANSIENT_DEVICE_ERRORS = (
    PasswordRequiredError,   # raised for lockdown "PasswordProtected"
    PasscodeRequiredError,
)

# Everything hold_location() recovers from in-place instead of giving up.
RECOVERABLE_ERRORS = CONNECTION_LOST_ERRORS + DEVICE_LOST_ERRORS + TRANSIENT_DEVICE_ERRORS

# How often we re-send the coordinate to keep the session warm (seconds). This
# is also the beat that route/drive/fly playback will hang off later: instead
# of re-sending the same point, the coordinate source will hand back the next
# point along the track on each tick.
DEFAULT_REASSERT_INTERVAL = 2.5

# Pause before rebuilding a dropped tunnel, to let the device settle.
RECONNECT_BACKOFF = 1.0


class RootRequiredError(RuntimeError):
    """Raised when a tunnel is attempted without root privileges."""


@asynccontextmanager
async def open_rsd(lockdown_client):
    """Start the iOS 17+ tunnel and yield a connected RemoteServiceDiscoveryService.

    Requires root (creates a utun interface). Tears the tunnel down on exit.
    """
    if os.geteuid() != 0:
        raise RootRequiredError("Starting the location tunnel requires root (run this with sudo).")

    proxy = await CoreDeviceTunnelProxy.create(lockdown_client)
    async with proxy.start_tcp_tunnel() as tunnel_result:
        rsd = RemoteServiceDiscoveryService((tunnel_result.address, tunnel_result.port))
        await rsd.connect()
        try:
            yield rsd
        finally:
            await rsd.close()


@asynccontextmanager
async def open_location_simulation(rsd):
    """Open the DVT LocationSimulation channel over an already-connected RSD tunnel."""
    async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as location_simulation:
        yield location_simulation


async def hold_location(lockdown_client, coordinate_source, stop_event, *,
                        reassert_interval=DEFAULT_REASSERT_INTERVAL, on_event=None,
                        wake_event=None):
    """Assert a simulated location and keep it alive until ``stop_event`` is set.

    Opens the tunnel + DVT session, then loops: pull the current target from
    ``coordinate_source`` and send it, every ``reassert_interval`` seconds. If
    the tunnel drops, rebuild it and resume - this is the whole point of the
    function, so a flaky cable or a brief device hiccup doesn't kill the spoof.

    :param lockdown_client: a trusted lockdown client (from ``device.connect``).
    :param coordinate_source: a zero-arg callable returning the ``(lat, lon)``
        to assert *right now*, or ``None`` to assert nothing yet (keep the
        tunnel warm and idle). For a static teleport it always returns the same
        point; the map server returns ``None`` until the first click, then the
        clicked point; route/drive/fly playback will later return successive
        points. Kept deliberately dumb (a plain callable) so this loop doesn't
        need to know anything about how the path is generated.
    :param stop_event: an ``asyncio.Event``; set it to stop holding, clear the
        location, and return.
    :param reassert_interval: seconds between re-asserts.
    :param on_event: optional ``callable(kind, info_dict)`` for progress/status.
        All user-facing wording lives in the caller, not here. Event kinds:
        ``connected``, ``reconnected``, ``asserting``, ``connection_lost``,
        ``cleared``, ``clear_failed``.
    :param wake_event: optional ``asyncio.Event`` that, when set, makes the loop
        stop waiting and re-read ``coordinate_source`` immediately - so a live
        retarget (a new map click) takes effect at once instead of on the next
        tick. The loop clears it after acting on it.
    """
    def emit(kind, **info):
        if on_event is not None:
            on_event(kind, info)

    established_once = False
    while not stop_event.is_set():
        try:
            async with open_rsd(lockdown_client) as rsd, \
                    open_location_simulation(rsd) as location_simulation:
                emit("reconnected" if established_once else "connected")
                established_once = True

                await _assert_until_stopped(
                    location_simulation, coordinate_source, stop_event, reassert_interval,
                    emit, wake_event,
                )
                # We only fall out of the assert loop when a stop was requested
                # while the session is healthy - clear cleanly before teardown.
                await _clear_best_effort(location_simulation, emit)
                return
        except RootRequiredError:
            # Not a transient drop - the caller forgot sudo. Let it surface.
            raise
        except RECOVERABLE_ERRORS as error:
            if stop_event.is_set():
                # Dropped exactly as we were asked to stop; nothing to clear
                # (the closed session already reverted the phone to real GPS).
                return
            emit("connection_lost", error=error)
            if await _sleep_or_stopped(stop_event, RECONNECT_BACKOFF):
                return
            # loop around and rebuild the tunnel


async def clear_via_new_session(lockdown_client) -> None:
    """Open a fresh tunnel + DVT session purely to clear any stuck simulated location.

    Standalone reset path: useful if a previous hold session was killed
    uncleanly and the phone is still reporting a spoofed location.
    """
    async with open_rsd(lockdown_client) as rsd:
        async with open_location_simulation(rsd) as location_simulation:
            await location_simulation.clear()


async def _assert_until_stopped(location_simulation, coordinate_source, stop_event, interval,
                                emit, wake_event=None):
    """Re-send the current coordinate every ``interval`` seconds until stopped.

    A ``None`` from ``coordinate_source`` means "nothing to assert yet" - we
    just idle-wait, keeping the tunnel open. A set ``wake_event`` cuts the wait
    short so a fresh target is asserted immediately.

    Returns normally on stop. Propagates connection errors so the caller can
    rebuild the tunnel.
    """
    while not stop_event.is_set():
        target = coordinate_source()
        if target is not None:
            latitude, longitude = target
            await location_simulation.set(latitude, longitude)
            emit("asserting", latitude=latitude, longitude=longitude)
        if await _wait_tick(stop_event, wake_event, interval):
            return


async def _clear_best_effort(location_simulation, emit) -> None:
    """Clear the simulated location, reporting but not raising on failure."""
    try:
        await location_simulation.clear()
        emit("cleared")
    except Exception as error:  # best-effort: we're tearing down anyway
        emit("clear_failed", error=error)


async def _sleep_or_stopped(stop_event, timeout) -> bool:
    """Wait up to ``timeout`` seconds, or until ``stop_event`` is set.

    :returns: True if we woke because the event was set, False on timeout.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _wait_tick(stop_event, wake_event, timeout) -> bool:
    """Wait up to ``timeout`` seconds, cut short by either event being set.

    ``stop_event`` means "stop the whole hold"; ``wake_event`` (optional) means
    "a new target is ready, re-assert now" and is cleared here once observed.

    :returns: True only if ``stop_event`` is set (caller should stop); False on
        timeout or a wake (caller should loop and re-assert).
    """
    waiters = [asyncio.ensure_future(stop_event.wait())]
    if wake_event is not None:
        waiters.append(asyncio.ensure_future(wake_event.wait()))
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    if stop_event.is_set():
        return True
    if wake_event is not None and wake_event.is_set():
        wake_event.clear()
    return False
