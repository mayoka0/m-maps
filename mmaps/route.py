"""Turning a road-route polyline into a stream of timed points to drive.

Pure functions, no I/O — easy to unit-test. The map GUI fetches the actual
road route from a routing service (OSRM) in the browser; the coordinates land
here, we resample them by speed into one point per movement tick, and the
session feeds those points to ``set_target`` on a timer.

Speed presets (walk / bicycle / motorcycle / car / fly) are the same road
route at different km/h — "fly" here is just the fastest preset along the
route, not a separate great-circle mode.
"""
import math
import random

# Road speed presets in km/h — real-world averages for prank believability.
# Duration ≈ path_length / speed (no time-compression). Keys are what
# POST /drive accepts as ``mode`` (historical field name — it's a speed).
# Fly is NOT here: planes use POST /fly (great-circle).
#
# Sanity checks (distance / speed ≈ wall-clock time):
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
# At car 60 km/h, 0.25 s → ~4.2 m/step — fluid in Find My without huge point lists
# on multi-hour trips. (Faster ticks at realistic speeds add little visual value.)
TICK_SECONDS = 0.25

# Lateral noise scaled for the smaller step size (was too large vs ~4 m steps).
JITTER_METERS = 0.35

_EARTH_RADIUS_M = 6_371_000.0
_METERS_PER_DEGREE_LAT = 111_320.0


def haversine_m(a, b) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _interpolate(a, b, frac):
    """Point ``frac`` of the way from a to b (both (lat, lon)); linear is fine
    at the metre spacing we resample to."""
    return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)


def _jitter(point, jitter_m, rng):
    if jitter_m <= 0:
        return point
    lat, lon = point
    dlat = rng.uniform(-jitter_m, jitter_m) / _METERS_PER_DEGREE_LAT
    lon_scale = _METERS_PER_DEGREE_LAT * max(math.cos(math.radians(lat)), 1e-6)
    dlon = rng.uniform(-jitter_m, jitter_m) / lon_scale
    return (lat + dlat, lon + dlon)


def _clean(points):
    """Drop consecutive duplicate points, which would be zero-length segments."""
    cleaned = [points[0]]
    for p in points[1:]:
        if p != cleaned[-1]:
            cleaned.append(p)
    return cleaned


def resample_by_speed(coordinates, speed_kmh, *, tick_seconds=TICK_SECONDS,
                      jitter_m=JITTER_METERS, rng=None):
    """Resample a route polyline into one (lat, lon) point per ``tick_seconds``.

    :param coordinates: the route geometry as ``[[lon, lat], ...]`` — GeoJSON
        order, exactly what OSRM returns. (We convert to (lat, lon) internally
        and return (lat, lon), which is what ``set_target`` expects.)
    :param speed_kmh: travel speed; points are spaced this far apart per tick.
    :returns: a list of (lat, lon) points starting one step ahead of the origin
        and ending exactly on the destination. Empty input -> empty list.
    """
    rng = rng or random
    if not coordinates:
        return []

    points = _clean([(c[1], c[0]) for c in coordinates])
    if len(points) == 1:
        return [points[0]]

    step = (speed_kmh / 3.6) * tick_seconds  # metres per tick
    return walk_path(points, step, _interpolate, jitter_m=jitter_m, rng=rng)


def walk_path(points, step_m, interpolate, *, jitter_m=0.0, rng=None):
    """Emit a point every ``step_m`` metres along a polyline of (lat, lon) points.

    Shared by drive (straight-line ``_interpolate``) and fly (great-circle
    interpolation) — the two only differ in how a segment is subdivided and how
    far apart the points are spaced. ``points`` must be cleaned and have length
    >= 2. Always ends exactly on the last point (no jitter on the endpoint).
    """
    rng = rng or random

    # Precompute per-segment length and where each segment starts along the path.
    segments = []  # (start, end, length_m, cumulative_start_m)
    cumulative = 0.0
    for i in range(len(points) - 1):
        length = haversine_m(points[i], points[i + 1])
        segments.append((points[i], points[i + 1], length, cumulative))
        cumulative += length
    total = cumulative

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
        out.append(_jitter(interpolate(start, end, frac), jitter_m, rng))
        distance += step_m

    out.append(points[-1])  # land exactly on the destination, no jitter
    return out
