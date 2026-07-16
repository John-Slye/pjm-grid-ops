-- Build the hourly, population-weighted weather composite.
SET TimeZone = 'UTC';


-- One temperature per station per whole UTC hour, then a
-- population-weighted composite across the stations present.
--
-- Each raw observation carries its exact source minute. The whole hour
-- it belongs to is the nearest hour to that minute, and within that
-- hour the observation closest to the top of the hour wins. This
-- reproduces the practical hourly intent of the retired ISD-Lite feed
-- without altering or discarding the more precise source timestamps.
CREATE OR REPLACE TABLE stg.weather AS
WITH observations AS (
    SELECT
        station,
        ts_utc,
        temp_c
    FROM raw.weather
    WHERE temp_c IS NOT NULL
),
bucketed AS (
    SELECT
        station,
        temp_c,
        date_trunc(
            'hour',
            ts_utc + INTERVAL 30 MINUTE
        ) AS hour_utc,
        abs(
            epoch(ts_utc)
            - epoch(
                date_trunc(
                    'hour',
                    ts_utc + INTERVAL 30 MINUTE
                )
            )
        ) AS distance_seconds
    FROM observations
),
nearest AS (
    SELECT
        station,
        hour_utc AS ts_utc,
        arg_min(
            temp_c,
            distance_seconds
        ) AS temp_c
    FROM bucketed
    GROUP BY station, hour_utc
),
station_weights (station, weight) AS (
    VALUES
        ('KPHL', 0.21),
        ('KORD', 0.32),
        ('KPIT', 0.08),
        ('KDCA', 0.21),
        ('KCMH', 0.07),
        ('KEWR', 0.11)
)
SELECT
    n.ts_utc,
    sum(w.weight * n.temp_c)
        / sum(w.weight) AS temp_composite_c,
    count(*) AS n_stations
FROM nearest AS n
JOIN station_weights AS w USING (station)
GROUP BY n.ts_utc
ORDER BY n.ts_utc;
