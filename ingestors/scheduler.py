"""GEON ingestor scheduler.

Runs ingestion jobs on a fixed schedule using the ``schedule`` library.
Designed to run as PID 1 inside the geon-ingestor container.

Usage::

    # Normal cron mode (GDELT every 15 min)
    python scheduler.py

    # One-shot seed (N days of 15-min CSV windows), then cron
    python scheduler.py --seed 1
"""

from __future__ import annotations

import argparse
import time

import schedule

from common.config import setup_logging
from gdelt.ingestor import GDELTIngestor

logger = setup_logging(name="scheduler")


def run_gdelt() -> None:
    """Run the GDELT ingestor (latest 15-minute CSV window)."""
    try:
        ingestor = GDELTIngestor()
        count = ingestor.ingest(windows=1)
        logger.info("GDELT cron: %d events indexed.", count)
    except Exception:
        logger.exception("GDELT cron failed.")


def seed_gdelt(days: int) -> None:
    """Seed GDELT data by fetching 15-min CSV exports for *days* days."""
    windows = days * 96  # 96 fifteen-minute windows per day
    logger.info("Seeding GDELT: %d windows (%d days) …", windows, days)
    ingestor = GDELTIngestor()
    total = ingestor.ingest(windows=windows)
    logger.info("GDELT seed complete: %d total events.", total)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed",
        metavar="DAYS",
        type=int,
        help="Seed N days of historical data before starting the cron (e.g. 30).",
    )
    args = parser.parse_args()

    if args.seed:
        seed_gdelt(int(args.seed))

    # --- Schedule recurring jobs ---
    schedule.every(15).minutes.do(run_gdelt)

    # Run GDELT once immediately so we don't wait 15 min for first data.
    run_gdelt()

    logger.info("Scheduler started. Jobs: GDELT every 15 min.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
