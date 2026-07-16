"""Main entry point for the PJM grid operations project."""

from ingest.eia_metadata import download_region_data_metadata


def main() -> None:
    """Download and save EIA region-data metadata."""

    print("Requesting EIA region-data metadata...")

    output_path = download_region_data_metadata()

    print(f"Metadata saved to: {output_path}")


if __name__ == "__main__":
    main()
