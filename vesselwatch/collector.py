"""One live collection pass: fetch AIS positions in the AOI, store to SQLite.

Run on a schedule (Windows Task Scheduler / cron), e.g. every 2 minutes:

    python -m vesselwatch.collector

Each pass records the latest fix per vessel currently inside the area of
interest. Repeated passes accrue a per-vessel track over time, which is what the
anomaly logic needs: a single snapshot can't tell you a ship stopped, only a
sequence can. UNIQUE(mmsi, msgtime) makes re-polling the same message idempotent.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import barentswatch, db
from .config import Config


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_once(cfg: Config) -> int:
    client = barentswatch.from_config(cfg)
    fetched = now_utc_iso()
    positions = barentswatch.positions_in(client, cfg.aoi)

    conn = db.connect(cfg.db_path)
    stored = 0
    try:
        for p in positions:
            if not p.get("msgtime"):
                continue
            p.update(fetched_at=fetched, source="barentswatch")
            db.upsert_position(conn, p)
            stored += 1
        conn.commit()
    finally:
        conn.close()
    return stored


def main() -> int:
    cfg = Config.load()
    try:
        stored = run_once(cfg)
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"{now_utc_iso()} aoi={cfg.aoi_name} stored={stored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
