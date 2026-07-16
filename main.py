"""Command-line entry point for the PJM grid operations project."""

from __future__ import annotations

import argparse

from ingest.eia_historical import download_historical_eia
from ingest.eia_metadata import download_region_data_metadata


def build_parser() -> argparse.ArgumentParser:
    """Create the project command-line parser."""

    parser = argparse.ArgumentParser(
        description="Run PJM grid operations data-pipeline tasks."
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
            "Download historical PJM D, DF, NG, and TI "
            "series from the EIA API."
        ),
    )

    return parser


def main() -> None:
    """Run the selected pipeline task."""

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "metadata":
        output_path = download_region_data_metadata()

        print(
            f"EIA metadata saved to: {output_path}"
        )

    elif args.command == "eia-history":
        run_directory = download_historical_eia()

        print(
            "\nHistorical EIA ingestion completed."
        )

        print(
            f"Raw run saved to: {run_directory}"
        )

    else:
        parser.error(
            f"Unsupported command: {args.command}"
        )


if __name__ == "__main__":
    main()
