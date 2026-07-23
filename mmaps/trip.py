"""Multi-stop trip helpers: decide fly vs drive per leg.

Pure, testable. The UI plans legs (OSRM + nearest airport) and POST /trip runs
them sequentially through SpoofSession — each leg is a normal drive or fly.
"""
from typing import Optional

from mmaps.route import haversine_m

# Same distance rule as the old mode menu: long hops / no road → fly.
FLY_MIN_STRAIGHT_KM = 1000.0


def choose_leg_kind(
    from_latlon,
    to_latlon,
    road_distance_m: Optional[float],
    routing_ok: bool,
) -> str:
    """Return ``\"fly\"`` or ``\"drive\"`` for one multi-stop leg.

    - No road route (ocean / island) → fly.
    - Straight-line distance ≥ FLY_MIN_STRAIGHT_KM → fly.
    - Otherwise → drive (when a road exists).
    - If routing failed but the hop is long → fly; if short → drive (client may
      still fall back to fly if OSRM has no geometry).
    """
    straight_km = haversine_m(from_latlon, to_latlon) / 1000.0
    road_ok = routing_ok and road_distance_m is not None
    no_road = routing_ok and road_distance_m is None

    if no_road or straight_km >= FLY_MIN_STRAIGHT_KM:
        return "fly"
    if road_ok:
        return "drive"
    if straight_km >= FLY_MIN_STRAIGHT_KM:
        return "fly"
    return "drive"
