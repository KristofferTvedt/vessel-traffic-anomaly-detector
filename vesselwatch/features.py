"""Per-fix features shared by the model and any analysis.

One row per position transition, describing how a vessel moved between two
consecutive fixes. These are the same signals the rules in ``anomaly.py`` reason
about, laid out numerically so an unsupervised model can be run over them.
"""
from __future__ import annotations

import pandas as pd

from . import db
from .anomaly import _parse
from .config import Config
from .geo import angle_diff_deg, bearing_deg, haversine_m

FEATURES = ["dt_min", "implied_kn", "d_sog", "abs_cog_change", "gap_ratio"]


def _track_rows(fixes: list[dict]) -> list[dict]:
    usable = [f for f in fixes if f.get("msgtime") and f.get("lat") is not None]
    rows = []
    for prev, cur in zip(usable, usable[1:]):
        t0, t1 = _parse(prev["msgtime"]), _parse(cur["msgtime"])
        dt_min = (t1 - t0).total_seconds() / 60.0
        if dt_min <= 0:
            continue
        dist_m = haversine_m(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
        implied_kn = (dist_m / (dt_min * 60.0)) / 0.514444
        s0, s1 = prev.get("sog"), cur.get("sog")
        travelled = bearing_deg(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
        cog_change = (angle_diff_deg(travelled, prev["cog"])
                      if prev.get("cog") is not None else 0.0)
        rows.append({
            "mmsi": cur["mmsi"],
            "at_time": cur["msgtime"],
            "lat": cur["lat"], "lon": cur["lon"],
            "dt_min": dt_min,
            "implied_kn": implied_kn,
            "d_sog": (s1 - s0) if (s0 is not None and s1 is not None) else 0.0,
            "abs_cog_change": cog_change,
            "gap_ratio": dt_min / 2.0,  # relative to the ~2 min nominal poll
        })
    return rows


def build_frame(cfg: Config | None = None) -> pd.DataFrame:
    cfg = cfg or Config.load()
    conn = db.connect(cfg.db_path)
    try:
        rows: list[dict] = []
        for mmsi in db.mmsis(conn):
            rows.extend(_track_rows([dict(r) for r in db.track(conn, mmsi)]))
    finally:
        conn.close()
    return pd.DataFrame(rows)
