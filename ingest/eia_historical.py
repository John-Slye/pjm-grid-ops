"""Download immutable historical PJM series from the EIA API v2."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

RAW_RUNS_DIRECTORY = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "eia"
    / "region_data"
    / "runs"
)

EIA_REGION_DATA_URL = (
    "https://api.eia.gov/v2/electricity/rto/region-data/data/"
)

START_PERIOD = "2019-01-01T00"
END_PERIOD = "2026-07-01T00"

PAGE_SIZE = 5_000
REQUEST_PAUSE_SECONDS = 0.4

SERIES_TYPES = {
    "D": "Demand",
    "DF": "Day-ahead demand forecast",
    "NG": "Net generation",
    "TI": "Total interchange",
}


class EIAHTTPError(requests.HTTPError):
    """HTTP error that does not expose the API key in its message."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"EIA API returned HTTP {status_code}")


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def format_run_id(value: datetime) -> str:
    """Format a UTC datetime as a filesystem-safe run identifier."""

    return value.strftime("%Y%m%dT%H%M%SZ")


def iso_utc(value: datetime) -> str:
    """Format a UTC datetime for a manifest."""

    return value.isoformat().replace("+00:00", "Z")


def get_api_key() -> str:
    """Load and validate the EIA API key from the project .env file."""

    load_dotenv(ENV_FILE)
    api_key = os.getenv("EIA_API_KEY")

    if not api_key:
        raise RuntimeError(
            "EIA_API_KEY was not found. "
            "Check the .env file in the project root."
        )

    return api_key


def should_retry(exception: BaseException) -> bool:
    """Retry network errors plus HTTP 429 and 5xx responses."""

    if isinstance(
        exception,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    ):
        return True

    if isinstance(exception, EIAHTTPError):
        return (
            exception.status_code == 429
            or exception.status_code >= 500
        )

    return False


def report_retry(retry_state: RetryCallState) -> None:
    """Print a safe message before retrying a transient failure."""

    exception = (
        retry_state.outcome.exception()
        if retry_state.outcome
        else None
    )

    print(
        f"Transient request failure ({exception}); retrying..."
    )


def build_query_params(
    *,
    api_key: str,
    series_type: str,
    offset: int,
    start: str = START_PERIOD,
    end: str = END_PERIOD,
) -> dict[str, str | int]:
    """Build one deterministic, paginated EIA request."""

    if series_type not in SERIES_TYPES:
        raise ValueError(
            f"Unsupported EIA series type: {series_type}"
        )

    return {
        "api_key": api_key,
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": "PJM",
        "facets[type][]": series_type,
        "start": start,
        "end": end,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "offset": offset,
        "length": PAGE_SIZE,
    }


def public_query_params(
    *,
    series_type: str,
    start: str = START_PERIOD,
    end: str = END_PERIOD,
) -> dict[str, Any]:
    """Return manifest-safe parameters without the API key."""

    return {
        "frequency": "hourly",
        "data": ["value"],
        "facets": {
            "respondent": ["PJM"],
            "type": [series_type],
        },
        "start": start,
        "end": end,
        "sort": [
            {
                "column": "period",
                "direction": "asc",
            }
        ],
        "page_size": PAGE_SIZE,
    }


@retry(
    retry=retry_if_exception(should_retry),
    stop=stop_after_attempt(5),
    wait=wait_random_exponential(
        multiplier=1,
        max=30,
    ),
    before_sleep=report_retry,
    reraise=True,
)
def fetch_page(
    session: requests.Session,
    params: dict[str, str | int],
) -> tuple[dict[str, Any], bytes]:
    """Fetch and validate one EIA response page."""

    response = session.get(
        EIA_REGION_DATA_URL,
        params=params,
        timeout=(10, 60),
    )

    try:
        response.raise_for_status()
    except requests.HTTPError:
        # Replace the original exception because its message may
        # contain the request URL, including the API key.
        raise EIAHTTPError(response.status_code) from None

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError(
            "EIA response was not a JSON object."
        )

    envelope = payload.get("response")

    if not isinstance(envelope, dict):
        raise RuntimeError(
            "EIA response did not contain a 'response' object."
        )

    if "total" not in envelope or "data" not in envelope:
        raise RuntimeError(
            "EIA response was missing 'total' or 'data'."
        )

    if not isinstance(envelope["data"], list):
        raise RuntimeError(
            "EIA response 'data' was not a list."
        )

    return payload, response.content


def write_bytes_atomically(
    path: Path,
    content: bytes,
) -> None:
    """Write bytes to a temporary file, then rename into place."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_bytes(content)
    temporary_path.replace(path)


def write_json_atomically(
    path: Path,
    payload: dict[str, Any],
) -> None:
    """Write formatted JSON atomically."""

    encoded = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")

    write_bytes_atomically(path, encoded)


def validate_rows(
    rows: list[dict[str, Any]],
    *,
    series_type: str,
    seen_periods: set[str],
) -> None:
    """Validate identity fields and reject duplicate periods."""

    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError(
                f"{series_type}: encountered a non-object row."
            )

        if row.get("respondent") != "PJM":
            raise RuntimeError(
                f"{series_type}: received respondent "
                f"{row.get('respondent')!r}."
            )

        if row.get("type") != series_type:
            raise RuntimeError(
                f"{series_type}: received type "
                f"{row.get('type')!r}."
            )

        period = row.get("period")

        if not isinstance(period, str) or not period:
            raise RuntimeError(
                f"{series_type}: encountered a missing period."
            )

        if period in seen_periods:
            raise RuntimeError(
                f"{series_type}: duplicate period encountered: "
                f"{period}"
            )

        seen_periods.add(period)


def download_series(
    session: requests.Session,
    *,
    run_directory: Path,
    api_key: str,
    series_type: str,
    start: str = START_PERIOD,
    end: str = END_PERIOD,
) -> dict[str, Any]:
    """Download every page for one series and assert completeness."""

    series_directory = run_directory / series_type

    series_directory.mkdir(
        parents=True,
        exist_ok=False,
    )

    started_at = utc_now()

    offset = 0
    page_number = 1
    expected_total: int | None = None
    rows_downloaded = 0

    seen_periods: set[str] = set()
    page_files: list[str] = []

    while True:
        params = build_query_params(
            api_key=api_key,
            series_type=series_type,
            offset=offset,
            start=start,
            end=end,
        )

        payload, raw_content = fetch_page(
            session,
            params,
        )

        envelope = payload["response"]
        current_total = int(envelope["total"])
        rows = envelope["data"]

        if expected_total is None:
            expected_total = current_total

        elif current_total != expected_total:
            raise RuntimeError(
                f"{series_type}: server total changed from "
                f"{expected_total} to {current_total} "
                "during the pull."
            )

        if not rows and rows_downloaded < expected_total:
            raise RuntimeError(
                f"{series_type}: received an empty page before "
                f"reaching the server total of {expected_total}."
            )

        validate_rows(
            rows,
            series_type=series_type,
            seen_periods=seen_periods,
        )

        page_name = (
            f"page_{page_number:03d}_"
            f"offset_{offset:06d}.json"
        )

        page_path = series_directory / page_name

        write_bytes_atomically(
            page_path,
            raw_content,
        )

        page_files.append(page_name)
        rows_downloaded += len(rows)

        print(
            f"[{series_type}] page {page_number:02d}: "
            f"{rows_downloaded:,}/{expected_total:,} rows"
        )

        if rows_downloaded >= expected_total:
            break

        offset += len(rows)
        page_number += 1

        time.sleep(REQUEST_PAUSE_SECONDS)

    if expected_total is None:
        raise RuntimeError(
            f"{series_type}: no server total was received."
        )

    if rows_downloaded != expected_total:
        raise AssertionError(
            f"{series_type}: downloaded {rows_downloaded} rows, "
            f"but the server reported {expected_total}."
        )

    completed_at = utc_now()
    sorted_periods = sorted(seen_periods)

    manifest = {
        "status": "complete",
        "series_type": series_type,
        "series_name": SERIES_TYPES[series_type],
        "endpoint": EIA_REGION_DATA_URL,
        "query": public_query_params(
            series_type=series_type,
            start=start,
            end=end,
        ),
        "started_at_utc": iso_utc(started_at),
        "completed_at_utc": iso_utc(completed_at),
        "server_total": expected_total,
        "downloaded_rows": rows_downloaded,
        "page_count": len(page_files),
        "first_period": (
            sorted_periods[0]
            if sorted_periods
            else None
        ),
        "last_period": (
            sorted_periods[-1]
            if sorted_periods
            else None
        ),
        "page_files": page_files,
    }

    write_json_atomically(
        series_directory / "manifest.json",
        manifest,
    )

    return manifest


def download_historical_eia(
    *,
    start: str = START_PERIOD,
    end: str = END_PERIOD,
) -> Path:
    """Download all four historical PJM series into one raw run."""

    api_key = get_api_key()
    started_at = utc_now()

    run_id = format_run_id(started_at)
    run_directory = RAW_RUNS_DIRECTORY / run_id

    run_directory.mkdir(
        parents=True,
        exist_ok=False,
    )

    run_manifest_path = (
        run_directory / "run_manifest.json"
    )

    run_manifest: dict[str, Any] = {
        "status": "running",
        "run_id": run_id,
        "dataset": "electricity/rto/region-data",
        "endpoint": EIA_REGION_DATA_URL,
        "started_at_utc": iso_utc(started_at),
        "start": start,
        "end": end,
        "frequency": "hourly",
        "respondent": "PJM",
        "series_types": list(SERIES_TYPES),
        "series": {},
    }

    write_json_atomically(
        run_manifest_path,
        run_manifest,
    )

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": "pjm-grid-ops/0.1.0",
        }
    )

    try:
        for series_type in SERIES_TYPES:
            print(
                f"\nDownloading {series_type}: "
                f"{SERIES_TYPES[series_type]}"
            )

            manifest = download_series(
                session,
                run_directory=run_directory,
                api_key=api_key,
                series_type=series_type,
                start=start,
                end=end,
            )

            run_manifest["series"][series_type] = manifest

        run_manifest["status"] = "complete"
        run_manifest["completed_at_utc"] = iso_utc(
            utc_now()
        )

        write_json_atomically(
            run_manifest_path,
            run_manifest,
        )

    except Exception as exception:
        run_manifest["status"] = "failed"
        run_manifest["failed_at_utc"] = iso_utc(
            utc_now()
        )

        run_manifest["error"] = {
            "type": type(exception).__name__,
            "message": str(exception),
        }

        write_json_atomically(
            run_manifest_path,
            run_manifest,
        )

        raise

    finally:
        session.close()

    return run_directory
