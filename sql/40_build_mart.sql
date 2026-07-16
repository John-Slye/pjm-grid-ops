-- Build the rebuildable analytical mart layer.
--
-- Local wall-clock columns come from converting the canonical UTC
-- timestamp to America/New_York. ts_utc is naive but documented as UTC:
-- it is first anchored to UTC, then rendered as Eastern wall clock. The
-- canonical UTC timestamp is preserved and never replaced.
SET TimeZone = 'UTC';


-- Hourly demand and forecast aligned to the weather composite, with
-- Eastern calendar attributes. EIA hourly demand is the backbone, so
-- the weather composite is joined in and may be NULL when a given hour
-- has no station coverage; every EIA hour is kept.
CREATE OR REPLACE TABLE mart.hourly AS
WITH joined AS (
    SELECT
        e.ts_utc,
        e.demand_mw,
        e.demand_forecast_mw AS forecast_mw,
        w.temp_composite_c,
        (
            e.ts_utc
                AT TIME ZONE 'UTC'
                AT TIME ZONE 'America/New_York'
        ) AS ts_local
    FROM stg.eia_hourly AS e
    LEFT JOIN stg.weather AS w USING (ts_utc)
)
SELECT
    ts_utc,
    demand_mw,
    forecast_mw,
    temp_composite_c,
    ts_local,
    extract('hour' FROM ts_local) AS hr_local,
    extract('isodow' FROM ts_local) AS dow_local,
    extract('month' FROM ts_local) AS month_local,
    CAST(ts_local AS DATE) AS date_local
FROM joined
ORDER BY ts_utc;


-- One row per Eastern calendar day: the demand peak, when it occurred,
-- the forecast peak, and the day's temperature summary. summer_year is
-- the calendar year, which is unambiguous for the summer months this
-- layer is built to analyze.
CREATE OR REPLACE TABLE mart.daily AS
SELECT
    date_local,
    CAST(
        extract('year' FROM date_local) AS INTEGER
    ) AS summer_year,
    any_value(month_local) AS month_local,
    any_value(dow_local) AS dow_local,
    max(demand_mw) AS daily_peak_mw,
    arg_max(hr_local, demand_mw) AS hour_of_peak,
    max(forecast_mw) AS df_peak_mw,
    max(temp_composite_c) AS temp_max_c,
    avg(temp_composite_c) AS temp_mean_c
FROM mart.hourly
GROUP BY date_local
ORDER BY date_local;


-- Rank summer days (June-September) by demand peak within each summer
-- and flag the five highest, the days that drive seasonal planning.
--
-- is_complete_summer marks whether the summer season has finished. A
-- summer is complete once its calendar year is in the past, or once the
-- current date passes its September 30 season end. This self-heals: the
-- in-progress current summer flips to complete in October with no code
-- change. Provisional (incomplete) summers must be excluded from model
-- training, backtesting, and EVT fitting.
CREATE OR REPLACE TABLE mart.summer_labels AS
WITH summer_days AS (
    SELECT
        date_local,
        summer_year,
        daily_peak_mw
    FROM mart.daily
    WHERE month_local BETWEEN 6 AND 9
),
ranked AS (
    SELECT
        date_local,
        summer_year,
        daily_peak_mw,
        rank() OVER (
            PARTITION BY summer_year
            ORDER BY daily_peak_mw DESC
        ) AS peak_rank
    FROM summer_days
)
SELECT
    date_local,
    summer_year,
    daily_peak_mw,
    peak_rank,
    peak_rank <= 5 AS is_top5,
    CAST(
        summer_year < extract('year' FROM current_date)
        OR current_date > make_date(summer_year, 9, 30)
        AS BOOLEAN
    ) AS is_complete_summer
FROM ranked
ORDER BY summer_year, peak_rank;
