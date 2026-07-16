-- Create the permanent warehouse structure.
--
-- This setting applies to the current DuckDB connection. Every pipeline
-- SQL script sets it explicitly so timestamp behavior is deterministic.
SET TimeZone = 'UTC';

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS mart;


-- One record per complete EIA ingestion run.
CREATE TABLE IF NOT EXISTS raw.eia_runs (
    run_id VARCHAR PRIMARY KEY,
    dataset VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    query_start VARCHAR NOT NULL,
    query_end VARCHAR NOT NULL,
    frequency VARCHAR NOT NULL,
    respondent VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    source_directory VARCHAR NOT NULL,
    started_at_utc TIMESTAMP NOT NULL,
    completed_at_utc TIMESTAMP,
    manifest_json JSON NOT NULL,
    loaded_at_utc TIMESTAMP NOT NULL
);


-- One record per series within an EIA ingestion run.
CREATE TABLE IF NOT EXISTS raw.eia_series_runs (
    run_id VARCHAR NOT NULL,
    series_type VARCHAR NOT NULL,
    series_name VARCHAR NOT NULL,
    server_total BIGINT NOT NULL,
    downloaded_rows BIGINT NOT NULL,
    page_count INTEGER NOT NULL,
    first_period VARCHAR,
    last_period VARCHAR,
    manifest_json JSON NOT NULL,
    PRIMARY KEY (run_id, series_type)
);


-- Append-only observations parsed from immutable source-page files.
--
-- period and value remain VARCHAR here because raw preserves the
-- source representation. They become TIMESTAMP and DOUBLE in stg.
CREATE TABLE IF NOT EXISTS raw.eia_landed (
    run_id VARCHAR NOT NULL,
    source_file VARCHAR NOT NULL,
    source_page INTEGER NOT NULL,
    source_offset BIGINT NOT NULL,
    period VARCHAR NOT NULL,
    respondent VARCHAR NOT NULL,
    respondent_name VARCHAR,
    series_type VARCHAR NOT NULL,
    series_name VARCHAR,
    value VARCHAR,
    value_units VARCHAR,
    raw_row JSON NOT NULL,
    loaded_at_utc TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, series_type, period)
);
