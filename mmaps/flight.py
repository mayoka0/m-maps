"""Turning a straight great-circle flight into a stream of timed points to fly.

Fly is Drive without the router: the path is a straight great-circle between
waypoints (usually current location → destination / nearest airport), not an
OSRM road route and not a freehand-drawn path. We interpolate along the great
circle (the realistic shortest path over the sphere) and resample at **real
commercial cruise speed** so a long-haul hop takes roughly real wall-clock time
(e.g. Nairobi→NYC ~14–15 h). The points then feed ``set_target`` on a timer,
exactly like Drive.

Pure functions, no I/O.
"""
import math
import random

from mmaps.route import _clean, _jitter, haversine_m, walk_path

# Typical jet cruise (mach ~0.8 band). No artificial time-compression multipliers.
# Sanity: ~11_800 km NBO–JFK / 875 km/h ≈ 13.5 h (door-to-door real flights ~15 h
# including taxi/climb; pure cruise distance/speed is the right prank scale).
PLANE_SPEED_KMH = 875.0

# Mild variation around real cruise — still realistic jet speeds, not 60–240× playback.
# effective_kmh = PLANE_SPEED_KMH * SPEED_PRESETS[name]
SPEED_PRESETS = {
    "slow": 0.85,    # ~744 km/h
    "normal": 1.0,   # 875 km/h
    "fast": 1.1,     # ~963 km/h
}
DEFAULT_SPEED = "normal"

# At cruise, 0.5 s → ~120 m/step — smooth for plane-scale motion; half the points
# of 0.25 s on 15-hour flights (still fine in memory).
TICK_SECONDS = 0.5
# Planes track smoothly; no jitter (unlike a car nudging along a road).
JITTER_METERS = 0.0


def effective_speed_kmh(speed: str) -> float:
    """Plane ground speed (km/h) after the mild slow/normal/fast scale."""
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
    :param speed: a key of ``SPEED_PRESETS`` (mild scale around real cruise).
    :returns: (lat, lon) points spaced at real cruise speed, ending exactly on
        the final waypoint. Empty input -> empty list.
    """
    rng = rng or random
    if not waypoints:
        return []

    points = _clean([(w[1], w[0]) for w in waypoints])
    if len(points) == 1:
        return [points[0]]

    step = (effective_speed_kmh(speed) / 3.6) * tick_seconds  # metres per tick
    return walk_path(points, step, gc_interpolate, jitter_m=jitter_m, rng=rng)
