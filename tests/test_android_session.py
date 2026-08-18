"""Offline tests for Android session state and shared movement scheduling."""
import asyncio
import sys

import pytest

if sys.platform == "win32":
    pytest.skip(
        "Android session reuses the macOS/Unix SpoofSession module",
        allow_module_level=True,
    )

from mmaps.android_device import AndroidDevice, CompanionUnavailableError
from mmaps.android_session import AndroidSpoofSession


class FakeController:
    def __init__(self):
        self.locations = []
        self.clears = 0
        self.connects = 0
        self.closed = 0
        self.fail_sets = 0
        self.fail_clear = False

    def connect(self):
        self.connects += 1
        return AndroidDevice("SERIAL", "Samsung", "16")

    def set_location(self, lat, lon):
        if self.fail_sets:
            self.fail_sets -= 1
            raise CompanionUnavailableError("lost")
        self.locations.append((lat, lon))

    def clear_location(self):
        self.clears += 1
        if self.fail_clear:
            raise CompanionUnavailableError("lost")

    def close(self):
        self.closed += 1


def make_session(controller=None):
    controller = controller or FakeController()
    device = AndroidDevice("SERIAL", "Samsung A15", "16")
    return AndroidSpoofSession(controller, device), controller


def test_android_set_clear_and_status():
    async def scenario():
        session, controller = make_session()
        await session.start()
        await session.set_target(48.8584, 2.2945)
        status = await session.status()
        assert status["applied"] is True
        assert status["device"]["platform"] == "android"
        assert status["target"] == {"lat": 48.8584, "lon": 2.2945}
        await session.stop()
        assert controller.clears == 1
        assert (await session.status())["target"] is None

    asyncio.run(scenario())


def test_android_movement_reuses_shared_scheduler():
    async def scenario():
        session, controller = make_session()
        points = [(1.0, 2.0), (1.1, 2.1), (1.2, 2.2)]
        await session.start_movement(points, 0.001, "fly")
        await asyncio.wait_for(session._move_task, timeout=1)
        assert controller.locations == points
        assert session._target == points[-1]
        assert (await session.status())["moving"] is False

    asyncio.run(scenario())


def test_android_transient_set_failure_reconnects_without_killing_movement():
    async def scenario():
        session, controller = make_session()
        controller.fail_sets = 1
        await session.set_target(10.0, 20.0)
        assert controller.connects == 1
        assert controller.locations == [(10.0, 20.0)]
        assert session.applied is True
        assert session._connected is True

    asyncio.run(scenario())


def test_android_refresh_marks_disconnect_and_recovers_target():
    async def scenario():
        session, controller = make_session()
        await session.set_target(10.0, 20.0)

        original_connect = controller.connect
        controller.connect = lambda: (_ for _ in ()).throw(CompanionUnavailableError("gone"))
        await session.refresh_connection()
        assert (await session.status())["device"]["connected"] is False

        controller.connect = original_connect
        await session.refresh_connection()
        assert controller.locations[-1] == (10.0, 20.0)
        assert (await session.status())["device"]["connected"] is True

    asyncio.run(scenario())


def test_android_reconnect_status_escalates_only_after_sustained_loss():
    async def scenario():
        session, controller = make_session()
        await session.set_target(10.0, 20.0)
        controller.connect = lambda: (_ for _ in ()).throw(CompanionUnavailableError("gone"))
        await session.refresh_connection()
        assert (await session.status())["reconnect_stale"] is False

        session._reconnect_since -= session.RECONNECT_STALE_SECONDS + 1
        status = await session.status()
        assert status["reconnect_stale"] is True
        assert status["reconnect_seconds"] >= session.RECONNECT_STALE_SECONDS
        assert "disconnected" in status["error"].lower()

    asyncio.run(scenario())


def test_android_restore_reports_reconnect_guidance_when_usb_is_lost():
    async def scenario():
        session, controller = make_session()
        await session.set_target(10.0, 20.0)
        controller.fail_clear = True
        with pytest.raises(RuntimeError, match="Reconnect the Android phone"):
            await session.stop()
        status = await session.status()
        assert status["error"] == "Reconnect the Android phone over USB, then restore GPS again."
        assert status["error_fatal"] is False

    asyncio.run(scenario())
