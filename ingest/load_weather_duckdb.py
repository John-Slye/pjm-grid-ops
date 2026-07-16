"""Load immutable GHCNh weather PSV files into the local DuckDB warehouse."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from ingest.ghcnh_historical import STATIONS


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "pjm_grid_ops.duckdb"
)

RUNS_DIRECTORY = (
    PROJECT_ROOT
    / "data"
    / "raw_weather"
    / "ghcnh"
    / "runs"
)

INITIALIZE_SQL_PATH = (
    PROJECT_ROOT
    / "sql"
    / "00_initialize.sql"
)

WEATHER_STAGING_SQL_PATH = (
    PROJECT_ROOT
    / "sql"
    / "30_build_weather_staging.sql"
)

# Values GHCNh uses to mark a missing temperature.
MISSING_TEMPERATURE_VALUES = ("", "-9999", "-9999.0")

# Physically plausible surface-temperature band, in Celsius.
MIN_PLAUSIBLE_TEMP_C = -40.0
MAX_PLAUSIBLE_TEMP_C = 50.0


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""

    payload = json.loads(
        path.read_text(encoding="utf-8")
    )

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Expected a JSON object in {path}"
        )

    return payload


def latest_complete_run(
    runs_directory: Path = RUNS_DIRECTORY,
) -> Path:
    """Find the newest complete GHCNh weather run."""

    if not runs_directory.exists():
        raise FileNotFoundError(
            f"Run directory not found: {runs_directory}"
        )

    run_directories = sorted(
        runs_directory.iterdir(),
        reverse=True,
    )

    for run_directory in run_directories:
        manifest_path = (
            run_directory
            / "run_manifest.json"
        )

        if not run_directory.is_dir():
            continue

        if not manifest_path.exists():
            continue

        manifest = read_json(manifest_path)

        if manifest.get("status") == "complete":
            return run_directory

    raise FileNotFoundError(
        "No complete GHCNh weather run was found."
    )


def station_frame() -> pd.DataFrame:
    """Return the GHCNh-id to ICAO station mapping as a frame."""

    return pd.DataFrame.from_records(
        [
            {
                "ghcnh_id": str(
                    station["ghcnh_id"]
                ),
                "station": icao,
            }
            for icao, station in STATIONS.items()
        ]
    )


def load_weather_run(
    run_directory: Path | None = None,
    *,
    database_path: Path = DATABASE_PATH,
    runs_directory: Path = RUNS_DIRECTORY,
    initialize_sql_path: Path = INITIALIZE_SQL_PATH,
    weather_staging_sql_path: Path = WEATHER_STAGING_SQL_PATH,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Load one complete weather run and rebuild the weather composite.

    raw.weather is rebuilt in full from the immutable PSV files of the
    selected run, so the load is idempotent: re-running it reproduces
    the same tables. The PSV files remain the immutable source evidence.
    """

    selected_run = (
        latest_complete_run(runs_directory)
        if run_directory is None
        else run_directory.resolve()
    )

    manifest_path = (
        selected_run / "run_manifest.json"
    )

    run_manifest = read_json(manifest_path)

    run_id = str(run_manifest.get("run_id"))

    if run_manifest.get("status") != "complete":
        raise RuntimeError(
            f"Weather run {run_id!r} is not complete."
        )

    if run_id != selected_run.name:
        raise RuntimeError(
            "Weather run ID does not match its directory name."
        )

    psv_glob = str(
        selected_run / "*" / "*.psv"
    )

    psv_files = sorted(
        selected_run.glob("*/*.psv")
    )

    if not psv_files:
        raise FileNotFoundError(
            f"No PSV files found under {selected_run}"
        )

    try:
        source_directory = str(
            selected_run.relative_to(project_root)
        )
    except ValueError:
        source_directory = str(selected_run)

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = duckdb.connect(
        str(database_path)
    )

    try:
        connection.execute("SET TimeZone = 'UTC'")

        connection.execute(
            initialize_sql_path.read_text(
                encoding="utf-8"
            )
        )

        loaded_at_utc = datetime.now(
            timezone.utc
        ).replace(tzinfo=None)

        connection.register(
            "_station_map",
            station_frame(),
        )

        connection.execute("BEGIN TRANSACTION")

        try:
            # Parse only the columns we need and classify each row.
            # Missing/sentinel temperatures become NULL; unparseable
            # timestamps or temperatures and physically implausible
            # temperatures are flagged for quarantine.
            connection.execute(
                """
                CREATE OR REPLACE TEMP TABLE _weather_classified AS
                WITH source AS (
                    SELECT
                        m.station AS station,
                        r.STATION AS ghcnh_id,
                        r.DATE AS date_raw,
                        r.temperature AS temp_raw,
                        try_strptime(
                            r.DATE,
                            '%Y-%m-%dT%H:%M:%S'
                        ) AS ts_utc,
                        CASE
                            WHEN trim(r.temperature)
                                IN ('', '-9999', '-9999.0')
                                THEN NULL
                            ELSE try_cast(
                                r.temperature AS DOUBLE
                            )
                        END AS temp_c
                    FROM read_csv(
                        ?,
                        delim = '|',
                        header = true,
                        all_varchar = true,
                        union_by_name = true
                    ) AS r
                    JOIN _station_map AS m
                        ON m.ghcnh_id = r.STATION
                )
                SELECT
                    *,
                    CASE
                        WHEN ts_utc IS NULL
                            THEN 'invalid_timestamp'
                        WHEN trim(temp_raw)
                                NOT IN ('', '-9999', '-9999.0')
                            AND temp_c IS NULL
                            THEN 'unparseable_temp'
                        WHEN temp_c IS NOT NULL
                            AND (
                                temp_c < -40.0
                                OR temp_c > 50.0
                            )
                            THEN 'implausible_temp'
                        ELSE NULL
                    END AS reason
                FROM source
                """,
                [psv_glob],
            )

            connection.execute("DELETE FROM raw.weather")

            connection.execute(
                """
                DELETE FROM raw.weather_quarantine
                WHERE source_run_id = ?
                """,
                [run_id],
            )

            connection.execute(
                """
                INSERT INTO raw.weather
                SELECT station, ts_utc, temp_c
                FROM _weather_classified
                WHERE reason IS NULL
                """
            )

            connection.execute(
                """
                INSERT INTO raw.weather_quarantine
                SELECT
                    station,
                    ghcnh_id,
                    ts_utc,
                    temp_raw,
                    temp_c,
                    reason,
                    ? AS source_run_id,
                    ? AS loaded_at_utc
                FROM _weather_classified
                WHERE reason IS NOT NULL
                """,
                [run_id, loaded_at_utc],
            )

            connection.execute(
                "DROP TABLE _weather_classified"
            )

            observation_rows = connection.execute(
                "SELECT count(*) FROM raw.weather"
            ).fetchone()[0]

            quarantine_rows = connection.execute(
                """
                SELECT count(*)
                FROM raw.weather_quarantine
                WHERE source_run_id = ?
                """,
                [run_id],
            ).fetchone()[0]

            first_ts_utc, last_ts_utc = connection.execute(
                "SELECT min(ts_utc), max(ts_utc) FROM raw.weather"
            ).fetchone()

            station_count = connection.execute(
                "SELECT count(DISTINCT station) FROM raw.weather"
            ).fetchone()[0]

            connection.execute(
                """
                DELETE FROM raw.weather_runs
                WHERE run_id = ?
                """,
                [run_id],
            )

            connection.execute(
                """
                INSERT INTO raw.weather_runs
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    CAST(? AS JSON), ?
                )
                """,
                [
                    run_id,
                    source_directory,
                    int(station_count),
                    len(psv_files),
                    int(observation_rows),
                    int(quarantine_rows),
                    first_ts_utc,
                    last_ts_utc,
                    json.dumps(
                        run_manifest,
                        sort_keys=True,
                    ),
                    loaded_at_utc,
                ],
            )

            connection.execute(
                weather_staging_sql_path.read_text(
                    encoding="utf-8"
                )
            )

            connection.execute("COMMIT")

        except Exception:
            connection.execute("ROLLBACK")
            raise

        finally:
            connection.unregister("_station_map")

        staging_rows = connection.execute(
            "SELECT count(*) FROM stg.weather"
        ).fetchone()[0]

        print(
            f"[weather] loaded {observation_rows:,} "
            f"observations from {len(psv_files)} PSV files"
        )
        print(
            f"[weather] quarantined {quarantine_rows:,} rows; "
            f"built {staging_rows:,} composite hours"
        )

        return {
            "database_path": database_path,
            "run_id": run_id,
            "file_count": len(psv_files),
            "observation_rows": observation_rows,
            "quarantine_rows": quarantine_rows,
            "station_count": station_count,
            "first_ts_utc": first_ts_utc,
            "last_ts_utc": last_ts_utc,
            "staging_rows": staging_rows,
        }

    finally:
        connection.close()
