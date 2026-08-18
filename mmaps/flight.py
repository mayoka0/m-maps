"""Turning a straight great-circle flight into a stream of timed points to fly.

Fly is Drive without the router: the path is a straight great-circle between
waypoints (usually current location → destination / nearest airport), not an
OSRM road route and not a freehand-drawn path. We interpolate along the great
circle (the realistic shortest path over the sphere) and resample at **real
commercial cruise speed** so a long-haul hop takes roughly real wall-clock time
(e.g. Nairobi→NYC ~14-15 h). Takeoff/landing use the same cosine speed ease as
drive (ramp up from rest, ease down into the destination) so the start/end are
not an instant jump to full cruise.

The points then feed ``set_target`` on a timer, exactly like Drive.

Pure functions, no I/O.
"""
from __future__ import annotations

import math
import random

from mmaps.route import (
    _clean,
    walk_path_eased,
)

# Typical jet cruise (mach ~0.8 band). No artificial time-compression multipliers.
# Sanity: ~11_800 km NBO-JFK / 875 km/h ≈ 13.5 h (door-to-door real flights ~15 h
# including taxi/climb; pure cruise distance/speed is the right prank scale).
PLANE_SPEED_KMH = 875.0

# Mild variation around real cruise - still realistic jet speeds, not 60-240× playback.
# effective_kmh = PLANE_SPEED_KMH * SPEED_PRESETS[name]
SPEED_PRESETS = {
    "slow": 0.85,    # ~744 km/h
    "normal": 1.0,   # 875 km/h
    "fast": 1.1,     # ~963 km/h
}
DEFAULT_SPEED = "normal"

# At cruise, 0.5 s → ~120 m/step - smooth for plane-scale motion; half the points
# of 0.25 s on 15-hour flights (still fine in memory).
TICK_SECONDS = 0.5
# Planes track smoothly; no jitter (unlike a car nudging along a road).
JITTER_METERS = 0.0

# Takeoff / landing speed ramps (wall-clock seconds of cruise-distance scale).
# Short vs long-haul: walk_path_eased shrinks ramps when they would dominate.
TAKEOFF_SECONDS = 45.0
LANDING_SECONDS = 60.0


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
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    h = max(0.0, min(1.0, h))
    d = 2 * math.asin(math.sqrt(h))
    if d == 0:
        return (a[0], a[1])
    sin_d = math.sin(d)
    if abs(sin_d) < 1e-8:
        # Antipodal endpoints have multiple equally short great circles and
        # make ordinary slerp divide by nearly zero. Pick a deterministic
        # perpendicular plane so long flights remain finite and smooth.
        ax = math.cos(lat1) * math.cos(lon1)
        ay = math.cos(lat1) * math.sin(lon1)
        az = math.sin(lat1)
        ox, oy, oz = ay, -ax, 0.0  # cross(a, north)
        norm = math.sqrt(ox * ox + oy * oy + oz * oz)
        if norm < 1e-8:  # endpoint is near a pole; use cross(a, east)
            ox, oy, oz = 0.0, az, -ay
            norm = math.sqrt(ox * ox + oy * oy + oz * oz)
        ox, oy, oz = ox / norm, oy / norm, oz / norm
        angle = math.pi * fraction
        ca, sa = math.cos(angle), math.sin(angle)
        x, y, z = ca * ax + sa * ox, ca * ay + sa * oy, ca * az + sa * oz
    else:
        ka = math.sin((1 - fraction) * d) / sin_d
        kb = math.sin(fraction * d) / sin_d
        x = ka * math.cos(lat1) * math.cos(lon1) + kb * math.cos(lat2) * math.cos(lon2)
        y = ka * math.cos(lat1) * math.sin(lon1) + kb * math.cos(lat2) * math.sin(lon2)
        z = ka * math.sin(lat1) + kb * math.sin(lat2)
    lat = math.atan2(z, math.hypot(x, y))
    lon = math.atan2(y, x)
    return (math.degrees(lat), math.degrees(lon))


def resample_flight(
    waypoints,
    speed=DEFAULT_SPEED,
    *,
    tick_seconds=TICK_SECONDS,
    jitter_m=JITTER_METERS,
    rng=None,
    ease: bool = True,
    duration_seconds=None,
):
    """Resample a great-circle flight into one (lat, lon) point per tick.

    :param waypoints: the path as ``[[lon, lat], ...]`` (GeoJSON order, matching
        Drive's input) - typically ``[current, destination]`` (destination is
        often the nearest passenger airport). Multi-point waypoint lists are
        supported and joined as sequential great-circle segments.
    :param speed: a key of ``SPEED_PRESETS`` (mild scale around real cruise).
    :param ease: cosine takeoff / landing speed ramps (no turn spline - path is
        already a smooth great-circle). Ignored when ``duration_seconds`` is set.
    :param duration_seconds: if set, pace the same great-circle so the flight
        finishes in about this many seconds (destination fixed).
    :returns: (lat, lon) points at real cruise with eased ends, finishing exactly
        on the final waypoint. Empty input -> empty list.
    """
    from mmaps.route import path_length_m, walk_path

    rng = rng or random
    if not waypoints:
        return []

    points = _clean([(w[1], w[0]) for w in waypoints])
    if len(points) == 1:
        return [points[0]]
    if len(points) < 2:
        return []

    # Custom wall-clock duration: same path, constant step → exact ETA.
    if duration_seconds is not None and float(duration_seconds) > 0:
        total = path_length_m(points)
        n = max(1, int(round(float(duration_seconds) / max(tick_seconds, 1e-6))))
        step = total / n if n > 0 else total
        return walk_path(points, step, gc_interpolate, jitter_m=jitter_m, rng=rng)

    cruise = effective_speed_kmh(speed)
    # No turn zones on a great-circle; only start/end ease.
    return walk_path_eased(
        points,
        cruise,
        tick_seconds,
        gc_interpolate,
        jitter_m=jitter_m,
        rng=rng,
        ease=ease,
        turn_zones=(),
        accel_seconds=TAKEOFF_SECONDS,
        decel_seconds=LANDING_SECONDS,
    )
