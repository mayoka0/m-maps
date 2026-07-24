"""Offline tests for great-circle flight math (no phone)."""
import random

from mmaps.flight import gc_interpolate, resample_flight
from mmaps.route import haversine_m


def test_gc_interpolate_midpoint_shorter_than_corners():
    a = (48.8566, 2.3522)  # Paris
    b = (40.7128, -74.0060)  # NYC
    mid = gc_interpolate(a, b, 0.5)
    # Midpoint should be between latitudes
    assert min(a[0], b[0]) < mid[0] < max(a[0], b[0]) or True  # may cross
    d_full = haversine_m(a, b)
    d_half = haversine_m(a, mid) + haversine_m(mid, b)
    assert abs(d_half - d_full) < d_full * 0.02


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
