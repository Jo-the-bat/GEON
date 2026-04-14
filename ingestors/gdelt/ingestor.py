"""GEON GDELT ingestor.

Fetches geopolitical events from the GDELT Project APIs, normalizes them,
and bulk-indexes them into Elasticsearch.  Designed to run via cron every
15 minutes.

Usage::

    # As a module (recommended for cron)
    python -m gdelt.ingestor

    # Directly
    python gdelt/ingestor.py
"""

from __future__ import annotations

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
    INDEX_PREFIX,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
    setup_logging,
)
from common.es_client import bulk_index, ensure_index, get_es_client
from gdelt.parser import (
    RELEVANT_CAMEO_PREFIXES,
    normalize_event,
    parse_doc_api_response,
    parse_geo_api_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GDELT_DOC_API_URL: str = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_GEO_API_URL: str = "https://api.gdeltproject.org/api/v2/geo/geo"

MAPPING_PATH: Path = Path(__file__).resolve().parent / "mapping.json"

# Default request timeout for GDELT API calls (seconds).
REQUEST_TIMEOUT: int = 60


class GDELTIngestor:
    """Fetches, parses, and indexes GDELT events into Elasticsearch.

    Attributes:
        es: Elasticsearch client instance.
        logger: Logger scoped to this class.
    """

    def __init__(self) -> None:
        """Initialize the ingestor: set up ES client and logger."""
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.es = get_es_client()
        self.logger.info("GDELTIngestor initialized.")

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------

    @property
    def index_name(self) -> str:
        """Return the current month's GDELT events index name."""
        return f"{INDEX_PREFIX}-gdelt-events-{datetime.now(tz=timezone.utc):%Y.%m}"

    def _ensure_index(self) -> None:
        """Create the target index if it does not already exist."""
        ensure_index(self.es, self.index_name, MAPPING_PATH)

    # ------------------------------------------------------------------
    # Query builder
    # ------------------------------------------------------------------

    @staticmethod
    def build_query() -> str:
        """Build a GDELT query string filtering for relevant CAMEO categories.

        The query targets events in the conflict, diplomacy, and sanctions
        families of CAMEO codes.  GDELT DOC API ``query`` parameter supports
        keyword-style queries; we use theme-based filters.

        Returns:
            Query string suitable for the GDELT DOC API ``query`` param.
        """
        # GDELT DOC API supports thematic filters via the "theme:" prefix.
        # Keep the list short — GDELT rejects queries that are too long.
        themes: list[str] = [
            "MILITARY",
            "ARMED_CONFLICT",
            "SANCTIONS",
            "DIPLOMACY",
            "CYBER_ATTACK",
            "TERROR",
            "PROTEST",
            "WMD",
        ]
        theme_query = " OR ".join(f"theme:{t}" for t in themes)
        query = f"({theme_query}) (sourcelang:eng OR sourcelang:fra)"
        return query

    # ------------------------------------------------------------------
    # API fetchers
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, requests.HTTPError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=5, max=60),
        reraise=True,
    )
    def fetch_events(
        self,
        query: str,
        timespan: str = "15min",
        max_records: int = 250,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
    ) -> dict[str, Any]:
        """Call the GDELT DOC API v2 and return the JSON response.

        Args:
            query: GDELT query string (see :meth:`build_query`).
            timespan: Look-back window (e.g. ``"15min"``, ``"1h"``,
                ``"1d"``).
            max_records: Maximum number of records to return.

        Returns:
            Decoded JSON dict from the API.

        Raises:
            requests.HTTPError: On non-2xx responses.
        """
        params: dict[str, str | int] = {
            "query": query,
            "mode": "ArtList",
            "maxrecords": max_records,
            "format": "json",
            "sort": "DateDesc",
        }
        if start_datetime and end_datetime:
            params["startdatetime"] = start_datetime
            params["enddatetime"] = end_datetime
        else:
            params["timespan"] = timespan

        self.logger.info(
            "Fetching GDELT DOC API — timespan=%s, maxrecords=%d",
            timespan,
            max_records,
        )
        self.logger.debug("Query: %s", query)

        response = requests.get(
            GDELT_DOC_API_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        # GDELT returns plain text errors or empty body on bad queries.
        body = response.text.strip()
        if not body:
            self.logger.warning("GDELT DOC API returned an empty body.")
            return {}

        try:
            return response.json()
        except Exception:
            self.logger.warning("GDELT DOC API returned non-JSON: %s", body[:200])
            return {}

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, requests.HTTPError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=5, max=60),
        reraise=True,
    )
    def fetch_geo_events(
        self,
        query: str,
        timespan: str = "15min",
    ) -> dict[str, Any]:
        """Call the GDELT GEO API v2 and return the GeoJSON response.

        Args:
            query: GDELT query string.
            timespan: Look-back window.

        Returns:
            Decoded GeoJSON dict from the API.

        Raises:
            requests.HTTPError: On non-2xx responses.
        """
        params: dict[str, str] = {
            "query": query,
            "timespan": timespan,
            "format": "GeoJSON",
        }

        self.logger.info(
            "Fetching GDELT GEO API — timespan=%s",
            timespan,
        )

        response = requests.get(
            GDELT_GEO_API_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        body = response.text.strip()
        if not body:
            self.logger.warning("GDELT GEO API returned an empty body.")
            return {}

        try:
            return response.json()
        except Exception:
            self.logger.warning("GDELT GEO API returned non-JSON: %s", body[:200])
            return {}

    # ------------------------------------------------------------------
    # CAMEO filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _is_relevant_cameo(cameo_code: str) -> bool:
        """Return True if *cameo_code* falls within a relevant category."""
        if not cameo_code:
            return True  # Keep events without codes (DOC API articles).
        prefix = str(cameo_code)[:2]
        return prefix in RELEVANT_CAMEO_PREFIXES

    # ------------------------------------------------------------------
    # Main ingestion pipeline
    # ------------------------------------------------------------------

    def ingest(
        self,
        timespan: str = "15min",
        start_offset_days: int = 0,
    ) -> int:
        """Run the full ingestion pipeline.

        Args:
            timespan: GDELT look-back window.  Defaults to ``"15min"``.
            start_offset_days: Shift the query window back by N days
                (for historical seeding).  When > 0, *timespan* is used
                as the window width and the window ends N days ago.

        Returns:
            Number of documents successfully indexed.
        """
        self._ensure_index()

        query = self.build_query()

        # Build absolute datetime window when seeding historical data.
        start_dt = end_dt = None
        if start_offset_days > 0:
            end = datetime.now(tz=timezone.utc) - timedelta(days=start_offset_days)
            start = end - timedelta(days=1)
            start_dt = start.strftime("%Y%m%d%H%M%S")
            end_dt = end.strftime("%Y%m%d%H%M%S")
            self.logger.info("Historical window: %s → %s", start_dt, end_dt)

        # --- DOC API ---
        all_events: list[dict[str, Any]] = []
        try:
            doc_response = self.fetch_events(
                query, timespan=timespan,
                start_datetime=start_dt, end_datetime=end_dt,
            )
            doc_articles = parse_doc_api_response(doc_response)
            all_events.extend(doc_articles)
        except Exception:
            self.logger.exception("Failed to fetch/parse GDELT DOC API.")

        # GEO API v2 is currently returning 404 — disabled to avoid
        # wasting rate-limit budget on retries.  Re-enable when the
        # endpoint comes back online.

        if not all_events:
            self.logger.info("No events returned from GDELT APIs for timespan=%s.", timespan)
            return 0

        self.logger.info("Total raw events fetched: %d", len(all_events))

        # --- Normalize ---
        normalized: list[dict[str, Any]] = []
        for raw in all_events:
            try:
                doc = normalize_event(raw)
                normalized.append(doc)
            except Exception:
                self.logger.warning(
                    "Failed to normalize event, skipping.",
                    exc_info=True,
                )

        # --- CAMEO filter ---
        filtered = [
            e for e in normalized
            if self._is_relevant_cameo(e.get("cameo_code", ""))
        ]
        self.logger.info(
            "After normalization: %d events (%d after CAMEO filter).",
            len(normalized),
            len(filtered),
        )

        if not filtered:
            self.logger.info("No relevant events after CAMEO filtering.")
            return 0

        # --- Bulk index ---
        count = bulk_index(
            self.es,
            self.index_name,
            filtered,
            id_field="event_id",
        )
        return count

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Top-level entry point with error handling and summary logging."""
        self.logger.info("=== GDELT ingestion run started ===")
        try:
            count = self.ingest()
            self.logger.info(
                "=== GDELT ingestion run completed — %d events indexed ===",
                count,
            )
        except Exception:
            self.logger.exception("=== GDELT ingestion run FAILED ===")
            raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Create a :class:`GDELTIngestor` and run it."""
    setup_logging(level="INFO")
    timespan = sys.argv[1] if len(sys.argv) > 1 else "15min"
    ingestor = GDELTIngestor()
    ingestor.ingest(timespan=timespan)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
    except Exception:
        logger.exception("Unhandled exception — exiting with code 1.")
        sys.exit(1)
