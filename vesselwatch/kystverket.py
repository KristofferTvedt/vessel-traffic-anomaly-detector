"""Load historical AIS from Kystverket exports into the same positions table.

Kystverket's open AIS (Historisk AIS, https://hais.kystverket.no/, NLOD licence)
is used offline to tune and validate the anomaly thresholds against real traffic,
including windows where something actually happened. It is not a live source.

HAIS ships Parquet; older tools shipped CSV. Both are handled. Rather than
hard-code one header layout we map known column aliases onto our canonical
fields (HAIS uses ``date_time_utc`` / ``speed_over_ground`` / ``true_heading``,
already covered below). If a file uses names we don't recognise, ``COLUMN_ALIASES``
is the one place to extend. HAIS positions are EPSG:4326 degrees; positions that
instead arrive as raw 1/10000-minute integers are handled by ``--scale-minutes``.
Note HAIS has no vessel name/type in the position feed, so those stay null.
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


def _iso(value) -> str | None:
    """Normalise a timestamp (str or pandas/py datetime) to an ISO UTC string."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value.strip()
    # pandas Timestamp / datetime: assume UTC when tz-naive (HAIS is UTC).
    ts = value
    if getattr(ts, "tzinfo", None) is None and hasattr(ts, "tz_localize"):
        ts = ts.tz_localize("UTC")
    return ts.isoformat()


def _store(conn, cols: dict, get, *, scale_minutes: bool) -> bool:
    """Upsert one position from a canonical->column map and a getter callable."""
    lat = _num(get(cols["lat"]))
    lon = _num(get(cols["lon"]))
    mmsi = get(cols["mmsi"])
    msgtime = _iso(get(cols["msgtime"]))
    if lat is None or lon is None or mmsi in (None, "") or not msgtime:
        return False
    if scale_minutes:  # 1/10000 min -> decimal degrees
        lat /= 600_000.0
        lon /= 600_000.0
    name = (str(get(cols["name"])).strip() or None) if "name" in cols else None
    st = _num(get(cols["ship_type"])) if "ship_type" in cols else None
    db.upsert_position(conn, {
        "mmsi": int(float(mmsi)),
        "name": name,
        "ship_type": int(st) if st is not None else None,
        "lat": lat,
        "lon": lon,
        "sog": _num(get(cols["sog"])) if "sog" in cols else None,
        "cog": _num(get(cols["cog"])) if "cog" in cols else None,
        "heading": _num(get(cols["heading"])) if "heading" in cols else None,
        "msgtime": msgtime,
        "fetched_at": msgtime,
        "source": "kystverket",
    })
    return True


def load_csv(conn, path: Path, *, scale_minutes: bool = False) -> int:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        # Sniff the delimiter (older exports are sometimes ';'-separated).
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
            if _store(conn, cols, lambda c: r.get(c), scale_minutes=scale_minutes):
                n += 1
        return n


def load_parquet(conn, path: Path, *, scale_minutes: bool = False) -> int:
    import pandas as pd  # local import so CSV-only use needs no pyarrow

    df = pd.read_parquet(path)
    cols = _resolve(list(df.columns))
    n = 0
    # itertuples is fast; index=False keeps positional access off, so use a dict.
    for rec in df.to_dict("records"):
        val = {k: (None if pd.isna(v) else v) for k, v in rec.items()}
        if _store(conn, cols, lambda c: val.get(c), scale_minutes=scale_minutes):
            n += 1
    return n


def load_file(conn, path: Path, *, scale_minutes: bool = False) -> int:
    if path.suffix.lower() == ".parquet":
        return load_parquet(conn, path, scale_minutes=scale_minutes)
    return load_csv(conn, path, scale_minutes=scale_minutes)


def main() -> int:
    ap = argparse.ArgumentParser(description="Load Kystverket AIS export(s) into SQLite.")
    ap.add_argument("paths", nargs="*",
                    help="parquet/csv files; defaults to everything in RAW_DIR")
    ap.add_argument("--scale-minutes", action="store_true",
                    help="positions are 1/10000-minute integers, not degrees")
    args = ap.parse_args()

    cfg = Config.load()
    files = [Path(p) for p in args.paths] or (
        sorted(cfg.raw_dir.glob("*.parquet")) + sorted(cfg.raw_dir.glob("*.csv")))
    if not files:
        print(f"No files given and no .parquet/.csv found in {cfg.raw_dir}.")
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
