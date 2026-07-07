# Vessel Traffic & Anomaly Detector

Flags anomalous vessel behaviour in AIS ship-tracking data along the Bergen
approaches: AIS gaps (going dark), sudden stops, impossible position jumps, and
sharp route deviations. Live vessel positions come from BarentsWatch; the
anomaly logic is tuned and validated against Kystverket's open historical AIS.

**Live demo:** https://www.bakketvedt.no/demo/vessels/ (map of current traffic +
a gallery of validated incidents). Frontend lives in the portfolio repo; this
repo is the Python pipeline behind it.

## Why it exists

A portfolio piece aimed at Norwegian maritime/ocean tech. AIS broadcasts every
ship's position, but in a steady stream of ordinary traffic the few tracks that
matter (a vessel gone silent, a drift in open water, a spoofed position) are easy
to miss. There is no labelled dataset of "anomalies" to learn from, so the
interesting engineering is defining what counts as anomalous and separating it
from normal traffic, not fitting a model.

## Architecture

Two paths, by design. Anomaly detection needs a *sequence* of fixes over time, so
it runs offline; the live map is served by a stateless edge function.

```
Kystverket HAIS ┐
                ├─► collector ─► SQLite ─► detector ─► incidents.json ─┐
BarentsWatch    ┘   (Python)     tracks    (rules +      (curated)      ├─► Vue + Leaflet
                                           iforest)                     │   (portfolio repo)
BarentsWatch ───────────────────────────► Cloudflare edge proxy ───────┘
                                           (live positions)
```

- **Collector** (`collector.py`) polls BarentsWatch AIS for the area of interest
  into SQLite. Runs on a schedule (Windows Task Scheduler, every 2 min).
- **Historical loader** (`kystverket.py`) reads Kystverket HAIS exports (Parquet
  or CSV) into the same table, for offline tuning against real traffic.
- **Detector** (`anomaly.py`, `detect.py`) applies the flag rules per vessel
  track, with an isolation-forest second pass (`model.py`) as a comparison.
- **Export** (`export.py`) writes a curated `incidents.json` for the site, and
  resolves vessel names from the live feed.

## The interesting part: tuning against real data

Run against **1.28M real AIS positions** (two days over the Bergen approaches),
the naive thresholds produced **4,072 flags, 3,236 of them bogus**. Getting to a
defensible handful was the actual work, and most of it was AIS domain knowledge:

- **GPS jitter reads as teleporting.** At full time resolution a moored vessel's
  position wanders a few metres between fixes seconds apart, which computes to
  60+ kn. Fix: a position jump must clear a real distance, not just an impossible
  implied speed.
- **The AIS "not available" sentinel.** Stops and jumps came from a speed of
  102.3 kn: the raw-1023 "speed unavailable" marker leaking through as a real
  value. Clamp SOG ≥ 102.2, COG ≥ 360 and heading 511 to null at ingestion.
- **Ferries and moored objects.** One ferry dwelling at its quay and one
  permanently-moored object generated hundreds of "sudden stops". Fixes: require
  the vessel to have actually been moving before a stop, and learn each vessel's
  own habitual stop cells (a ferry's quays) rather than hard-coding berths.
- **Full resolution retriggers.** One behaviour fires across dozens of
  consecutive fixes, so same-kind flags for a vessel within 30 min collapse to a
  single incident.
- **Boundary artifacts.** Gaps and course-changes pile up at the edge of the
  area (vessels leaving the box), so detection runs on the area inset ~500 m.

Result: **4,072 → 6** defensible incidents. Full decision log in
[`working-log.md`](working-log.md).

## Anomaly types

| Flag | What it catches | Why it matters |
|------|-----------------|----------------|
| `ais_gap` | A vessel underway goes silent for a bounded window | The classic "going dark" |
| `sudden_stop` | Fast → stopped away from any berth, and stays stopped | Breakdown, grounding, drift |
| `speed_jump` | Distance between fixes implies an impossible speed | Bad or spoofed position |
| `route_deviation` | Course over ground swings hard off the reported heading | Departure from an expected track |

Rules are deliberately conservative (precision over recall): a monitoring tool
that cries wolf is worse than one that stays quiet.

## Quickstart

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env            # add BarentsWatch client id/secret, set the AOI

# Offline path (historical data -> incidents):
#   download a HAIS export from https://hais.kystverket.no/ into raw/
python -m vesselwatch.kystverket        # load raw/*.parquet|*.csv into SQLite
python -m vesselwatch.detect --summary  # flag anomalies
python -m vesselwatch.export --names    # write incidents.json (names from live feed)

# Live path:
python -m vesselwatch.collector         # one poll of current positions (schedule it)
python -m vesselwatch.healthcheck       # OK/STALE, exits non-zero when stale

# Validate the rules offline, no data needed (planted known-answer tracks):
python -m vesselwatch.sample && python -m vesselwatch.detect --summary
```

## Data & licensing

- **Kystverket Historisk AIS** (hais.kystverket.no): open data under NLOD.
  Excludes small craft (fishing < 15 m, recreational < 45 m) for privacy.
- **BarentsWatch AIS API** (live): free client registration, OAuth2
  client-credentials, `ais` scope.

## Stack

Python, Pandas, scikit-learn, SQLite. Frontend (separate repo) is Vue 3 +
TypeScript + Leaflet on Cloudflare Pages. Built as a documented collaboration
with an AI coding agent; the corrections above are where domain judgement had to
override the agent's assumptions about AIS.
