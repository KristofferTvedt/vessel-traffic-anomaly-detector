"""Synthetic AIS tracks with planted anomalies.

Two jobs. First, it validates the whole pipeline offline: the collector needs
live API credentials and the Kystverket loader needs downloaded files, but this
seeds a database with known-answer tracks so ``detect`` can be shown to fire on
exactly the behaviours it should, and miss the normal traffic. Second, it gives
the site a real-shaped ``incidents.json`` to render while genuine data accrues.

    python -m vesselwatch.sample            # seed synthetic tracks into the DB

Each planted vessel exercises one flag; a few clean transits are added so the
false-positive rate is visible. This is the AIS analogue of the ferry model's
"planted-signal" test: prove the code path is correct before trusting real data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db
from .collector import now_utc_iso
from .config import Config
from .geo import bearing_deg

# A nominal transit line across Korsfjorden, into Bergen (WGS84).
START = (60.16, 4.98)
END = (60.40, 5.32)


def _lerp(a, b, t):
    return a + (b - a) * t


def _leg(steps: int):
    for i in range(steps):
        t = i / max(steps - 1, 1)
        yield _lerp(START[0], END[0], t), _lerp(START[1], END[1], t)


def _cog_for(pts):
    """Reported course = bearing to the next point (a well-behaved transponder)."""
    cogs = []
    for i in range(len(pts)):
        j = min(i + 1, len(pts) - 1)
        cogs.append(bearing_deg(pts[i][0], pts[i][1], pts[j][0], pts[j][1]))
    return cogs


def _emit(conn, mmsi, name, ship_type, fixes, base: datetime):
    for f in fixes:
        ts = (base + timedelta(minutes=f["min"])).isoformat(timespec="seconds")
        db.upsert_position(conn, {
            "mmsi": mmsi, "name": name, "ship_type": ship_type,
            "lat": f["lat"], "lon": f["lon"], "sog": f["sog"],
            "cog": f.get("cog"), "heading": f.get("cog"),
            "msgtime": ts, "fetched_at": ts, "source": "synthetic",
        })


def seed(conn) -> dict:
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=6)
    pts = list(_leg(24))
    cogs = _cog_for(pts)
    counts = {}

    # --- clean transits: should raise nothing -------------------------------
    for k, mmsi in enumerate((257801001, 257801002)):
        fixes = [{"min": i * 3 + k, "lat": p[0], "lon": p[1],
                  "sog": 11.0, "cog": cogs[i]} for i, p in enumerate(pts)]
        _emit(conn, mmsi, f"CLEAN TRANSIT {k+1}", 70, fixes, base)
    counts["clean"] = 2

    # --- AIS gap: goes quiet mid-fjord for 35 min while underway ------------
    fixes = []
    for i, p in enumerate(pts):
        if 8 <= i <= 14:          # drop a run of fixes -> a long silence
            continue
        fixes.append({"min": i * 3, "lat": p[0], "lon": p[1],
                      "sog": 12.0, "cog": cogs[i]})
    _emit(conn, 257802001, "GONE DARK", 80, fixes, base)
    counts["ais_gap"] = 1

    # --- sudden stop: 9 kn -> 0 in 4 min, open water -----------------------
    fixes = [{"min": i * 3, "lat": p[0], "lon": p[1], "sog": 9.0, "cog": cogs[i]}
             for i, p in enumerate(pts[:10])]
    stop = pts[10]
    fixes.append({"min": 32, "lat": stop[0], "lon": stop[1], "sog": 0.1,
                  "cog": cogs[10]})
    _emit(conn, 257803001, "DEAD IN WATER", 70, fixes, base)
    counts["sudden_stop"] = 1

    # --- speed jump: a teleport of ~15 nm in 3 min -------------------------
    fixes = [{"min": i * 3, "lat": p[0], "lon": p[1], "sog": 10.0, "cog": cogs[i]}
             for i, p in enumerate(pts[:6])]
    fixes.append({"min": 18, "lat": 60.30, "lon": 5.10, "sog": 10.0, "cog": 90})
    _emit(conn, 257804001, "GHOST FIX", 60, fixes, base)
    counts["speed_jump"] = 1

    # --- route deviation: reports NE course but veers hard east ------------
    fixes = []
    for i, p in enumerate(pts[:10]):
        fixes.append({"min": i * 3, "lat": p[0], "lon": p[1],
                      "sog": 8.0, "cog": cogs[i]})
    # Next fix veers hard south-east off a NE-bound track while still claiming
    # the old heading: a ~105 deg divergence from its reported course.
    veer = (pts[9][0] - 0.03, pts[9][1] + 0.05)
    fixes.append({"min": 30, "lat": veer[0], "lon": veer[1], "sog": 8.0,
                  "cog": cogs[9]})
    _emit(conn, 257805001, "OFF CORRIDOR", 70, fixes, base)
    counts["route_deviation"] = 1

    return counts


def main() -> int:
    cfg = Config.load()
    conn = db.connect(cfg.db_path)
    try:
        counts = seed(conn)
        conn.commit()
    finally:
        conn.close()
    planted = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"{now_utc_iso()} seeded synthetic tracks ({planted})")
    print("Next: python -m vesselwatch.detect --summary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
