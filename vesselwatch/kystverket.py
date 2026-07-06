"""Load historical AIS from Kystverket exports into the same positions table.

Kystverket's open AIS (https://ais-public.kystverket.no/, NLOD licence, no auth)
is used offline to tune and validate the anomaly thresholds against real traffic,
including windows where something actually happened. It is not a live source.

The export format is not perfectly stable across their tools, so instead of
hard-coding one header layout we map a set of known column aliases onto our
canonical fields. If a file uses names we don't recognise, ``COLUMN_ALIASES``
is the one place to extend. Positions arriving as raw 1/10000-minute integers
(unusual for the CSV exports, common in decoded NMEA) are handled by
``--scale-minutes``.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from . import db
from .config import Config

# canonical field -> accepted source-column names (lowercased)
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "mmsi": ("mmsi",),
    "msgtime": ("date_time_utc", "datetime_utc", "timestamp", "msgtime",
                "time", "date_time"),
    "lat": ("latitude", "lat", "y"),
    "lon": ("longitude", "lon", "long", "x"),
    "sog": ("sog", "speedoverground", "speed_over_ground", "speed"),
    "cog": ("cog", "courseoverground", "course_over_ground", "course"),
    "heading": ("true_heading", "trueheading", "heading"),
    "name": ("name", "shipname", "ship_name"),
    "ship_type": ("ship_type", "shiptype", "type"),
}


def _resolve(header: list[str]) -> dict[str, str]:
    """Map our canonical fields to the file's actual column names."""
    lower = {h.lower().strip(): h for h in header}
    mapping: dict[str, str] = {}
    for canon, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in lower:
                mapping[canon] = lower[a]
                break
    missing = {"mmsi", "msgtime", "lat", "lon"} - mapping.keys()
    if missing:
        raise RuntimeError(
            f"AIS export is missing required columns {sorted(missing)}. "
            f"Header was: {header}. Extend COLUMN_ALIASES if the names differ."
        )
    return mapping


def _num(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_file(conn, path: Path, *, scale_minutes: bool = False) -> int:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        # Sniff the delimiter (Kystverket exports are sometimes ';'-separated).
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        cols = _resolve(reader.fieldnames or [])

        n = 0
        for r in reader:
            lat = _num(r.get(cols["lat"]))
            lon = _num(r.get(cols["lon"]))
            mmsi = r.get(cols["mmsi"])
            msgtime = r.get(cols["msgtime"])
            if lat is None or lon is None or not mmsi or not msgtime:
                continue
            if scale_minutes:  # 1/10000 min -> decimal degrees
                lat /= 600_000.0
                lon /= 600_000.0
            db.upsert_position(conn, {
                "mmsi": int(float(mmsi)),
                "name": (r.get(cols["name"]) or "").strip() or None if "name" in cols else None,
                "ship_type": int(float(r[cols["ship_type"]])) if cols.get("ship_type") and _num(r.get(cols["ship_type"])) is not None else None,
                "lat": lat,
                "lon": lon,
                "sog": _num(r.get(cols["sog"])) if "sog" in cols else None,
                "cog": _num(r.get(cols["cog"])) if "cog" in cols else None,
                "heading": _num(r.get(cols["heading"])) if "heading" in cols else None,
                "msgtime": msgtime.strip(),
                "fetched_at": msgtime.strip(),
                "source": "kystverket",
            })
            n += 1
        return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Load Kystverket AIS export(s) into SQLite.")
    ap.add_argument("paths", nargs="*", help="CSV files; defaults to everything in RAW_DIR")
    ap.add_argument("--scale-minutes", action="store_true",
                    help="positions are 1/10000-minute integers, not degrees")
    args = ap.parse_args()

    cfg = Config.load()
    files = [Path(p) for p in args.paths] or sorted(cfg.raw_dir.glob("*.csv"))
    if not files:
        print(f"No CSV files given and none found in {cfg.raw_dir}.")
        return 1

    conn = db.connect(cfg.db_path)
    total = 0
    try:
        for f in files:
            n = load_file(conn, f, scale_minutes=args.scale_minutes)
            conn.commit()
            print(f"{f.name}: {n} positions")
            total += n
    finally:
        conn.close()
    print(f"Loaded {total} positions from {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
