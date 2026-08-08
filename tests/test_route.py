"""Offline tests for road resampling (no phone)."""
import math
import random

from mmaps.route import (
    MODE_SPEEDS_KMH,
    TICK_SECONDS,
    catmull_rom_smooth,
    detect_turn_zones,
    ease_cosine,
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
    assert resample_by_speed([], 60) == []
    pts = resample_by_speed([[2.0, 48.0]], 60, jitter_m=0)
    assert pts == [(48.0, 2.0)]


def test_resample_finer_tick_more_points():
    coords = [[2.0, 48.0], [2.01, 48.0]]
    coarse = resample_by_speed(
        coords, 60, tick_seconds=1.0, jitter_m=0, rng=random.Random(0), ease=False, smooth=False
    )
    fine = resample_by_speed(
        coords, 60, tick_seconds=0.25, jitter_m=0, rng=random.Random(0), ease=False, smooth=False
    )
    assert len(fine) >= len(coarse) * 3
    # Ends on destination
    assert abs(fine[-1][0] - 48.0) < 1e-6
    assert abs(fine[-1][1] - 2.01) < 1e-6


def test_car_duration_roughly_distance_over_speed():
    # ~60 km east-ish polyline; at 60 km/h ETA ~1 hour of points.
    # Ease=False so we measure pure distance/speed (ease adds a few %).
    coords = [[2.0, 48.0], [2.8, 48.0]]  # ~60 km
    speed = MODE_SPEEDS_KMH["car"]
    pts = resample_by_speed(
        coords, speed, jitter_m=0, rng=random.Random(0), ease=False, smooth=False
    )
    eta_h = (len(pts) * TICK_SECONDS) / 3600.0
    dist_km = haversine_m((48.0, 2.0), (48.0, 2.8)) / 1000.0
    expected_h = dist_km / speed
    assert abs(eta_h - expected_h) < expected_h * 0.15


def test_ease_makes_trip_slightly_longer():
    coords = [[2.0, 48.0], [2.15, 48.0]]  # ~11 km
    speed = MODE_SPEEDS_KMH["car"]
    flat = resample_by_speed(
        coords, speed, jitter_m=0, rng=random.Random(0), ease=False, smooth=False
    )
    eased = resample_by_speed(
        coords, speed, jitter_m=0, rng=random.Random(0), ease=True, smooth=False
    )
    assert len(eased) >= len(flat)


def test_resample_step_matches_speed_mid_cruise():
    # Long straight run; mid-path steps should be near cruise step once eased.
    coords = [[2.0, 48.0], [2.3, 48.0]]
    speed = MODE_SPEEDS_KMH["car"]
    pts = resample_by_speed(
        coords, speed, tick_seconds=TICK_SECONDS, jitter_m=0,
        rng=random.Random(1), ease=True, smooth=False,
    )
    assert len(pts) >= 20
    # Sample near the middle of the trip (past accel, before decel).
    i = len(pts) // 2
    step = haversine_m(pts[i], pts[i + 1])
    expected = (speed / 3.6) * TICK_SECONDS
    assert abs(step - expected) < expected * 0.35


def test_easing_smaller_steps_at_start_than_mid():
    coords = [[2.0, 48.0], [2.2, 48.0]]
    speed = MODE_SPEEDS_KMH["car"]
    pts = resample_by_speed(
        coords, speed, jitter_m=0, rng=random.Random(0), ease=True, smooth=False
    )
    assert len(pts) >= 30
    start_step = haversine_m(pts[0], pts[1])
    mid = len(pts) // 2
    mid_step = haversine_m(pts[mid], pts[mid + 1])
    assert start_step < mid_step * 0.85


def test_catmull_rom_rounds_sharp_corner_only_when_explicitly_requested():
    # L-shaped control path: origin → east → north.
    # Pure polygon corner sits at (0, 0.01) lat/lon delta; spline should cut inside.
    a = (48.0, 2.0)
    corner = (48.0, 2.01)   # east of a
    c = (48.01, 2.01)       # north of corner
    smooth = catmull_rom_smooth([a, corner, c], sample_m=5.0)
    assert len(smooth) >= 4
    assert abs(smooth[0][0] - a[0]) < 1e-9 and abs(smooth[0][1] - a[1]) < 1e-9
    assert abs(smooth[-1][0] - c[0]) < 1e-6 and abs(smooth[-1][1] - c[1]) < 1e-6

    # Mid-path points should not all lie on the two legs of the L.
    # The sharp corner of the L is at `corner`; a rounded path gets closer to the
    # diagonal shortcut than a pure L walk would at the same fraction.
    def dist_to_l_legs(p):
        # Distance to nearest of the two segments a→corner and corner→c (approx lat/lon).
        def seg_d(p, u, v):
            # crude local metres
            cos = max(math.cos(math.radians(u[0])), 1e-6)
            m = 111_320.0
            def xy(q):
                return ((q[1] - u[1]) * m * cos, (q[0] - u[0]) * m)
            # re-origin each call is fine for short segments
            cos = max(math.cos(math.radians(48.0)), 1e-6)
            def to_xy(q):
                return ((q[1] - 2.0) * m * cos, (q[0] - 48.0) * m)
            px, py = to_xy(p)
            ax, ay = to_xy(u)
            bx, by = to_xy(v)
            dx, dy = bx - ax, by - ay
            len2 = dx * dx + dy * dy
            if len2 < 1e-12:
                return math.hypot(px - ax, py - ay)
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
            return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        return min(seg_d(p, a, corner), seg_d(p, corner, c))

    # At least one interior sample sits clearly inside the corner (off both legs).
    interior = smooth[1:-1]
    max_inside = max(dist_to_l_legs(p) for p in interior)
    assert max_inside > 8.0  # metres off the hard L — a real curve cut


def test_default_drive_stays_on_osrm_polyline_at_tight_parallel_turn():
    """Regression: an unconstrained spline sent the phone onto a wrong road.

    This U-turn represents two nearby parallel roads.  Default production
    resampling must stay on the supplied OSRM segments; otherwise Find My can
    map-match an off-route sample to the neighboring road and then appear to
    double back.
    """
    lat0 = 42.0
    metres_per_lat = 111_320.0
    metres_per_lon = metres_per_lat * math.cos(math.radians(lat0))

    def geo(x_m, y_m):
        return [x_m / metres_per_lon, lat0 + y_m / metres_per_lat]

    coordinates = [geo(0, 0), geo(100, 0), geo(100, 12), geo(0, 12)]
    points = resample_by_speed(
        coordinates,
        MODE_SPEEDS_KMH["car"],
        jitter_m=0,
        rng=random.Random(0),
    )

    def xy(point):
        lat, lon = point
        return lon * metres_per_lon, (lat - lat0) * metres_per_lat

    raw = [(c[1], c[0]) for c in coordinates]

    def distance_to_segment(point, start, end):
        px, py = xy(point)
        ax, ay = xy(start)
        bx, by = xy(end)
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if length_sq == 0:
            return math.hypot(px - ax, py - ay)
        fraction = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        return math.hypot(px - (ax + fraction * dx), py - (ay + fraction * dy))

    max_off_route = max(
        min(distance_to_segment(point, raw[i], raw[i + 1]) for i in range(len(raw) - 1))
        for point in points
    )
    assert max_off_route < 0.05


def test_detect_turn_zones_finds_right_angle():
    a = (48.0, 2.0)
    b = (48.0, 2.02)
    c = (48.02, 2.02)
    zones = detect_turn_zones([a, b, c], cruise_m_s=16.0)
    assert len(zones) >= 1
    assert zones[0][2] < 0.9  # min speed frac below cruise


def test_ease_cosine_bounds():
    assert ease_cosine(0) == 0.0
    assert ease_cosine(1) == 1.0
    assert 0.45 < ease_cosine(0.5) < 0.55


def test_mode_speeds_ordered():
    assert "walk" in MODE_SPEEDS_KMH
    assert MODE_SPEEDS_KMH["walk"] < MODE_SPEEDS_KMH["car"]


def test_production_road_jitter_is_disabled():
    from mmaps.route import JITTER_METERS

    assert JITTER_METERS == 0.0


def test_drive_with_smooth_and_ease_ends_at_dest():
    # Multi-vertex path (turn) through full pipeline.
    coords = [[2.0, 48.0], [2.02, 48.0], [2.02, 48.02], [2.04, 48.02]]
    pts = resample_by_speed(
        coords, MODE_SPEEDS_KMH["car"], jitter_m=0, rng=random.Random(0)
    )
    assert abs(pts[-1][0] - 48.02) < 1e-5
    assert abs(pts[-1][1] - 2.04) < 1e-5
    assert len(pts) > 10


def test_duration_override_paces_same_path():
    # Same geometry; forced 120 s should yield ~120/tick points.
    coords = [[2.0, 48.0], [2.3, 48.0]]
    target = 120.0
    pts = resample_by_speed(
        coords,
        MODE_SPEEDS_KMH["car"],
        jitter_m=0,
        rng=random.Random(0),
        duration_seconds=target,
        smooth=False,
        ease=False,
    )
    eta = len(pts) * TICK_SECONDS
    assert abs(eta - target) < target * 0.12
    assert abs(pts[-1][1] - 2.3) < 1e-5
