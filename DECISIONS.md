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





















