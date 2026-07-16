## EIA Historical Ingestion

### Initial historical window

The first reproducible EIA snapshot covers hourly periods from `2019-01-01T00` through `2026-07-01T00`. The cutoff is intentionally fixed rather than based on the current date so that repeated historical runs use the same query window. Data after this cutoff, along with recent EIA revisions, will be handled by the later incremental-loading process.

### Raw landing format

Each paginated EIA API response is preserved as a separate JSON file under a timestamped run directory. A manifest is written for each series and for the overall run. Raw response files are not manually edited, combined, or type-converted during ingestion.

### Timestamp convention

The EIA `period` field is treated as a UTC timestamp at the raw layer. No Eastern Time conversion or hour-beginning/hour-ending adjustment is performed during ingestion. At the staging and weather-join stages, EIA and NOAA observations will be aligned consistently, and the remaining hourly-label ambiguity will be documented rather than silently shifted.

### Completeness requirement

An EIA series is considered successfully landed only when the number of downloaded rows exactly matches the API's reported `total`. An empty intermediate page, a changing server total, a duplicate period, or a mismatched respondent or series type causes the ingestion to fail loudly.
























