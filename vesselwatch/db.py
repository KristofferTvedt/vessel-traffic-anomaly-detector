"""SQLite storage for AIS position reports.

One table does the work:

  positions — one row per (mmsi, msgtime) fix. A vessel's track is just its rows
              ordered by time. Static fields (name, ship_type) are denormalised
              onto each row because BarentsWatch's combined feed already carries
              them; no separate voyage table needed for this scope.

Anomaly flags are written back to ``anomalies``, keyed to the position that
triggered them, so a run is reproducible and the export step can join a flag to
the surrounding track.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi        INTEGER NOT NULL,
    name        TEXT,
    ship_type   INTEGER,
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    sog         REAL,                       -- speed over ground, knots
    cog         REAL,                       -- course over ground, degrees
    heading     REAL,                       -- true heading, degrees (511 = n/a)
    nav_status  INTEGER,                    -- AIS nav status (1 anchor, 5 moored, ...)
    msgtime     TEXT    NOT NULL,           -- ISO UTC, from the AIS message
    fetched_at  TEXT    NOT NULL,           -- ISO UTC, when we polled
    source      TEXT    NOT NULL,           -- 'barentswatch' | 'kystverket'
    UNIQUE(mmsi, msgtime)
);

CREATE TABLE IF NOT EXISTS anomalies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi        INTEGER NOT NULL,
    kind        TEXT    NOT NULL,           -- 'ais_gap' | 'sudden_stop' | ...
    at_time     TEXT    NOT NULL,           -- ISO UTC of the triggering fix
    lat         REAL,
    lon         REAL,
    score       REAL,                       -- how far past threshold / model score
    detail      TEXT,                       -- human-readable one-liner (English)
    params      TEXT,                       -- JSON structured values for the UI
    detected_at TEXT    NOT NULL,
    UNIQUE(mmsi, kind, at_time)
);

CREATE INDEX IF NOT EXISTS ix_pos_mmsi_time ON positions(mmsi, msgtime);
CREATE INDEX IF NOT EXISTS ix_anom_kind ON anomalies(kind);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def upsert_position(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO positions (mmsi, name, ship_type, lat, lon, sog, cog,
            heading, nav_status, msgtime, fetched_at, source)
        VALUES (:mmsi, :name, :ship_type, :lat, :lon, :sog, :cog,
            :heading, :nav_status, :msgtime, :fetched_at, :source)
        ON CONFLICT(mmsi, msgtime) DO UPDATE SET
            name=coalesce(excluded.name, positions.name),
            ship_type=coalesce(excluded.ship_type, positions.ship_type),
            sog=excluded.sog, cog=excluded.cog, heading=excluded.heading,
            nav_status=excluded.nav_status
        """,
        {**{k: None for k in ("name", "ship_type", "sog", "cog", "heading",
                              "nav_status")}, **row},
    )


def upsert_anomaly(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO anomalies (mmsi, kind, at_time, lat, lon, score, detail,
            params, detected_at)
        VALUES (:mmsi, :kind, :at_time, :lat, :lon, :score, :detail,
            :params, :detected_at)
        ON CONFLICT(mmsi, kind, at_time) DO UPDATE SET
            score=excluded.score, detail=excluded.detail,
            params=excluded.params, detected_at=excluded.detected_at
        """,
        row,
    )


def track(conn: sqlite3.Connection, mmsi: int) -> list[sqlite3.Row]:
    """All fixes for one vessel, oldest first."""
    return conn.execute(
        "SELECT * FROM positions WHERE mmsi = ? ORDER BY msgtime", (mmsi,)
    ).fetchall()


def mmsis(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT DISTINCT mmsi FROM positions ORDER BY mmsi").fetchall()
    return [r[0] for r in rows]
