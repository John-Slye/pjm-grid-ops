"""Download immutable GHCNh weather files for the PJM stations."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

RAW_RUNS_DIRECTORY = (
    PROJECT_ROOT
    / "data"
    / "raw_weather"
    / "ghcnh"
    / "runs"
)

GHCNH_BASE_URL = (
    "https://www.ncei.noaa.gov/oa/"
    "global-historical-climatology-network/hourly/"
    "access/by-year"
)

START_YEAR = 2019
END_YEAR = 2026

ANALYSIS_START_UTC = "2019-01-01T00:00:00Z"
ANALYSIS_END_UTC = "2026-07-01T00:00:00Z"

REQUEST_PAUSE_SECONDS = 0.2

STATIONS: dict[str, dict[str, Any]] = {
    "KPHL": {
        "ghcnh_id": "USW00013739",
        "name": "Philadelphia International Airport",
        "weight": 0.21,
    },
    "KORD": {
        "ghcnh_id": "USW00094846",
        "name": "Chicago O'Hare International Airport",
        "weight": 0.32,
    },
    "KPIT": {
        "ghcnh_id": "USW00094823",
        "name": "Pittsburgh International Airport",
        "weight": 0.08,
    },
    "KDCA": {
        "ghcnh_id": "USW00013743",
        "name": "Ronald Reagan Washington National Airport",
        "weight": 0.21,
    },
    "KCMH": {
        "ghcnh_id": "USW00014821",
        "name": "John Glenn Columbus International Airport",
        "weight": 0.07,
    },
    "KEWR": {
        "ghcnh_id": "USW00014734",
        "name": "Newark Liberty International Airport",
        "weight": 0.11,
    },
}

REQUIRED_COLUMNS = {
    "STATION",
    "Station_name",
    "DATE",
    "LATITUDE",
    "LONGITUDE",
    "temperature",
    "temperature_Measurement_Code",
    "temperature_Quality_Code",
    "temperature_Report_Type",
    "temperature_Source_Code",
    "temperature_Source_Station_ID",
    "dew_point_temperature",
    "dew_point_temperature_Measurement_Code",
    "dew_point_temperature_Quality_Code",
    "dew_point_temperature_Report_Type",
    "dew_point_temperature_Source_Code",
    "dew_point_temperature_Source_Station_ID",
}


class NOAAHTTPError(requests.HTTPError):
    """HTTP error whose message contains no full request URL."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            f"NOAA GHCNh server returned HTTP {status_code}"
        )


def utc_now() -> datetime:
    """Return the current UTC datetime."""

    return datetime.now(timezone.utc)


def format_run_id(value: datetime) -> str:
    """Format a timestamp as a filesystem-safe UTC run ID."""

    return value.strftime("%Y%m%dT%H%M%SZ")


def iso_utc(value: datetime) -> str:
    """Format a UTC datetime for a manifest."""

    return value.isoformat().replace("+00:00", "Z")


def write_json_atomically(
    path: Path,
    payload: dict[str, Any],
) -> None:
    """Write formatted JSON through a temporary file."""

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(path)


def build_file_url(
    station_id: str,
    year: int,
) -> str:
    """Build the official NOAA yearly-station PSV URL."""

    return (
        f"{GHCNH_BASE_URL}/{year}/psv/"
        f"GHCNh_{station_id}_{year}.psv"
    )


def should_retry(exception: BaseException) -> bool:
    """Retry network failures, HTTP 429, and server errors."""

    if isinstance(
        exception,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    ):
        return True

    if isinstance(exception, NOAAHTTPError):
        return (
            exception.status_code == 429
            or exception.status_code >= 500
        )

    return False


def report_retry(
    retry_state: RetryCallState,
) -> None:
    """Print a safe message before retrying."""

    exception = (
        retry_state.outcome.exception()
        if retry_state.outcome
        else None
    )

    print(
        f"Transient NOAA request failure "
        f"({exception}); retrying..."
    )


def is_present_weather_value(value: str | None) -> bool:
    """Return whether a GHCNh weather value is nonmissing."""

    if value is None:
        return False

    normalized = value.strip()

    return normalized not in {
        "",
        "-9999",
        "-9999.0",
    }


def inspect_psv_file(
    path: Path,
    *,
    expected_station_id: str,
    expected_year: int,
) -> dict[str, Any]:
    """Validate and summarize one GHCNh PSV file."""

    row_count = 0
    temperature_count = 0
    dewpoint_count = 0

    first_date: str | None = None
    last_date: str | None = None

    with path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file_handle:
        reader = csv.DictReader(
            file_handle,
            delimiter="|",
        )

        fieldnames = set(
            reader.fieldnames or []
        )

        missing_columns = (
            REQUIRED_COLUMNS - fieldnames
        )

        if missing_columns:
            missing_text = ", ".join(
                sorted(missing_columns)
            )

            raise RuntimeError(
                f"{path.name} is missing required "
                f"columns: {missing_text}"
            )

        for row in reader:
            station_id = (
                row.get("STATION") or ""
            ).strip()

            if station_id != expected_station_id:
                raise RuntimeError(
                    f"{path.name} contains station "
                    f"{station_id!r}; expected "
                    f"{expected_station_id!r}."
                )

            observation_date = (
                row.get("DATE") or ""
            ).strip()

            if not observation_date.startswith(
                f"{expected_year}-"
            ):
                raise RuntimeError(
                    f"{path.name} contains a DATE "
                    f"outside {expected_year}: "
                    f"{observation_date!r}"
                )

            if first_date is None:
                first_date = observation_date

            last_date = observation_date
            row_count += 1

            if is_present_weather_value(
                row.get("temperature")
            ):
                temperature_count += 1

            if is_present_weather_value(
                row.get(
                    "dew_point_temperature"
                )
            ):
                dewpoint_count += 1

    if row_count == 0:
        raise RuntimeError(
            f"{path.name} contained no observations."
        )

    if temperature_count == 0:
        raise RuntimeError(
            f"{path.name} contained no valid "
            "temperature observations."
        )

    return {
        "row_count": row_count,
        "temperature_nonmissing": (
            temperature_count
        ),
        "dewpoint_nonmissing": dewpoint_count,
        "first_date": first_date,
        "last_date": last_date,
        "column_count": len(fieldnames),
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
def download_file(
    session: requests.Session,
    *,
    url: str,
    destination: Path,
) -> dict[str, Any]:
    """Stream one NOAA file to disk and return file metadata."""

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    hasher = hashlib.sha256()
    size_bytes = 0

    try:
        with session.get(
            url,
            stream=True,
            timeout=(10, 120),
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError:
                raise NOAAHTTPError(
                    response.status_code
                ) from None

            with temporary_path.open(
                "wb"
            ) as file_handle:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if not chunk:
                        continue

                    file_handle.write(chunk)
                    hasher.update(chunk)
                    size_bytes += len(chunk)

        if size_bytes == 0:
            raise RuntimeError(
                f"NOAA returned an empty file for {url}"
            )

        return {
            "temporary_path": temporary_path,
            "size_bytes": size_bytes,
            "sha256": hasher.hexdigest(),
        }

    except Exception:
        temporary_path.unlink(
            missing_ok=True
        )
        raise


def download_weather_history() -> Path:
    """Download all station-year GHCNh files into one run."""

    started_at = utc_now()
    run_id = format_run_id(started_at)

    run_directory = (
        RAW_RUNS_DIRECTORY / run_id
    )

    run_directory.mkdir(
        parents=True,
        exist_ok=False,
    )

    manifest_path = (
        run_directory / "run_manifest.json"
    )

    manifest: dict[str, Any] = {
        "status": "running",
        "run_id": run_id,
        "source": "NOAA GHCNh",
        "started_at_utc": iso_utc(
            started_at
        ),
        "analysis_window": {
            "start_utc": ANALYSIS_START_UTC,
            "end_utc": ANALYSIS_END_UTC,
        },
        "downloaded_years": list(
            range(
                START_YEAR,
                END_YEAR + 1,
            )
        ),
        "stations": STATIONS,
        "files": [],
    }

    write_json_atomically(
        manifest_path,
        manifest,
    )

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                "pjm-grid-ops/0.1.0 "
                "(educational energy analytics project)"
            )
        }
    )

    try:
        for year in range(
            START_YEAR,
            END_YEAR + 1,
        ):
            print(f"\nYear {year}")

            for icao, station in STATIONS.items():
                station_id = str(
                    station["ghcnh_id"]
                )

                filename = (
                    f"GHCNh_{station_id}_{year}.psv"
                )

                destination = (
                    run_directory
                    / str(year)
                    / filename
                )

                url = build_file_url(
                    station_id,
                    year,
                )

                print(
                    f"  Downloading {icao} "
                    f"({station_id})..."
                )

                file_result = download_file(
                    session,
                    url=url,
                    destination=destination,
                )

                temporary_path = Path(
                    file_result[
                        "temporary_path"
                    ]
                )

                inspection = inspect_psv_file(
                    temporary_path,
                    expected_station_id=(
                        station_id
                    ),
                    expected_year=year,
                )

                temporary_path.replace(
                    destination
                )

                file_manifest = {
                    "icao": icao,
                    "station_id": station_id,
                    "station_name": (
                        station["name"]
                    ),
                    "weight": station["weight"],
                    "year": year,
                    "url": url,
                    "relative_path": str(
                        destination.relative_to(
                            PROJECT_ROOT
                        )
                    ),
                    "retrieved_at_utc": (
                        iso_utc(utc_now())
                    ),
                    "size_bytes": (
                        file_result[
                            "size_bytes"
                        ]
                    ),
                    "sha256": (
                        file_result["sha256"]
                    ),
                    **inspection,
                }

                manifest["files"].append(
                    file_manifest
                )

                write_json_atomically(
                    manifest_path,
                    manifest,
                )

                print(
                    f"    {inspection['row_count']:,} "
                    "observations; "
                    f"{inspection['temperature_nonmissing']:,} "
                    "temperatures"
                )

                time.sleep(
                    REQUEST_PAUSE_SECONDS
                )

        expected_file_count = (
            len(STATIONS)
            * (
                END_YEAR
                - START_YEAR
                + 1
            )
        )

        actual_file_count = len(
            manifest["files"]
        )

        if actual_file_count != expected_file_count:
            raise AssertionError(
                f"Downloaded {actual_file_count} "
                f"files; expected "
                f"{expected_file_count}."
            )

        manifest["status"] = "complete"
        manifest["completed_at_utc"] = (
            iso_utc(utc_now())
        )
        manifest["file_count"] = (
            actual_file_count
        )

        write_json_atomically(
            manifest_path,
            manifest,
        )

    except Exception as exception:
        manifest["status"] = "failed"
        manifest["failed_at_utc"] = (
            iso_utc(utc_now())
        )
        manifest["error"] = {
            "type": type(
                exception
            ).__name__,
            "message": str(exception),
        }

        write_json_atomically(
            manifest_path,
            manifest,
        )

        raise

    finally:
        session.close()

    return run_directory
