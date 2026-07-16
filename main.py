"""Command-line entry point for the PJM grid operations project."""

from __future__ import annotations

import argparse
from pathlib import Path

from ingest.build_mart import build_mart
from ingest.eia_historical import download_historical_eia
from ingest.eia_metadata import download_region_data_metadata
from ingest.ghcnh_historical import download_weather_history
from ingest.load_eia_duckdb import load_eia_run
from ingest.load_weather_duckdb import load_weather_run


def build_parser() -> argparse.ArgumentParser:
    """Create the project command-line parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Run PJM grid operations "
            "data-pipeline tasks."
        )
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    subparsers.add_parser(
        "metadata",
        help="Download EIA region-data metadata.",
    )

    subparsers.add_parser(
        "eia-history",
        help=(
            "Download historical PJM D, DF, "
            "NG, and TI series."
        ),
    )

    duckdb_parser = subparsers.add_parser(
        "duckdb-load-eia",
        help=(
            "Load a complete historical EIA "
            "run into DuckDB."
        ),
    )

    duckdb_parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help=(
            "Optional path to a specific EIA run. "
            "Defaults to the newest complete run."
        ),
    )

    subparsers.add_parser(
        "weather-history",
        help=(
            "Download historical NOAA GHCNh "
            "files for the six PJM stations."
        ),
    )

    weather_load_parser = subparsers.add_parser(
        "duckdb-load-weather",
        help=(
            "Parse a complete GHCNh weather run "
            "into DuckDB and build the composite."
        ),
    )

    weather_load_parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help=(
            "Optional path to a specific weather run. "
            "Defaults to the newest complete run."
        ),
    )

    subparsers.add_parser(
        "build-mart",
        help=(
            "Rebuild the mart layer from the "
            "EIA and weather staging tables."
        ),
    )

    subparsers.add_parser(
        "build-all",
        help=(
            "Load EIA, load weather, and build "
            "the mart layer end to end."
        ),
    )

    return parser


def main() -> None:
    """Run the selected pipeline task."""

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "metadata":
        output_path = (
            download_region_data_metadata()
        )

        print(
            f"EIA metadata saved to: "
            f"{output_path}"
        )

    elif args.command == "eia-history":
        run_directory = (
            download_historical_eia()
        )

        print(
            "\nHistorical EIA ingestion "
            "completed."
        )

        print(
            f"Raw run saved to: "
            f"{run_directory}"
        )

    elif args.command == "duckdb-load-eia":
        result = load_eia_run(
            args.run_dir
        )

        print(
            "\nDuckDB EIA load completed."
        )
        print(
            f"Database: "
            f"{result['database_path']}"
        )
        print(
            f"Run ID: "
            f"{result['run_id']}"
        )
        print(
            f"Already loaded: "
            f"{result['already_loaded']}"
        )
        print(
            f"Raw rows: "
            f"{result['raw_rows']:,}"
        )
        print(
            f"Valid staging rows: "
            f"{result['staging_rows']:,}"
        )
        print(
            f"Hourly rows: "
            f"{result['hourly_rows']:,}"
        )
        print(
            f"Quarantine rows: "
            f"{result['quarantine_rows']:,}"
        )

    elif args.command == "weather-history":
        run_directory = (
            download_weather_history()
        )

        print(
            "\nHistorical GHCNh weather "
            "ingestion completed."
        )

        print(
            f"Raw weather run saved to: "
            f"{run_directory}"
        )

    elif args.command == "duckdb-load-weather":
        result = load_weather_run(
            args.run_dir
        )

        print(
            "\nDuckDB weather load completed."
        )
        print(
            f"Database: "
            f"{result['database_path']}"
        )
        print(
            f"Run ID: "
            f"{result['run_id']}"
        )
        print(
            f"Observation rows: "
            f"{result['observation_rows']:,}"
        )
        print(
            f"Quarantine rows: "
            f"{result['quarantine_rows']:,}"
        )
        print(
            f"Composite hours: "
            f"{result['staging_rows']:,}"
        )

    elif args.command == "build-mart":
        result = build_mart()

        print(
            "\nMart build completed."
        )
        print(
            f"Database: "
            f"{result['database_path']}"
        )

        for table, count in (
            result["row_counts"].items()
        ):
            print(
                f"mart.{table}: {count:,} rows"
            )

    elif args.command == "build-all":
        print("Loading EIA data...")
        load_eia_run()

        print("\nLoading weather data...")
        load_weather_run()

        print("\nBuilding mart layer...")
        build_mart()

        print(
            "\nEnd-to-end build completed "
            "against data/pjm_grid_ops.duckdb."
        )

    else:
        parser.error(
            f"Unsupported command: "
            f"{args.command}"
        )


if __name__ == "__main__":
    main()
