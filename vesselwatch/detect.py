"""Run the rule-based flags over every stored track and persist the results.

    python -m vesselwatch.detect            # scan all vessels, write anomalies
    python -m vesselwatch.detect --summary  # ...and print a per-kind tally

This is the batch pass. The collector accrues positions; this turns them into
flags. Kept separate so detection can be re-run with new thresholds without
re-collecting anything.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

from . import anomaly, db
from .collector import now_utc_iso
from .config import Config


STOP_ZONE_MIN_VESSELS = 5   # a cell where this many distinct vessels stop is a
                            # de-facto berth/anchorage, not an anomaly


def learn_stop_zones(conn) -> frozenset:
    """Grid cells where many different vessels sit still: terminals, anchorages,
    pilot-boarding areas. Learned from the data so we don't hard-code berths."""
    rows = conn.execute(
        "SELECT lat, lon, mmsi FROM positions WHERE sog <= ?",
        (anomaly.STOP_KNOTS,),
    ).fetchall()
    by_cell: dict[tuple, set] = {}
    for r in rows:
        by_cell.setdefault(anomaly.cell(r["lat"], r["lon"]), set()).add(r["mmsi"])
    return frozenset(c for c, ships in by_cell.items()
                     if len(ships) >= STOP_ZONE_MIN_VESSELS)


def run(cfg: Config) -> Counter:
    conn = db.connect(cfg.db_path)
    tally: Counter = Counter()
    try:
        detected = now_utc_iso()
        conn.execute("DELETE FROM anomalies")  # full recompute each run
        stop_zones = learn_stop_zones(conn)
        inner = cfg.aoi.inset(0.005)  # ~500 m in: drops literal box-edge crossings
        for mmsi in db.mmsis(conn):
            fixes = [dict(r) for r in db.track(conn, mmsi)]
            for a in anomaly.detect_track(fixes, stop_zones, inner):
                db.upsert_anomaly(conn, {
                    "mmsi": a.mmsi, "kind": a.kind, "at_time": a.at_time,
                    "lat": a.lat, "lon": a.lon, "score": a.score,
                    "detail": a.detail, "params": json.dumps(a.params),
                    "detected_at": detected,
                })
                tally[a.kind] += 1
        conn.commit()
    finally:
        conn.close()
    return tally


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true", help="print a per-kind tally")
    args = ap.parse_args()

    cfg = Config.load()
    tally = run(cfg)
    total = sum(tally.values())
    print(f"{now_utc_iso()} flagged {total} anomalies across "
          f"{len(tally)} kind(s)")
    if args.summary:
        for kind, n in tally.most_common():
            print(f"  {kind:<16} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
