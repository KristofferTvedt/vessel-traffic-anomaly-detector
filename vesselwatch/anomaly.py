"""The flag logic: what counts as anomalous vessel behaviour.

This is the part that carries the project. Everything else is plumbing around
these rules. The design principle is *legible thresholds first*: each flag is a
statement a harbourmaster would recognise, with a number you can defend, not an
opaque model score. An isolation forest (see ``model.py``) runs as a second pass
over the same features, but the rules below are the baseline it has to beat.

The four flags, and why each is a real maritime safety/security concern:

* ``ais_gap`` — a vessel that was underway stops transmitting for a long stretch
  then reappears. The classic "going dark". Often innocent (receiver coverage
  holes near terrain), sometimes not (transponder switched off to hide a
  rendezvous or a fishing incursion). We flag it and let a human judge.
* ``sudden_stop`` — speed collapses from underway to ~0 away from any known berth
  or anchorage. Could be a breakdown, a grounding, or a drift.
* ``speed_jump`` — the distance between two consecutive fixes implies a speed no
  vessel of this kind can do. Almost always a bad/spoofed position, occasionally
  a duplicated MMSI. The GPS-spoofing tell.
* ``route_deviation`` — course over ground swings hard and stays swung, i.e. the
  vessel leaves the corridor it had been holding. Worth a look on a monitored lane.

Thresholds are deliberately conservative (favour precision over recall): a
portfolio demo of false alarms is worse than one that misses a marginal case.
Tuned against Kystverket history, see working-log.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .geo import angle_diff_deg, bearing_deg, haversine_m, knots_to_ms

# --- thresholds -------------------------------------------------------------
GAP_MINUTES = 20.0          # silence longer than this, while underway, is a gap
UNDERWAY_KNOTS = 3.0        # above this a vessel is "moving", not moored/drifting
STOP_KNOTS = 0.5            # at/under this it has effectively stopped
STOP_FROM_KNOTS = 6.0       # a stop only counts if it was doing at least this
STOP_WINDOW_MIN = 12.0      # ...and dropped within this many minutes
SPEED_JUMP_MS = 25.0        # implied speed over this (~49 kn) is not a real ship
COG_DEVIATION_DEG = 60.0    # sustained course change past this is a deviation
DEVIATION_MIN_KNOTS = 4.0   # only on a vessel actually making way
BERTH_RADIUS_M = 800.0      # don't flag stops within this of a known berth

# Known berths / anchorages in the Bergen approaches where stopping is normal.
# (lat, lon). Kept short and explicit; extend from the data as needed.
KNOWN_BERTHS: tuple[tuple[float, float], ...] = (
    (60.3913, 5.3242),   # Bergen port (Skoltegrunnskaien area)
    (60.3980, 5.3050),   # Dokken / Jekteviken
    (60.2980, 5.2180),   # Sotra / Ågotnes base
)


@dataclass
class Anomaly:
    mmsi: int
    kind: str
    at_time: str
    lat: float
    lon: float
    score: float
    detail: str            # English one-liner (fallback / log)
    params: dict           # structured values so the UI can localise the text


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _near_berth(lat: float, lon: float) -> bool:
    return any(haversine_m(lat, lon, blat, blon) <= BERTH_RADIUS_M
              for blat, blon in KNOWN_BERTHS)


def _minutes(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 60.0


def detect_track(fixes: list[dict]) -> list[Anomaly]:
    """Flags for a single vessel's track (list of fix dicts, sorted by time).

    Each fix needs: mmsi, lat, lon, sog (knots), cog (deg, optional), msgtime.
    """
    usable = [f for f in fixes if f.get("msgtime") and f.get("lat") is not None]
    if len(usable) < 2:
        return []

    mmsi = usable[0]["mmsi"]
    out: list[Anomaly] = []

    for prev, cur in zip(usable, usable[1:]):
        t0, t1 = _parse(prev["msgtime"]), _parse(cur["msgtime"])
        dt_min = _minutes(t0, t1)
        if dt_min <= 0:
            continue

        dist_m = haversine_m(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
        implied_ms = dist_m / (dt_min * 60.0)
        s0 = prev.get("sog")
        s1 = cur.get("sog")

        # 1) Impossible position jump -> almost certainly a bad/spoofed fix.
        if implied_ms > SPEED_JUMP_MS:
            implied_kn = implied_ms / knots_to_ms(1)
            nm = dist_m / 1852
            out.append(Anomaly(
                mmsi, "speed_jump", cur["msgtime"], cur["lat"], cur["lon"],
                round(implied_kn, 1),
                f"{implied_kn:.0f} kn implied over {dt_min:.0f} min "
                f"({nm:.1f} nm), not physically plausible",
                {"impliedKn": round(implied_kn), "dtMin": round(dt_min),
                 "nm": round(nm, 1)},
            ))
            # A jump makes the other per-step checks meaningless; skip them.
            continue

        # 2) AIS gap: long silence while it had been underway.
        if dt_min >= GAP_MINUTES and (s0 or 0) >= UNDERWAY_KNOTS:
            out.append(Anomaly(
                mmsi, "ais_gap", cur["msgtime"], cur["lat"], cur["lon"],
                round(dt_min, 1),
                f"{dt_min:.0f} min silence while underway "
                f"({s0:.1f} kn before going quiet)",
                {"gapMin": round(dt_min), "sogBefore": round(s0, 1)},
            ))

        # 3) Sudden stop: fast -> near-still quickly, away from any berth.
        if (s0 is not None and s1 is not None
                and s0 >= STOP_FROM_KNOTS and s1 <= STOP_KNOTS
                and dt_min <= STOP_WINDOW_MIN
                and not _near_berth(cur["lat"], cur["lon"])):
            out.append(Anomaly(
                mmsi, "sudden_stop", cur["msgtime"], cur["lat"], cur["lon"],
                round(s0 - s1, 1),
                f"dropped {s0:.1f} -> {s1:.1f} kn in {dt_min:.0f} min, "
                f"not at a known berth",
                {"fromKn": round(s0, 1), "toKn": round(s1, 1),
                 "dtMin": round(dt_min)},
            ))

        # 4) Route deviation: heading swings hard while still making way.
        #    Compare the actual bearing travelled against the vessel's reported
        #    course before the leg; a big, real-motion divergence is the signal.
        if (s1 is not None and s1 >= DEVIATION_MIN_KNOTS
                and prev.get("cog") is not None and dist_m > 100):
            travelled = bearing_deg(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
            dev = angle_diff_deg(travelled, prev["cog"])
            if dev >= COG_DEVIATION_DEG:
                out.append(Anomaly(
                    mmsi, "route_deviation", cur["msgtime"], cur["lat"], cur["lon"],
                    round(dev, 1),
                    f"course {dev:.0f}° off its reported heading while making "
                    f"{s1:.1f} kn",
                    {"devDeg": round(dev), "sog": round(s1, 1)},
                ))

    return out
