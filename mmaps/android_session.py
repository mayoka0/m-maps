"""Android implementation of the M Maps session interface.

The shared movement and multi-stop schedulers still come from ``SpoofSession``:
they repeatedly call ``set_target``. Only the platform-specific target writer
is replaced-from pymobiledevice3/DVT on iPhone to the ADB companion on Android.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple

from mmaps.android_device import AndroidController, AndroidDevice
from mmaps.session import SpoofSession


class AndroidSpoofSession(SpoofSession):
    """A SpoofSession-compatible Android target using the companion APK."""

    RECONNECT_STALE_SECONDS = 20.0

    def __init__(self, controller: AndroidController, device: AndroidDevice) -> None:
        # Do not call SpoofSession.__init__: it expects a pymobiledevice3 client.
        self._controller = controller
        self._android_device = device
        self._target: Optional[Tuple[float, float]] = None
        self._device_info = {
            "name": device.model,
            "ios_version": f"Android {device.android_version}",
            "android_version": device.android_version,
            "product_type": device.model,
            "trusted": True,
            "developer_mode": True,
            "platform": "android",
        }
        self._state = self.READY
        self._connected = True
        self._error = None
        self._assert_live = False
        self._reconnect_since = None
        self._link_lost_sticky = False
        self._move_task = None
        self._move_kind = None
        self._move_total = 0
        self._move_index = 0
        self._trip_task = None
        self._trip_leg = 0
        self._trip_legs = 0
        self._trip_wait_until = None
        self._trip_wait_event = asyncio.Event()

    async def start(self) -> None:
        self._state = self.HOLDING if self._target else self.READY
        self._error = None
        self._reconnect_since = None

    def _mark_reconnecting(self) -> None:
        if self._reconnect_since is None:
            self._reconnect_since = time.monotonic()
        self._connected = False
        self._assert_live = False
        self._state = self.RECONNECTING
        self._error = "Android phone disconnected. Reconnect it to continue."

    def _mark_connected(self) -> None:
        self._connected = True
        self._reconnect_since = None

    async def refresh_connection(self) -> None:
        """Fast status probe plus automatic companion restart after reconnect."""
        try:
            await asyncio.to_thread(self._controller.connect)
            was_disconnected = not self._connected
            self._mark_connected()
            if was_disconnected and self._target is not None:
                await self.set_target(*self._target)
            elif self._state == self.RECONNECTING:
                self._state = self.HOLDING if self._target else self.READY
                self._error = None
        except Exception:
            self._mark_reconnecting()

    async def set_target(self, latitude: float, longitude: float) -> None:
        """Send one target; the phone-side service keeps it fresh every 3 seconds."""
        self._target = (latitude, longitude)
        try:
            await asyncio.to_thread(
                self._controller.set_location, latitude, longitude
            )
            self._mark_connected()
            self._assert_live = True
            self._state = self.HOLDING
            self._error = None
        except Exception as error:
            self._mark_reconnecting()
            # Keep movement alive. Its next tick will try again; first rebuild
            # ADB forwarding in case the cable was unplugged and reconnected.
            try:
                await asyncio.to_thread(self._controller.connect)
                await asyncio.to_thread(
                    self._controller.set_location, latitude, longitude
                )
                self._mark_connected()
                self._assert_live = True
                self._state = self.HOLDING
                self._error = None
            except Exception:
                pass

    async def stop(self) -> None:
        await self.stop_movement()
        try:
            if not self._connected:
                await asyncio.to_thread(self._controller.connect)
            await asyncio.to_thread(self._controller.clear_location)
            self._connected = True
        except Exception as error:
            self._mark_reconnecting()
            self._error = "Reconnect the Android phone over USB, then restore GPS again."
            raise RuntimeError(self._error) from error
        self._target = None
        self._assert_live = False
        self._reconnect_since = None
        self._state = self.STOPPED
        self._error = None

    async def close(self) -> None:
        try:
            if self._target is not None:
                await self.stop()
        finally:
            await asyncio.to_thread(self._controller.close)

    @property
    def engine_live(self) -> bool:
        # Keepalive lives in the Android foreground service, not a Python task.
        return self._state not in (self.STOPPED, self.ERROR)

    @property
    def applied(self) -> bool:
        return self._assert_live

    @property
    def reconnect_stale(self) -> bool:
        return bool(
            self._reconnect_since is not None
            and time.monotonic() - self._reconnect_since >= self.RECONNECT_STALE_SECONDS
        )

    @property
    def reconnect_seconds(self):
        if self._reconnect_since is None:
            return None
        return max(0, round(time.monotonic() - self._reconnect_since))

    async def wait_for_applied(self, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            if self.applied:
                return True
            await asyncio.sleep(0.1)
        return self.applied

    async def status(self) -> dict:
        moving = self._is_moving
        on_trip = self._is_on_trip
        waiting = on_trip and self._trip_wait_until is not None
        state = "trip" if on_trip else (self._move_kind if moving else self._state)
        target = None
        if self._target is not None:
            target = {"lat": self._target[0], "lon": self._target[1]}
        trip = None
        if on_trip or (self._trip_legs and moving):
            trip = {
                "leg": self._trip_leg,
                "legs": self._trip_legs,
                "kind": self._move_kind,
                "phase": "waiting" if waiting else "moving",
                "wait_remaining_seconds": max(
                    0, round(self._trip_wait_until - time.monotonic())
                ) if waiting else None,
            }
        return {
            "device": {
                **self._device_info,
                "connected": self._connected,
                "link_lost": not self._connected,
            },
            "state": state,
            "target": target,
            "error": self._error,
            "error_fatal": self._state == self.ERROR,
            "engine_live": self.engine_live,
            "applied": self.applied,
            "assert_live": self._assert_live,
            "reconnect_stale": self.reconnect_stale,
            "reconnect_seconds": self.reconnect_seconds,
            "moving": moving or on_trip,
            "movement": {
                "kind": self._move_kind,
                "index": self._move_index,
                "total": self._move_total,
            } if moving else None,
            "trip": trip,
        }
