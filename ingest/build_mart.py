"""Build the rebuildable DuckDB mart layer from the staging tables."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "pjm_grid_ops.duckdb"
)

INITIALIZE_SQL_PATH = (
    PROJECT_ROOT
    / "sql"
    / "00_initialize.sql"
)

MART_SQL_PATH = (
    PROJECT_ROOT
    / "sql"
    / "40_build_mart.sql"
)

REQUIRED_STAGING_TABLES = (
    ("stg", "eia_hourly"),
    ("stg", "weather"),
)

MART_TABLES = (
    "hourly",
    "daily",
    "summer_labels",
)


def require_staging(
    connection: duckdb.DuckDBPyConnection,
) -> None:
    """Fail loudly if the staging inputs are missing."""

    for schema, table in REQUIRED_STAGING_TABLES:
        exists = connection.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = ?
              AND table_name = ?
            """,
            [schema, table],
        ).fetchone()

        if exists is None:
            raise RuntimeError(
                f"Required staging table {schema}.{table} "
                "is missing. Load EIA and weather data first."
            )


def build_mart(
    *,
    database_path: Path = DATABASE_PATH,
    initialize_sql_path: Path = INITIALIZE_SQL_PATH,
    mart_sql_path: Path = MART_SQL_PATH,
) -> dict[str, Any]:
    """Rebuild the mart tables from staging. Idempotent."""

    if not database_path.exists():
        raise FileNotFoundError(
            f"Warehouse not found: {database_path}"
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

        require_staging(connection)

        connection.execute(
            mart_sql_path.read_text(
                encoding="utf-8"
            )
        )

        row_counts = {
            table: connection.execute(
                f"SELECT count(*) FROM mart.{table}"
            ).fetchone()[0]
            for table in MART_TABLES
        }

        for table in MART_TABLES:
            print(
                f"[mart] mart.{table}: "
                f"{row_counts[table]:,} rows"
            )

        return {
            "database_path": database_path,
            "row_counts": row_counts,
        }

    finally:
        connection.close()
