"""GEON GDELT ingestor.

Fetches geopolitical events from the GDELT v2 Events Export CSV files,
normalizes them, and bulk-indexes them into Elasticsearch.  Designed to
run via cron every 15 minutes.

Usage::

    # As a module (recommended for cron)
    python -m gdelt.ingestor

    # Directly
    python gdelt/ingestor.py
"""

from __future__ import annotations

import io
import logging
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from common.config import (
    INDEX_PREFIX,
    RETRY_MAX_ATTEMPTS,
    setup_logging,
)
from common.es_client import bulk_index, ensure_index, get_es_client
from common.settings import setting
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from gdelt.parser import (
    RELEVANT_CAMEO_PREFIXES,
    normalize_event,
    parse_events_csv,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GDELT_EVENTS_LASTUPDATE_URL: str = (
    "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
)
GDELT_EVENTS_BASE_URL: str = "http://data.gdeltproject.org/gdeltv2"

MAPPING_PATH: Path = Path(__file__).resolve().parent / "mapping.json"

# Default request timeout for GDELT downloads (seconds).
REQUEST_TIMEOUT: int = 90

# Delay between successive CSV downloads to respect rate limits.
FETCH_DELAY_SECONDS: float = 2.0

# Watermark: the last successfully processed 15-minute window is persisted
# in Elasticsearch so windows missed during downtime are backfilled on the
# next run instead of being silently lost.
META_INDEX: str = f"{INDEX_PREFIX}-meta"
WATERMARK_DOC_ID: str = "gdelt-events-watermark"
MAX_BACKFILL_WINDOWS: int = setting("ingestion.gdelt.max_backfill_windows", 96)


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
    # CSV fetchers
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (requests.ConnectionError, requests.Timeout, requests.HTTPError)
        ),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=5, max=60),
        reraise=True,
    )
    def _download_csv_zip(self, url: str) -> str:
        """Download a GDELT Events Export ZIP and return the CSV text.

        4xx responses (e.g. a 404 for a window GDELT skipped) raise
        ``ValueError`` so tenacity does not retry them.
        """
        self.logger.debug("Downloading %s", url)
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        if 400 <= response.status_code < 500:
            raise ValueError(f"GDELT returned {response.status_code} for {url}")
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            csv_names = [n for n in zf.namelist() if n.upper().endswith(".CSV")]
            if not csv_names:
                raise ValueError(f"No CSV found in {url}")
            return zf.read(csv_names[0]).decode("utf-8", errors="replace")

    def latest_window(self) -> tuple[datetime, str]:
        """Resolve the most recent available 15-minute export window.

        Downloads ``lastupdate.txt`` and parses the ``.export.CSV.zip``
        URL plus its embedded timestamp.

        Returns:
            Tuple of (window datetime in UTC, CSV ZIP URL).
        """
        self.logger.info("Fetching GDELT lastupdate.txt …")
        resp = requests.get(GDELT_EVENTS_LASTUPDATE_URL, timeout=30)
        resp.raise_for_status()

        csv_url: str | None = None
        for line in resp.text.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3 and ".export.CSV" in parts[2]:
                csv_url = parts[2]
                break

        if not csv_url:
            raise ValueError("No .export.CSV.zip URL in lastupdate.txt")

        ts = csv_url.rsplit("/", 1)[-1].split(".")[0]
        window = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return window, csv_url

    def fetch_latest_csv(self) -> list[dict[str, Any]]:
        """Fetch the most recent GDELT Events Export CSV.

        Returns:
            List of raw event dicts from :func:`parse_events_csv`.
        """
        _, csv_url = self.latest_window()
        self.logger.info("Latest export: %s", csv_url.rsplit("/", 1)[-1])
        csv_text = self._download_csv_zip(csv_url)
        return parse_events_csv(csv_text)

    def fetch_csv_for_timestamp(self, dt: datetime) -> list[dict[str, Any]]:
        """Fetch the Events Export CSV for a specific 15-minute window.

        Args:
            dt: Any datetime; rounded down to the nearest 15-minute mark.

        Returns:
            List of raw event dicts.
        """
        minute = (dt.minute // 15) * 15
        rounded = dt.replace(minute=minute, second=0, microsecond=0)
        ts = rounded.strftime("%Y%m%d%H%M%S")
        url = f"{GDELT_EVENTS_BASE_URL}/{ts}.export.CSV.zip"
        csv_text = self._download_csv_zip(url)
        return parse_events_csv(csv_text)

    # ------------------------------------------------------------------
    # Watermark (gap-free incremental ingestion)
    # ------------------------------------------------------------------

    def _read_watermark(self) -> datetime | None:
        """Return the last successfully processed window, or ``None``."""
        try:
            doc = self.es.get(index=META_INDEX, id=WATERMARK_DOC_ID)
            raw = doc["_source"].get("last_window", "")
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None

    def _write_watermark(self, window: datetime) -> None:
        """Persist *window* as the last successfully processed window."""
        try:
            self.es.index(
                index=META_INDEX,
                id=WATERMARK_DOC_ID,
                document={
                    "last_window": window.isoformat(),
                    "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
        except Exception:
            self.logger.warning("Could not persist GDELT watermark.", exc_info=True)

    @staticmethod
    def _missed_windows(watermark: datetime, latest: datetime) -> list[datetime]:
        """All 15-minute windows after *watermark* up to *latest*, capped.

        When the gap exceeds :data:`MAX_BACKFILL_WINDOWS`, only the most
        recent windows are kept (the older ones are dropped explicitly —
        better a logged loss than an unbounded catch-up run).
        """
        windows: list[datetime] = []
        current = watermark + timedelta(minutes=15)
        while current <= latest:
            windows.append(current)
            current += timedelta(minutes=15)
        if len(windows) > MAX_BACKFILL_WINDOWS:
            windows = windows[-MAX_BACKFILL_WINDOWS:]
        return windows

    # ------------------------------------------------------------------
    # CAMEO filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _is_relevant_cameo(cameo_code: str) -> bool:
        """Return True if *cameo_code* falls within a relevant category."""
        if not cameo_code:
            return False
        prefix = str(cameo_code)[:2]
        return prefix in RELEVANT_CAMEO_PREFIXES

    # ------------------------------------------------------------------
    # Processing helpers
    # ------------------------------------------------------------------

    def _process_and_index(self, raw_events: list[dict[str, Any]]) -> int:
        """Normalize, CAMEO-filter, and bulk-index a batch of raw events.

        Returns:
            Number of documents successfully indexed.
        """
        normalized: list[dict[str, Any]] = []
        for raw in raw_events:
            try:
                doc = normalize_event(raw)
                normalized.append(doc)
            except Exception:
                self.logger.warning("Failed to normalize event, skipping.", exc_info=True)

        filtered = [
            e for e in normalized
            if self._is_relevant_cameo(e.get("cameo_code", ""))
        ]
        self.logger.info(
            "Batch: %d raw → %d normalized → %d after CAMEO filter.",
            len(raw_events), len(normalized), len(filtered),
        )

        if not filtered:
            return 0

        return bulk_index(self.es, self.index_name, filtered, id_field="event_id")

    # ------------------------------------------------------------------
    # Main ingestion pipeline
    # ------------------------------------------------------------------

    def ingest(self, windows: int = 1) -> int:
        """Run the full ingestion pipeline.

        Args:
            windows: Number of 15-minute CSV windows to fetch.
                ``1`` (default) fetches only the latest update.
                Use ``96`` for ~1 day, ``672`` for ~1 week.

        Returns:
            Number of documents successfully indexed.
        """
        self._ensure_index()
        total = 0

        if windows <= 1:
            return self._ingest_incremental()

        # Historical / seed: iterate backwards through 15-min windows.
        now = datetime.now(tz=timezone.utc)
        for i in range(windows):
            dt = now - timedelta(minutes=15 * i)
            try:
                raw = self.fetch_csv_for_timestamp(dt)
                total += self._process_and_index(raw)
            except Exception:
                self.logger.warning(
                    "Window %d/%d (%s) failed, skipping.",
                    i + 1, windows, dt.strftime("%Y%m%d%H%M%S"),
                )
            if i < windows - 1:
                time.sleep(FETCH_DELAY_SECONDS)
            if (i + 1) % 48 == 0:
                self.logger.info(
                    "Seed progress: %d/%d windows, %d events indexed.",
                    i + 1, windows, total,
                )

        # Seeding walked back from "now": record now as the watermark.
        minute = (now.minute // 15) * 15
        self._write_watermark(now.replace(minute=minute, second=0, microsecond=0))
        return total

    def _ingest_incremental(self) -> int:
        """Watermark-driven incremental ingestion.

        Processes every 15-minute window published since the last
        successful run (capped at :data:`MAX_BACKFILL_WINDOWS`), so
        ingestor downtime no longer leaves silent gaps in the event
        timeline. The watermark advances after EACH window — a crash
        mid-backfill resumes where it stopped.

        Returns:
            Number of documents successfully indexed.
        """
        total = 0
        try:
            latest, latest_url = self.latest_window()
        except Exception:
            self.logger.exception("Could not resolve the latest GDELT window.")
            return total

        watermark = self._read_watermark()
        if watermark is None:
            # First run: process only the latest window and anchor there.
            self.logger.info("No GDELT watermark yet — starting at %s.", latest)
            try:
                raw = parse_events_csv(self._download_csv_zip(latest_url))
                total += self._process_and_index(raw)
                self._write_watermark(latest)
            except Exception:
                self.logger.exception("Failed to process latest GDELT CSV.")
            return total

        missed = self._missed_windows(watermark, latest)
        if not missed:
            self.logger.info(
                "GDELT up to date (watermark %s, latest %s).", watermark, latest
            )
            return total

        gap = int((latest - watermark).total_seconds() // 900)
        if gap > MAX_BACKFILL_WINDOWS:
            self.logger.warning(
                "GDELT gap of %d windows exceeds the backfill cap (%d) — "
                "the oldest %d window(s) are dropped.",
                gap, MAX_BACKFILL_WINDOWS, gap - MAX_BACKFILL_WINDOWS,
            )
        if len(missed) > 1:
            self.logger.info(
                "Backfilling %d missed GDELT window(s) since %s.",
                len(missed), watermark,
            )

        for i, dt in enumerate(missed):
            try:
                raw = self.fetch_csv_for_timestamp(dt)
                total += self._process_and_index(raw)
            except ValueError:
                # 4xx — GDELT occasionally skips a window; advance anyway.
                self.logger.warning(
                    "GDELT window %s unavailable (4xx) — skipping.",
                    dt.strftime("%Y%m%d%H%M%S"),
                )
            except Exception:
                self.logger.exception(
                    "GDELT window %s failed — will retry next run.",
                    dt.strftime("%Y%m%d%H%M%S"),
                )
                return total  # keep watermark before the failed window
            self._write_watermark(dt)
            if i < len(missed) - 1:
                time.sleep(FETCH_DELAY_SECONDS)

        return total

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
    windows = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    ingestor = GDELTIngestor()
    ingestor.ingest(windows=windows)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
    except Exception:
        logger.exception("Unhandled exception — exiting with code 1.")
        sys.exit(1)
