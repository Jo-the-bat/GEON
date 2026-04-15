"""GEON GDELT GKG ingestor.

Fetches the Global Knowledge Graph CSV from GDELT v2, parses themes,
persons, organizations, tone, and locations, and indexes into
Elasticsearch.  Designed to run every 15 minutes alongside the Events
ingestor.

Usage::

    python -m gkg.ingestor           # latest 15-min window
    python -m gkg.ingestor 96        # last 96 windows (~1 day)
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
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.config import (
    INDEX_PREFIX,
    RETRY_MAX_ATTEMPTS,
    setup_logging,
)
from common.es_client import bulk_index, ensure_index, get_es_client
from gkg.parser import parse_gkg_csv

logger = logging.getLogger(__name__)

GDELT_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_BASE_URL = "http://data.gdeltproject.org/gdeltv2"
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"
REQUEST_TIMEOUT = 90
FETCH_DELAY = 2.0


class GKGIngestor:
    """Fetches and indexes GDELT GKG data."""

    def __init__(self) -> None:
        self.es = get_es_client()
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def index_name(self) -> str:
        return f"{INDEX_PREFIX}-gkg-{datetime.now(tz=timezone.utc):%Y.%m}"

    def _ensure_index(self) -> None:
        ensure_index(self.es, self.index_name, MAPPING_PATH)

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, requests.HTTPError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=5, max=60),
        reraise=True,
    )
    def _download_zip(self, url: str) -> str:
        self.logger.debug("Downloading %s", url)
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = [n for n in zf.namelist() if n.upper().endswith(".CSV")]
            if not names:
                raise ValueError(f"No CSV in {url}")
            return zf.read(names[0]).decode("utf-8", errors="replace")

    def fetch_latest(self) -> list[dict[str, Any]]:
        """Fetch the latest GKG CSV from lastupdate.txt."""
        resp = requests.get(GDELT_LASTUPDATE_URL, timeout=30)
        resp.raise_for_status()
        gkg_url: str | None = None
        for line in resp.text.strip().splitlines():
            parts = line.split()
            if len(parts) >= 3 and ".gkg.csv" in parts[2].lower():
                gkg_url = parts[2]
                break
        if not gkg_url:
            raise ValueError("No .gkg.csv.zip URL in lastupdate.txt")
        self.logger.info("Latest GKG: %s", gkg_url.rsplit("/", 1)[-1])
        return parse_gkg_csv(self._download_zip(gkg_url))

    def fetch_for_timestamp(self, dt: datetime) -> list[dict[str, Any]]:
        """Fetch GKG CSV for a specific 15-min window."""
        minute = (dt.minute // 15) * 15
        rounded = dt.replace(minute=minute, second=0, microsecond=0)
        ts = rounded.strftime("%Y%m%d%H%M%S")
        url = f"{GDELT_BASE_URL}/{ts}.gkg.csv.zip"
        return parse_gkg_csv(self._download_zip(url))

    def _index_batch(self, docs: list[dict[str, Any]]) -> int:
        if not docs:
            return 0
        return bulk_index(self.es, self.index_name, docs, id_field="gkg_id")

    def ingest(self, windows: int = 1) -> int:
        """Run ingestion for *windows* 15-minute GKG files."""
        self._ensure_index()
        total = 0

        if windows <= 1:
            try:
                total += self._index_batch(self.fetch_latest())
            except Exception:
                self.logger.exception("Failed to fetch/process latest GKG.")
            return total

        now = datetime.now(tz=timezone.utc)
        for i in range(windows):
            dt = now - timedelta(minutes=15 * i)
            try:
                total += self._index_batch(self.fetch_for_timestamp(dt))
            except Exception:
                self.logger.warning("GKG window %d/%d failed, skipping.", i + 1, windows)
            if i < windows - 1:
                time.sleep(FETCH_DELAY)
            if (i + 1) % 48 == 0:
                self.logger.info("GKG seed: %d/%d windows, %d docs.", i + 1, windows, total)

        return total


def main() -> None:
    setup_logging(level="INFO")
    windows = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    count = GKGIngestor().ingest(windows=windows)
    logger.info("GKG ingestion complete: %d documents.", count)


if __name__ == "__main__":
    main()
