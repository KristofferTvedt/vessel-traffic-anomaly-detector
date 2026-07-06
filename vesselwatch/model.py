"""Isolation-forest second pass over the per-fix features.

The rules in ``anomaly.py`` are the product. This exists to answer one honest
question a reviewer will ask: does an unsupervised model find anything the rules
miss? Isolation forest is the right tool for "flag the odd ones out" without
labels, and it's cheap. It is emphatically *not* the headline; per the project
note, don't over-engineer. Kept as a comparison, not a replacement.

    python -m vesselwatch.model

Prints how the forest's outliers overlap the rule flags. High overlap = the
rules already cover the space; extra forest-only points are candidates worth
eyeballing (and possibly a new rule).
"""
from __future__ import annotations

from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import db
from .config import Config
from .features import FEATURES, build_frame

CONTAMINATION = 0.02   # expect ~2% of transitions to look unusual
MIN_ROWS = 200         # below this the forest is just memorising noise


def _pipeline() -> Pipeline:
    return Pipeline([
        ("scale", StandardScaler()),
        ("iforest", IsolationForest(
            contamination=CONTAMINATION, random_state=0, n_estimators=200)),
    ])


def main() -> int:
    cfg = Config.load()
    df = build_frame(cfg)
    if df.empty:
        print("No positions yet, nothing to model.")
        return 0

    n = len(df)
    print(f"transitions={n}  vessels={df['mmsi'].nunique()}")
    if n < MIN_ROWS:
        print(f"SCAFFOLD READY — need >={MIN_ROWS} transitions for a meaningful "
              f"forest (have {n}). Re-run once more data has accrued.")
        return 0

    pipe = _pipeline().fit(df[FEATURES])
    df = df.assign(outlier=pipe.predict(df[FEATURES]) == -1)
    flagged = df[df["outlier"]]
    print(f"forest outliers: {len(flagged)} ({len(flagged) / n:.1%})")

    conn = db.connect(cfg.db_path)
    try:
        ruled = {
            (r["mmsi"], r["at_time"])
            for r in conn.execute("SELECT mmsi, at_time FROM anomalies")
        }
    finally:
        conn.close()

    keys = set(zip(flagged["mmsi"], flagged["at_time"]))
    overlap = len(keys & ruled)
    print(f"overlap with rule flags: {overlap}/{len(keys)} forest points already "
          f"caught by a rule")
    forest_only = keys - ruled
    if forest_only:
        print(f"{len(forest_only)} forest-only point(s) — review as rule candidates:")
        for mmsi, at in sorted(forest_only)[:10]:
            print(f"  mmsi={mmsi} at={at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
