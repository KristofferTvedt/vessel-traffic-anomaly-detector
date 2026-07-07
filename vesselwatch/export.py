"""Export a curated set of flagged incidents for the portfolio site.

The live map on the site shows current traffic; the anomaly story is carried by
this file: a handful of real, validated incidents with the track that produced
them. Selection favours the clearest example of each kind (highest score),
because a portfolio wants a legible gallery, not a firehose.

    python -m vesselwatch.export --out incidents.json --per-kind 1

Output shape (consumed by VesselDemo.vue):
{
  "generated": ISO,
  "aoi": {name, bbox:[minLon,minLat,maxLon,maxLat]},
  "incidents": [{
     mmsi, name, shipType, kind, at, detail, score,
     flag: [lat, lon],
     track: [[lat, lon], ...]        # ordered, the leg around the event
  }]
}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db
from .anomaly import _parse
from .collector import now_utc_iso
from .config import Config

TRACK_WINDOW_MIN = 90.0   # minutes of track either side of the flag to include
TRACK_MAX_POINTS = 120    # downsample: full-resolution AIS is far denser than a
                          # map polyline needs, and it bloats the payload


def _track_around(conn, mmsi: int, at_time: str) -> list[list[float]]:
    center = _parse(at_time)
    pts = []
    for r in db.track(conn, mmsi):
        if abs((_parse(r["msgtime"]) - center).total_seconds()) / 60.0 <= TRACK_WINDOW_MIN:
            pts.append([round(r["lat"], 5), round(r["lon"], 5)])
    if len(pts) > TRACK_MAX_POINTS:
        stride = len(pts) // TRACK_MAX_POINTS + 1
        pts = pts[::stride] + [pts[-1]]  # keep the last point so the line ends true
    return pts


def select(conn, per_kind: int) -> list[dict]:
    kinds = [r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM anomalies ORDER BY kind")]
    chosen = []
    for kind in kinds:
        rows = conn.execute(
            "SELECT * FROM anomalies WHERE kind = ? ORDER BY score DESC",
            (kind,),
        ).fetchall()
        # One incident per vessel per kind: a single faulty transponder shouldn't
        # fill the gallery with copies of itself.
        seen: set[int] = set()
        rows = [a for a in rows if not (a["mmsi"] in seen or seen.add(a["mmsi"]))][:per_kind]
        for a in rows:
            pos = conn.execute(
                "SELECT name, ship_type FROM positions WHERE mmsi = ? "
                "AND name IS NOT NULL LIMIT 1", (a["mmsi"],)).fetchone()
            chosen.append({
                "mmsi": a["mmsi"],
                "name": (pos["name"] if pos else None),
                "shipType": (pos["ship_type"] if pos else None),
                "kind": a["kind"],
                "at": a["at_time"],
                "detail": a["detail"],
                "params": json.loads(a["params"]) if a["params"] else {},
                "score": a["score"],
                "flag": [round(a["lat"], 5), round(a["lon"], 5)],
                "track": _track_around(conn, a["mmsi"], a["at_time"]),
            })
    return chosen


def resolve_names(cfg: Config, incidents: list[dict]) -> int:
    """Fill in vessel names from BarentsWatch for incidents that have none.

    Kystverket's historical feed carries MMSI but not vessel names; the live AIS
    feed does. Most coastal vessels transmit continuously, so a current snapshot
    resolves the MMSIs in the gallery. Best-effort: no creds or no match just
    leaves the MMSI showing.
    """
    if not (cfg.bw_client_id and cfg.bw_client_secret):
        return 0
    from . import barentswatch
    try:
        feed = barentswatch.from_config(cfg).latest_positions()
    except Exception as exc:  # names are a nice-to-have, never fail the export
        print(f"WARN name lookup skipped: {exc}")
        return 0
    by_mmsi = {int(r["mmsi"]): (r.get("name") or "").strip()
               for r in feed if isinstance(r, dict) and r.get("mmsi") is not None}
    filled = 0
    for inc in incidents:
        if not inc.get("name") and by_mmsi.get(inc["mmsi"]):
            inc["name"] = by_mmsi[inc["mmsi"]]
            filled += 1
    return filled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="incidents.json")
    ap.add_argument("--per-kind", type=int, default=1,
                    help="how many examples of each anomaly kind to include")
    ap.add_argument("--names", action="store_true",
                    help="resolve vessel names from the live BarentsWatch feed")
    args = ap.parse_args()

    cfg = Config.load()
    conn = db.connect(cfg.db_path)
    try:
        incidents = select(conn, args.per_kind)
    finally:
        conn.close()

    if args.names:
        n = resolve_names(cfg, incidents)
        print(f"resolved {n} vessel name(s) from the live feed")

    payload = {
        "generated": now_utc_iso(),
        "aoi": {
            "name": cfg.aoi_name,
            "bbox": [cfg.aoi.min_lon, cfg.aoi.min_lat,
                     cfg.aoi.max_lon, cfg.aoi.max_lat],
        },
        "incidents": incidents,
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {len(incidents)} incident(s) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
