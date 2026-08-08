"""Offline tests for multi-stop dwell timing (no phone or tunnel)."""
import asyncio
import time

from mmaps.session import SpoofSession


class _FakeClient:
    async def close(self):
        return None


def test_trip_wait_can_be_skipped_and_holds_arrival():
    async def scenario():
        session = SpoofSession(_FakeClient())
        arrivals = []

        async def fake_begin_leg(points, tick, kind):
            session._move_kind = kind

            async def finish_leg():
                session._target = points[-1]
                arrivals.append(points[-1])

            session._move_task = asyncio.create_task(finish_leg())

        session._begin_leg = fake_begin_leg

        legs = [
            {
                "kind": "drive",
                "coordinates": [[0.0, 0.0], [0.001, 0.001]],
                "wait_seconds": 60,
            },
            {
                "kind": "drive",
                "coordinates": [[0.001, 0.001], [0.002, 0.002]],
                "wait_seconds": 60,  # final wait is intentionally ignored
            },
        ]

        # Keep this test focused on orchestration rather than route resampling.
        from mmaps import route
        original = route.resample_by_speed
        route.resample_by_speed = lambda coords, speed: [
            (coords[-1][1], coords[-1][0])
        ]
        try:
            await session.start_trip(legs)
            deadline = time.monotonic() + 1
            while session._trip_wait_until is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert session._trip_wait_until is not None
            status = await session.status()
            assert status["trip"]["phase"] == "waiting"
            assert status["trip"]["wait_remaining_seconds"] > 0
            assert status["moving"] is True
            assert session._target == (0.001, 0.001)

            assert await session.leave_now() is True
            await asyncio.wait_for(session._trip_task, timeout=1)
            assert arrivals == [(0.001, 0.001), (0.002, 0.002)]
            assert session._target == (0.002, 0.002)
            assert session._trip_wait_until is None
            assert await session.leave_now() is False
        finally:
            route.resample_by_speed = original

    asyncio.run(scenario())


def test_cancelling_trip_interrupts_wait():
    async def scenario():
        session = SpoofSession(_FakeClient())
        session._trip_task = asyncio.create_task(session._wait_at_stop(60))
        session._trip_legs = 2
        deadline = time.monotonic() + 1
        while session._trip_wait_until is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.wait_for(session.stop_movement(), timeout=1)
        assert session._trip_wait_until is None
        assert session._trip_task is None

    asyncio.run(scenario())
