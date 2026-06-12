"""GEON ingestor scheduler.

Runs all ingestion jobs on a fixed schedule using the ``schedule`` library.
Designed to run as PID 1 inside the geon-ingestor container.

Every job is instrumented with Prometheus metrics, exposed on
``:9108/metrics`` (scraped by the geon-prometheus container):

- ``geon_job_runs_total{job,status}`` — success / failure / skipped counts
- ``geon_job_duration_seconds{job}`` — duration of the last run
- ``geon_job_last_success_timestamp_seconds{job}`` — staleness detection
- ``geon_job_last_result_docs{job}`` — documents produced by the last run

Usage::

    # Normal cron mode (all jobs on their schedules, no immediate run)
    python scheduler.py

    # Bootstrap mode: seed data sources once, then enter cron loop
    python scheduler.py --bootstrap

    # One-shot seed (N days of GDELT + ACLED), then cron
    python scheduler.py --seed 1
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any, Callable

import schedule
from common.config import ACLED_API_KEY, setup_logging
from prometheus_client import Counter, Gauge, start_http_server

# Configure the ROOT logger so every module's INFO output (rules,
# ingestors, opencti client) reaches the container logs — a named
# setup_logging("scheduler") only made the scheduler's own lines visible.
setup_logging()
logger = logging.getLogger("scheduler")

METRICS_PORT = 9108

JOB_RUNS = Counter(
    "geon_job_runs_total",
    "Ingestion/correlation job executions by outcome.",
    ["job", "status"],
)
JOB_DURATION = Gauge(
    "geon_job_duration_seconds",
    "Duration of the last run of each job.",
    ["job"],
)
JOB_LAST_SUCCESS = Gauge(
    "geon_job_last_success_timestamp_seconds",
    "Unix timestamp of the last successful run of each job.",
    ["job"],
)
JOB_LAST_DOCS = Gauge(
    "geon_job_last_result_docs",
    "Documents produced by the last successful run of each job.",
    ["job"],
)


class _Skipped(Exception):
    """Raised by a job to record a 'skipped' run (e.g. missing API key)."""


def _instrumented(job_name: str, fn: Callable[[], Any]) -> Callable[[], None]:
    """Wrap a job so every run feeds the Prometheus metrics.

    The wrapper owns error handling: a crashing job increments the
    failure counter and never propagates (one bad job must not kill the
    scheduler loop).

    Args:
        job_name: Metric label for the job.
        fn: Zero-arg callable returning a document count (or ``None``).

    Returns:
        The instrumented zero-arg callable to register with ``schedule``.
    """
    def wrapper() -> None:
        start = time.monotonic()
        try:
            result = fn()
        except _Skipped as exc:
            JOB_RUNS.labels(job_name, "skipped").inc()
            logger.warning("%s skipped: %s", job_name, exc)
            return
        except Exception:
            JOB_DURATION.labels(job_name).set(time.monotonic() - start)
            JOB_RUNS.labels(job_name, "failure").inc()
            logger.exception("%s cron failed.", job_name)
            return

        duration = time.monotonic() - start
        JOB_DURATION.labels(job_name).set(duration)
        JOB_RUNS.labels(job_name, "success").inc()
        JOB_LAST_SUCCESS.labels(job_name).set(time.time())

        docs = len(result) if isinstance(result, list) else result
        if isinstance(docs, (int, float)):
            JOB_LAST_DOCS.labels(job_name).set(docs)
            logger.info("%s cron: %s document(s) in %.1fs.", job_name, docs, duration)
        else:
            logger.info("%s cron completed in %.1fs.", job_name, duration)

    wrapper.__name__ = f"job_{job_name}"
    return wrapper


# ---------------------------------------------------------------------------
# Jobs (bare functions — error handling lives in _instrumented)
# ---------------------------------------------------------------------------

def run_gdelt() -> int:
    """Run the GDELT ingestor (watermark-driven incremental)."""
    from gdelt.ingestor import GDELTIngestor
    return GDELTIngestor().ingest(windows=1)


def run_gkg() -> int:
    """Run the GDELT GKG ingestor (latest 15-minute CSV window)."""
    from gkg.ingestor import GKGIngestor
    return GKGIngestor().ingest(windows=1)


def run_opencti_export() -> int:
    """Export CTI entities from OpenCTI → Elasticsearch."""
    from opencti_export.exporter import OpenCTIExporter
    return OpenCTIExporter().run(full=False)


def run_acled() -> int:
    """Run the ACLED ingestor (incremental)."""
    if not ACLED_API_KEY:
        raise _Skipped("ACLED_API_KEY not set")
    from acled.ingestor import ACLEDIngestor
    return ACLEDIngestor().run()


def run_sanctions() -> int:
    """Run the sanctions ingestor (OFAC + EU + UN)."""
    from sanctions.ingestor import SanctionsIngestor
    return SanctionsIngestor().run()


def run_polymarket() -> int:
    """Ingest Polymarket geopolitical prediction markets."""
    from polymarket.ingestor import PolymarketIngestor
    return PolymarketIngestor().ingest()


def run_polymarket_enrich() -> int:
    """Enrich existing Polymarket cases with GEON context."""
    from polymarket.ingestor import PolymarketIngestor
    return PolymarketIngestor().enrich()


def run_sipri() -> int:
    """Seed/update SIPRI arms transfers and military spending data."""
    from sipri.ingestor import SIPRIIngestor
    return SIPRIIngestor().run(seed=True)


def run_prediction_consensus() -> int:
    """Ingest Metaculus/Manifold prediction markets and compute consensus."""
    from prediction_consensus.ingestor import PredictionConsensusIngestor
    return PredictionConsensusIngestor().ingest()


def run_cloudflare_radar() -> int:
    """Ingest Cloudflare Radar internet outages."""
    from cloudflare_radar.ingestor import CloudflareRadarIngestor
    return CloudflareRadarIngestor().ingest(date_range="7d")


def run_risk_scores() -> int:
    """Calculate and index country risk scores."""
    from risk_score.calculator import RiskScoreCalculator
    return RiskScoreCalculator().run()


def run_correlation() -> list[dict[str, Any]]:
    """Run the correlation engine (all 10 rules)."""
    from correlation.engine import CorrelationEngine
    return CorrelationEngine().run()


# Registry: job name -> (callable, how it is scheduled).
JOBS: dict[str, Callable[[], Any]] = {
    "gdelt": run_gdelt,
    "gkg": run_gkg,
    "opencti_export": run_opencti_export,
    "acled": run_acled,
    "sanctions": run_sanctions,
    "correlation": run_correlation,
    "polymarket": run_polymarket,
    "polymarket_enrich": run_polymarket_enrich,
    "cloudflare_radar": run_cloudflare_radar,
    "prediction_consensus": run_prediction_consensus,
    "sipri": run_sipri,
    "risk_scores": run_risk_scores,
}


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
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Run each data source once (GDELT, SIPRI, sanctions, etc.) "
             "before entering the normal schedule loop.",
    )
    args = parser.parse_args()

    # --- Metrics endpoint (scraped by Prometheus) ---
    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics exposed on :%d/metrics.", METRICS_PORT)

    # --- Optional seed phase ---
    if args.seed:
        seed_gdelt(args.seed)
        seed_acled(args.seed)

    # --- Instrumented wrappers ---
    job = {name: _instrumented(name, fn) for name, fn in JOBS.items()}

    # --- Schedule recurring jobs ---
    schedule.every(15).minutes.do(job["gdelt"])
    schedule.every(15).minutes.do(job["gkg"])
    schedule.every(1).hours.do(job["opencti_export"])
    schedule.every(1).days.at("03:00").do(job["acled"])
    schedule.every().sunday.at("04:00").do(job["sanctions"])
    schedule.every(30).minutes.do(job["correlation"])
    schedule.every(1).hours.do(job["polymarket"])
    schedule.every(2).hours.do(job["polymarket_enrich"])
    schedule.every(30).minutes.do(job["cloudflare_radar"])
    schedule.every(2).hours.do(job["prediction_consensus"])
    schedule.every().monday.at("02:00").do(job["sipri"])
    schedule.every(1).days.at("05:00").do(job["risk_scores"])

    # --- Optional bootstrap: run each source once, correlation last ---
    if args.bootstrap:
        logger.info("Bootstrap mode: running each data source once.")
        for name in ("gdelt", "gkg", "opencti_export", "acled", "sanctions",
                     "polymarket", "sipri", "cloudflare_radar",
                     "prediction_consensus", "risk_scores", "correlation"):
            job[name]()
        logger.info("Bootstrap complete. Entering scheduled loop.")

    logger.info(
        "Scheduler started. Jobs: GDELT/15min, OpenCTI export/1h, "
        "ACLED/daily, Sanctions/weekly, Correlation/30min."
    )
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
