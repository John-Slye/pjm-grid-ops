"""Unit tests for historical EIA ingestion."""

import requests

from ingest.eia_historical import (
    EIAHTTPError,
    build_query_params,
    should_retry,
)


def test_build_query_params() -> None:
    """The request should contain the required EIA filters."""

    params = build_query_params(
        api_key="test-key",
        series_type="D",
        offset=5_000,
    )

    assert params["frequency"] == "hourly"
    assert params["data[0]"] == "value"
    assert params["facets[respondent][]"] == "PJM"
    assert params["facets[type][]"] == "D"
    assert params["start"] == "2019-01-01T00"
    assert params["end"] == "2026-07-01T00"
    assert params["sort[0][column]"] == "period"
    assert params["sort[0][direction]"] == "asc"
    assert params["offset"] == 5_000
    assert params["length"] == 5_000


def test_retry_http_429() -> None:
    """Rate limiting should be retried."""

    assert should_retry(EIAHTTPError(429))


def test_retry_http_500() -> None:
    """Server errors should be retried."""

    assert should_retry(EIAHTTPError(500))


def test_do_not_retry_http_400() -> None:
    """Invalid queries should fail immediately."""

    assert not should_retry(EIAHTTPError(400))


def test_do_not_retry_http_403() -> None:
    """Invalid or unauthorized keys should fail immediately."""

    assert not should_retry(EIAHTTPError(403))


def test_retry_timeout() -> None:
    """Network timeouts should be retried."""

    assert should_retry(
        requests.exceptions.Timeout()
    )


def test_retry_connection_error() -> None:
    """Connection failures should be retried."""

    assert should_retry(
        requests.exceptions.ConnectionError()
    )
