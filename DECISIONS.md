## EIA Historical Ingestion

### Initial historical window

The first reproducible EIA snapshot covers hourly periods from `2019-01-01T00` through `2026-07-01T00`. The cutoff is intentionally fixed rather than based on the current date so that repeated historical runs use the same query window. Data after this cutoff, along with recent EIA revisions, will be handled by the later incremental-loading process.

### Raw landing format

Each paginated EIA API response is preserved as a separate JSON file under a timestamped run directory. A manifest is written for each series and for the overall run. Raw response files are not manually edited, combined, or type-converted during ingestion.

### Timestamp convention

The EIA `period` field is treated as a UTC timestamp at the raw layer. No Eastern Time conversion or hour-beginning/hour-ending adjustment is performed during ingestion. At the staging and weather-join stages, EIA and NOAA observations will be aligned consistently, and the remaining hourly-label ambiguity will be documented rather than silently shifted.

### Completeness requirement

An EIA series is considered successfully landed only when the number of downloaded rows exactly matches the API's reported `total`. An empty intermediate page, a changing server total, a duplicate period, or a mismatched respondent or series type causes the ingestion to fail loudly.

## DuckDB Warehouse Architecture

The local analytics warehouse is stored at `data/pjm_grid_ops.duckdb`. The database file is generated from source landings and is excluded from Git, while all schema definitions, transformations, loaders, and tests are version controlled. `data/pjm_grid_ops.duckdb` is the one and only canonical warehouse path. A stray empty `grid.duckdb` that had been created at the repository root by an accidental default connection was deleted; no code references that path, and it must not be recreated.

The warehouse uses three schemas. `raw` preserves source-level values, run manifests, page lineage, and the original JSON representation of each observation. `stg` converts timestamps and numeric values, quarantines records that cannot be parsed, selects the latest complete version of each EIA timestamp and series, and creates the hourly wide table. `mart` is reserved for rebuildable analytical tables such as daily peaks, weather features, and summer labels.

`raw.eia_landed` is append-only and versioned by `run_id`. Later incremental pulls will preserve revised observations as new run versions instead of destroying the original version. The staging layer determines the current value by selecting the observation from the most recently completed run.

`ts_utc` is stored as a naive DuckDB `TIMESTAMP` whose documented meaning is UTC. Every SQL script and database connection used by the pipeline explicitly sets the DuckDB session timezone to UTC. Eastern wall-clock timestamps will be derived later at the mart layer and will not replace the canonical UTC timestamp.

The DuckDB database itself is not the immutable system of record. The timestamped JSON API landings remain the source evidence, while the database is a reproducible and query-optimized representation of those files.

## Hourly Weather Source

The original project specification selected NOAA ISD-Lite for hourly
station weather. NOAA discontinued updates to ISD and ISD-Lite in
August 2025 and replaced them with Global Historical Climatology
Network Hourly (GHCNh). Because this project requires weather through
July 1, 2026, GHCNh is used as the operational weather source.

The six station locations and population weights remain unchanged:
KPHL 0.21, KORD 0.32, KPIT 0.08, KDCA 0.21, KCMH 0.07, and KEWR 0.11.
Annual station PSV files are preserved immutably under timestamped raw
run directories.

GHCNh observations retain their exact UTC minute. The staging layer
maps observations to hourly UTC timestamps by selecting, per station,
the valid observation closest to each whole hour. This reproduces the
practical hourly intent of ISD-Lite without silently discarding the more
precise source timestamp. Raw timestamps will never be altered.

`stg.weather` is then a population-weighted composite of the six station
temperatures for each hour. The weights above are renormalized each hour
over only the stations that actually reported a valid temperature that
hour, so missing stations do not bias the mean, and `n_stations` records
how many contributed. The current run parses 792,022 raw observations
into `raw.weather` with 0 quarantined: this dataset contains no missing
sentinels and no physically implausible temperatures. The loader still
enforces the guard — temperatures outside the `< -40 or > 50 C` band, or
unparseable values, are diverted to `raw.weather_quarantine` rather than
landing in `raw.weather`.

## Demand Magnitude Validity Rule

The EIA PJM hourly `D` (demand) and `DF` (day-ahead demand forecast)
series contain rare but severe upstream errors: unit glitches producing
~1.5x-2x inflated values and INT32 overflow sentinels near 2.147e9. The
staging layer now quarantines any `D` or `DF` value outside the
plausible band `[20000, 175000]` MW with reason `implausible_magnitude`,
following the same pattern as `missing_value`. Eleven `D` hours are
caught (one in Dec 2019, seven across 2020, three consecutive Oct 2021
sentinel hours); no `DF` hours currently fall outside the band.

The band is scoped to `D` and `DF` only. Net generation (`NG`) and total
interchange (`TI`) have different magnitudes and signs, so the band does
not apply to them. `NG` carries its own uncaught 2.147e9 sentinel that
this rule intentionally leaves in place; filtering it is deferred until
`NG` enters an analytical mart.

### Patch-versus-quarantine reconciliation

The preferred remedy for a quarantined `D`/`DF` hour is to patch it from
the EIA-930 six-month balance file's *adjusted* demand when that value is
present and inside the band, recording provenance in stg
(`value_source` in `{'api', 'bulk_adjusted'}`, plus `patched_at`) rather
than overwriting silently, and never modifying raw. Only hours the bulk
file cannot rescue stay quarantined.

For this build the EIA-930 bulk files were not present on disk and
downloading them was out of scope, so the patch attempt was skipped and
all eleven flagged hours were quarantined. Bulk reconciliation is
deferred, and the `value_source` / `patched_at` provenance columns will
be added to stg when the bulk-adjusted patch path is implemented. This
removed the inflated values from summer 2020's peak ranking, which now
reads as real mid-July heat-wave days peaking at 142-145 GW in the late
afternoon.

### DST-transition demand gaps

The four daylight-saving transition days (2020-03-08, 2022-03-13,
2023-11-05, 2024-03-10) carry EIA-reported null demand for most of their
hours, which the `missing_value` rule quarantines. These days are left
as-is: they have only 1-2 hourly rows in `mart.hourly` and are the sole
interior dates that fall outside the 23/24/25-rows-per-local-day
expectation. The gap is upstream EIA data, surfaced rather than
backfilled.

## Mart Layer Conventions

### Local calendar columns and day-of-week

`mart.hourly` derives Eastern wall-clock columns by anchoring the naive
UTC `ts_utc` to UTC and rendering it in `America/New_York`, producing
`ts_local`, `hr_local`, `dow_local`, `month_local`, and `date_local`.
`dow_local` uses the ISO convention (`extract('isodow')`): 1 = Monday
through 7 = Sunday. This convention is used throughout the mart —
`mart.daily.dow_local` carries the same ISO values. Downstream weekend
logic must therefore test `dow_local IN (6, 7)`, not the Sunday-zero
`(0, 6)`.

### daily_peak_mw is a lower bound on quarantined days

`mart.daily.daily_peak_mw` is `max(demand_mw)` over the surviving hours
of the local day. On a day whose true peak hour was removed by the
`implausible_magnitude` quarantine, the reported peak is a lower bound
computed from the remaining hours, not the day's actual maximum. Example:
2020-07-27's inflated 245,799 MW hour is quarantined, so its
`daily_peak_mw` is 144,562 MW — the highest surviving hour, which is
plausible but may understate the true peak.

### Summer 2026 is in progress

The historical window ends 2026-07-01, so summer 2026 (`summer_year =
2026`) is only partially observed. Its `is_top5` labels are provisional
and will shift as the rest of the season lands. `mart.summer_labels`
carries an `is_complete_summer` flag, false for 2026 until the season
ends, that self-heals to true once the current date passes September 30
(no code change needed). `summer_year = 2026` must be excluded from all
model training, backtesting, and EVT (extreme-value) fitting until the
season completes.

## DF Timestamp Alignment

EIA publishes the day-ahead demand forecast (`DF`) labeled one hour
earlier than the demand hour it actually predicts. The forecast stored at
period `t` best tracks demand at `t+1`. Evidence, measured as MAPE of `DF`
against demand at three alignments:

| Alignment | MAPE |
| --- | --- |
| `DF[t]` vs `D[t]` (as published) | 3.60% |
| `DF[t]` vs `D[t+1]` | 2.42% |
| `DF[t]` vs `D[t-1]` | 5.63% |

Root cause: the offset is in the EIA source, not in our pipeline. The
same three alignments computed on `raw.eia_landed` (3.61% / 2.43% /
5.64%) match the staging figures, and the raw JSON for both `D` and `DF`
carries only a `period` field with no secondary timestamp. The raw layer
faithfully preserves EIA's published period, so it carried the
misalignment through unchanged.

Fix: applied in `sql/20_build_eia_staging.sql` when building the wide
`stg.eia_hourly` table. Each `DF` observation's effective timestamp is
advanced by one hour (`ts_utc + INTERVAL 1 HOUR`); `D`, `NG`, and `TI`
keep their published timestamps, and neither `raw` nor
`stg.eia_hourly_long` is modified. After the fix, `forecast_mw` at
`ts_utc = t` is the forecast for `demand_mw` at `t`, and the stored
alignment measures 2.42% MAPE while both one-hour shifts are worse
(+1h → 3.46%, -1h → 3.60%). The shift adds two forecast-only hours at
series edges (`stg.eia_hourly` and `mart.hourly` go from 65,621 to
65,623 rows) and changes `mart.daily.df_peak_mw` on 12 dates where the
daily peak forecast crossed a local-day boundary (max change 6,585 MW on
2022-06-18).

All `DF`-benchmark results computed before 2026-07-17 used misaligned
`DF` and are superseded. Any forecast-skill numbers, error metrics, or
models that consumed `forecast_mw` (or `df_peak_mw`) from before this fix
must be recomputed.

## Modeling goalposts (2023 test year)

Established 2026-07-17 from `r/02_baseline.R`, evaluated on all 2023 hours with
valid demand and a lag-168 value available.

- Seasonal naive (demand = same hour, 7 days prior): **MAPE 7.24%**
- PJM day-ahead forecast (DF), same hours:            **MAPE 3.45%**

Every model we fit must beat 7.24% to justify existing. The 3.8-point gap
between the two numbers represents the value of information (weather, calendar,
operational knowledge) over pure repetition — closing that gap, and mapping
where it can't be closed, is the modeling chapter's objective. PJM's DF is the
incumbent benchmark throughout; we do not expect to beat it overall, and any
regime where we reach parity is a finding.


## GAM v1 (r/03_gam.R) — fitted 2026-07-17

Model: demand ~ s(temp, k=20) + s(hour, cc, k=24) + s(doy, cc, k=30) + dow
       + ti(temp, hour, bs=(tp, cc)); mgcv::bam, discrete=TRUE;
       train 2019-2022 (n=34,988), test 2023 (n=8,735).

Results (2023 test year):
- Deviance explained: 91.9% (adj R-sq 0.919)
- MAPE 3.75% vs goalposts: naive 7.24%, PJM DF 3.45%
- Closes ~92% of the naive-to-incumbent gap; remaining 0.30pt gap understates
  the true gap because this model uses actual temperatures where PJM's DF used
  day-ahead weather forecasts (standing caveat, to be stress-tested later).
- dow effects confirm ISO day numbering: Sat/Sun (dow 6/7) run 6-7 GW below
  weekdays; midweek slightly above Monday.

Technical notes:
- ti() marginal bases set explicitly to (tp, cc): default cr marginals conflict
  with endpoint knot specs, and hour should be cyclic inside the interaction
  regardless. (Initial fit errored on this; fixed, not worked around.)
- Known limitation: s(doy) edf 27.4/28 and s(temp) edf 17.1/18.3 press their
  k ceilings; deviance suggests low practical impact. Remedy if regime analysis
  implicates seasonal transitions: raise k and refit.
- All smooth terms and the temp x hour interaction highly significant -- the
  interaction's significance confirms the heat-load response differs by hour
  (the AC effect), which is the physical basis of the peak-day project.

## Regime findings, corrected (r/04_regimes.R)

1. PJM's weakest weekday pockets are summer pre-dawn hours (Jun-Aug,
   ~03:00-06:00 local): MAPE 5.0-5.6% vs 2.4% overall, with persistent
   under-forecast of ~4.3-4.7 GW. This pattern survived the alignment fix
   and is judged real.

2. Overall bias is -1,117 MW (PJM runs slightly under actual on average) --
   essentially invariant to the alignment fix, as expected for a pure time
   shift.

3. Head-to-head (2023, monthly, corrected DF; mape_pjm with na.rm=TRUE --
   post-shift forecast gaps leave a few demand-only hours, e.g. Nov 2023
   DST). PJM wins all 12 months. Full table (gam_minus_pjm, pts):

       Aug +0.23 | Jul +0.30 | Jan +0.86 | Feb +0.92 | Mar +1.09
       Sep +1.50 | Jun +1.53 | Oct +1.65 | Dec +1.97 | Nov +2.08
       Apr +2.68 | May +2.77

   The gap compresses to near-parity in exactly the peak-season months
   (Jul/Aug, +0.2-0.3pts) and widens most in the spring/fall transitions
   (Apr/May, +2.7-2.8pts; all transition months +1.5 or worse). Interpretation:
   summer demand is temperature-dominated and the GAM's temperature structure
   captures most of what the incumbent knows; transition seasons reward
   operational information the GAM lacks. Supersedes the pre-fix table, whose
   apparent Jul/Aug GAM wins were artifacts of misaligned DF. Statistical
   verdict on the Jul/Aug near-parity claim pending DM test (r/05_dm_test.R).














