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
GAP_MAX_MINUTES = 90.0      # ...but past this it has just left our small AOI, not
                            # gone dark; a coverage shadow reappears sooner
UNDERWAY_KNOTS = 3.0        # above this a vessel is "moving", not moored/drifting
STOP_KNOTS = 0.5            # at/under this it has effectively stopped
STOP_FROM_KNOTS = 6.0       # a stop only counts if it was doing at least this
STOP_WINDOW_MIN = 12.0      # ...and dropped within this many minutes
SPEED_JUMP_MS = 25.0        # implied speed over this (~49 kn) is not a real ship
JUMP_MIN_DIST_M = 500.0     # ...but only a real leap counts; below this it's GPS
                            # jitter at full time resolution, not a teleport
COG_DEVIATION_DEG = 60.0    # sustained course change past this is a deviation
DEVIATION_MAX_DEG = 135.0   # ...but a near-180 is a reversal / bad COG, not a
                            # lane departure; those aren't what we're after
DEVIATION_MIN_KNOTS = 4.0   # only on a vessel actually making way
BERTH_RADIUS_M = 800.0      # don't flag stops within this of a known berth
CELL = 0.01                 # ~0.5-1 km grid used to learn busy stop zones
STOP_STAY_MIN = 8.0         # a real dead-in-water stop stays stopped this long;
                            # a slow-and-go at a quay resumes sooner
STOP_APPROACH_MIN = 10.0    # a real stop is preceded by the vessel actually
STOP_APPROACH_M = 500.0     # covering ground; a moored object with a bad SOG
                            # spike hasn't moved at all
VISIT_GAP_MIN = 20.0        # still-fixes separated by more than this are separate
                            # visits to a place
HABITUAL_VISITS = 2         # a place a vessel stops at this many separate times is
                            # its own berth / route endpoint (e.g. a ferry quay),
                            # not an anomaly
DEDUPE_GAP_MIN = 30.0       # same-kind flags for one vessel closer than this are
                            # one event (full time resolution retriggers a lot)

# AIS navigational status codes where a stop is expected, not anomalous.
MOORED_STATUSES = frozenset({1, 5, 6})  # 1 at anchor, 5 moored, 6 aground

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


def cell(lat: float, lon: float) -> tuple[float, float]:
    """Snap a position to the stop-zone grid."""
    return (round(lat / CELL) * CELL, round(lon / CELL) * CELL)


def _stays_stopped(usable: list[dict], stop_idx: int, stop_time: datetime) -> bool:
    """True if the vessel remains stopped (or goes silent) for STOP_STAY_MIN after
    a candidate stop, rather than resuming way. Distinguishes a genuine dead-in-
    water event from routine slow-and-go at a quay."""
    for k in range(stop_idx + 1, len(usable)):
        tk = _parse(usable[k]["msgtime"])
        if _minutes(stop_time, tk) > STOP_STAY_MIN:
            break
        if (usable[k].get("sog") or 0) >= UNDERWAY_KNOTS:
            return False
    return True


def _travelled_before(usable: list[dict], stop_idx: int, stop_time: datetime) -> float:
    """How far the vessel was from its stop position STOP_APPROACH_MIN earlier.
    Near zero means it never moved (a moored object with a bad SOG spike), not a
    vessel that was underway and then stopped."""
    stop = usable[stop_idx]
    ref = None
    for k in range(stop_idx - 1, -1, -1):
        ref = usable[k]
        if _minutes(_parse(usable[k]["msgtime"]), stop_time) >= STOP_APPROACH_MIN:
            break
    if ref is None:
        return 0.0
    return haversine_m(ref["lat"], ref["lon"], stop["lat"], stop["lon"])


def _habitual_stop_cells(usable: list[dict]) -> frozenset:
    """Grid cells this vessel stops at on more than one separate occasion: its own
    berths and route endpoints (a ferry's quays), where stopping is routine. A
    one-off stop somewhere it never otherwise stops is what we want to keep."""
    visits: dict[tuple, int] = {}
    last_seen: dict[tuple, datetime] = {}
    for f in usable:
        if (f.get("sog") if f.get("sog") is not None else 99) > STOP_KNOTS:
            continue
        cl = cell(f["lat"], f["lon"])
        t = _parse(f["msgtime"])
        prev = last_seen.get(cl)
        if prev is None or _minutes(prev, t) > VISIT_GAP_MIN:
            visits[cl] = visits.get(cl, 0) + 1
        last_seen[cl] = t
    return frozenset(cl for cl, v in visits.items() if v >= HABITUAL_VISITS)


def _dedupe(anoms: list["Anomaly"]) -> list["Anomaly"]:
    """Collapse same-kind flags for one vessel that fall within DEDUPE_GAP_MIN of
    each other into a single incident, keeping the strongest. One behaviour at
    full time resolution otherwise fires across many consecutive fixes."""
    kept: list[Anomaly] = []
    for kind in {a.kind for a in anoms}:
        group = sorted((a for a in anoms if a.kind == kind), key=lambda a: a.at_time)
        cluster: list[Anomaly] = []
        last: datetime | None = None
        for a in group:
            t = _parse(a.at_time)
            if last is not None and _minutes(last, t) > DEDUPE_GAP_MIN:
                kept.append(max(cluster, key=lambda x: x.score))
                cluster = []
            cluster.append(a)
            last = t
        if cluster:
            kept.append(max(cluster, key=lambda x: x.score))
    return kept


def _minutes(a: datetime, b: datetime) -> float:
    return abs((b - a).total_seconds()) / 60.0


def detect_track(fixes: list[dict], stop_zones: frozenset = frozenset(),
                 aoi=None) -> list[Anomaly]:
    """Flags for a single vessel's track (list of fix dicts, sorted by time).

    Each fix needs: mmsi, lat, lon, sog (knots), cog (deg, optional), msgtime.
    ``stop_zones`` is a set of grid cells (see ``cell``) where many vessels stop,
    learned from the data, so stops and port-basin manoeuvres there are ignored.
    ``aoi`` (an inset BBox) restricts detection to fixes comfortably inside the
    area; behaviour at the boundary is dominated by vessels leaving the box.
    """
    usable = [f for f in fixes if f.get("msgtime") and f.get("lat") is not None]
    if len(usable) < 2:
        return []

    mmsi = usable[0]["mmsi"]
    habitual = _habitual_stop_cells(usable)
    out: list[Anomaly] = []

    for i in range(len(usable) - 1):
        prev, cur = usable[i], usable[i + 1]
        t0, t1 = _parse(prev["msgtime"]), _parse(cur["msgtime"])
        dt_min = _minutes(t0, t1)
        if dt_min <= 0:
            continue
        if aoi is not None and not aoi.contains(cur["lon"], cur["lat"]):
            continue

        dist_m = haversine_m(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
        implied_ms = dist_m / (dt_min * 60.0)
        s0 = prev.get("sog")
        s1 = cur.get("sog")

        # 1) Impossible position jump -> a bad/spoofed fix. Gate on a real
        #    distance: at full time resolution a moored vessel's GPS scatters a
        #    few metres between fixes seconds apart, which computes to a huge
        #    implied speed but is only jitter. A genuine teleport moves hundreds
        #    of metres or more.
        if implied_ms > SPEED_JUMP_MS and dist_m >= JUMP_MIN_DIST_M:
            implied_kn = implied_ms / knots_to_ms(1)
            nm = dist_m / 1852
            out.append(Anomaly(
                mmsi, "speed_jump", cur["msgtime"], cur["lat"], cur["lon"],
                round(implied_kn, 1),
                f"{implied_kn:.0f} kn implied across a {nm:.1f} nm jump, "
                f"not physically plausible",
                {"impliedKn": round(implied_kn), "nm": round(nm, 1)},
            ))
            # A jump makes the other per-step checks meaningless; skip them.
            continue

        # 2) AIS gap: long silence while it had been underway. Bounded above so a
        #    vessel that simply left this small AOI and returned hours later
        #    isn't mislabelled "went dark".
        if (GAP_MINUTES <= dt_min <= GAP_MAX_MINUTES
                and (s0 or 0) >= UNDERWAY_KNOTS):
            out.append(Anomaly(
                mmsi, "ais_gap", cur["msgtime"], cur["lat"], cur["lon"],
                round(dt_min, 1),
                f"{dt_min:.0f} min silence while underway "
                f"({s0:.1f} kn before going quiet)",
                {"gapMin": round(dt_min), "sogBefore": round(s0, 1)},
            ))

        # 3) Sudden stop: fast -> near-still quickly, away from any berth and not
        #    reporting a moored/anchored status (where stopping is normal).
        if (s0 is not None and s1 is not None
                and s0 >= STOP_FROM_KNOTS and s1 <= STOP_KNOTS
                and dt_min <= STOP_WINDOW_MIN
                and cur.get("nav_status") not in MOORED_STATUSES
                and cell(cur["lat"], cur["lon"]) not in stop_zones
                and cell(cur["lat"], cur["lon"]) not in habitual
                and not _near_berth(cur["lat"], cur["lon"])
                and _travelled_before(usable, i + 1, t1) >= STOP_APPROACH_M
                and _stays_stopped(usable, i + 1, t1)):
            out.append(Anomaly(
                mmsi, "sudden_stop", cur["msgtime"], cur["lat"], cur["lon"],
                round(s0 - s1, 1),
                f"dropped {s0:.1f} -> {s1:.1f} kn in {dt_min:.0f} min, "
                f"then stayed stopped, not at a known berth",
                {"fromKn": round(s0, 1), "toKn": round(s1, 1),
                 "dtMin": round(dt_min)},
            ))

        # 4) Route deviation: heading swings hard while still making way.
        #    Compare the actual bearing travelled against the vessel's reported
        #    course before the leg; a big, real-motion divergence is the signal.
        if (s1 is not None and s1 >= DEVIATION_MIN_KNOTS
                and prev.get("cog") is not None and dist_m > 100
                and cell(cur["lat"], cur["lon"]) not in stop_zones):
            travelled = bearing_deg(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
            dev = angle_diff_deg(travelled, prev["cog"])
            if COG_DEVIATION_DEG <= dev <= DEVIATION_MAX_DEG:
                out.append(Anomaly(
                    mmsi, "route_deviation", cur["msgtime"], cur["lat"], cur["lon"],
                    round(dev, 1),
                    f"course {dev:.0f}° off its reported heading while making "
                    f"{s1:.1f} kn",
                    {"devDeg": round(dev), "sog": round(s1, 1)},
                ))

    return _dedupe(out)
