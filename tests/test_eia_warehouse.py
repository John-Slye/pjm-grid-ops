"""Data-quality tests for the local EIA DuckDB warehouse."""

from pathlib import Path

import duckdb
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "pjm_grid_ops.duckdb"
)


@pytest.fixture
def warehouse():
    """Open the local warehouse or skip if it has not been built."""

    if not DATABASE_PATH.exists():
        pytest.skip(
            "Local DuckDB warehouse has not been built."
        )

    connection = duckdb.connect(
        str(DATABASE_PATH),
        read_only=True,
    )

    connection.execute(
        "SET TimeZone = 'UTC'"
    )

    try:
        yield connection

    finally:
        connection.close()


def test_raw_counts_match_manifests(
    warehouse,
) -> None:
    """Every loaded series must match its run manifest."""

    mismatch_count = warehouse.execute(
        """
        SELECT count(*)
        FROM (
            SELECT
                s.run_id,
                s.series_type,
                s.downloaded_rows,
                count(l.period) AS loaded_rows
            FROM raw.eia_series_runs AS s
            LEFT JOIN raw.eia_landed AS l
                USING (run_id, series_type)
            GROUP BY
                s.run_id,
                s.series_type,
                s.downloaded_rows
            HAVING loaded_rows
                <> s.downloaded_rows
        )
        """
    ).fetchone()[0]

    assert mismatch_count == 0


def test_no_duplicate_current_series_rows(
    warehouse,
) -> None:
    """Current staging cannot contain duplicate series timestamps."""

    duplicate_count = warehouse.execute(
        """
        SELECT count(*)
        FROM (
            SELECT
                ts_utc,
                series_type,
                count(*) AS row_count
            FROM stg.eia_hourly_long
            GROUP BY
                ts_utc,
                series_type
            HAVING row_count > 1
        )
        """
    ).fetchone()[0]

    assert duplicate_count == 0


def test_quarantine_rate_below_half_percent(
    warehouse,
) -> None:
    """Unparseable current EIA rows must remain below 0.5%."""

    valid_rows = warehouse.execute(
        """
        SELECT count(*)
        FROM stg.eia_hourly_long
        """
    ).fetchone()[0]

    quarantined_rows = warehouse.execute(
        """
        SELECT count(*)
        FROM stg.eia_quarantine
        """
    ).fetchone()[0]

    total_rows = valid_rows + quarantined_rows

    assert total_rows > 0

    quarantine_rate = (
        quarantined_rows
        / total_rows
    )

    assert quarantine_rate < 0.005


def test_series_count_is_valid(
    warehouse,
) -> None:
    """An hourly row can contain at most the four expected series."""

    invalid_count = warehouse.execute(
        """
        SELECT count(*)
        FROM stg.eia_hourly
        WHERE series_count < 1
           OR series_count > 4
        """
    ).fetchone()[0]

    assert invalid_count == 0
