"""Geodesy helpers for AIS tracks.

AIS positions are WGS84 lat/lon. For the short distances between consecutive
fixes on a coastal track the haversine great-circle distance is plenty accurate,
and far simpler than a full geodesic. Bearings are used to compare a vessel's
actual course over ground against where it is heading.
"""
from __future__ import annotations

import math

EARTH_R_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings, 0..180."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


# Metres per second per knot. AIS speed over ground is reported in knots.
MS_PER_KNOT = 0.514444


def knots_to_ms(knots: float) -> float:
    return knots * MS_PER_KNOT
