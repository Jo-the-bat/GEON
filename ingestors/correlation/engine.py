"""GEON correlation engine.

Orchestrates the execution of all correlation rules, indexes the results
into Elasticsearch, and dispatches alerts for significant findings.

This is the core value engine of the GEON platform: it detects patterns
that span geopolitical events (GDELT, ACLED, sanctions) and cyber threat
intelligence (OpenCTI) to surface actionable intelligence.

Usage::

    python -m correlation.engine                 # run all rules
    python -m correlation.engine --rules 1 2     # run specific rules
    python -m correlation.engine --dry-run       # preview without indexing
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from elasticsearch import Elasticsearch
from pycti import OpenCTIApiClient

from common.config import INDEX_PREFIX, setup_logging
from common.es_client import bulk_index, ensure_index, get_es_client
from common.opencti_client import get_opencti_client
from correlation.alerting import send_alerts
from correlation.rules.conflict_cyber import ConflictCyberRule
from correlation.rules.diplomatic_apt import DiplomaticAPTRule
from correlation.rules.arms_escalation import ArmsEscalationRule
from correlation.rules.internet_outage import InternetOutageRule
from correlation.rules.military_buildup import MilitaryBuildupRule
from correlation.rules.multi_signal_convergence import MultiSignalConvergenceRule
from correlation.rules.outage_apt import OutageAPTRule
from correlation.rules.prediction_validated import PredictionValidatedRule
from correlation.rules.rhetoric_shift import RhetoricShiftRule
from correlation.rules.sanction_cyber import SanctionCyberRule

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CORRELATIONS_INDEX = f"{INDEX_PREFIX}-correlations"
CORRELATIONS_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "correlation_id": {"type": "keyword"},
            "timestamp": {"type": "date"},
            "date": {"type": "date"},
            "rule_name": {"type": "keyword"},
            "severity": {"type": "keyword"},
            "countries_involved": {"type": "keyword"},
            "diplomatic_event": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "keyword"},
                    "description": {"type": "text"},
                    "goldstein": {"type": "float"},
                },
            },
            "cyber_event": {
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "keyword"},
                    "apt_group": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "techniques": {"type": "keyword"},
                },
            },
            "description": {"type": "text"},
            "timeline": {
                "type": "nested",
                "properties": {
                    "date": {"type": "date"},
                    "type": {"type": "keyword"},
                    "description": {"type": "text"},
                },
            },
        },
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
}

# Temporary mapping file path (written at runtime).
_MAPPING_TMP_PATH = Path("/tmp/geon-correlations-mapping.json")

# Map rule numbers (for CLI) to rule classes.
RULE_REGISTRY: dict[int, type] = {
    1: DiplomaticAPTRule,
    2: SanctionCyberRule,
    3: ConflictCyberRule,
    4: RhetoricShiftRule,
    5: InternetOutageRule,
    6: MilitaryBuildupRule,
    7: ArmsEscalationRule,
    8: PredictionValidatedRule,
    9: OutageAPTRule,
    10: MultiSignalConvergenceRule,
}

# Minimum severity to trigger alerting.
ALERT_SEVERITY_THRESHOLD: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}
MIN_ALERT_SEVERITY: int = 2  # "high" and above


class CorrelationEngine:
    """Main correlation engine that orchestrates all rules.

    The engine:
    1. Initialises Elasticsearch and OpenCTI clients.
    2. Loads and runs each correlation rule.
    3. Deduplicates results against previously indexed correlations.
    4. Indexes new correlations into ``geon-correlations``.
    5. Dispatches alerts for high/critical findings.

    Attributes:
        es: Elasticsearch client.
        octi: OpenCTI client (may be ``None`` if unavailable).
        rules: List of instantiated rule objects.
        dry_run: If ``True``, correlations are logged but not indexed or
            alerted.
    """

    def __init__(
        self,
        rule_numbers: list[int] | None = None,
        dry_run: bool = False,
    ) -> None:
        """Initialise the correlation engine.

        Args:
            rule_numbers: Optional list of rule numbers (1-4) to run.
                If ``None``, all rules are run.
            dry_run: If ``True``, skip indexing and alerting.
        """
        self.dry_run = dry_run

        # --- Elasticsearch ---
        self.es: Elasticsearch = get_es_client()

        # --- OpenCTI ---
        self.octi: OpenCTIApiClient | None = None
        try:
            self.octi = get_opencti_client()
        except Exception:
            logger.warning(
                "Could not connect to OpenCTI. Rules requiring CTI data "
                "will be skipped."
            )

        # --- Load rules ---
        self.rules: list[Any] = []
        selected = rule_numbers or list(RULE_REGISTRY.keys())
        for num in selected:
            rule_cls = RULE_REGISTRY.get(num)
            if rule_cls is None:
                logger.warning("Unknown rule number %d — skipping.", num)
                continue
            self.rules.append(self._instantiate_rule(rule_cls))

        logger.info(
            "Correlation engine initialised with %d rule(s): %s",
            len(self.rules),
            [r.RULE_NAME for r in self.rules],
        )

    def _instantiate_rule(self, rule_cls: type) -> Any:
        """Create a rule instance, passing the appropriate clients.

        The ``RhetoricShiftRule`` only requires ``es``; all other rules
        require both ``es`` and ``octi``.

        Args:
            rule_cls: The rule class to instantiate.

        Returns:
            An instance of the rule.
        """
        # Rules that only need Elasticsearch (no OpenCTI dependency).
        _es_only = (
            RhetoricShiftRule, InternetOutageRule, ArmsEscalationRule,
            PredictionValidatedRule, MultiSignalConvergenceRule,
        )
        if rule_cls in _es_only:
            return rule_cls(es=self.es)
        if self.octi is None:
            logger.warning(
                "Rule %s requires OpenCTI but it is unavailable — "
                "rule will be loaded but may produce no results.",
                getattr(rule_cls, "RULE_NAME", rule_cls.__name__),
            )
            # Instantiate with a dummy octi; the rule will fail gracefully
            # when it tries to query OpenCTI.
            return rule_cls(es=self.es, octi=self.octi)  # type: ignore[arg-type]
        return rule_cls(es=self.es, octi=self.octi)

    # ------------------------------------------------------------------
    # Main execution
    # ------------------------------------------------------------------

    def run(self) -> list[dict[str, Any]]:
        """Execute all loaded rules and process the results.

        Returns:
            List of all correlation documents generated in this run.
        """
        start_time = datetime.now(timezone.utc)
        logger.info(
            "Correlation engine run started at %s", start_time.isoformat()
        )

        all_correlations: list[dict[str, Any]] = []

        for rule in self.rules:
            correlations = self.execute_rule(rule)
            all_correlations.extend(correlations)

        # --- Deduplication ---
        new_correlations = self._deduplicate(all_correlations)

        logger.info(
            "Total correlations: %d generated, %d new after deduplication.",
            len(all_correlations),
            len(new_correlations),
        )

        if self.dry_run:
            logger.info("DRY RUN — skipping indexing and alerting.")
            for c in new_correlations:
                logger.info(
                    "  [DRY] %s | %s | %s | %s",
                    c.get("rule_name"),
                    c.get("severity"),
                    c.get("countries_involved"),
                    c.get("description", "")[:120],
                )
            return new_correlations

        # --- Index ---
        if new_correlations:
            self._ensure_correlations_index()
            indexed = self.index_correlations(new_correlations)
            logger.info("Indexed %d new correlation(s).", indexed)

            # --- Alert ---
            self._dispatch_alerts(new_correlations)

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(
            "Correlation engine run completed in %.1f seconds. "
            "%d correlation(s) produced.",
            elapsed,
            len(new_correlations),
        )

        return new_correlations

    def execute_rule(self, rule: Any) -> list[dict[str, Any]]:
        """Execute a single correlation rule safely.

        Catches all exceptions so that one failing rule does not prevent
        the others from running.

        Args:
            rule: An instantiated rule object with a ``run()`` method.

        Returns:
            List of correlation dicts (may be empty if the rule found
            nothing or raised an error).
        """
        rule_name = getattr(rule, "RULE_NAME", rule.__class__.__name__)
        logger.info("Executing rule: %s", rule_name)

        try:
            correlations = rule.run()
            logger.info(
                "Rule %s produced %d correlation(s).", rule_name, len(correlations)
            )
            return correlations
        except Exception:
            logger.exception("Rule %s failed with an exception.", rule_name)
            return []

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _ensure_correlations_index(self) -> None:
        """Ensure the correlations index exists in Elasticsearch."""
        if self.es.indices.exists(index=CORRELATIONS_INDEX):
            return

        # Write mapping to temp file for ensure_index().
        _MAPPING_TMP_PATH.write_text(
            json.dumps(CORRELATIONS_MAPPING), encoding="utf-8"
        )
        ensure_index(self.es, CORRELATIONS_INDEX, _MAPPING_TMP_PATH)

    def index_correlations(self, correlations: list[dict[str, Any]]) -> int:
        """Bulk-index correlation documents.

        Args:
            correlations: List of correlation dicts.

        Returns:
            Number of successfully indexed documents.
        """
        return bulk_index(
            self.es,
            CORRELATIONS_INDEX,
            correlations,
            id_field="correlation_id",
        )

    def index_correlation(self, correlation: dict[str, Any]) -> None:
        """Index a single correlation document.

        Convenience method that wraps :meth:`index_correlations` for a
        single document.

        Args:
            correlation: Correlation document dict.
        """
        self._ensure_correlations_index()
        self.index_correlations([correlation])

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _deduplicate(
        self, correlations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Remove correlations that have already been indexed.

        Checks Elasticsearch for existing ``correlation_id`` values.

        Args:
            correlations: List of candidate correlation dicts.

        Returns:
            Filtered list containing only new correlations.
        """
        if not correlations:
            return []

        if not self.es.indices.exists(index=CORRELATIONS_INDEX):
            return correlations  # Index doesn't exist yet; all are new.

        ids = [c["correlation_id"] for c in correlations if "correlation_id" in c]
        if not ids:
            return correlations

        # Multi-get to check which IDs already exist.
        try:
            resp = self.es.mget(
                index=CORRELATIONS_INDEX,
                body={"ids": ids},
            )
            existing_ids = {
                doc["_id"]
                for doc in resp.get("docs", [])
                if doc.get("found", False)
            }
        except Exception:
            logger.warning(
                "Could not check for duplicate correlations — "
                "proceeding with all %d candidates.",
                len(correlations),
            )
            return correlations

        if existing_ids:
            logger.info(
                "Deduplication: %d of %d correlations already exist.",
                len(existing_ids),
                len(correlations),
            )

        return [
            c
            for c in correlations
            if c.get("correlation_id") not in existing_ids
        ]

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    def _dispatch_alerts(self, correlations: list[dict[str, Any]]) -> None:
        """Send alerts for correlations that meet the severity threshold.

        Only correlations with severity >= "high" trigger notifications.

        Args:
            correlations: List of new correlation dicts.
        """
        for correlation in correlations:
            severity = correlation.get("severity", "low")
            severity_level = ALERT_SEVERITY_THRESHOLD.get(severity, 0)

            if severity_level >= MIN_ALERT_SEVERITY:
                logger.info(
                    "Dispatching alert for correlation %s (severity=%s).",
                    correlation.get("correlation_id"),
                    severity,
                )
                try:
                    send_alerts(correlation)
                except Exception:
                    logger.exception(
                        "Failed to send alerts for correlation %s.",
                        correlation.get("correlation_id"),
                    )
            else:
                logger.debug(
                    "Correlation %s (severity=%s) below alert threshold.",
                    correlation.get("correlation_id"),
                    severity,
                )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the correlation engine."""
    setup_logging("correlation.engine")

    parser = argparse.ArgumentParser(
        description="GEON correlation engine — detect cross-domain patterns"
    )
    parser.add_argument(
        "--rules",
        type=int,
        nargs="*",
        default=None,
        help=(
            "Rule numbers to run (1=diplomatic+APT, 2=sanction+cyber, "
            "3=conflict+cyber, 4=rhetoric shift, 5=internet outage, "
            "6=military buildup, 7=arms escalation, 8=prediction match, "
            "9=outage+APT, 10=multi-signal convergence). Default: all."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview correlations without indexing or alerting.",
    )
    args = parser.parse_args()

    try:
        engine = CorrelationEngine(
            rule_numbers=args.rules,
            dry_run=args.dry_run,
        )
        results = engine.run()

        if args.dry_run and results:
            logger.info("Dry-run results:")
            for r in results:
                logger.info("  %s", json.dumps(r, indent=2, default=str))

    except Exception:
        logger.exception("Correlation engine failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
