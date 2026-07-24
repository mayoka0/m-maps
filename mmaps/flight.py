"""Turning a straight great-circle flight into a stream of timed points to fly.

Fly is Drive without the router: the path is a straight great-circle between
waypoints (usually current location → destination / nearest airport), not an
OSRM road route and not a freehand-drawn path. We interpolate along the great
circle (the realistic shortest path over the sphere) and resample at plane
speed — time-compressed so a flight that would really take hours plays out
live in a couple of minutes. The points then feed ``set_target`` on a timer,
exactly like Drive.

Pure functions, no I/O.
"""
import math
import random

from mmaps.route import _clean, _jitter, haversine_m, walk_path

# Realistic cruising speed. On its own this would make a long flight take hours,
# so we multiply it by a playback factor (time-compression) below.
PLANE_SPEED_KMH = 900.0

# Playback multipliers offered to the UI as a simple speed control (time-
# compression on top of PLANE_SPEED_KMH). Tuned so a typical hop lands in about
# 1-3 minutes at "normal": e.g. a ~4,500 km flight is ~2.5 min, a ~1,000 km hop
# ~30 s. "slow" to savour a long flight, "fast" for long hauls. Easy to tweak.
SPEED_PRESETS = {"slow": 60.0, "normal": 120.0, "fast": 240.0}
DEFAULT_SPEED = "normal"

# Slightly finer than the old 1 s so the fly path doesn't look like discrete
# hops on the map; still coarse enough for time-compressed long hauls.
TICK_SECONDS = 0.25
# Planes track smoothly; no jitter (unlike a car nudging along a road).
JITTER_METERS = 0.0


def effective_speed_kmh(speed: str) -> float:
    """Plane speed after the chosen time-compression multiplier."""
    return PLANE_SPEED_KMH * SPEED_PRESETS.get(speed, SPEED_PRESETS[DEFAULT_SPEED])


def gc_interpolate(a, b, fraction):
    """Point ``fraction`` of the way along the great circle from a to b.

    a, b are (lat, lon) in degrees. Uses spherical interpolation (slerp), so
    long hops follow the true shortest path over the globe rather than a
    straight line on the flat map.
    """
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    # Central angle between the two points.
    d = 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    if d == 0:
        return (a[0], a[1])
    ka = math.sin((1 - fraction) * d) / math.sin(d)
    kb = math.sin(fraction * d) / math.sin(d)
    x = ka * math.cos(lat1) * math.cos(lon1) + kb * math.cos(lat2) * math.cos(lon2)
    y = ka * math.cos(lat1) * math.sin(lon1) + kb * math.cos(lat2) * math.sin(lon2)
    z = ka * math.sin(lat1) + kb * math.sin(lat2)
    lat = math.atan2(z, math.hypot(x, y))
    lon = math.atan2(y, x)
    return (math.degrees(lat), math.degrees(lon))


def resample_flight(waypoints, speed=DEFAULT_SPEED, *, tick_seconds=TICK_SECONDS,
                    jitter_m=JITTER_METERS, rng=None):
    """Resample a great-circle flight into one (lat, lon) point per tick.

    :param waypoints: the path as ``[[lon, lat], ...]`` (GeoJSON order, matching
        Drive's input) — typically ``[current, destination]`` (destination is
        often the nearest passenger airport). Multi-point waypoint lists are
        supported and joined as sequential great-circle segments.
    :param speed: a key of ``SPEED_PRESETS`` selecting the time-compression.
    :returns: (lat, lon) points, great-circle spaced at the compressed plane
        speed, ending exactly on the final waypoint. Empty input -> empty list.
    """
    rng = rng or random
    if not waypoints:
        return []

    points = _clean([(w[1], w[0]) for w in waypoints])
    if len(points) == 1:
        return [points[0]]

    step = (effective_speed_kmh(speed) / 3.6) * tick_seconds  # metres per tick
    return walk_path(points, step, gc_interpolate, jitter_m=jitter_m, rng=rng)
