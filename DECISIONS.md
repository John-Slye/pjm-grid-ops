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

The local analytics warehouse is stored at `data/pjm_grid_ops.duckdb`. The database file is generated from source landings and is excluded from Git, while all schema definitions, transformations, loaders, and tests are version controlled.

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
will map observations to hourly UTC timestamps by selecting the valid
observation closest to each whole hour. This reproduces the practical
hourly intent of ISD-Lite without silently discarding the more precise
source timestamp. Raw timestamps will never be altered.





















