"""Nearest-airport lookup from a bundled OurAirports extract.

The CSV in ``mmaps/data/airports.csv`` is filtered offline from the public
OurAirports dataset to medium/large airports with scheduled service — real
passenger airports, not tiny strips or heliports. Lookup is pure distance
(haversine); no network calls.
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mmaps.route import haversine_m

_DATA_PATH = Path(__file__).resolve().parent / "data" / "airports.csv"

# Prefer larger airports when two are almost equally close (within this margin).
_TIE_MARGIN_M = 25_000.0
_TYPE_RANK = {"large_airport": 0, "medium_airport": 1}


@lru_cache(maxsize=1)
def load_airports() -> Tuple[Dict, ...]:
    """Load the bundled airport list once (immutable tuples for cacheability)."""
    if not _DATA_PATH.is_file():
        raise FileNotFoundError(
            f"Airport dataset missing at {_DATA_PATH}. "
            "Re-run the project setup / restore mmaps/data/airports.csv."
        )
    rows: List[Dict] = []
    with _DATA_PATH.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({
                "name": row.get("name") or "Airport",
                "iata": (row.get("iata") or "").strip(),
                "icao": (row.get("icao") or "").strip(),
                "lat": lat,
                "lon": lon,
                "municipality": (row.get("municipality") or "").strip(),
                "country": (row.get("country") or "").strip(),
                "type": (row.get("type") or "").strip(),
            })
    if not rows:
        raise RuntimeError("Airport dataset is empty.")
    return tuple(rows)


def airport_label(airport: dict) -> str:
    """Human label, e.g. 'CDG · Charles de Gaulle Airport'."""
    code = airport.get("iata") or airport.get("icao") or ""
    name = airport.get("name") or "Airport"
    if code:
        return f"{code} · {name}"
    return name


def nearest_airport(lat: float, lon: float) -> Optional[dict]:
    """Return the nearest medium/large scheduled airport to (lat, lon).

    Pure offline scan (~3k rows). On a near-tie, prefer large_airport over
    medium. Returns None only if the dataset failed to load / is empty.
    """
    airports = load_airports()
    target = (lat, lon)
    best = None
    best_dist = float("inf")
    best_rank = 99

    for ap in airports:
        d = haversine_m(target, (ap["lat"], ap["lon"]))
        rank = _TYPE_RANK.get(ap.get("type") or "", 9)
        if d < best_dist - _TIE_MARGIN_M:
            best, best_dist, best_rank = ap, d, rank
        elif abs(d - best_dist) <= _TIE_MARGIN_M and rank < best_rank:
            best, best_dist, best_rank = ap, d, rank
        elif d < best_dist and rank == best_rank:
            best, best_dist, best_rank = ap, d, rank

    if best is None:
        return None
    out = dict(best)
    out["distance_m"] = best_dist
    out["label"] = airport_label(out)
    return out
