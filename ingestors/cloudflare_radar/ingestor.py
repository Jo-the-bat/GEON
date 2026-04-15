"""GEON Cloudflare Radar internet outage ingestor.

Fetches internet outage annotations from the Cloudflare Radar API and
indexes them into Elasticsearch (``geon-outages``).

The Cloudflare Radar API requires an API token with Radar:Read permissions.
Set ``CLOUDFLARE_RADAR_TOKEN`` in your ``.env`` file.

Usage::

    python -m cloudflare_radar.ingestor
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from cloudflare_radar.parser import normalize_outage
from common.config import INDEX_PREFIX, setup_logging
from common.es_client import bulk_index, ensure_index, get_es_client

logger = logging.getLogger(__name__)

INDEX_NAME = f"{INDEX_PREFIX}-outages"
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"

CF_RADAR_API = "https://api.cloudflare.com/client/v4/radar/annotations/outages"
CF_TOKEN = os.getenv("CLOUDFLARE_RADAR_TOKEN", "")


class CloudflareRadarIngestor:
    """Fetches and indexes Cloudflare Radar internet outage data."""

    def __init__(self) -> None:
        self.es = get_es_client()

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=5, max=30),
        reraise=True,
    )
    def _fetch_outages(self, date_range: str = "7d") -> list[dict[str, Any]]:
        """Fetch outage annotations from the Cloudflare Radar API.

        Args:
            date_range: Time range (e.g. '7d', '30d').

        Returns:
            List of raw annotation dicts.
        """
        if not CF_TOKEN:
            logger.warning("CLOUDFLARE_RADAR_TOKEN not set — skipping.")
            return []

        headers = {
            "Authorization": f"Bearer {CF_TOKEN}",
            "Content-Type": "application/json",
        }
        params: dict[str, str] = {"dateRange": date_range, "limit": "200"}

        resp = requests.get(CF_RADAR_API, headers=headers, params=params, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        if not data.get("success", False):
            errors = data.get("errors", [])
            logger.error("Cloudflare Radar API error: %s", errors)
            return []

        annotations = data.get("result", {}).get("annotations", [])
        logger.info("Fetched %d outage annotation(s) from Cloudflare Radar.", len(annotations))
        return annotations

    def ingest(self, date_range: str = "7d") -> int:
        """Fetch outages, normalize, and index into Elasticsearch.

        Args:
            date_range: Time range to query.

        Returns:
            Number of documents indexed.
        """
        ensure_index(self.es, INDEX_NAME, MAPPING_PATH)

        annotations = self._fetch_outages(date_range)
        if not annotations:
            return 0

        docs: list[dict[str, Any]] = []
        for ann in annotations:
            docs.extend(normalize_outage(ann))

        if not docs:
            logger.info("No outage documents produced after parsing.")
            return 0

        count = bulk_index(self.es, INDEX_NAME, docs, id_field="outage_id")
        logger.info("Indexed %d outage document(s).", count)
        return count


def main() -> None:
    setup_logging("cloudflare_radar.ingestor")
    ing = CloudflareRadarIngestor()
    count = ing.ingest(date_range="30d")
    logger.info("Done. %d documents indexed.", count)


if __name__ == "__main__":
    main()
