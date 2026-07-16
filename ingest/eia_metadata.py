"""Download metadata for the EIA region-data dataset."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
RAW_DATA_DIRECTORY = PROJECT_ROOT / "data" / "raw"

EIA_REGION_DATA_METADATA_URL = (
    "https://api.eia.gov/v2/electricity/rto/region-data/"
)


def get_api_key() -> str:
    """Load the EIA API key from the project's local .env file."""

    load_dotenv(ENV_FILE)

    api_key = os.getenv("EIA_API_KEY")

    if not api_key:
        raise RuntimeError(
            "EIA_API_KEY was not found. "
            "Check the .env file in the project root."
        )

    return api_key


def download_region_data_metadata() -> Path:
    """Request EIA region-data metadata and save the raw JSON response."""

    response = requests.get(
        EIA_REGION_DATA_METADATA_URL,
        params={"api_key": get_api_key()},
        timeout=30,
    )

    response.raise_for_status()
    metadata = response.json()

    RAW_DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)

    retrieved_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = (
        RAW_DATA_DIRECTORY
        / f"eia_region_data_metadata_{retrieved_at}.json"
    )

    output_path.write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )

    return output_path
