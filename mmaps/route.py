"""Turning a road-route polyline into a stream of timed points to drive.

Pure functions, no I/O — easy to unit-test. The map GUI fetches the actual
road route from a routing service (OSRM) in the browser; the coordinates land
here and are resampled with a **variable speed profile** (cosine ease at
start/end and through sharp turns) into one point per movement tick. The
session feeds those points to ``set_target`` on a timer.

Road accuracy is the first priority: production playback follows OSRM's
polyline exactly.  Do not spline the route by default.  An unconstrained
Catmull-Rom curve can leave the source polyline by several metres at sharp or
uneven junctions, which is enough to put Find My on a nearby wrong road before
the curve returns.  Natural motion comes from speed easing; OSRM's full
geometry already describes the road's curves.

The DVT location protocol only accepts lat/lon — all natural-looking motion
comes from this coordinate sequence and its timing (not speed/heading fields).

Speed presets (walk / bicycle / motorcycle / car) are the same road route at
different cruise km/h. Fly is NOT here: planes use ``mmaps.flight``
(great-circle).
"""
from __future__ import annotations

import math
import random
from typing import Callable, List, Optional, Sequence, Tuple

# Road speed presets in km/h — real-world averages for prank believability.
# Duration ≈ path_length / speed (no time-compression), plus mild ease overhead.
# Fly is NOT here: planes use POST /fly (great-circle).
#
# Sanity checks (distance / cruise ≈ wall-clock time):
#   Walk 5 km → ~1 h; Bicycle 16 km → ~1 h; Motorcycle 55 km → ~1 h
#   Car Detroit→Ann Arbor ~60 km @ 60 km/h → ~1 h (mixed; pure highway ~45 min)
MODE_SPEEDS_KMH = {
    "walk": 5.0,         # pedestrian stroll
    "bicycle": 16.0,     # casual cycling (15–18 km/h band)
    "motorcycle": 55.0,  # mixed city/highway pace, not top speed
    "car": 60.0,         # mixed driving average (not highway-only cruise)
}

# Stable display order for road speeds (Fly is a separate path in the UI).
SPEED_PRESET_ORDER = ("walk", "bicycle", "motorcycle", "car")
DEFAULT_SPEED_PRESET = "car"

# How often we push a new location while driving.
# At car 60 km/h, 0.25 s → ~4.2 m/step at cruise — fluid in Find My.
TICK_SECONDS = 0.25

# Road playback should remain on the router's centreline.  Even sub-metre
# synthetic noise works against that guarantee and can make a dot appear near
# a kerb when different map datasets are already offset slightly.
JITTER_METERS = 0.0

# --- Natural motion (coordinate sequence only; protocol has no speed/heading) ---

# Light RDP before the spline so long colinear runs don't pin every vertex.
RDP_EPSILON_M = 4.0
# Densify the Catmull-Rom curve at about this spacing (metres).
SPLINE_SAMPLE_M = 2.5
# Cosine ramp distances are expressed as seconds-of-cruise-travel.
ACCEL_SECONDS = 10.0
DECEL_SECONDS = 12.0
# Turns: heading change (deg) above this gets a speed dip.
TURN_ANGLE_DEG = 28.0
# Half-width of the speed dip around a turn, as seconds of cruise travel.
TURN_SLOW_SECONDS = 2.2
# Floor for turn slowdown (never stop dead mid-corner).
TURN_MIN_SPEED_FRAC = 0.28
# Never stall the integrator (end is still exact via final append).
MIN_SPEED_FRAC = 0.04

_EARTH_RADIUS_M = 6_371_000.0
_METERS_PER_DEGREE_LAT = 111_320.0

Point = Tuple[float, float]  # (lat, lon)


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _interpolate(a: Point, b: Point, frac: float) -> Point:
    """Linear blend in lat/lon (fine at metre spacing after the spline densifies)."""
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


def _jitter(point: Point, jitter_m: float, rng) -> Point:
    if jitter_m <= 0:
        return point
    lat, lon = point
    dlat = rng.uniform(-jitter_m, jitter_m) / _METERS_PER_DEGREE_LAT
    lon_scale = _METERS_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 1e-6)
    dlon = rng.uniform(-jitter_m, jitter_m) / lon_scale
    return (lat + dlat, lon + dlon)


def _clean(points: Sequence[Point]) -> List[Point]:
    """Drop consecutive duplicate points, which would be zero-length segments."""
    if not points:
        return []
    cleaned = [points[0]]
    for p in points[1:]:
        if p != cleaned[-1]:
            cleaned.append(p)
    return cleaned


def ease_cosine(t: float) -> float:
    """Smooth 0→1 (or 1→0) cosine S-curve. ``t`` clamped to [0, 1]."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * t)


# ---------------------------------------------------------------------------
# Local metres projection (city-scale routes; origin = first control point)
# ---------------------------------------------------------------------------

def _to_xy(points: Sequence[Point]):
    lat0, lon0 = points[0]
    cos_lat = max(math.cos(math.radians(lat0)), 1e-6)
    xy = []
    for lat, lon in points:
        x = (lon - lon0) * _METERS_PER_DEGREE_LAT * cos_lat
        y = (lat - lat0) * _METERS_PER_DEGREE_LAT
        xy.append((x, y))
    return xy, lat0, lon0, cos_lat


def _from_xy(x: float, y: float, lat0: float, lon0: float, cos_lat: float) -> Point:
    lat = lat0 + y / _METERS_PER_DEGREE_LAT
    lon = lon0 + x / (_METERS_PER_DEGREE_LAT * cos_lat)
    return (lat, lon)


def _dist_point_to_segment_m(px, py, ax, ay, bx, by) -> float:
    """Perpendicular distance from P to segment AB in metres (local plane)."""
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    if len2 < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rdp_thin(points: Sequence[Point], epsilon_m: float = RDP_EPSILON_M) -> List[Point]:
    """Douglas–Peucker simplification in local metres (keeps endpoints)."""
    pts = _clean(list(points))
    if len(pts) <= 2 or epsilon_m <= 0:
        return pts
    xy, lat0, lon0, cos_lat = _to_xy(pts)

    def _rdp(indices: List[int]) -> List[int]:
        if len(indices) < 3:
            return indices
        i0, i1 = indices[0], indices[-1]
        ax, ay = xy[i0]
        bx, by = xy[i1]
        max_d, max_i = -1.0, None
        for i in indices[1:-1]:
            d = _dist_point_to_segment_m(xy[i][0], xy[i][1], ax, ay, bx, by)
            if d > max_d:
                max_d, max_i = d, i
        if max_d > epsilon_m and max_i is not None:
            left = _rdp([i for i in indices if i <= max_i])
            right = _rdp([i for i in indices if i >= max_i])
            return left[:-1] + right
        return [i0, i1]

    kept = _rdp(list(range(len(pts))))
    return [pts[i] for i in kept]


def _catmull_rom_xy(p0, p1, p2, p3, t: float):
    """Uniform Catmull–Rom: passes through p1 at t=0 and p2 at t=1."""
    t2 = t * t
    t3 = t2 * t
    x = 0.5 * (
        (2.0 * p1[0])
        + (-p0[0] + p2[0]) * t
        + (2.0 * p0[0] - 5.0 * p1[0] + 4.0 * p2[0] - p3[0]) * t2
        + (-p0[0] + 3.0 * p1[0] - 3.0 * p2[0] + p3[0]) * t3
    )
    y = 0.5 * (
        (2.0 * p1[1])
        + (-p0[1] + p2[1]) * t
        + (2.0 * p0[1] - 5.0 * p1[1] + 4.0 * p2[1] - p3[1]) * t2
        + (-p0[1] + 3.0 * p1[1] - 3.0 * p2[1] + p3[1]) * t3
    )
    return (x, y)


def catmull_rom_smooth(
    points: Sequence[Point],
    sample_m: float = SPLINE_SAMPLE_M,
) -> List[Point]:
    """Densify a polyline with Catmull–Rom so turns are curves, not hard corners.

    Endpoints are clamped (ghost points = endpoints). Two-point paths stay linear.
    """
    pts = _clean(list(points))
    if len(pts) < 2:
        return pts
    if len(pts) == 2:
        return [pts[0], pts[1]]

    xy, lat0, lon0, cos_lat = _to_xy(pts)
    n = len(xy)
    out_xy = [xy[0]]
    for i in range(n - 1):
        p0 = xy[i - 1] if i > 0 else xy[i]
        p1 = xy[i]
        p2 = xy[i + 1]
        p3 = xy[i + 2] if i + 2 < n else xy[i + 1]
        seg_len = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        if seg_len < 1e-9:
            continue
        n_samples = max(1, int(math.ceil(seg_len / max(sample_m, 0.25))))
        for k in range(1, n_samples + 1):
            t = k / n_samples
            out_xy.append(_catmull_rom_xy(p0, p1, p2, p3, t))
    out_xy[-1] = xy[-1]
    # Drop near-duplicates from dense sampling
    smoothed: List[Point] = []
    for x, y in out_xy:
        p = _from_xy(x, y, lat0, lon0, cos_lat)
        if not smoothed or haversine_m(smoothed[-1], p) > 0.05:
            smoothed.append(p)
    if smoothed[-1] != pts[-1]:
        smoothed.append(pts[-1])
    return smoothed


def _bearing_deg(a: Point, b: Point) -> float:
    """Initial bearing degrees from a → b (0 = north, clockwise)."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _heading_delta_deg(h1: float, h2: float) -> float:
    """Signed smallest turn from heading h1 to h2 in (-180, 180]."""
    return (h2 - h1 + 180.0) % 360.0 - 180.0


def detect_turn_zones(
    control_points: Sequence[Point],
    cruise_m_s: float,
    *,
    angle_deg: float = TURN_ANGLE_DEG,
    slow_seconds: float = TURN_SLOW_SECONDS,
) -> List[Tuple[float, float, float]]:
    """Find sharp corners on a control polyline.

    Returns a list of ``(arc_s_m, half_width_m, min_speed_frac)`` for speed dips.
    """
    pts = _clean(list(control_points))
    if len(pts) < 3:
        return []
    half_w = max(10.0, cruise_m_s * slow_seconds)
    zones: List[Tuple[float, float, float]] = []
    cum = 0.0
    for i in range(len(pts) - 1):
        if i >= 1:
            h_in = _bearing_deg(pts[i - 1], pts[i])
            h_out = _bearing_deg(pts[i], pts[i + 1])
            turn = abs(_heading_delta_deg(h_in, h_out))
            if turn >= angle_deg:
                # Sharper corner → lower min speed (floor TURN_MIN_SPEED_FRAC).
                severity = min(1.0, turn / 90.0)
                min_f = max(TURN_MIN_SPEED_FRAC, 1.0 - 0.55 * severity)
                zones.append((cum, half_w, min_f))
        cum += haversine_m(pts[i], pts[i + 1])
    return zones


def speed_factor_at(
    s: float,
    total: float,
    cruise_m_s: float,
    turn_zones: Sequence[Tuple[float, float, float]],
    *,
    accel_seconds: float = ACCEL_SECONDS,
    decel_seconds: float = DECEL_SECONDS,
    ease: bool = True,
) -> float:
    """Cruise-speed multiplier in (0, 1] at arc distance ``s`` along the path."""
    if not ease or total <= 0 or cruise_m_s <= 0:
        return 1.0

    accel_m = cruise_m_s * accel_seconds
    decel_m = cruise_m_s * decel_seconds
    # Don't let ramps eat the whole short trip.
    if accel_m + decel_m > total * 0.85:
        scale = (total * 0.85) / max(accel_m + decel_m, 1e-6)
        accel_m *= scale
        decel_m *= scale

    f = 1.0
    if accel_m > 1e-6 and s < accel_m:
        f *= ease_cosine(s / accel_m)
    remaining = total - s
    if decel_m > 1e-6 and remaining < decel_m:
        # remaining=decel → 1; remaining=0 → 0
        f *= ease_cosine(remaining / decel_m)

    for center, half_w, min_f in turn_zones:
        d = abs(s - center)
        if d < half_w and half_w > 0:
            # Edge of zone → 1; centre → min_f (cosine bowl).
            edge = ease_cosine(d / half_w)  # 0 at centre, 1 at edge
            dip = min_f + (1.0 - min_f) * edge
            f = min(f, dip)

    return max(f, MIN_SPEED_FRAC) if remaining > 0.5 else max(f, MIN_SPEED_FRAC * 0.5)


def _build_segments(
    points: Sequence[Point],
    interpolate: Callable[[Point, Point, float], Point],
):
    """Per-segment length and cumulative start distance for arc-length walk."""
    segments = []  # (start, end, length_m, cumulative_start_m)
    cumulative = 0.0
    for i in range(len(points) - 1):
        length = haversine_m(points[i], points[i + 1])
        segments.append((points[i], points[i + 1], length, cumulative))
        cumulative += length
    return segments, cumulative, interpolate


def point_at_distance(segments, total: float, s: float, interpolate) -> Point:
    """Point at arc distance ``s`` along precomputed segments (clamped)."""
    if not segments:
        return (0.0, 0.0)
    if s <= 0:
        return segments[0][0]
    if s >= total:
        return segments[-1][1]
    # Linear scan is fine: callers walk forward monotonically.
    for start, end, length, seg_start in segments:
        if length <= 0:
            continue
        if s <= seg_start + length:
            frac = (s - seg_start) / length
            return interpolate(start, end, frac)
    return segments[-1][1]


def walk_path_eased(
    points: Sequence[Point],
    cruise_kmh: float,
    tick_seconds: float,
    interpolate: Callable[[Point, Point, float], Point],
    *,
    jitter_m: float = 0.0,
    rng=None,
    ease: bool = True,
    turn_zones: Optional[Sequence[Tuple[float, float, float]]] = None,
    accel_seconds: float = ACCEL_SECONDS,
    decel_seconds: float = DECEL_SECONDS,
) -> List[Point]:
    """Emit one point per tick along ``points``, with optional speed easing.

    ``points`` must be cleaned and length >= 2. Always ends exactly on the last
    point (no jitter on the endpoint).
    """
    rng = rng or random
    zones = list(turn_zones or ())
    segments, total, interp = _build_segments(points, interpolate)
    if total <= 0 or tick_seconds <= 0:
        return [points[-1]]

    cruise_m_s = (cruise_kmh / 3.6) if cruise_kmh > 0 else 0.0
    if cruise_m_s <= 0:
        return [points[-1]]

    out: List[Point] = []
    s = 0.0
    # Cap iterations so a pathological profile can't hang.
    max_steps = int(total / (cruise_m_s * tick_seconds * MIN_SPEED_FRAC)) + 1000
    steps = 0
    while s < total - 1e-4 and steps < max_steps:
        steps += 1
        factor = speed_factor_at(
            s, total, cruise_m_s, zones,
            accel_seconds=accel_seconds,
            decel_seconds=decel_seconds,
            ease=ease,
        )
        ds = cruise_m_s * factor * tick_seconds
        # Guaranteed forward progress even at min factor.
        ds = max(ds, cruise_m_s * MIN_SPEED_FRAC * tick_seconds * 0.5)
        if s + ds >= total:
            break
        s += ds
        out.append(_jitter(point_at_distance(segments, total, s, interp), jitter_m, rng))

    out.append(points[-1])  # land exactly on the destination, no jitter
    return out


def walk_path(
    points,
    step_m,
    interpolate,
    *,
    jitter_m=0.0,
    rng=None,
):
    """Emit a point every ``step_m`` metres along a polyline (constant step).

    Shared by callers that want uniform spacing. Drive/fly prefer
    :func:`walk_path_eased` for natural acceleration. ``points`` must be
    cleaned and have length >= 2. Always ends exactly on the last point.
    """
    rng = rng or random
    segments, total, interp = _build_segments(points, interpolate)
    if step_m <= 0 or total == 0:
        return [points[-1]]

    out = []
    distance = step_m
    seg_index = 0
    while distance < total and seg_index < len(segments):
        start, end, length, seg_start = segments[seg_index]
        if length == 0 or distance > seg_start + length:
            seg_index += 1
            continue
        frac = (distance - seg_start) / length
        out.append(_jitter(interp(start, end, frac), jitter_m, rng))
        distance += step_m

    out.append(points[-1])
    return out


def _prepare_road_path(coordinates, *, smooth: bool = False) -> List[Point]:
    """GeoJSON ``[[lon,lat],...]`` → cleaned (and optionally spline-smoothed) path."""
    points = _clean([(c[1], c[0]) for c in coordinates])
    if len(points) < 2:
        return points
    if not smooth:
        return points
    control = rdp_thin(points, RDP_EPSILON_M)
    if len(control) >= 3:
        return catmull_rom_smooth(control, SPLINE_SAMPLE_M)
    return control


def path_length_m(points: Sequence[Point]) -> float:
    """Total great-circle length of a (lat, lon) polyline in metres."""
    pts = _clean(list(points))
    if len(pts) < 2:
        return 0.0
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def estimate_eta_seconds(path_length_meters: float, speed_kmh: float) -> float:
    """Wall-clock seconds at constant cruise (no ease overhead)."""
    if speed_kmh <= 0 or path_length_meters <= 0:
        return 0.0
    return (path_length_meters / 1000.0) / speed_kmh * 3600.0


def resample_by_speed(
    coordinates,
    speed_kmh,
    *,
    tick_seconds=TICK_SECONDS,
    jitter_m=JITTER_METERS,
    rng=None,
    smooth: bool = False,
    ease: bool = True,
    duration_seconds: Optional[float] = None,
):
    """Resample a route polyline into one (lat, lon) point per ``tick_seconds``.

    :param coordinates: the route geometry as ``[[lon, lat], ...]`` — GeoJSON
        order, exactly what OSRM returns. (We convert to (lat, lon) internally
        and return (lat, lon), which is what ``set_target`` expects.)
    :param speed_kmh: cruise travel speed; actual spacing eases around this
        unless ``duration_seconds`` overrides the whole trip length.
    :param smooth: opt-in legacy visual smoothing. Production callers leave
        this False so every emitted point remains on the OSRM road polyline.
        Catmull–Rom is unconstrained and can cut across or overshoot junctions.
    :param ease: if True, cosine ramp at start/end and slow through sharp turns
        (ignored when ``duration_seconds`` is set — custom duration is exact).
    :param duration_seconds: if set, pace the **same path** so it finishes in
        about this many seconds (destination fixed; speed is derived).
    :returns: a list of (lat, lon) points ending exactly on the destination.
        Empty input -> empty list.
    """
    rng = rng or random
    if not coordinates:
        return []

    path = _prepare_road_path(coordinates, smooth=smooth)
    if len(path) == 1:
        return [path[0]]
    if len(path) < 2:
        return []

    # Custom wall-clock duration: same geometry, constant step so ETA matches.
    if duration_seconds is not None and float(duration_seconds) > 0:
        total = path_length_m(path)
        n = max(1, int(round(float(duration_seconds) / max(tick_seconds, 1e-6))))
        step = total / n if n > 0 else total
        return walk_path(path, step, _interpolate, jitter_m=jitter_m, rng=rng)

    cruise_m_s = speed_kmh / 3.6
    # Turn detection may use a lightly simplified copy, but the coordinates
    # sent to the phone always walk ``path`` itself unless a caller explicitly
    # opts into the legacy smoother.  This preserves road fidelity while still
    # slowing naturally for significant heading changes.
    raw = _clean([(c[1], c[0]) for c in coordinates])
    control = rdp_thin(raw, RDP_EPSILON_M)
    zones = detect_turn_zones(control, cruise_m_s) if ease else []

    return walk_path_eased(
        path,
        speed_kmh,
        tick_seconds,
        _interpolate,
        jitter_m=jitter_m,
        rng=rng,
        ease=ease,
        turn_zones=zones,
    )
