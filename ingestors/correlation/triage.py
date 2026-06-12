"""Analyst triage CLI for correlation situations.

Correlations are situation documents with a lifecycle status:

- ``open`` — fresh detection, nobody looked at it yet (engine default)
- ``acknowledged`` — an analyst is investigating: severity escalations
  still alert, dormancy reactivations do NOT (you are already engaged)
- ``resolved`` — handled; reopens (and re-alerts) on escalation or on
  reactivation after dormancy. Note: a situation whose rule keeps
  firing never goes dormant, so resolving it effectively mutes it until
  a severity escalation — choose ``fp`` instead when the detection
  itself is wrong
- ``false_positive`` — analyst verdict: the engine keeps tracking the
  situation silently, NEVER re-alerts it, stops refreshing its ``date``
  (so it ages out of the 30-day windows), and downstream consumers
  (risk score, rule 10's APT signal, Polymarket enrichment) exclude it

The engine never clobbers these fields when refreshing a situation, and
the direct geon-correlations writers (Polymarket shift, prediction
divergence) carry them over on rewrite.

Usage (inside the ingestor container)::

    python -m correlation.triage list [--status open] [--severity high]
    python -m correlation.triage show <correlation_id>
    python -m correlation.triage ack <correlation_id> [--note "..."]
    python -m correlation.triage resolve <correlation_id> [--note "..."]
    python -m correlation.triage fp <correlation_id> [--note "..."]
    python -m correlation.triage reopen <correlation_id> [--note "..."]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from common.config import INDEX_PREFIX, setup_logging
from common.es_client import get_es_client

logger = logging.getLogger(__name__)

CORRELATIONS_INDEX = f"{INDEX_PREFIX}-correlations"

STATUSES = ("open", "acknowledged", "resolved", "false_positive")
_ACTION_TO_STATUS = {
    "ack": "acknowledged",
    "resolve": "resolved",
    "fp": "false_positive",
    "reopen": "open",
}


def set_status(es: Any, correlation_id: str, status: str, note: str = "") -> bool:
    """Set the lifecycle status of one correlation document.

    Args:
        es: Elasticsearch client.
        correlation_id: Document id in ``geon-correlations``.
        status: One of :data:`STATUSES`.
        note: Optional analyst note stored with the verdict.

    Returns:
        True when the document was updated.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r} (expected one of {STATUSES})")
    doc: dict[str, Any] = {
        "status": status,
        "triaged_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        doc["triage_note"] = note
    try:
        es.update(index=CORRELATIONS_INDEX, id=correlation_id, doc=doc)
        return True
    except Exception:
        logger.exception("Could not update correlation %s.", correlation_id)
        return False


def list_correlations(
    es: Any,
    status: str | None = None,
    severity: str | None = None,
    size: int = 25,
) -> list[dict[str, Any]]:
    """List situations, newest activity first.

    Documents created before the lifecycle existed have no ``status``
    field — ``--status open`` includes them (they are de-facto open).
    """
    filters: list[dict[str, Any]] = []
    if status == "open":
        filters.append({
            "bool": {
                "should": [
                    {"term": {"status": "open"}},
                    {"bool": {"must_not": [{"exists": {"field": "status"}}]}},
                ],
                "minimum_should_match": 1,
            }
        })
    elif status:
        filters.append({"term": {"status": status}})
    if severity:
        filters.append({"term": {"severity": severity}})

    query: dict[str, Any] = (
        {"bool": {"filter": filters}} if filters else {"match_all": {}}
    )
    resp = es.search(
        index=CORRELATIONS_INDEX,
        query=query,
        size=size,
        sort=[{"date": {"order": "desc", "unmapped_type": "date"}}],
    )
    results = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit["_source"]
        src["correlation_id"] = src.get("correlation_id", hit["_id"])
        results.append(src)
    return results


def _print_row(doc: dict[str, Any]) -> None:
    print(
        f"{doc.get('correlation_id', '?'):22} "
        f"{doc.get('status', 'open'):15} "
        f"{doc.get('severity', '?'):9} "
        f"conf={doc.get('confidence', '–'):>3} "
        f"x{doc.get('times_seen', 1):<4} "
        f"{doc.get('rule_name', '?'):28} "
        f"{','.join(doc.get('countries_involved', []))[:30]:30} "
        f"{str(doc.get('last_seen', doc.get('date', '')))[:16]}"
    )


def main() -> None:
    """CLI entry point."""
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Triage GEON correlation situations."
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List situations.")
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.add_argument("--severity",
                        choices=["low", "medium", "high", "critical"],
                        default=None)
    p_list.add_argument("--size", type=int, default=25)

    p_show = sub.add_parser("show", help="Print one situation as JSON.")
    p_show.add_argument("correlation_id")

    for action, status in _ACTION_TO_STATUS.items():
        p = sub.add_parser(action, help=f"Mark a situation {status}.")
        p.add_argument("correlation_id")
        p.add_argument("--note", default="")

    args = parser.parse_args()
    es = get_es_client()

    if args.action == "list":
        rows = list_correlations(es, args.status, args.severity, args.size)
        print(f"{len(rows)} situation(s):")
        for doc in rows:
            _print_row(doc)
        return

    if args.action == "show":
        try:
            doc = es.get(index=CORRELATIONS_INDEX, id=args.correlation_id)
        except Exception:
            print(f"correlation {args.correlation_id} introuvable")
            sys.exit(1)
        print(json.dumps(doc["_source"], indent=2, ensure_ascii=False))
        return

    status = _ACTION_TO_STATUS[args.action]
    ok = set_status(es, args.correlation_id, status, args.note)
    if ok:
        print(f"{args.correlation_id} -> {status}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
