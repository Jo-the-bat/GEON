"""GEON ingestor scheduler.

Runs all ingestion jobs on a fixed schedule using the ``schedule`` library.
Designed to run as PID 1 inside the geon-ingestor container.

Usage::

    # Normal cron mode (all jobs on their schedules)
    python scheduler.py

    # One-shot seed (N days of GDELT + ACLED), then cron
    python scheduler.py --seed 1
"""

from __future__ import annotations

import argparse
import time

import schedule

from common.config import ACLED_API_KEY, setup_logging

logger = setup_logging(name="scheduler")


# ---------------------------------------------------------------------------
# Job wrappers
# ---------------------------------------------------------------------------

def run_gdelt() -> None:
    """Run the GDELT ingestor (latest 15-minute CSV window)."""
    try:
        from gdelt.ingestor import GDELTIngestor
        count = GDELTIngestor().ingest(windows=1)
        logger.info("GDELT cron: %d events indexed.", count)
    except Exception:
        logger.exception("GDELT cron failed.")


def run_gkg() -> None:
    """Run the GDELT GKG ingestor (latest 15-minute CSV window)."""
    try:
        from gkg.ingestor import GKGIngestor
        count = GKGIngestor().ingest(windows=1)
        logger.info("GKG cron: %d documents indexed.", count)
    except Exception:
        logger.exception("GKG cron failed.")


def run_opencti_export() -> None:
    """Export CTI entities from OpenCTI → Elasticsearch."""
    try:
        from opencti_export.exporter import OpenCTIExporter
        count = OpenCTIExporter().run(full=False)
        logger.info("OpenCTI export cron: %d documents indexed.", count)
    except Exception:
        logger.exception("OpenCTI export cron failed.")


def run_acled() -> None:
    """Run the ACLED ingestor (incremental)."""
    if not ACLED_API_KEY:
        logger.warning("ACLED_API_KEY not set, skipping ACLED ingestion.")
        return
    try:
        from acled.ingestor import ACLEDIngestor
        count = ACLEDIngestor().run()
        logger.info("ACLED cron: %d events indexed.", count)
    except Exception:
        logger.exception("ACLED cron failed.")


def run_sanctions() -> None:
    """Run the sanctions ingestor (OFAC SDN)."""
    try:
        from sanctions.ingestor import SanctionsIngestor
        count = SanctionsIngestor().run()
        logger.info("Sanctions cron: %d entities indexed.", count)
    except Exception:
        logger.exception("Sanctions cron failed.")


def run_polymarket() -> None:
    """Ingest and enrich Polymarket geopolitical prediction markets."""
    try:
        from polymarket.ingestor import PolymarketIngestor
        ing = PolymarketIngestor()
        count = ing.ingest()
        logger.info("Polymarket cron: %d cases indexed.", count)
    except Exception:
        logger.exception("Polymarket cron failed.")


def run_polymarket_enrich() -> None:
    """Enrich existing Polymarket cases with GEON context."""
    try:
        from polymarket.ingestor import PolymarketIngestor
        count = PolymarketIngestor().enrich()
        logger.info("Polymarket enrich cron: %d cases updated.", count)
    except Exception:
        logger.exception("Polymarket enrich cron failed.")


def run_sipri() -> None:
    """Seed/update SIPRI arms transfers and military spending data."""
    try:
        from sipri.ingestor import SIPRIIngestor
        count = SIPRIIngestor().run(seed=True)
        logger.info("SIPRI cron: %d documents indexed.", count)
    except Exception:
        logger.exception("SIPRI cron failed.")


def run_prediction_consensus() -> None:
    """Ingest Metaculus/Manifold prediction markets and compute consensus."""
    try:
        from prediction_consensus.ingestor import PredictionConsensusIngestor
        count = PredictionConsensusIngestor().ingest()
        logger.info("Prediction consensus cron: %d markets indexed.", count)
    except Exception:
        logger.exception("Prediction consensus cron failed.")


def run_cloudflare_radar() -> None:
    """Ingest Cloudflare Radar internet outages."""
    try:
        from cloudflare_radar.ingestor import CloudflareRadarIngestor
        count = CloudflareRadarIngestor().ingest(date_range="7d")
        logger.info("Cloudflare Radar cron: %d outages indexed.", count)
    except Exception:
        logger.exception("Cloudflare Radar cron failed.")


def run_risk_scores() -> None:
    """Calculate and index country risk scores."""
    try:
        from risk_score.calculator import RiskScoreCalculator
        count = RiskScoreCalculator().run()
        logger.info("Risk scores cron: %d countries scored.", count)
    except Exception:
        logger.exception("Risk scores cron failed.")


def run_correlation() -> None:
    """Run the correlation engine (all 4 rules)."""
    try:
        from correlation.engine import CorrelationEngine
        CorrelationEngine().run()
        logger.info("Correlation cron completed.")
    except Exception:
        logger.exception("Correlation cron failed.")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_gdelt(days: int) -> None:
    """Seed GDELT data by fetching 15-min CSV exports for *days* days."""
    from gdelt.ingestor import GDELTIngestor
    windows = days * 96
    logger.info("Seeding GDELT: %d windows (%d days) …", windows, days)
    total = GDELTIngestor().ingest(windows=windows)
    logger.info("GDELT seed complete: %d total events.", total)


def seed_acled(days: int) -> None:
    """Seed ACLED data for *days* days."""
    if not ACLED_API_KEY:
        logger.warning("ACLED_API_KEY not set, skipping ACLED seed.")
        return
    from acled.ingestor import ACLEDIngestor
    logger.info("Seeding ACLED: %d days …", days)
    count = ACLEDIngestor().run(days=days)
    logger.info("ACLED seed complete: %d events.", count)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed",
        metavar="DAYS",
        type=int,
        help="Seed N days of historical data before starting the cron.",
    )
    args = parser.parse_args()

    # --- Optional seed phase ---
    if args.seed:
        seed_gdelt(args.seed)
        seed_acled(args.seed)

    # --- Schedule recurring jobs ---
    schedule.every(15).minutes.do(run_gdelt)
    schedule.every(15).minutes.do(run_gkg)
    schedule.every(1).hours.do(run_opencti_export)
    schedule.every(1).days.at("03:00").do(run_acled)
    schedule.every().sunday.at("04:00").do(run_sanctions)
    schedule.every(30).minutes.do(run_correlation)
    schedule.every(1).hours.do(run_polymarket)
    schedule.every(2).hours.do(run_polymarket_enrich)
    schedule.every(30).minutes.do(run_cloudflare_radar)
    schedule.every(2).hours.do(run_prediction_consensus)
    schedule.every().monday.at("02:00").do(run_sipri)
    schedule.every(1).days.at("05:00").do(run_risk_scores)

    # Run each once immediately.
    run_gdelt()
    run_gkg()
    run_opencti_export()
    run_acled()
    run_sanctions()
    run_correlation()
    run_polymarket()
    run_sipri()
    run_cloudflare_radar()
    run_prediction_consensus()
    run_risk_scores()

    logger.info(
        "Scheduler started. Jobs: GDELT/15min, OpenCTI export/1h, "
        "ACLED/daily, Sanctions/weekly, Correlation/30min."
    )
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
