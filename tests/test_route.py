"""Offline tests for road resampling (no phone)."""
import random

from mmaps.route import (
    MODE_SPEEDS_KMH,
    TICK_SECONDS,
    haversine_m,
    resample_by_speed,
)


def test_haversine_known_short_segment():
    # ~1.11 km due east at equator-ish latitude
    a = (48.0, 2.0)
    b = (48.0, 2.01)
    d = haversine_m(a, b)
    assert 700 < d < 900


def test_resample_empty_and_single():
    assert resample_by_speed([], 90) == []
    pts = resample_by_speed([[2.0, 48.0]], 90, jitter_m=0)
    assert pts == [(48.0, 2.0)]


def test_resample_car_finer_than_1s():
    coords = [[2.0, 48.0], [2.01, 48.0]]
    coarse = resample_by_speed(coords, 90, tick_seconds=1.0, jitter_m=0, rng=random.Random(0))
    fine = resample_by_speed(coords, 90, tick_seconds=0.2, jitter_m=0, rng=random.Random(0))
    assert len(fine) >= len(coarse) * 4
    # Ends on destination
    assert abs(fine[-1][0] - 48.0) < 1e-6
    assert abs(fine[-1][1] - 2.01) < 1e-6


def test_resample_step_matches_speed():
    coords = [[2.0, 48.0], [2.02, 48.0]]
    speed = MODE_SPEEDS_KMH["car"]
    pts = resample_by_speed(
        coords, speed, tick_seconds=TICK_SECONDS, jitter_m=0, rng=random.Random(1)
    )
    assert len(pts) >= 2
    expected = (speed / 3.6) * TICK_SECONDS
    step = haversine_m(pts[0], pts[1])
    assert abs(step - expected) < expected * 0.35  # tolerate geometry


def test_mode_speeds_ordered():
    assert "walk" in MODE_SPEEDS_KMH
    assert MODE_SPEEDS_KMH["walk"] < MODE_SPEEDS_KMH["car"]
