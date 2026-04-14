"""GEON ingestor scheduler.

Runs ingestion jobs on a fixed schedule using the ``schedule`` library.
Designed to run as PID 1 inside the geon-ingestor container.

Usage::

    # Normal cron mode (GDELT every 15 min)
    python scheduler.py

    # One-shot seed, then cron
    python scheduler.py --seed 30d
"""

from __future__ import annotations

import argparse
import time

import schedule

from common.config import setup_logging
from gdelt.ingestor import GDELTIngestor

logger = setup_logging(name="scheduler")


def run_gdelt() -> None:
    """Run the GDELT ingestor (15-minute window)."""
    try:
        ingestor = GDELTIngestor()
        count = ingestor.ingest(timespan="15min")
        logger.info("GDELT cron: %d events indexed.", count)
    except Exception:
        logger.exception("GDELT cron failed.")


def seed_gdelt(days: int) -> None:
    """Seed GDELT data day-by-day (API caps at 250 results per request)."""
    logger.info("Seeding GDELT: %d daily windows ...", days)
    ingestor = GDELTIngestor()
    total = 0
    for i in range(days):
        try:
            count = ingestor.ingest(timespan="1d",
                                    start_offset_days=i)
            total += count
            logger.info("Seed day -%d: %d events (total: %d)", i, count, total)
        except Exception:
            logger.exception("Seed day -%d failed, continuing.", i)
        # Respect GDELT rate limit (1 req / 5s, we do 2 per ingest cycle)
        time.sleep(12)
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
