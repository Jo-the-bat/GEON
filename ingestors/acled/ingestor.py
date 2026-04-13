"""GEON ACLED ingestor.

Fetches armed-conflict events from the ACLED API and indexes them into
Elasticsearch for use in correlation analysis and Kibana dashboards.

Usage::

    python -m acled.ingestor            # incremental (since last indexed event)
    python -m acled.ingestor --days 30  # explicit look-back window
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.config import (
    ACLED_API_KEY,
    ACLED_EMAIL,
    INDEX_PREFIX,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
    setup_logging,
)
from common.es_client import bulk_index, ensure_index, get_es_client, get_latest_timestamp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ACLED_API_URL = "https://api.acleddata.com/acled/read/"
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"
INDEX_PATTERN = f"{INDEX_PREFIX}-acled-events"
DEFAULT_LOOKBACK_DAYS = 7
MAX_RESULTS = 5000


class ACLEDIngestor:
    """Fetches events from the ACLED API and indexes them in Elasticsearch.

    Attributes:
        es: Elasticsearch client instance.
        api_key: ACLED API key.
        email: ACLED account email.
        lookback_days: Default number of days to look back when no prior data
            exists in the index.
    """

    def __init__(self, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> None:
        """Initialise the ingestor.

        Args:
            lookback_days: How many days to look back on a fresh run.
        """
        if not ACLED_API_KEY or not ACLED_EMAIL:
            raise ValueError(
                "ACLED_API_KEY and ACLED_EMAIL must be set in the environment."
            )

        self.es = get_es_client()
        self.api_key = ACLED_API_KEY
        self.email = ACLED_EMAIL
        self.lookback_days = lookback_days

    # ------------------------------------------------------------------
    # API interaction
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _fetch_events(
        self, date_from: str, date_to: str
    ) -> list[dict[str, Any]]:
        """Fetch events from the ACLED API for a date range.

        Args:
            date_from: Start date in ``YYYY-MM-DD`` format.
            date_to: End date in ``YYYY-MM-DD`` format.

        Returns:
            List of raw event dicts from the ACLED API response.

        Raises:
            requests.HTTPError: On non-2xx response.
        """
        params: dict[str, str | int] = {
            "key": self.api_key,
            "email": self.email,
            "event_date": f"{date_from}|{date_to}",
            "event_date_where": "BETWEEN",
            "limit": MAX_RESULTS,
        }

        logger.info(
            "Fetching ACLED events from %s to %s (limit=%d)",
            date_from,
            date_to,
            MAX_RESULTS,
        )
        response = requests.get(ACLED_API_URL, params=params, timeout=60)
        response.raise_for_status()

        payload = response.json()

        if not payload.get("success"):
            logger.error("ACLED API returned failure: %s", payload.get("error"))
            return []

        events: list[dict[str, Any]] = payload.get("data", [])
        logger.info("ACLED API returned %d events.", len(events))
        return events

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_event(raw: dict[str, Any]) -> dict[str, Any]:
        """Convert a raw ACLED API event dict into the ES document schema.

        Args:
            raw: Single event dict as returned by the ACLED API.

        Returns:
            Normalised document dict matching the Elasticsearch mapping.
        """
        lat = raw.get("latitude")
        lon = raw.get("longitude")

        # Build geo_point only when both coordinates are present and valid.
        geo_location: dict[str, float] | None = None
        try:
            if lat is not None and lon is not None:
                lat_f = float(lat)
                lon_f = float(lon)
                geo_location = {"lat": lat_f, "lon": lon_f}
        except (ValueError, TypeError):
            geo_location = None

        # Parse fatalities safely.
        fatalities: int = 0
        try:
            fatalities = int(raw.get("fatalities", 0))
        except (ValueError, TypeError):
            pass

        return {
            "event_id": str(raw.get("data_id", raw.get("event_id_cnty", ""))),
            "event_date": raw.get("event_date", ""),
            "event_type": raw.get("event_type", ""),
            "sub_event_type": raw.get("sub_event_type", ""),
            "actor1": raw.get("actor1", ""),
            "actor2": raw.get("actor2", ""),
            "country": raw.get("country", ""),
            "admin1": raw.get("admin1", ""),
            "location": raw.get("location", ""),
            "geo_location": geo_location,
            "latitude": float(lat) if lat is not None else None,
            "longitude": float(lon) if lon is not None else None,
            "fatalities": fatalities,
            "notes": raw.get("notes", ""),
            "source": raw.get("source", ""),
        }

    # ------------------------------------------------------------------
    # Index name helper
    # ------------------------------------------------------------------

    @staticmethod
    def _index_name_for_date(date_str: str) -> str:
        """Derive the monthly index name from an event date string.

        Args:
            date_str: Date in ``YYYY-MM-DD`` format.

        Returns:
            Index name like ``geon-acled-events-2026.04``.
        """
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return f"{INDEX_PATTERN}-{dt.strftime('%Y.%m')}"
        except (ValueError, TypeError):
            # Fall back to current month.
            return f"{INDEX_PATTERN}-{datetime.now(timezone.utc).strftime('%Y.%m')}"

    # ------------------------------------------------------------------
    # Main ingestion flow
    # ------------------------------------------------------------------

    def run(self, days: int | None = None) -> int:
        """Execute the ingestion pipeline.

        1. Determine the time window (incremental or explicit).
        2. Fetch events from the ACLED API.
        3. Normalise and group by target index.
        4. Ensure indices exist and bulk-index.

        Args:
            days: Override look-back window.  When ``None`` the ingestor
                checks the latest indexed timestamp for incremental sync.

        Returns:
            Total number of documents indexed.
        """
        # --- Determine date range ---
        date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if days is not None:
            date_from = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).strftime("%Y-%m-%d")
            logger.info("Explicit look-back: %d days (%s to %s)", days, date_from, date_to)
        else:
            latest = get_latest_timestamp(
                self.es, f"{INDEX_PATTERN}-*", timestamp_field="event_date"
            )
            if latest:
                date_from = latest[:10]  # YYYY-MM-DD portion
                logger.info("Incremental ingestion since %s", date_from)
            else:
                date_from = (
                    datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
                ).strftime("%Y-%m-%d")
                logger.info(
                    "No prior data found. Defaulting to %d-day look-back (%s).",
                    self.lookback_days,
                    date_from,
                )

        # --- Fetch ---
        raw_events = self._fetch_events(date_from, date_to)
        if not raw_events:
            logger.info("No new ACLED events to ingest.")
            return 0

        # --- Normalise and group by monthly index ---
        by_index: dict[str, list[dict[str, Any]]] = {}
        for raw in raw_events:
            doc = self._normalise_event(raw)
            idx = self._index_name_for_date(doc.get("event_date", ""))
            by_index.setdefault(idx, []).append(doc)

        # --- Index ---
        total_indexed = 0
        for idx_name, docs in by_index.items():
            ensure_index(self.es, idx_name, MAPPING_PATH)
            count = bulk_index(self.es, idx_name, docs, id_field="event_id")
            total_indexed += count

        logger.info("ACLED ingestion complete: %d documents indexed.", total_indexed)
        return total_indexed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the ACLED ingestor."""
    setup_logging("acled.ingestor")

    parser = argparse.ArgumentParser(description="GEON ACLED event ingestor")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to look back (default: incremental)",
    )
    args = parser.parse_args()

    try:
        ingestor = ACLEDIngestor()
        ingestor.run(days=args.days)
    except Exception:
        logger.exception("ACLED ingestion failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
