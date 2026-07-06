"""Is the live collector still alive?

Prints OK / STALE based on how long ago the last position was stored, and exits
non-zero when stale so a scheduler can act on it.

    python -m vesselwatch.healthcheck            # 15 min default threshold
    python -m vesselwatch.healthcheck --max 30
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from . import db
from .config import Config


def latest_write(conn) -> datetime | None:
    row = conn.execute(
        "SELECT max(fetched_at) FROM positions WHERE source = 'barentswatch'"
    ).fetchone()[0]
    return datetime.fromisoformat(row) if row else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=15,
                    help="minutes before the collector is considered stale")
    args = ap.parse_args()

    cfg = Config.load()
    conn = db.connect(cfg.db_path)
    try:
        last = latest_write(conn)
    finally:
        conn.close()

    if last is None:
        print("STALE: no live positions yet, collector has never written.")
        return 1

    age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    stamp = last.astimezone().strftime("%Y-%m-%d %H:%M")
    if age_min > args.max:
        print(f"STALE: last write {stamp} local ({age_min:.0f} min ago, "
              f"threshold {args.max}). Check the VesselWatchCollector task.")
        return 1
    print(f"OK: last write {stamp} local ({age_min:.0f} min ago).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
