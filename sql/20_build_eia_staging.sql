-- Build the typed, deduplicated EIA staging layer.
SET TimeZone = 'UTC';


-- Keep records that cannot be safely converted instead of silently
-- discarding them.
CREATE OR REPLACE TABLE stg.eia_quarantine AS
WITH ranked AS (
    SELECT
        l.*,
        r.completed_at_utc,
        row_number() OVER (
            PARTITION BY l.period, l.series_type
            ORDER BY
                r.completed_at_utc DESC NULLS LAST,
                l.run_id DESC
        ) AS version_rank
    FROM raw.eia_landed AS l
    JOIN raw.eia_runs AS r USING (run_id)
    WHERE r.status = 'complete'
),
parsed AS (
    SELECT
        *,
        try_strptime(
            period,
            '%Y-%m-%dT%H'
        ) AS parsed_ts_utc,
        try_cast(value AS DOUBLE) AS parsed_value_mw
    FROM ranked
    WHERE version_rank = 1
),
classified AS (
    SELECT
        *,
        CASE
            WHEN parsed_ts_utc IS NULL
                THEN 'invalid_period'
            WHEN value IS NULL
                THEN 'missing_value'
            WHEN parsed_value_mw IS NULL
                THEN 'invalid_value'
            -- Demand and forecast outside the plausible PJM band are
            -- upstream EIA errors (nulls, unit glitches, and INT32
            -- overflow sentinels near 2.147e9). Net generation and
            -- total interchange have different scales and sign, so the
            -- band applies only to D and DF.
            WHEN series_type IN ('D', 'DF')
                AND (
                    parsed_value_mw < 20000
                    OR parsed_value_mw > 175000
                )
                THEN 'implausible_magnitude'
            ELSE NULL
        END AS reason
    FROM parsed
)
SELECT
    run_id,
    source_file,
    source_page,
    source_offset,
    period,
    respondent,
    respondent_name,
    series_type,
    series_name,
    value,
    value_units,
    raw_row,
    reason,
    loaded_at_utc
FROM classified
WHERE reason IS NOT NULL;


-- Current valid EIA values at long grain:
-- one timestamp and one series per row.
CREATE OR REPLACE TABLE stg.eia_hourly_long AS
WITH ranked AS (
    SELECT
        l.*,
        r.completed_at_utc,
        row_number() OVER (
            PARTITION BY l.period, l.series_type
            ORDER BY
                r.completed_at_utc DESC NULLS LAST,
                l.run_id DESC
        ) AS version_rank
    FROM raw.eia_landed AS l
    JOIN raw.eia_runs AS r USING (run_id)
    WHERE r.status = 'complete'
),
parsed AS (
    SELECT
        *,
        try_strptime(
            period,
            '%Y-%m-%dT%H'
        ) AS parsed_ts_utc,
        try_cast(value AS DOUBLE) AS parsed_value_mw
    FROM ranked
    WHERE version_rank = 1
)
SELECT
    parsed_ts_utc AS ts_utc,
    respondent,
    respondent_name,
    series_type,
    series_name,
    parsed_value_mw AS value_mw,
    value_units,
    run_id AS source_run_id,
    source_file,
    loaded_at_utc
FROM parsed
WHERE parsed_ts_utc IS NOT NULL
  AND value IS NOT NULL
  AND parsed_value_mw IS NOT NULL
  AND NOT (
      series_type IN ('D', 'DF')
      AND (
          parsed_value_mw < 20000
          OR parsed_value_mw > 175000
      )
  );


-- Current valid EIA values at hourly wide grain.
CREATE OR REPLACE TABLE stg.eia_hourly AS
SELECT
    ts_utc,

    max(
        CASE
            WHEN series_type = 'D'
                THEN value_mw
        END
    ) AS demand_mw,

    max(
        CASE
            WHEN series_type = 'DF'
                THEN value_mw
        END
    ) AS demand_forecast_mw,

    max(
        CASE
            WHEN series_type = 'NG'
                THEN value_mw
        END
    ) AS net_generation_mw,

    max(
        CASE
            WHEN series_type = 'TI'
                THEN value_mw
        END
    ) AS total_interchange_mw,

    count(*) AS series_count

FROM stg.eia_hourly_long
GROUP BY ts_utc
ORDER BY ts_utc;
