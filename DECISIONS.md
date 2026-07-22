# DECISIONS.md — PJM Peak-Day Early Warning

This log records design decisions, data-quality findings, and results in the
order the project learned them. Superseded results are retained and marked
rather than deleted: the corrections are part of the record, and reviewing how
errors were caught matters as much as the final numbers.

---

# Part I — Data Platform

## EIA Historical Ingestion

### Initial historical window

The first reproducible EIA snapshot covers hourly periods from `2019-01-01T00`
through `2026-07-01T00`. The cutoff is intentionally fixed rather than based on
the current date so that repeated historical runs use the same query window.
Data after this cutoff, along with recent EIA revisions, will be handled by the
later incremental-loading process.

### Raw landing format

Each paginated EIA API response is preserved as a separate JSON file under a
timestamped run directory. A manifest is written for each series and for the
overall run. Raw response files are not manually edited, combined, or
type-converted during ingestion.

### Timestamp convention

The EIA `period` field is treated as a UTC timestamp at the raw layer. No
Eastern Time conversion or hour-beginning/hour-ending adjustment is performed
during ingestion. At the staging and weather-join stages, EIA and NOAA
observations are aligned consistently, and any remaining hourly-label ambiguity
is documented rather than silently shifted. (See "DF Timestamp Alignment" for
the one case where the published label was demonstrably wrong and corrected at
staging.)

### Completeness requirement

An EIA series is considered successfully landed only when the number of
downloaded rows exactly matches the API's reported `total`. An empty
intermediate page, a changing server total, a duplicate period, or a mismatched
respondent or series type causes the ingestion to fail loudly.

## DuckDB Warehouse Architecture

The local analytics warehouse is stored at `data/pjm_grid_ops.duckdb`. The
database file is generated from source landings and is excluded from Git, while
all schema definitions, transformations, loaders, and tests are version
controlled. `data/pjm_grid_ops.duckdb` is the one and only canonical warehouse
path. A stray empty `grid.duckdb` that had been created at the repository root
by an accidental default connection was deleted; no code references that path,
and it must not be recreated.

The warehouse uses three schemas. `raw` preserves source-level values, run
manifests, page lineage, and the original JSON representation of each
observation. `stg` converts timestamps and numeric values, quarantines records
that cannot be parsed, selects the latest complete version of each EIA
timestamp and series, and creates the hourly wide table. `mart` holds
rebuildable analytical tables (hourly joined facts, daily peaks, summer
labels).

`raw.eia_landed` is append-only and versioned by `run_id`. Later incremental
pulls will preserve revised observations as new run versions instead of
destroying the original version. The staging layer determines the current value
by selecting the observation from the most recently completed run.

`ts_utc` is stored as a naive DuckDB `TIMESTAMP` whose documented meaning is
UTC. Every SQL script and database connection used by the pipeline explicitly
sets the DuckDB session timezone to UTC. Eastern wall-clock timestamps are
derived at the mart layer and do not replace the canonical UTC timestamp.

The DuckDB database itself is not the immutable system of record. The
timestamped JSON API landings remain the source evidence, while the database is
a reproducible and query-optimized representation of those files.

## Hourly Weather Source

The original project specification selected NOAA ISD-Lite for hourly station
weather. NOAA discontinued updates to ISD and ISD-Lite in August 2025 and
replaced them with Global Historical Climatology Network Hourly (GHCNh).
Because this project requires weather through July 1, 2026, GHCNh is used as
the operational weather source.

The six station locations and population weights remain unchanged: KPHL 0.21,
KORD 0.32, KPIT 0.08, KDCA 0.21, KCMH 0.07, and KEWR 0.11. Annual station PSV
files are preserved immutably under timestamped raw run directories.

GHCNh observations retain their exact UTC minute. The staging layer maps
observations to hourly UTC timestamps by selecting, per station, the valid
observation closest to each whole hour. This reproduces the practical hourly
intent of ISD-Lite without silently discarding the more precise source
timestamp. Raw timestamps are never altered.

`stg.weather` is then a population-weighted composite of the six station
temperatures for each hour. The weights above are renormalized each hour over
only the stations that actually reported a valid temperature that hour, so
missing stations do not bias the mean, and `n_stations` records how many
contributed. The current run parses 792,022 raw observations into `raw.weather`
with 0 quarantined: this dataset contains no missing sentinels and no
physically implausible temperatures. The loader still enforces the guard —
temperatures outside the `< -40 or > 50 C` band, or unparseable values, are
diverted to `raw.weather_quarantine` rather than landing in `raw.weather`.

## Demand Magnitude Validity Rule

The EIA PJM hourly `D` (demand) and `DF` (day-ahead demand forecast) series
contain rare but severe upstream errors: unit glitches producing ~1.5x–2x
inflated values and INT32 overflow sentinels near 2.147e9. The staging layer
quarantines any `D` or `DF` value outside the plausible band
`[20000, 175000]` MW with reason `implausible_magnitude`, following the same
pattern as `missing_value`. Eleven `D` hours are caught (one in Dec 2019, seven
across 2020, three consecutive Oct 2021 sentinel hours); no `DF` hours
currently fall outside the band.

The band is scoped to `D` and `DF` only. Net generation (`NG`) and total
interchange (`TI`) have different magnitudes and signs, so the band does not
apply to them. `NG` carries its own uncaught 2.147e9 sentinel that this rule
intentionally leaves in place; filtering it is deferred until `NG` enters an
analytical mart.

### Patch-versus-quarantine reconciliation

The preferred remedy for a quarantined `D`/`DF` hour is to patch it from the
EIA-930 six-month balance file's *adjusted* demand when that value is present
and inside the band, recording provenance in stg (`value_source` in
`{'api', 'bulk_adjusted'}`, plus `patched_at`) rather than overwriting
silently, and never modifying raw. Only hours the bulk file cannot rescue stay
quarantined.

For this build the EIA-930 bulk files were not present on disk and downloading
them was out of scope, so the patch attempt was skipped and all eleven flagged
hours were quarantined. Bulk reconciliation is deferred, and the
`value_source` / `patched_at` provenance columns will be added to stg when the
bulk-adjusted patch path is implemented. This removed the inflated values from
summer 2020's peak ranking, which now reads as real mid-July heat-wave days
peaking at 142–145 GW in the late afternoon.

### DST-transition demand gaps

The four daylight-saving transition days (2020-03-08, 2022-03-13, 2023-11-05,
2024-03-10) carry EIA-reported null demand for most of their hours, which the
`missing_value` rule quarantines. These days are left as-is: they have only 1–2
hourly rows in `mart.hourly` and are the sole interior dates that fall outside
the 23/24/25-rows-per-local-day expectation. The gap is upstream EIA data,
surfaced rather than backfilled.

## Mart Layer Conventions

### Local calendar columns and day-of-week

`mart.hourly` derives Eastern wall-clock columns by anchoring the naive UTC
`ts_utc` to UTC and rendering it in `America/New_York`, producing `ts_local`,
`hr_local`, `dow_local`, `month_local`, and `date_local`. `dow_local` uses the
ISO convention (`extract('isodow')`): 1 = Monday through 7 = Sunday. This
convention is used throughout the mart — `mart.daily.dow_local` carries the
same ISO values. Downstream weekend logic must therefore test
`dow_local IN (6, 7)`, not the Sunday-zero `(0, 6)`.

### daily_peak_mw is a lower bound on quarantined days

`mart.daily.daily_peak_mw` is `max(demand_mw)` over the surviving hours of the
local day. On a day whose true peak hour was removed by the
`implausible_magnitude` quarantine, the reported peak is a lower bound computed
from the remaining hours, not the day's actual maximum. Example: 2020-07-27's
inflated 245,799 MW hour is quarantined, so its `daily_peak_mw` is 144,562 MW —
the highest surviving hour, which is plausible but may understate the true
peak.

### Summer 2026 is in progress

The historical window ends 2026-07-01, so summer 2026 (`summer_year = 2026`)
is only partially observed. Its `is_top5` labels are provisional and will shift
as the rest of the season lands. `mart.summer_labels` carries an
`is_complete_summer` flag, false for 2026 until the season ends, that
self-heals to true once the current date passes September 30 (no code change
needed). `summer_year = 2026` must be excluded from all model training,
backtesting, and EVT (extreme-value) fitting until the season completes.

## DF Timestamp Alignment

EIA publishes the day-ahead demand forecast (`DF`) labeled one hour earlier
than the demand hour it actually predicts. The forecast stored at period `t`
best tracks demand at `t+1`. Evidence, measured as MAPE of `DF` against demand
at three alignments:

| Alignment | MAPE |
| --- | --- |
| `DF[t]` vs `D[t]` (as published) | 3.60% |
| `DF[t]` vs `D[t+1]` | 2.42% |
| `DF[t]` vs `D[t-1]` | 5.63% |

Root cause: the offset is in the EIA source, not in our pipeline. The same
three alignments computed on `raw.eia_landed` (3.61% / 2.43% / 5.64%) match the
staging figures, and the raw JSON for both `D` and `DF` carries only a `period`
field with no secondary timestamp. The raw layer faithfully preserves EIA's
published period, so it carried the misalignment through unchanged.

Fix: applied in `sql/20_build_eia_staging.sql` when building the wide
`stg.eia_hourly` table. Each `DF` observation's effective timestamp is advanced
by one hour (`ts_utc + INTERVAL 1 HOUR`); `D`, `NG`, and `TI` keep their
published timestamps, and neither `raw` nor `stg.eia_hourly_long` is modified.
After the fix, `forecast_mw` at `ts_utc = t` is the forecast for `demand_mw` at
`t`, and the stored alignment measures 2.42% MAPE while both one-hour shifts
are worse (+1h → 3.46%, −1h → 3.60%). The shift adds two forecast-only hours at
series edges (`stg.eia_hourly` and `mart.hourly` go from 65,621 to 65,623 rows)
and changes `mart.daily.df_peak_mw` on 12 dates where the daily peak forecast
crossed a local-day boundary (max change 6,585 MW on 2022-06-18).

All `DF`-benchmark results computed before 2026-07-17 used misaligned `DF` and
are superseded. Any forecast-skill numbers, error metrics, or models that
consumed `forecast_mw` (or `df_peak_mw`) from before this fix must be
recomputed. (The recomputation is recorded in Part II; the discovery story is
in the post-mortem at the end of this file.)

---

# Part II — Modeling Chapter

All results below reflect the corrected DF alignment. Where a number was first
computed against misaligned DF, the original value is noted as superseded
rather than removed.

## Evaluation frame

Fixed test year: 2023 (8,735 valid hours). Training for all fitted models:
2019–2022 (34,988 hours). 2024–mid-2026 remains untouched, reserved for one
final out-of-sample evaluation at project end. Summer 2026 is excluded from
all training, backtesting, and EVT fitting (see "Summer 2026 is in progress").

## Modeling goalposts (2023 test year, corrected)

From `r/02_baseline.R`, evaluated on all 2023 hours with valid demand and a
valid naive prediction.

| Forecast | MAPE | Notes |
| --- | --- | --- |
| Seasonal naive (t − 168 hours) | 7.24% | time-based join, see corollary fix below |
| PJM day-ahead forecast (DF), aligned | 2.29% | the incumbent benchmark |

Every model must beat 7.24% to justify existing. The ~5-point gap between the
two numbers represents the value of information (weather, calendar,
operational knowledge) over pure repetition; closing that gap, and mapping
where it cannot be closed, is the modeling chapter's objective. PJM's DF is
the incumbent benchmark throughout; we do not expect to beat it overall.

*Superseded:* the goalposts were first established as naive 7.24% / PJM 3.45%.
The PJM figure was computed against misaligned DF and overstated the
incumbent's error by ~1.2 points; 2.29% is the corrected benchmark.

*Corollary fix (naive baseline):* the original implementation used
`lag(demand_mw, 168)`, which counts rows, not hours, and silently misaligns
for roughly a week after any missing-row gap (the DST days). Rebuilt as a
timestamp join on `ts_utc + 168 hours` in `r/02_baseline.R`; the naive MAPE
moved only from 7.2398% to 7.2449%, confirming the defect was real but small.
Found the same day as the DF alignment bug, by auditing other row-versus-time
assumptions.

## GAM v1 (r/03_gam.R)

Model: `demand ~ s(temp, k=20) + s(hour, cc, k=24) + s(doy, cc, k=30) + dow +
ti(temp, hour, bs=(tp, cc))`; `mgcv::bam`, `discrete=TRUE`; train 2019–2022
(n = 34,988), test 2023 (n = 8,735). The GAM never consumes DF, so its fit and
predictions are unaffected by the alignment fix; only its comparison against
the incumbent changed.

Results (2023 test year):

- Deviance explained 91.9% (adj R² 0.919); all terms p < 2e-16.
- MAPE 3.75%: beats the 7.24% naive floor decisively; closes ~70% of the
  naive-to-incumbent gap; trails the corrected incumbent (2.29%) by ~1.5pts.
- *Superseded:* the gap was first reported as 0.30pts against the misaligned
  PJM figure of 3.45%. The corrected comparison is less flattering and stands.
- The gap is understated further by our use of actual temperatures where PJM
  forecast with day-ahead weather (standing caveat; to be stress-tested with
  noise injection in the classifier phase).
- dow effects validate ISO day numbering end-to-end: Sat/Sun (dow 6/7) at
  −6.0 and −7.1 GW vs Monday; midweek +0.9 to +1.3 GW; Friday −0.5 GW.

Technical notes:

- `ti()` marginal bases set explicitly to `(tp, cc)`: mgcv's default `cr`
  marginals conflict with endpoint knot specs, and hour must be cyclic inside
  the interaction regardless. (Initial fit errored on this; fixed at the
  specification, not worked around.)
- Known limitation: `s(doy)` edf 27.4/28 and `s(temp)` edf 17.1/18.3 press
  their k ceilings. Deviance suggests low practical impact; remedy (raise k,
  refit) deferred unless transition-season behavior implicates it.
- The temp × hour interaction is significant and physically sensible: the
  heat-load (AC) response concentrates in afternoon/evening hours — the
  mechanism the peak-day project runs on.

## Regime findings (r/04_regimes.R, corrected DF, all years)

1. PJM's weakest weekday pockets are summer pre-dawn hours (Jun–Aug,
   ~03:00–06:00 local): MAPE 5.0–5.6% versus 2.4% overall, with persistent
   under-forecast of ~4.3–4.7 GW. This pattern survived the alignment fix and
   is judged real.

2. Overall bias is −1,117 MW (PJM runs slightly under actual on average) —
   essentially invariant to the alignment fix, as expected for a pure time
   shift relabeling values without changing their mean.

3. Head-to-head with GAM v1 (2023, monthly; `mape_pjm` computed with
   `na.rm = TRUE` because post-shift forecast gaps leave a few demand-only
   hours, e.g. the Nov 2023 DST day). PJM wins all 12 months. Full table
   (`gam_minus_pjm`, percentage points):

   | Month | Gap | Month | Gap | Month | Gap |
   | --- | --- | --- | --- | --- | --- |
   | Aug | +0.23 | Jul | +0.30 | Jan | +0.86 |
   | Feb | +0.92 | Mar | +1.09 | Sep | +1.50 |
   | Jun | +1.53 | Oct | +1.65 | Dec | +1.97 |
   | Nov | +2.08 | Apr | +2.68 | May | +2.77 |

   The gap compresses to near-parity in exactly the peak-season months
   (Jul/Aug, +0.2–0.3pts) and widens most in the spring/fall transitions
   (Apr/May, +2.7–2.8pts; every transition month +1.5 or worse).
   Interpretation: summer demand is temperature-dominated and the GAM's
   temperature structure captures most of what the incumbent knows;
   transition seasons reward operational information the GAM lacks.
   Statistical verdict on the Jul/Aug claim: see DM tests below —
   "near-parity" describes magnitude, not statistical indistinguishability.

*Superseded:* the pre-fix regime map showed dominant 9–9.5% error bands
(summer midnight hours, shoulder-season 06:00) with −8 to −9 GW biases, and
the pre-fix head-to-head showed the GAM beating PJM in Jul/Aug by 1.1–1.2pts.
All of it was an artifact of the one-hour DF misalignment (error concentrating
where demand changes fastest, bias sign tracking slope direction). None of
those findings survive; the corrected findings above do.

## Diebold–Mariano tests (r/05_dm_test.R)

2023 test year; errors passed as (GAM, PJM), so positive DM = PJM better;
h = 24 HAC variance; squared loss unless noted.

- Overall (~8,700 hrs): DM = 9.36, p < 2.2e-16. PJM's full-year superiority is
  decisive.
- Apr/May contrast (1,464 hrs): DM = 4.37, p = 1.3e-05. Confirms the test is
  well-powered to detect regime gaps of transition-season size, making the
  summer verdict informative rather than underpowered.
- Jul/Aug (1,488 hrs): DM = 2.14, p = 0.033 under squared loss; DM = 1.74,
  p = 0.082 under absolute loss. Verdict: a small gap (+0.2–0.3pts MAPE) that
  is borderline-significant — likely real, not parity. We claim "economically
  minor, statistically detectable under squared loss" and nothing stronger.
- Interpretation of the loss-function split: significance under squared but
  not absolute loss implies PJM's summer edge concentrates in large-error
  hours, which are peak-adjacent. Consequence for the classifier: PJM's DF
  remains the lead feature for peak-day prediction; the GAM supplements
  rather than substitutes.
- Caveat carried forward: the GAM uses actual temperatures versus PJM's
  forecast weather, so all measured gaps understate PJM's true operational
  advantage.

---

## Extreme-value tail fit (r/06_evt.R)

GPD (peaks-over-threshold) on summer daily peaks, complete summers 2019-2025
only (854 days, 7 summers). Threshold u = 140,507 MW (90th percentile);
86 exceedance days reduce to 36 independent events after run-declustering
(r=1) -- the 58% reduction quantifies heat-wave clustering (typical hot spell
above u spans 2-3 consecutive days). Fitted scale sigma = 4,251 MW (SE 807);
shape xi = 0.023 (SE 0.182), statistically indistinguishable from zero: an
exponential-type tail with no evidence of heaviness, and sample too thin to
distinguish bounded from unbounded.

Return levels (MW, 95% CI):
- 5-year:  154,829 (148,921 - 160,738)
- 10-year: 158,025 (149,046 - 167,005)  <- headline; feeds Excel model
- 20-year: 161,272 (148,316 - 174,227)

Sanity check passed: the observed 7-summer maximum (160,560 MW) sits just
above the 10-yr point estimate and inside its CI -- where the largest event
in ~7 years should sit. Threshold sensitivity is strong: 10-yr RL of
158,780 / 158,025 / 157,505 at q = 0.85 / 0.90 / 0.95 (1.3 GW spread).

Fit caveat, logged not hidden: the empirical exceedance density humps at
4-7 GW excess while GPD density is monotone-decreasing by construction.
Attributed to kernel smoothing at N=36 and to declustered run-maxima skewing
moderate; the far tail tracks, and threshold robustness indicates return
levels are not sensitive to the mismatch. Standing caveats: 7 summers is
thin history (CIs honestly wide, especially at 20-yr); quarantined-day peaks
are lower bounds (Part I), biasing the fitted tail slightly downward if at
all. Exported to extracts/gpd_params.csv: u, sigma, xi,
exceed_per_summer = 5.14.

## Classifier features (ingest/build_features.py)

One row per summer day (Jun-Sep), 884 rows: 7 complete summers x 122 days
plus 30 provisional days of in-progress 2026 (excluded from all training via
is_complete_summer). Every feature is knowable at 6 AM of its day:

- df_peak_mw: PJM's day-ahead forecast peak -- genuinely as-of, no proxy.
- temp_fc_max: actual max temp standing in for a forecast (standing caveat;
  to be noise-stress-tested in the backtest phase).
- is_weekend: ISO convention, dow_local in (6, 7).
- days_left: days remaining in the season.
- s2d_top5_cutoff: the 5th-highest daily peak of this summer THROUGH
  YESTERDAY; None until five days have been observed.
- df_vs_cutoff, recent_max_7d: derived from the above.

Implementation is an explicit Python loop rather than SQL windows: ranked
quantities over ever-growing as-of windows are awkward in SQL, and the loop's
append-only-after-row-built ordering makes the leakage protection visible and
hand-verifiable.

Leakage spot-check, performed on the Jul 15 - Aug 5 2022 heat-wave window
(22 rows, verified by hand):
- Jul 20's 148.5 GW peak raises the cutoff only on Jul 21 (135.8 GW); no
  same-day cutoff response anywhere in the window.
- Jul 19's 142.6 GW peak enters as the cutoff only on Aug 4, after enough
  larger days accumulated above it -- correct ranked as-of behavior.
- Cutoff is monotone non-decreasing across the window (133.5 -> 142.8 GW).

Incidental observation from the same table: df_peak_mw tracks the realized
daily peak within ~1-3 GW throughout the heat wave -- the lead feature is
highly informative precisely on peak-relevant days, consistent with the DM
finding that PJM's edge concentrates in large-demand hours.

## Rule-based baseline (r/07_rule_baseline.R)

The one-parameter strategy a facilities manager would run without statistics:
alarm when df_peak_mw >= bar - buffer, where bar = max(s2d_top5_cutoff, FLOOR)
and FLOOR (130,568 MW, the 75th percentile of forecast peaks) stands in for
the early-season period before five days establish a cutoff. Caveat, accepted
for a baseline only: FLOOR is computed from pooled summers, a mild look-ahead;
the classifier will be held to strict leave-one-summer-out with no pooled
constants.

v1 (weekdays only) buffer sweep, complete summers 2019-2025 (35 true peaks):

| buffer (MW) | caught /35 | median alarms/summer |
| --- | --- | --- |
| 0     | 33 | 15 |
| 1000  | 34 | 16 |
| 2000  | 34 | 17 |
| 3000  | 34 | 20 |
| 5000  | 34 | 23 |
| 8000  | 34 | 28 |

The curve is flat above buffer 1000: alarms nearly double to 8000 MW with no
additional catches -- the remaining miss was unreachable by threshold.

Miss diagnosis (the session's key finding):
- 2019-07-20: a SATURDAY peak, forecast 146,692 vs bar 139,607 -- PJM's
  forecast called it loudly, 7 GW clear. Missed purely by the rule's
  hand-coded !is_weekend exclusion, not by the data. The one summer in seven
  whose defining heat wave crested on a weekend.
- 2022-08-08: Monday, missed by 275 MW (142,505 vs bar 142,780) -- a razor
  threshold case, recovered by buffer 1000 as designed.

v2 repair: weekends made eligible; the bar itself filters them (weekend
demand runs 6-7 GW below weekday, per GAM dow effects). Results:
- v2, buffer 0:    34/35, median 15 alarms (2019 recovered; 2022 pending)
- v2, buffer 1000: 35/35 -- perfect seven-summer catch -- median 17 alarms
- Measured cost of weekend eligibility: 9 alarms across all seven summers
  (~1.3/summer).

CHOSEN OPERATING POINT: v2, buffer = 1000 MW. 35/35 caught, median 17
alarms/summer, worst summer 21 (2020). Note: median 17 exceeds the ~15
working alarm budget by two; accepted pending the Module 4 EV model, where
one missed peak (~1/5 of gross savings) dwarfs two events' curtailment cost
at any plausible capacity rate.

Consequence for the classifier: catching more than 35/35 is impossible, so
the classifier's value proposition is alarm efficiency -- matching the
perfect catch on meaningfully fewer alarms (each saved alarm is worth
c_event in the EV model) -- and/or better probability calibration for
threshold tuning. If it cannot beat 35/35-at-17, the honest conclusion is
that a transparent one-parameter rule is the recommended system, and the
classifier's role is confirmatory.


## Peak-day classifier (r/08_classifier.R)

Ridge logistic regression (glmnet, alpha=0, lambda.1se) on the five as-of
features, evaluated leave-one-summer-out with fold-local early-season floors
computed from training summers only (no pooled constants -- the stricter
standard the rule baseline was exempted from). Three days excluded for
missing weather (2025-08-30 to 09-01, one consecutive Labor Day-weekend feed
gap): none top-5, forecast peaks 91-97 GW, 45+ GW below any bar --
exclusion safe, imputation unnecessary; n = 844 days, 35 positives intact.

Model quality: strong separation -- mean p_hat 0.348 on true peak days vs
0.0285 otherwise (12x), max 0.839. Probabilities are low in absolute terms
(35 positives / 844 days) but well-ranked, which is what threshold sweeps
consume.

Pooled efficiency frontier (tau chosen with all summers visible):
best perfect-catch point tau = 0.10 -> 35/35, median 15 alarms, worst 20 --
beats the rule on both matched comparisons. Flagged as flattering: pooled
tau selection is test-set tuning.

Nested evaluation (tau chosen blind from the other six summers, applied to
the held-out seventh -- the pre-registered honest standard):
34/35 caught, median 15 alarms/summer, worst 20. Six of seven folds chose
tau = 0.10 (stable); the 2022 fold chose 0.12, and that deviation produced
the single miss (2022-08-08, p_hat = 0.111). Threshold stability is good but
its one wobble cost a peak -- the nested check did exactly its job.

VERDICT (pre-registered middle branch): neither system dominates. The
classifier trades one missed peak per seven summers for ~2 fewer alarms per
summer vs the rule (35/35 at median 17). At plausible economics the
trade-off is close and c_event-dependent; Module 4 prices it. RECOMMENDATION:
the v2 rule as the operating system (perfect catch, one parameter,
explainable in a sentence), with the classifier as a validated confidence
layer whose probabilities inform dispatch conviction. This conclusion --
a transparent rule beating a tuned model on the metric that matters -- is
reported as a finding, not a failure.

## Capacity price inputs (verified 2026-07-17, pjm.com / Monitoring Analytics)

PJM Base Residual Auction clearing prices, $/MW-day UCAP, RTO footprint:
2025/26: $269.92 (BGE $466.35, Dominion $444.26); 2026/27 (current delivery
year): $329.17, at the FERC cap in all zones (uncapped counterfactual per
PJM simulation: $388.57); 2027/28: $333.44, at cap; 2028/29 (announced
2026-07-14): $325, at cap -- third consecutive auction pinned at the collar.
EV model inputs: low 269.92 / mid 329.17 / high 333.44. Caveats: UCAP
clearing prices; customer bill translation involves zone and tariff scaling
factors -- the per-MW-of-PLC framing at the RTO price is a stated
simplification, and BGE/Dominion-zone facilities face materially higher
values.

## Stress tests (r/09_stress_tests.R)

Study 1 -- weather-noise corruption (cashing the standing actual-vs-forecast
temperature caveat). Full LOSO rerun at tau = 0.10 with temp_fc_max
corrupted by N(0, sd) noise, 20 seeds per level:

| sd (C) | mean caught /35 | worst seed | mean median alarms |
| --- | --- | --- | --- |
| 0 (determinism check) | 35.0 | 35 | 15.0 |
| 1.0 | 34.1 | 32 | 15.3 |
| 1.5 (realistic day-ahead error) | 33.6 | 31 | 15.2 |
| 2.0 | 33.0 | 30 | 15.4 |

Verdict: robust in the main with a measured bound -- realistic forecast
error costs ~1-1.5 peaks per seven summers on average; alarm counts are
unaffected (noise blurs marginal days, no false-alarm cascade), consistent
with df_peak_mw (genuinely as-of, no proxy) carrying most of the signal.
Caveat status: retired as an unknown, retained as a quantified limitation.

Study 2 -- bootstrap-over-summers EV band. Economics per MW curtailable at
the pooled tau = 0.10 point (35/35): savings = rate x 365 x (caught/5) x
0.85 capture efficiency, minus alarms x $2,000/event (assumption, swept in
the Excel model). Per-summer EV at mid rate spans $62.1k (2020, 20 alarms)
to $88.1k (2023, 7 alarms). Bootstrap (2,000 reps over summers), p10/p50/p90
per MW-year:

- low rate ($269.92):  $50.6k / $54.6k / $58.9k
- mid rate ($329.17):  $69.0k / $73.0k / $77.3k
- high rate ($333.44): $70.3k / $74.3k / $78.6k

Integrity adjustment: the band sits on the pooled operating point; under the
nested-honest catch rate (34/35), the seven-summer mean shifts down ~$2.5k.
Headline figure for deliverables: ~$70k per MW-year at current capacity
prices, $50-79k across rate and summer scenarios. Even the low-band floor is
~25x the per-event cost assumption, so the recommendation is insensitive to
c_event within any plausible range.s

## Tableau dashboard (published 2026-07-22)

Public URL: https://public.tableau.com/app/profile/john.slye/viz/PJMPeak-DayEarlyWarningSeven-SummerBacktestEconomics_17847366384910/PJMPeak-DayEarlyWarningSeven-SummerBacktestEconomics

Four views, reading order = argument order: (1) the hook -- summer 2022 daily
peaks with the five top-5 days in red ("five afternoons determined the next
year's capacity charges"); (2) the proof -- seven-summer small-multiples
backtest at the pooled tau = 0.10 operating point, outcomes colored
(green caught / orange false alarm / gray quiet), aligned on a common
Jun-Sep axis via a dummy-year date transform; (3) the payoff -- bootstrap
EV distribution with p10/p90 reference lines ($69k/$77k, mid capacity
rate); (4) the risk chapter -- daily-peak histogram with the GPD 10-year
return level (158 GW) as a reference line.

Honesty conventions carried into presentation:
- View 2 shows the pooled operating point (35/35 by construction); the
  nested-honest result (34/35) is stated in the view title rather than
  hidden, and the marginal day (2022-08-08, p = 11.1%) is discoverable via
  tooltip. Convention: recommendation displayed, caveat stated, marginal
  case inspectable.
- Color semantics kept collision-free across views: red = top-5 days (the
  problem), green = catches (the solution), orange = false alarms (the
  cost); gray for everything else.
- Reference lines built on continuous calculated bins
  (FLOOR(x/2000)*2000) because Tableau bin fields do not support reference
  lines -- noted for reproducibility.

Data feeds: extracts/summer_days.csv, backtest_outcomes.csv, ev_bootstrap.csv
(r/10_dashboard_extracts.R). Fixed 1200x900 layout.

# Part III — Post-Mortems

Kept deliberately: how errors were caught is part of the project's evidence.

## The DF timestamp bug (found 2026-07-17)

Symptom: the first regime heatmap showed 9–9.5% MAPE with −8 to −9 GW bias
concentrated exactly where demand changes fastest (summer post-peak decline,
winter morning ramp), with the bias sign tracking the slope direction — the
fingerprint of a one-hour series misalignment rather than genuine forecast
difficulty. The finding looked *too* interesting, which triggered a
falsification test before it was logged.

Falsification test (three lines): MAPE of demand against DF as stored (3.60%),
DF lagged one hour (2.42%), DF led one hour (5.63%). The asymmetry was
decisive.

Root cause: EIA labels DF one hour earlier than the demand hour it predicts —
verified in the raw JSON; our staging had faithfully preserved the source's
misalignment. Fixed once, at the staging wide-table build (see "DF Timestamp
Alignment" for full detail and verification).

Consequences traced: PJM's true benchmark improved from 3.45% to 2.29%; the
GAM's apparent Jul/Aug wins evaporated into small deficits; all DF-derived
results recomputed the same day. Lesson applied immediately: an audit of other
row-versus-time assumptions caught the naive baseline's row-lag defect (see
goalposts corollary fix).

## The inflated-demand quarantine (found 2026-07-16)

Summer 2020's top-5 "peaks" of 192–262 GW were physically impossible
(PJM's all-time record is ~166 GW) and included INT32 overflow sentinels; the
eyeball check on the label answer key caught them before they poisoned the
labels, the EVT fit, and the backtest. Resolved by the
`[20000, 175000]` MW validity rule (Part I); summer 2020's corrected top-5
read as real mid-July heat-wave days at 142–145 GW. Residual effect: daily
peaks on quarantined days are lower bounds (Part I, Mart Layer Conventions).

