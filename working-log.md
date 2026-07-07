# Working log: vessel traffic anomaly detector

Running notes on building this with an AI agent: what I asked for, what landed,
what I redirected, and the calls I made myself. Source material for the "Working
with an AI agent" section on the project card.

## Scope and framing

### Decisions I made myself

- **Area of interest: the Bergen approaches (Korsfjorden into Bergen), not "the
  whole coast".** *My call.* Same reasoning as the ferry piece: narrow and local
  beats broad and abstract for a portfolio, and it's water I can sanity-check.
  Kept as a bounding box in config so it's one edit to move it.
- **Precision over recall on the flags.** A portfolio demo that cries wolf is
  worse than one that stays quiet on a marginal case. Every threshold is set
  conservative on purpose, and I'd rather add a rule than loosen one.
- **Rules are the product, the model is a footnote.** Isolation forest is in
  there so I can answer "did you try ML", but the headline is four legible
  thresholds a harbourmaster would recognise. Told the agent not to lead with the
  forest. Judgment, not model worship.

### Decisions I delegated to the agent

- **Data split: BarentsWatch live, Kystverket historical.** Asked the agent to
  pick the source strategy. Its call: BarentsWatch for the live map (OAuth2
  client-credentials, free registration, latest-position feed) and Kystverket's
  open archive (NLOD, no auth) offline to tune and validate thresholds against
  real traffic. The live feed is stateless-friendly, which matters because the
  site proxy runs on Cloudflare Functions that can't hold a stream.

## What the agent got right immediately

- Kept the AIS message-type reality straight: position reports (types 1/2/3
  Class A, 18/19 Class B) carry lat/lon/SOG/COG, static/voyage data (name, ship
  type) comes from type 5/24, and the two are joined on **MMSI**. BarentsWatch's
  `combined` feed hands you both already, so no separate voyage table for this
  scope, but the code comments say why.
- Distinguished **course over ground from true heading**. The route-deviation
  flag compares the bearing a vessel actually travelled against its reported
  course, which is the meaningful comparison; heading (where the bow points) is
  stored but not used for that rule.

## What I had to redirect / what broke

- **AIS speed units.** First pass mixed knots and m/s. AIS SOG is knots; the
  physical "impossible jump" check needs m/s. Pinned it: SOG stays knots in
  storage and in the human-readable detail strings, the speed-jump maths converts
  to m/s (`geo.knots_to_ms`) and the threshold is one clearly-named constant.
- **Position encoding.** The agent initially assumed decoded degrees everywhere.
  True for the API and most CSV exports, but raw/NMEA-decoded AIS gives lat/lon
  as 1/10000-minute integers. Rather than trust one format I made the Kystverket
  loader explicit: degrees by default, `--scale-minutes` when a file is raw. Also
  made it map column aliases instead of hard-coding one header layout, because
  the Kystverket export headers aren't stable across their tools.
- **Route-deviation threshold was mis-set against the test.** The planted
  "off-corridor" track only diverged ~50 deg from its reported course, under the
  60 deg rule, so it silently didn't fire while the other three did. Checked the
  actual geometry (`bearing_deg` on the planted points) instead of nudging the
  threshold blind: the plant was too gentle, not the rule too strict. Made the
  synthetic veer a genuine hard turn (~105 deg). Lesson: when a known-answer test
  fails, verify which side is wrong before touching the number.
- **BerentsWatch `latest/combined` returns everything, not a bbox query.** No
  server-side geometry filter on the simple GET, so filtering to the AOI happens
  client-side. Fine at this scale, noted so it's a deliberate choice not an
  oversight.

## Validation before real data

Same tactic as the ferry model's planted-signal test. `sample.py` seeds
known-answer synthetic tracks: two clean transits plus one vessel per flag
(gone-dark gap, dead-in-water stop, teleport fix, hard veer). `detect` catches
all four planted anomalies and raises nothing on the clean transits, so the code
path is known-good before a single real position arrives. The forest correctly
refuses to run on the thin synthetic set (needs >=200 transitions).

## Tuning against real AIS (the actual work)

Pulled two days of Kystverket HAIS over the Bergen approaches: 1.28M position
reports at full time resolution (a fix every ~9 s). The naive rules that passed
the synthetic test produced **4072 flags, 3236 of them speed_jumps**. Working
through why each was wrong is the substance of this project.

- **GPS jitter reads as teleporting.** At full resolution a moored vessel's
  position wanders a few metres between fixes seconds apart; distance/time then
  implies 60+ kn. The details gave it away: "65 kn implied over 0 min (0.0 nm)".
  Fix: a jump must clear a real distance (500 m), not just an impossible speed.
  3236 -> ~10.
- **The AIS "not available" sentinel.** Several stops and jumps came from a SOG
  of 102.3 kn. That's the raw-1023 "speed unavailable" marker leaking through as
  a real number. Clamped SOG >= 102.2, COG >= 360 and heading 511 to null at
  ingestion. Domain knowledge the data assumes you have.
- **One terminal dominated the stops.** 330 of ~380 sudden_stops sat in a single
  grid cell, a ferry berth, all still reporting status "underway". Hard-coding
  berths is whack-a-mole, so instead I **learn stop zones from the data**: any
  cell where >= 5 distinct vessels sit still is a de-facto berth/anchorage and is
  excluded. Added nav-status exclusion too, and a persistence check: a real
  dead-in-water stop stays stopped, a quay call resumes within minutes.
- **Full resolution retriggers one event many times.** A single behaviour fires
  across dozens of consecutive fixes. Added event de-bouncing: same-kind flags
  for a vessel within 30 min collapse to one incident (strongest kept).
- **The AOI boundary makes noise.** Gaps and course-changes piled up at the box
  edges, vessels leaving our small area, not going dark or turning oddly. Run
  detection on the box inset ~500 m so literal edge-crossings don't count.
- **Near-180 "deviations" are reversals, not lane departures.** Capped the
  course-change flag below 135 deg.
- **Ferries and moored objects, caught by eye.** Reviewing the two surviving
  sudden_stops, I recognised them as false positives from local knowledge: one
  was a vessel that never moves at all (a moored object whose "27 kn" was another
  bad SOG spike), the other a ferry dwelling at its quay. Two more guards: a real
  stop must be preceded by the vessel actually covering ground (kills the moored
  object), and a stop at a cell the vessel *returns to* across separate visits is
  its own berth/route endpoint, not an anomaly (kills the ferry, without needing
  a ferry list). Left one genuine stop: a wide-ranging work vessel that stopped
  once away from anywhere it usually does.

End result: **4072 -> ~37** defensible flags over the window, curated to a handful
per kind for the demo. Honest caveat carried forward: genuine interior AIS gaps
are rare here (coverage is good and the window was calm), so most silence sits at
the coverage edge and is correctly filtered out; the live collector will surface
real ones over time.

## Host

Runs on my laptop (kept on, never sleeps, stays logged in for remote desktop).
Scheduled task `VesselWatchCollector`, every 2 min, writing to `data\vessels.db`.
`detect` and `export` are separate passes so thresholds can be re-tuned and the
site's `incidents.json` regenerated without re-collecting.
