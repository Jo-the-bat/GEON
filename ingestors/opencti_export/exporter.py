"""HEGO OpenCTI exporter.

Exports CTI data (threat actors, campaigns, indicators, malware,
vulnerabilities) from OpenCTI into Elasticsearch indices so they can be
queried by the correlation engine and visualised in Kibana dashboards.

Usage::

    python -m opencti_export.exporter            # incremental
    python -m opencti_export.exporter --full      # full re-export
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pycti import OpenCTIApiClient
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
from common.es_client import bulk_index, ensure_index, get_es_client, get_latest_timestamp
from common.opencti_client import get_opencti_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"

INDEX_THREATS = f"{INDEX_PREFIX}-cti-threats"
INDEX_INDICATORS = f"{INDEX_PREFIX}-cti-indicators"
INDEX_CAMPAIGNS = f"{INDEX_PREFIX}-cti-campaigns"

DEFAULT_LOOKBACK_DAYS = 7
BATCH_SIZE = 100


class OpenCTIExporter:
    """Exports STIX objects from OpenCTI into Elasticsearch.

    Handles intrusion sets (threat actors), campaigns, indicators, malware,
    and vulnerabilities.  Supports incremental export based on the last
    indexed timestamp.

    Attributes:
        es: Elasticsearch client.
        octi: OpenCTI API client.
    """

    def __init__(self) -> None:
        self.es = get_es_client()
        self.octi: OpenCTIApiClient = get_opencti_client()

    # ------------------------------------------------------------------
    # Generic STIX-to-ES document mapper
    # ------------------------------------------------------------------

    @staticmethod
    def _map_stix_object(
        obj: dict[str, Any],
        stix_type: str,
    ) -> dict[str, Any]:
        """Convert a pycti STIX object dict into the ES document schema.

        Args:
            obj: Object dict as returned by pycti list methods.
            stix_type: The STIX type label (e.g. ``"intrusion-set"``).

        Returns:
            Normalised document dict.
        """
        # External references.
        ext_refs: list[dict[str, str]] = []
        for ref in obj.get("externalReferences", []) or []:
            ext_refs.append({
                "source_name": ref.get("source_name", ""),
                "url": ref.get("url", ""),
            })

        # Labels / tags.
        labels: list[str] = []
        for label in obj.get("objectLabel", []) or []:
            if isinstance(label, dict):
                labels.append(label.get("value", ""))
            elif isinstance(label, str):
                labels.append(label)

        # Kill chain phases.
        kill_chain: list[str] = []
        for kc in obj.get("killChainPhases", []) or []:
            phase_name = kc.get("phase_name", kc.get("kill_chain_name", ""))
            if phase_name:
                kill_chain.append(phase_name)

        # Country extraction — look for originatesFrom / targets relationships.
        countries: list[str] = []
        # TODO: Resolve country from relationships more thoroughly.
        # pycti may embed 'countries' or related location objects depending
        # on how the OpenCTI instance is configured.

        # Created-by.
        created_by = ""
        cb = obj.get("createdBy")
        if cb and isinstance(cb, dict):
            created_by = cb.get("name", "")

        return {
            "stix_id": obj.get("standard_id", obj.get("id", "")),
            "type": stix_type,
            "name": obj.get("name", ""),
            "description": obj.get("description", ""),
            "aliases": obj.get("aliases", []) or [],
            "country": countries,
            "first_seen": obj.get("first_seen", obj.get("created", "")),
            "last_seen": obj.get("last_seen", obj.get("modified", "")),
            "confidence": obj.get("confidence", 0) or 0,
            "labels": labels,
            "kill_chain_phases": kill_chain,
            "created_by": created_by,
            "external_references": ext_refs,
        }

    # ------------------------------------------------------------------
    # Object-type exporters
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _export_intrusion_sets(self, since: str | None) -> list[dict[str, Any]]:
        """Export intrusion set (threat actor) objects.

        Args:
            since: ISO timestamp; only export objects modified after this.

        Returns:
            List of normalised ES documents.
        """
        filters = self._build_modified_filter(since)
        objects = self.octi.intrusion_set.list(
            first=BATCH_SIZE,
            filters=filters,
        )
        logger.info("Fetched %d intrusion sets from OpenCTI.", len(objects))
        return [self._map_stix_object(o, "intrusion-set") for o in objects]

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _export_campaigns(self, since: str | None) -> list[dict[str, Any]]:
        """Export campaign objects.

        Args:
            since: ISO timestamp filter.

        Returns:
            List of normalised ES documents.
        """
        filters = self._build_modified_filter(since)
        objects = self.octi.campaign.list(
            first=BATCH_SIZE,
            filters=filters,
        )
        logger.info("Fetched %d campaigns from OpenCTI.", len(objects))
        return [self._map_stix_object(o, "campaign") for o in objects]

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _export_indicators(self, since: str | None) -> list[dict[str, Any]]:
        """Export indicator (IoC) objects.

        Args:
            since: ISO timestamp filter.

        Returns:
            List of normalised ES documents.
        """
        filters = self._build_modified_filter(since)
        objects = self.octi.indicator.list(
            first=BATCH_SIZE,
            filters=filters,
        )
        logger.info("Fetched %d indicators from OpenCTI.", len(objects))
        return [self._map_stix_object(o, "indicator") for o in objects]

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _export_malware(self, since: str | None) -> list[dict[str, Any]]:
        """Export malware objects.

        Args:
            since: ISO timestamp filter.

        Returns:
            List of normalised ES documents.
        """
        filters = self._build_modified_filter(since)
        objects = self.octi.malware.list(
            first=BATCH_SIZE,
            filters=filters,
        )
        logger.info("Fetched %d malware objects from OpenCTI.", len(objects))
        return [self._map_stix_object(o, "malware") for o in objects]

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _export_vulnerabilities(self, since: str | None) -> list[dict[str, Any]]:
        """Export vulnerability objects.

        Args:
            since: ISO timestamp filter.

        Returns:
            List of normalised ES documents.
        """
        filters = self._build_modified_filter(since)
        objects = self.octi.vulnerability.list(
            first=BATCH_SIZE,
            filters=filters,
        )
        logger.info("Fetched %d vulnerabilities from OpenCTI.", len(objects))
        return [self._map_stix_object(o, "vulnerability") for o in objects]

    # ------------------------------------------------------------------
    # Filter builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_modified_filter(since: str | None) -> dict[str, Any] | None:
        """Build an OpenCTI filter for objects modified after *since*.

        Args:
            since: ISO timestamp string, or ``None`` for no filter.

        Returns:
            Filter dict compatible with pycti ``list()`` methods, or
            ``None`` if no filtering is needed.
        """
        if not since:
            return None

        return {
            "mode": "and",
            "filters": [
                {
                    "key": "modified",
                    "values": [since],
                    "operator": "gte",
                },
            ],
            "filterGroups": [],
        }

    # ------------------------------------------------------------------
    # Main export flow
    # ------------------------------------------------------------------

    def run(self, full: bool = False) -> int:
        """Execute the export pipeline.

        Args:
            full: If ``True``, ignore the last-export timestamp and do a
                complete re-export.

        Returns:
            Total number of documents indexed across all CTI indices.
        """
        # --- Determine incremental timestamp ---
        since: str | None = None
        if not full:
            since = get_latest_timestamp(
                self.es, f"{INDEX_PREFIX}-cti-*", timestamp_field="ingested_at"
            )
            if since:
                logger.info("Incremental export since %s", since)
            else:
                logger.info("No prior CTI data — performing full export.")

        # --- Export each object type ---
        threats = self._export_intrusion_sets(since)
        campaigns = self._export_campaigns(since)
        indicators = self._export_indicators(since)
        malware_docs = self._export_malware(since)
        vuln_docs = self._export_vulnerabilities(since)

        # Merge threats + malware + vulnerabilities into the threats index.
        all_threats = threats + malware_docs + vuln_docs

        # --- Index ---
        total = 0

        if all_threats:
            ensure_index(self.es, INDEX_THREATS, MAPPING_PATH)
            total += bulk_index(self.es, INDEX_THREATS, all_threats, id_field="stix_id")

        if campaigns:
            ensure_index(self.es, INDEX_CAMPAIGNS, MAPPING_PATH)
            total += bulk_index(self.es, INDEX_CAMPAIGNS, campaigns, id_field="stix_id")

        if indicators:
            ensure_index(self.es, INDEX_INDICATORS, MAPPING_PATH)
            total += bulk_index(
                self.es, INDEX_INDICATORS, indicators, id_field="stix_id"
            )

        logger.info(
            "OpenCTI export complete: %d documents indexed "
            "(%d threats, %d campaigns, %d indicators).",
            total,
            len(all_threats),
            len(campaigns),
            len(indicators),
        )
        return total


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the OpenCTI exporter."""
    setup_logging("opencti_export.exporter")

    parser = argparse.ArgumentParser(description="HEGO OpenCTI → Elasticsearch exporter")
    parser.add_argument(
        "--full",
        action="store_true",
        default=False,
        help="Full re-export (ignore last timestamp)",
    )
    args = parser.parse_args()

    try:
        exporter = OpenCTIExporter()
        exporter.run(full=args.full)
    except Exception:
        logger.exception("OpenCTI export failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
