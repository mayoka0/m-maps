"""Offline tests for multi-stop leg choice (no phone)."""
from mmaps.trip import FLY_MIN_STRAIGHT_KM, choose_leg_kind


def test_short_road_is_drive():
    a = (48.0, 2.0)
    b = (48.1, 2.1)
    assert choose_leg_kind(a, b, road_distance_m=50_000, routing_ok=True) == "drive"


def test_no_road_is_fly():
    a = (48.0, 2.0)
    b = (40.0, -74.0)
    assert choose_leg_kind(a, b, road_distance_m=None, routing_ok=True) == "fly"


def test_long_straight_is_fly():
    # ~1110 km north
    a = (0.0, 0.0)
    b = (10.0, 0.0)
    assert haversine_km_approx(a, b) >= FLY_MIN_STRAIGHT_KM * 0.9
    assert choose_leg_kind(a, b, road_distance_m=2_000_000, routing_ok=True) == "fly"


def haversine_km_approx(a, b):
    from mmaps.route import haversine_m

    return haversine_m(a, b) / 1000.0
