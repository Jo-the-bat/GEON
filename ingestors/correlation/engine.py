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
            # Situation tracking: one document per ongoing situation,
            # refreshed on every run where the rule still fires.
            "first_seen": {"type": "date"},
            "last_seen": {"type": "date"},
            "times_seen": {"type": "integer"},
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

# A situation dormant for this many days that fires again is treated as a
# reactivation and re-alerted (instead of being silently refreshed).
REACTIVATION_DAYS: int = 14

# Cap merged timelines so long-running situations don't grow unbounded.
TIMELINE_MAX_ENTRIES: int = 50


class CorrelationEngine:
    """Main correlation engine that orchestrates all rules.

    The engine:
    1. Initialises Elasticsearch and OpenCTI clients.
    2. Loads and runs each correlation rule.
    3. Reconciles candidates against previously indexed situations
       (new document vs. refresh of an ongoing one).
    4. Indexes new and updated correlations into ``geon-correlations``.
    5. Dispatches one batched alert for new/escalated/reactivated
       high+ findings.

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
            rule_numbers: Optional list of rule numbers (1-10) to run.
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

        Candidates are reconciled against previously indexed situations:
        unknown correlation_ids become new documents (and alerts), known
        ones update the existing document (``last_seen``/``times_seen``),
        re-alerting only on severity escalation or reactivation after
        dormancy.

        Returns:
            List of all correlation documents indexed/updated in this run.
        """
        start_time = datetime.now(timezone.utc)
        logger.info(
            "Correlation engine run started at %s", start_time.isoformat()
        )

        all_correlations: list[dict[str, Any]] = []

        for rule in self.rules:
            correlations = self.execute_rule(rule)
            all_correlations.extend(correlations)

        # --- Reconcile against existing situations ---
        new_docs, updated_docs, alertable = self._reconcile(all_correlations)

        logger.info(
            "Reconciliation: %d candidate(s) -> %d new, %d updated, "
            "%d alertable.",
            len(all_correlations),
            len(new_docs),
            len(updated_docs),
            len(alertable),
        )

        to_index = new_docs + updated_docs

        if self.dry_run:
            logger.info("DRY RUN — skipping indexing and alerting.")
            for c in alertable:
                logger.info(
                    "  [DRY ALERT %s] %s | %s | %s | %s",
                    c.get("alert_context", "new"),
                    c.get("rule_name"),
                    c.get("severity"),
                    c.get("countries_involved"),
                    c.get("description", "")[:120],
                )
            for c in updated_docs:
                logger.info(
                    "  [DRY UPDATE] %s | %s | times_seen=%s",
                    c.get("rule_name"),
                    c.get("countries_involved"),
                    c.get("times_seen"),
                )
            return to_index

        # --- Index ---
        if to_index:
            self._ensure_correlations_index()
            indexed = self.index_correlations(to_index)
            logger.info(
                "Indexed %d correlation(s) (%d new, %d updated).",
                indexed,
                len(new_docs),
                len(updated_docs),
            )

            # --- Alert (single batch) ---
            self._dispatch_alerts(alertable)

        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(
            "Correlation engine run completed in %.1f seconds. "
            "%d correlation(s) produced.",
            elapsed,
            len(to_index),
        )

        return to_index

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
        """Ensure the correlations index exists with up-to-date mapping."""
        if self.es.indices.exists(index=CORRELATIONS_INDEX):
            # Idempotent: add the situation-tracking fields to indices
            # created before they existed.
            try:
                self.es.indices.put_mapping(
                    index=CORRELATIONS_INDEX,
                    properties={
                        "first_seen": {"type": "date"},
                        "last_seen": {"type": "date"},
                        "times_seen": {"type": "integer"},
                    },
                )
            except Exception:
                logger.warning(
                    "Could not add situation-tracking fields to the "
                    "correlations mapping."
                )
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
    # Reconciliation (situation tracking)
    # ------------------------------------------------------------------

    def _reconcile(
        self, correlations: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Reconcile candidates against previously indexed situations.

        Rules emit situation-stable ``correlation_id`` values (no date
        component), so an ongoing situation maps to ONE document that is
        refreshed on every run instead of producing a duplicate per day.

        Args:
            correlations: List of candidate correlation dicts.

        Returns:
            Tuple ``(new_docs, updated_docs, alertable)``. ``alertable``
            entries are copies carrying an ``alert_context`` key
            (``"new"``, ``"escalation"`` or ``"reactivation"``).
        """
        if not correlations:
            return [], [], []

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Fetch existing documents for the candidate ids.
        existing_docs: dict[str, dict[str, Any]] = {}
        ids = [c["correlation_id"] for c in correlations if c.get("correlation_id")]
        if ids and self.es.indices.exists(index=CORRELATIONS_INDEX):
            try:
                resp = self.es.mget(
                    index=CORRELATIONS_INDEX,
                    body={"ids": ids},
                )
                existing_docs = {
                    doc["_id"]: doc.get("_source", {}) or {}
                    for doc in resp.get("docs", [])
                    if doc.get("found", False)
                }
            except Exception:
                # Fail CLOSED: treating everything as new would overwrite
                # stored situations (first_seen/times_seen/timeline) and
                # re-alert all of them. Skip this run instead — the next
                # run (30 min later) retries.
                logger.exception(
                    "Could not fetch existing correlations — skipping "
                    "indexing and alerting for this run (%d candidates).",
                    len(correlations),
                )
                return [], [], []

        new_docs: list[dict[str, Any]] = []
        updated_docs: list[dict[str, Any]] = []
        alertable: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for cand in correlations:
            cid = cand.get("correlation_id", "")
            if cid:
                if cid in seen_ids:
                    continue  # In-run duplicate (same situation, two hits).
                seen_ids.add(cid)

            stored = existing_docs.get(cid)
            if stored is None:
                cand.setdefault("first_seen", cand.get("timestamp", now_iso))
                cand["last_seen"] = now_iso
                cand["times_seen"] = 1
                new_docs.append(cand)
                alertable.append({**cand, "alert_context": "new"})
            else:
                merged, alert_reason = self._merge_existing(stored, cand, now)
                updated_docs.append(merged)
                if alert_reason:
                    alertable.append({**merged, "alert_context": alert_reason})

        return new_docs, updated_docs, alertable

    def _merge_existing(
        self,
        stored: dict[str, Any],
        cand: dict[str, Any],
        now: datetime,
    ) -> tuple[dict[str, Any], str | None]:
        """Merge a re-detected candidate into its stored situation document.

        The stored document keeps its original ``timestamp``/``first_seen``;
        ``last_seen``/``times_seen`` are refreshed. When the candidate's
        severity is at least the stored one, the descriptive payload
        (severity, description, events) is refreshed to the latest
        assessment; severity never de-escalates.

        Args:
            stored: The previously indexed correlation document.
            cand: The freshly generated candidate for the same situation.
            now: Current UTC time.

        Returns:
            Tuple ``(merged_doc, alert_reason)`` where ``alert_reason`` is
            ``"escalation"``, ``"reactivation"`` or ``None`` (silent
            refresh).
        """
        merged = dict(stored)
        merged.setdefault("first_seen", stored.get("timestamp", now.isoformat()))
        prev_last_seen = stored.get("last_seen") or stored.get("timestamp") or ""
        merged["last_seen"] = now.isoformat()
        merged["times_seen"] = int(stored.get("times_seen", 1) or 1) + 1
        # ``date`` is the activity date used by every 30-day window reader
        # (risk score, rule 10, Grafana timeField) — it MUST track the
        # latest firing or long-running active situations silently vanish
        # from those windows. ``timestamp``/``first_seen`` keep the origin.
        merged["date"] = now.isoformat()

        alert_reason: str | None = None

        # Reactivation: the situation was dormant and fires again.
        try:
            prev_dt = datetime.fromisoformat(
                str(prev_last_seen).replace("Z", "+00:00")
            )
            if (now - prev_dt).days >= REACTIVATION_DAYS:
                alert_reason = "reactivation"
        except (ValueError, TypeError):
            pass

        cand_rank = ALERT_SEVERITY_THRESHOLD.get(cand.get("severity", "low"), 0)
        stored_rank = ALERT_SEVERITY_THRESHOLD.get(stored.get("severity", "low"), 0)

        if cand_rank > stored_rank:
            alert_reason = "escalation"

        # Refresh the rule payload (description, events, rule-specific
        # fields like rule 10's signals_detail) with the latest assessment
        # when the fresh firing is at least as severe — or on reactivation,
        # so a reactivation alert never describes a weeks-old event.
        if cand_rank >= stored_rank or alert_reason == "reactivation":
            protected = {
                "correlation_id", "timestamp", "first_seen", "last_seen",
                "times_seen", "timeline", "date",
            }
            for key, value in cand.items():
                if key not in protected:
                    merged[key] = value
            if cand_rank < stored_rank:
                # Severity never de-escalates.
                merged["severity"] = stored.get("severity", "low")

        merged["timeline"] = self._merge_timeline(
            stored.get("timeline"), cand.get("timeline")
        )

        return merged, alert_reason

    @staticmethod
    def _merge_timeline(
        stored_timeline: list[dict[str, Any]] | None,
        cand_timeline: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Union two timelines, dedup by (type, description), capped.

        The date is deliberately excluded from the dedup key: several
        rules emit run-relative entry dates (computed from ``now``), so a
        date-inclusive key would re-add the same logical entry on every
        run.  When capping, the oldest and newest halves are both kept so
        the situation's origin events are never evicted by churn.
        """
        seen: set[tuple[str, str]] = set()
        merged: list[dict[str, Any]] = []
        for entry in (stored_timeline or []) + (cand_timeline or []):
            key = (
                str(entry.get("type", "")),
                str(entry.get("description", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
        merged.sort(key=lambda e: str(e.get("date", "")))
        if len(merged) > TIMELINE_MAX_ENTRIES:
            head = TIMELINE_MAX_ENTRIES // 2
            tail = TIMELINE_MAX_ENTRIES - head
            merged = merged[:head] + merged[-tail:]
        return merged

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    def _dispatch_alerts(self, correlations: list[dict[str, Any]]) -> None:
        """Send one batched notification for alert-worthy correlations.

        Only correlations with severity >= "high" are included; the whole
        run produces a single Discord message batch and a single digest
        email instead of one notification per correlation.

        Args:
            correlations: Alertable correlation dicts (with
                ``alert_context``).
        """
        to_alert = [
            c
            for c in correlations
            if ALERT_SEVERITY_THRESHOLD.get(c.get("severity", "low"), 0)
            >= MIN_ALERT_SEVERITY
        ]
        below = len(correlations) - len(to_alert)
        if below:
            logger.debug(
                "%d correlation(s) below alert threshold — not notified.",
                below,
            )
        if not to_alert:
            return

        logger.info("Dispatching %d alert(s) in one batch.", len(to_alert))
        try:
            send_alerts(to_alert)
        except Exception:
            logger.exception("Failed to send batched alerts.")


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
