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

## Host

Runs on my laptop (kept on, never sleeps, stays logged in for remote desktop).
Scheduled task `VesselWatchCollector`, every 2 min, writing to `data\vessels.db`.
`detect` and `export` are separate passes so thresholds can be re-tuned and the
site's `incidents.json` regenerated without re-collecting.
