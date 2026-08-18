"""Offline tests for great-circle flight math (no phone)."""
import math
import random

from mmaps.flight import (
    PLANE_SPEED_KMH,
    SPEED_PRESETS,
    TICK_SECONDS,
    effective_speed_kmh,
    gc_interpolate,
    resample_flight,
)
from mmaps.route import haversine_m


def test_gc_interpolate_midpoint_shorter_than_corners():
    a = (48.8566, 2.3522)  # Paris
    b = (40.7128, -74.0060)  # NYC
    mid = gc_interpolate(a, b, 0.5)
    d_full = haversine_m(a, b)
    d_half = haversine_m(a, mid) + haversine_m(mid, b)
    assert abs(d_half - d_full) < d_full * 0.02


def test_gc_interpolate_antipodal_points_stays_finite():
    midpoint = gc_interpolate((0.0, 0.0), (0.0, 180.0), 0.5)
    assert all(-180.0 <= value <= 180.0 for value in midpoint)
    assert all(math.isfinite(value) for value in midpoint)


def test_resample_flight_empty_single():
    assert resample_flight([]) == []
    pts = resample_flight([[2.0, 48.0]], jitter_m=0)
    assert pts == [(48.0, 2.0)]


def test_resample_flight_ends_at_destination():
    # Short hop
    wps = [[2.35, 48.85], [2.45, 48.90]]
    pts = resample_flight(wps, speed="fast", jitter_m=0, rng=random.Random(0))
    assert len(pts) >= 1
    assert abs(pts[-1][0] - 48.90) < 1e-5
    assert abs(pts[-1][1] - 2.45) < 1e-5


def test_no_time_compression_multipliers():
    # Presets are mild scales around real cruise, not 60-240× playback.
    assert SPEED_PRESETS["normal"] == 1.0
    assert all(0.5 <= v <= 1.5 for v in SPEED_PRESETS.values())
    assert 800 <= effective_speed_kmh("normal") <= 950


def test_long_haul_duration_ballpark():
    # Nairobi → NYC-ish great-circle ~11_800 km @ 875 km/h → ~13.5 h
    # Takeoff/landing ease is negligible vs hours of cruise.
    nbo = (-1.3192, 36.9275)  # lat, lon approx JKIA
    jfk = (40.6413, -73.7781)
    dist_km = haversine_m(nbo, jfk) / 1000.0
    assert 11_000 < dist_km < 13_000
    wps = [[nbo[1], nbo[0]], [jfk[1], jfk[0]]]
    pts = resample_flight(wps, speed="normal", jitter_m=0, rng=random.Random(0))
    eta_h = (len(pts) * TICK_SECONDS) / 3600.0
    expected_h = dist_km / PLANE_SPEED_KMH
    assert abs(eta_h - expected_h) < expected_h * 0.12
    assert 12.0 < eta_h < 16.0


def test_flight_takeoff_steps_smaller_than_cruise():
    # Short-ish hop still long enough for a clear cruise middle.
    # ~200 km east
    a = (48.0, 2.0)
    b = (48.0, 4.5)
    wps = [[a[1], a[0]], [b[1], b[0]]]
    pts = resample_flight(wps, speed="normal", jitter_m=0, rng=random.Random(0), ease=True)
    assert len(pts) >= 40
    start_step = haversine_m(pts[0], pts[1])
    mid = len(pts) // 2
    mid_step = haversine_m(pts[mid], pts[mid + 1])
    assert start_step < mid_step * 0.9


def test_flight_duration_override():
    # Force a multi-hour hop into exactly 1 hour wall-clock.
    a = (48.0, 2.0)
    b = (40.7, -74.0)  # NYC-ish
    wps = [[a[1], a[0]], [b[1], b[0]]]
    target = 3600.0
    pts = resample_flight(
        wps, speed="normal", jitter_m=0, rng=random.Random(0), duration_seconds=target
    )
    eta = len(pts) * TICK_SECONDS
    assert abs(eta - target) < target * 0.12
    assert abs(pts[-1][0] - b[0]) < 1e-3


def test_release_version_and_beta_helper():
    from mmaps import __version__, is_beta_version

    assert __version__ == "1.0.5"
    assert not is_beta_version()
    assert is_beta_version("1.0.5-beta.2")
    assert not is_beta_version("1.0.3")
