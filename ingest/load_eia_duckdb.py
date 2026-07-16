"""Load immutable EIA JSON landings into the local DuckDB warehouse."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "pjm_grid_ops.duckdb"
)

RUNS_DIRECTORY = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "eia"
    / "region_data"
    / "runs"
)

INITIALIZE_SQL_PATH = (
    PROJECT_ROOT
    / "sql"
    / "00_initialize.sql"
)

STAGING_SQL_PATH = (
    PROJECT_ROOT
    / "sql"
    / "20_build_eia_staging.sql"
)

SERIES_TYPES = ("D", "DF", "NG", "TI")

PAGE_PATTERN = re.compile(
    r"^page_(\d+)_offset_(\d+)\.json$"
)


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


def utc_naive(value: str) -> datetime:
    """Convert an ISO timestamp to a naive datetime meaning UTC."""

    parsed = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        raise ValueError(
            f"Timestamp lacks a UTC offset: {value}"
        )

    return (
        parsed
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def latest_complete_run(
    runs_directory: Path = RUNS_DIRECTORY,
) -> Path:
    """Find the newest complete EIA run."""

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
        "No complete EIA historical run was found."
    )


def validate_manifests(
    run_directory: Path,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    """Validate the run and series manifests."""

    run_manifest = read_json(
        run_directory
        / "run_manifest.json"
    )

    run_id = run_manifest.get("run_id")

    if run_manifest.get("status") != "complete":
        raise RuntimeError(
            f"Run {run_id!r} is not complete."
        )

    if run_id != run_directory.name:
        raise RuntimeError(
            "Run ID does not match its directory name."
        )

    manifest_series = tuple(
        run_manifest.get("series_types", [])
    )

    if manifest_series != SERIES_TYPES:
        raise RuntimeError(
            f"Expected series {SERIES_TYPES}; "
            f"manifest contains {manifest_series}."
        )

    series_manifests: dict[
        str,
        dict[str, Any],
    ] = {}

    for series_type in SERIES_TYPES:
        series_directory = (
            run_directory
            / series_type
        )

        manifest = read_json(
            series_directory
            / "manifest.json"
        )

        if manifest.get("status") != "complete":
            raise RuntimeError(
                f"Series {series_type} is not complete."
            )

        if manifest.get("series_type") != series_type:
            raise RuntimeError(
                f"Wrong manifest in {series_directory}."
            )

        downloaded_rows = int(
            manifest["downloaded_rows"]
        )

        server_total = int(
            manifest["server_total"]
        )

        if downloaded_rows != server_total:
            raise RuntimeError(
                f"Series {series_type} failed "
                "the completeness check."
            )

        page_files = manifest.get("page_files")

        if not isinstance(page_files, list):
            raise RuntimeError(
                f"Series {series_type} "
                "has no valid page list."
            )

        if len(page_files) != int(
            manifest["page_count"]
        ):
            raise RuntimeError(
                f"Series {series_type} page count "
                "is inconsistent."
            )

        for page_name in page_files:
            page_path = (
                series_directory
                / page_name
            )

            if not page_path.exists():
                raise FileNotFoundError(page_path)

        series_manifests[
            series_type
        ] = manifest

    return run_manifest, series_manifests


def get_field(
    row: dict[str, Any],
    *names: str,
) -> Any:
    """Return the first matching EIA field spelling."""

    for name in names:
        if name in row:
            return row[name]

    return None


def page_frame(
    page_path: Path,
    *,
    project_root: Path,
    run_id: str,
    series_type: str,
    server_total: int,
    loaded_at_utc: datetime,
) -> pd.DataFrame:
    """Convert one source page to rows with lineage."""

    filename_match = PAGE_PATTERN.fullmatch(
        page_path.name
    )

    if filename_match is None:
        raise RuntimeError(
            f"Unexpected page filename: "
            f"{page_path.name}"
        )

    payload = read_json(page_path)
    response = payload.get("response")

    if not isinstance(response, dict):
        raise RuntimeError(
            f"Missing response object in {page_path}"
        )

    page_total = int(
        response.get("total", -1)
    )

    if page_total != server_total:
        raise RuntimeError(
            f"Server total mismatch in {page_path}"
        )

    rows = response.get("data")

    if not isinstance(rows, list):
        raise RuntimeError(
            f"Missing data list in {page_path}"
        )

    try:
        source_file = str(
            page_path.relative_to(project_root)
        )
    except ValueError:
        source_file = str(page_path)

    records: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Non-object row in {page_path}"
            )

        if row.get("respondent") != "PJM":
            raise RuntimeError(
                f"Non-PJM row in {page_path}"
            )

        if row.get("type") != series_type:
            raise RuntimeError(
                f"Wrong series row in {page_path}"
            )

        period = row.get("period")

        if not isinstance(period, str) or not period:
            raise RuntimeError(
                f"Missing period in {page_path}"
            )

        records.append(
            {
                "run_id": run_id,
                "source_file": source_file,
                "source_page": int(
                    filename_match.group(1)
                ),
                "source_offset": int(
                    filename_match.group(2)
                ),
                "period": period,
                "respondent": row["respondent"],
                "respondent_name": get_field(
                    row,
                    "respondent-name",
                    "respondentName",
                ),
                "series_type": row["type"],
                "series_name": get_field(
                    row,
                    "type-name",
                    "typeName",
                ),
                "value": row.get("value"),
                "value_units": get_field(
                    row,
                    "value-units",
                    "valueUnits",
                ),
                "raw_row": json.dumps(
                    row,
                    sort_keys=True,
                ),
                "loaded_at_utc": loaded_at_utc,
            }
        )

    return pd.DataFrame.from_records(records)


def insert_page(
    connection: duckdb.DuckDBPyConnection,
    frame: pd.DataFrame,
) -> None:
    """Insert one page into raw.eia_landed."""

    connection.register(
        "_eia_page",
        frame,
    )

    try:
        connection.execute(
            """
            INSERT INTO raw.eia_landed
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
                CAST(raw_row AS JSON),
                loaded_at_utc
            FROM _eia_page
            """
        )

    finally:
        connection.unregister("_eia_page")


def load_eia_run(
    run_directory: Path | None = None,
    *,
    database_path: Path = DATABASE_PATH,
    runs_directory: Path = RUNS_DIRECTORY,
    initialize_sql_path: Path = INITIALIZE_SQL_PATH,
    staging_sql_path: Path = STAGING_SQL_PATH,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Load one complete run and rebuild EIA staging."""

    selected_run = (
        latest_complete_run(runs_directory)
        if run_directory is None
        else run_directory.resolve()
    )

    (
        run_manifest,
        series_manifests,
    ) = validate_manifests(selected_run)

    run_id = str(run_manifest["run_id"])

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = duckdb.connect(
        str(database_path)
    )

    try:
        connection.execute(
            initialize_sql_path.read_text(
                encoding="utf-8"
            )
        )

        already_loaded = connection.execute(
            """
            SELECT 1
            FROM raw.eia_runs
            WHERE run_id = ?
            """,
            [run_id],
        ).fetchone()

        if already_loaded is None:
            loaded_at_utc = datetime.now(
                timezone.utc
            ).replace(tzinfo=None)

            connection.execute(
                "BEGIN TRANSACTION"
            )

            try:
                try:
                    source_directory = str(
                        selected_run.relative_to(
                            project_root
                        )
                    )
                except ValueError:
                    source_directory = str(
                        selected_run
                    )

                connection.execute(
                    """
                    INSERT INTO raw.eia_runs
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, CAST(? AS JSON), ?
                    )
                    """,
                    [
                        run_id,
                        run_manifest["dataset"],
                        run_manifest["endpoint"],
                        run_manifest["start"],
                        run_manifest["end"],
                        run_manifest["frequency"],
                        run_manifest["respondent"],
                        run_manifest["status"],
                        source_directory,
                        utc_naive(
                            run_manifest[
                                "started_at_utc"
                            ]
                        ),
                        utc_naive(
                            run_manifest[
                                "completed_at_utc"
                            ]
                        ),
                        json.dumps(
                            run_manifest,
                            sort_keys=True,
                        ),
                        loaded_at_utc,
                    ],
                )

                for series_type in SERIES_TYPES:
                    manifest = series_manifests[
                        series_type
                    ]

                    connection.execute(
                        """
                        INSERT INTO raw.eia_series_runs
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?,
                            CAST(? AS JSON)
                        )
                        """,
                        [
                            run_id,
                            series_type,
                            manifest["series_name"],
                            int(
                                manifest[
                                    "server_total"
                                ]
                            ),
                            int(
                                manifest[
                                    "downloaded_rows"
                                ]
                            ),
                            int(
                                manifest[
                                    "page_count"
                                ]
                            ),
                            manifest.get(
                                "first_period"
                            ),
                            manifest.get(
                                "last_period"
                            ),
                            json.dumps(
                                manifest,
                                sort_keys=True,
                            ),
                        ],
                    )

                    for page_name in manifest[
                        "page_files"
                    ]:
                        frame = page_frame(
                            selected_run
                            / series_type
                            / page_name,
                            project_root=project_root,
                            run_id=run_id,
                            series_type=series_type,
                            server_total=int(
                                manifest[
                                    "server_total"
                                ]
                            ),
                            loaded_at_utc=(
                                loaded_at_utc
                            ),
                        )

                        insert_page(
                            connection,
                            frame,
                        )

                    loaded_rows = (
                        connection.execute(
                            """
                            SELECT count(*)
                            FROM raw.eia_landed
                            WHERE run_id = ?
                              AND series_type = ?
                            """,
                            [
                                run_id,
                                series_type,
                            ],
                        ).fetchone()[0]
                    )

                    expected_rows = int(
                        manifest[
                            "downloaded_rows"
                        ]
                    )

                    if loaded_rows != expected_rows:
                        raise AssertionError(
                            f"{series_type}: loaded "
                            f"{loaded_rows}; manifest "
                            f"says {expected_rows}."
                        )

                    print(
                        f"[{series_type}] loaded "
                        f"{loaded_rows:,} raw rows"
                    )

                connection.execute(
                    staging_sql_path.read_text(
                        encoding="utf-8"
                    )
                )

                connection.execute("COMMIT")

            except Exception:
                connection.execute("ROLLBACK")
                raise

        else:
            print(
                f"Run {run_id} is already loaded; "
                "rebuilding staging tables."
            )

            connection.execute(
                staging_sql_path.read_text(
                    encoding="utf-8"
                )
            )

        result = {
            "database_path": database_path,
            "run_id": run_id,
            "already_loaded": (
                already_loaded is not None
            ),
            "raw_rows": connection.execute(
                """
                SELECT count(*)
                FROM raw.eia_landed
                WHERE run_id = ?
                """,
                [run_id],
            ).fetchone()[0],
            "staging_rows": connection.execute(
                """
                SELECT count(*)
                FROM stg.eia_hourly_long
                """
            ).fetchone()[0],
            "hourly_rows": connection.execute(
                """
                SELECT count(*)
                FROM stg.eia_hourly
                """
            ).fetchone()[0],
            "quarantine_rows": (
                connection.execute(
                    """
                    SELECT count(*)
                    FROM stg.eia_quarantine
                    """
                ).fetchone()[0]
            ),
        }

        return result

    finally:
        connection.close()
