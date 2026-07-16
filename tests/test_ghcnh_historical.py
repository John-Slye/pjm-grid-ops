"""Unit tests for NOAA GHCNh historical ingestion."""

import requests
import pytest

from ingest.ghcnh_historical import (
    NOAAHTTPError,
    STATIONS,
    build_file_url,
    is_present_weather_value,
    should_retry,
)


def test_station_weights_sum_to_one() -> None:
    """Population weights must form a complete composite."""

    total_weight = sum(
        station["weight"]
        for station in STATIONS.values()
    )

    assert total_weight == pytest.approx(
        1.0
    )


def test_expected_stations_are_present() -> None:
    """The project should use the specified six stations."""

    assert set(STATIONS) == {
        "KPHL",
        "KORD",
        "KPIT",
        "KDCA",
        "KCMH",
        "KEWR",
    }


def test_build_file_url() -> None:
    """Yearly NOAA file URLs should be deterministic."""

    url = build_file_url(
        "USW00013739",
        2024,
    )

    assert url.endswith(
        "/2024/psv/"
        "GHCNh_USW00013739_2024.psv"
    )


def test_retry_rate_limit() -> None:
    """HTTP 429 should be retried."""

    assert should_retry(
        NOAAHTTPError(429)
    )


def test_retry_server_error() -> None:
    """HTTP 5xx should be retried."""

    assert should_retry(
        NOAAHTTPError(503)
    )


def test_do_not_retry_missing_file() -> None:
    """HTTP 404 indicates a permanent file problem."""

    assert not should_retry(
        NOAAHTTPError(404)
    )


def test_retry_timeout() -> None:
    """Network timeouts should be retried."""

    assert should_retry(
        requests.exceptions.Timeout()
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("215", True),
        ("0", True),
        ("", False),
        (" ", False),
        ("-9999", False),
        ("-9999.0", False),
        (None, False),
    ],
)
def test_present_weather_value(
    value,
    expected,
) -> None:
    """GHCNh missing sentinels should be identified."""

    assert (
        is_present_weather_value(value)
        is expected
    )
