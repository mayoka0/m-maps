"""Pure API-boundary validation checks (no phone or network needed)."""
import asyncio
import math

import pytest
from fastapi import HTTPException

from mmaps import server


def test_geo_validation_rejects_nonfinite_and_out_of_range_points():
    with pytest.raises(HTTPException, match="finite"):
        server._validate_geo_points([[0.0, math.nan], [1.0, 1.0]], label="Route")
    with pytest.raises(HTTPException, match="valid coordinate range"):
        server._validate_geo_points([[181.0, 1.0], [1.0, 1.0]], label="Route")


def test_geo_validation_rejects_short_and_oversized_paths():
    with pytest.raises(HTTPException, match="at least 2"):
        server._validate_geo_points([[0.0, 0.0]], label="Flight")
    with pytest.raises(HTTPException) as caught:
        server._validate_geo_points(
            [[0.0, 0.0]] * (server.MAX_PATH_INPUT_POINTS + 1),
            label="Flight",
        )
    assert caught.value.status_code == 413


def test_timing_validation_requires_geometry_indexes_and_finite_duration():
    valid = server.TimingSection(start_index=0, end_index=1, duration_seconds=2.0)
    assert server._timing_dicts([valid], point_count=2)[0]["end_index"] == 1

    with pytest.raises(HTTPException, match="does not match"):
        server._timing_dicts([valid], point_count=1)
    with pytest.raises(HTTPException, match="positive"):
        server._timing_dicts(
            [server.TimingSection(start_index=0, end_index=1, duration_seconds=math.inf)],
            point_count=2,
        )


def test_movement_budget_rejects_large_preflight_work():
    with pytest.raises(HTTPException) as caught:
        server._validate_movement_budget(
            server.MAX_PATH_INPUT_POINTS * 0.25,
            0.25,
            label="The route",
        )
    assert caught.value.status_code == 413
    assert "too many movement points" in str(caught.value.detail)


def test_timing_sections_have_an_input_limit():
    section = server.TimingSection(start_index=0, end_index=1, duration_seconds=1.0)
    with pytest.raises(HTTPException) as caught:
        server._timing_dicts([section] * (server.MAX_TIMING_SECTIONS + 1))
    assert caught.value.status_code == 413


def test_trip_rejects_too_many_legs_before_session_lookup():
    request = server.TripRequest(
        legs=[
            server.TripLeg(kind="fly", waypoints=[[0.0, 0.0], [1.0, 1.0]])
            for _ in range(server.MAX_TRIP_LEGS + 1)
        ]
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.trip(request))
    assert caught.value.status_code == 413


def test_fly_snap_preserves_explicit_intermediate_waypoints(monkeypatch):
    monkeypatch.setattr(
        server.airports,
        "nearest_airport",
        lambda lat, lon: {"lat": 4.0, "lon": 5.0},
    )
    waypoints = [[1.0, 2.0], [2.0, 3.0], [3.0, 4.0]]
    assert server._snap_fly_waypoints(waypoints) == [
        [1.0, 2.0],
        [2.0, 3.0],
        [5.0, 4.0],
    ]
