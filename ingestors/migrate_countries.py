"""One-shot migration: normalize country values in existing indices.

The ingestors now write canonical country names (see
:mod:`common.countries`), but documents indexed before the change still
carry the old source spellings ("Russia" from ACLED, "RUSSIAN FEDERATION"
from the EU list, "IVORY COAST" from Cloudflare Radar, demonyms from the
UN list, raw ISO3 codes from GDELT for countries missing from the old
table).  Those values silently break the exact ``term`` joins used by the
correlation rules and the risk-score calculator.

Two strategies, depending on whether the document ``_id`` derives from
the country value:

- **update_by_query** (default): aggregate the distinct values of the
  field, compute which ones change under :func:`normalize_country`, and
  rewrite only the affected documents in place.
- **reindex**: for ``geon-military-spending`` (``_id = country:year``),
  ``geon-risk-scores`` (``_id = country``) and ``geon-arms-transfers``
  (``_id = transfer_id``, a content hash that includes both country
  fields), an in-place update would leave a stale ``_id`` and the next
  cron ingest of the same row would create a duplicate under the new
  canonical ``_id``.  Affected documents are therefore re-indexed under
  their recomputed ``_id`` and the old document is deleted.

Some legacy prediction documents also carry the token ``EUROPE`` where
both extractors now emit ``EUROPEAN UNION``; this is handled with an
extra static mapping (EUROPE is a region, not a country, so it must NOT
become a global alias).

Usage (inside the ingestor container)::

    python -m migrate_countries --dry-run   # show what would change
    python -m migrate_countries             # apply

Safe to re-run: already-canonical values are never touched.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from typing import Any, Callable

from elasticsearch import Elasticsearch, helpers

from common.config import INDEX_PREFIX, setup_logging
from common.countries import normalize_country
from common.es_client import get_es_client

logger = logging.getLogger(__name__)

# Legacy non-country tokens to converge in the prediction indices only.
_PREDICTION_EXTRA_MAP = {"EUROPE": "EUROPEAN UNION"}

# update_by_query targets: (index pattern, field, kind, extra_map).
# kind is "scalar" or "list".
UPDATE_TARGETS: list[tuple[str, str, str, dict[str, str]]] = [
    (f"{INDEX_PREFIX}-acled-events-*", "country", "scalar", {}),
    (f"{INDEX_PREFIX}-sanctions", "country", "scalar", {}),
    (f"{INDEX_PREFIX}-outages", "country", "scalar", {}),
    (f"{INDEX_PREFIX}-gdelt-events-*", "source_country", "scalar", {}),
    (f"{INDEX_PREFIX}-gdelt-events-*", "target_country", "scalar", {}),
    (f"{INDEX_PREFIX}-gdelt-events-*", "actor1_country", "scalar", {}),
    (f"{INDEX_PREFIX}-gdelt-events-*", "actor2_country", "scalar", {}),
    (f"{INDEX_PREFIX}-correlations", "countries_involved", "list", {}),
    (f"{INDEX_PREFIX}-polymarket-cases", "countries_involved", "list",
     _PREDICTION_EXTRA_MAP),
    (f"{INDEX_PREFIX}-predictions", "countries_involved", "list",
     _PREDICTION_EXTRA_MAP),
]


def _rebuild_spending(src: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Recompute the geon-military-spending document and its _id."""
    src["country"] = normalize_country(src.get("country") or "") or src.get("country", "")
    return src, f"{src['country']}:{src.get('year', '')}"


def _rebuild_risk_score(src: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Recompute the geon-risk-scores document and its _id.

    Mirrors risk_score/calculator.py: ``{country}:{YYYY-MM-DD}`` (daily
    history scheme).
    """
    src["country"] = normalize_country(src.get("country") or "") or src.get("country", "")
    return src, f"{src['country']}:{str(src.get('date', ''))[:10]}"


def _rebuild_transfer(src: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Recompute the geon-arms-transfers document, transfer_id and _id.

    Mirrors the hash in sipri/parser.py so future ingests of the same
    (normalized) row dedup against the migrated document.
    """
    src["supplier_country"] = normalize_country(
        src.get("supplier_country") or "") or src.get("supplier_country", "")
    src["recipient_country"] = normalize_country(
        src.get("recipient_country") or "") or src.get("recipient_country", "")
    transfer_id = hashlib.sha256(
        f"{src.get('year', 0)}:{src['supplier_country']}:"
        f"{src['recipient_country']}:{src.get('weapon_description', '')}:"
        f"{src.get('quantity', 0)}".encode()
    ).hexdigest()[:20]
    src["transfer_id"] = transfer_id
    return src, transfer_id


# reindex targets: (index, country fields, document rebuilder).
REINDEX_TARGETS: list[
    tuple[str, list[str], Callable[[dict[str, Any]], tuple[dict[str, Any], str]]]
] = [
    (f"{INDEX_PREFIX}-military-spending", ["country"], _rebuild_spending),
    (f"{INDEX_PREFIX}-risk-scores", ["country"], _rebuild_risk_score),
    (f"{INDEX_PREFIX}-arms-transfers",
     ["supplier_country", "recipient_country"], _rebuild_transfer),
]

# Painless scripts parameterized by field name (params.f) and the
# old->new mapping (params.m).
_SCALAR_SCRIPT = (
    "if (ctx._source[params.f] != null && "
    "params.m.containsKey(ctx._source[params.f])) "
    "{ ctx._source[params.f] = params.m[ctx._source[params.f]]; }"
)
_LIST_SCRIPT = (
    "if (ctx._source[params.f] != null) { "
    "def out = new ArrayList(); "
    "for (item in ctx._source[params.f]) { "
    "def v = params.m.containsKey(item) ? params.m[item] : item; "
    "if (!out.contains(v)) { out.add(v); } } "
    "ctx._source[params.f] = out; }"
)


def find_divergent_values(
    es: Elasticsearch,
    index: str,
    field: str,
    extra_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """Aggregate distinct values of *field* and return those that change.

    Args:
        es: Elasticsearch client.
        index: Index name or pattern.
        field: Keyword field holding country values.
        extra_map: Additional static value rewrites (applied on top of
            :func:`normalize_country`).

    Returns:
        Mapping of stored value -> canonical value (only divergent ones).
    """
    extra_map = extra_map or {}
    divergent: dict[str, str] = {}
    after: dict | None = None
    while True:
        composite: dict = {
            "size": 1000,
            "sources": [{"v": {"terms": {"field": field}}}],
        }
        if after:
            composite["after"] = after
        resp = es.search(
            index=index,
            size=0,
            aggs={"vals": {"composite": composite}},
        )
        try:
            agg = resp["aggregations"]["vals"]
        except (KeyError, TypeError):
            # A wildcard matching zero indices passes the exists() check
            # (allow_no_indices) but searches return no aggregations.
            break
        for bucket in agg["buckets"]:
            value = bucket["key"]["v"]
            if not value:
                continue
            canonical = extra_map.get(value) or normalize_country(value)
            if canonical and canonical != value:
                divergent[value] = canonical
        after = agg.get("after_key")
        if not after or not agg["buckets"]:
            break
    return divergent


def migrate_field(
    es: Elasticsearch,
    index: str,
    field: str,
    kind: str,
    extra_map: dict[str, str],
    dry_run: bool,
) -> int:
    """Normalize one field of one index pattern via update_by_query.

    Args:
        es: Elasticsearch client.
        index: Index name or pattern.
        field: Field to rewrite.
        kind: ``"scalar"`` or ``"list"``.
        extra_map: Additional static value rewrites.
        dry_run: If True, only report what would change.

    Returns:
        Number of documents updated (or that would be updated).
    """
    if not es.indices.exists(index=index):
        logger.info("[%s] %s — index absent, skipped.", index, field)
        return 0

    divergent = find_divergent_values(es, index, field, extra_map)
    if not divergent:
        logger.info("[%s] %s — all values already canonical.", index, field)
        return 0

    for old, new in sorted(divergent.items()):
        logger.info("[%s] %s: %r -> %r", index, field, old, new)

    query = {"terms": {field: list(divergent.keys())}}
    affected = es.count(index=index, query=query)["count"]

    if dry_run:
        logger.info(
            "[%s] %s — DRY RUN: %d document(s) would be updated.",
            index, field, affected,
        )
        return affected

    script = _SCALAR_SCRIPT if kind == "scalar" else _LIST_SCRIPT
    resp = es.update_by_query(
        index=index,
        query=query,
        script={
            "source": script,
            "lang": "painless",
            "params": {"f": field, "m": divergent},
        },
        conflicts="proceed",
        refresh=True,
        request_timeout=600,
    )
    updated = resp.get("updated", 0)
    failures = resp.get("failures", [])
    if failures:
        logger.error(
            "[%s] %s — %d failure(s) during update_by_query: %s",
            index, field, len(failures), failures[:3],
        )
    logger.info("[%s] %s — %d document(s) updated.", index, field, updated)
    return updated


def migrate_reindex(
    es: Elasticsearch,
    index: str,
    fields: list[str],
    rebuild: Callable[[dict[str, Any]], tuple[dict[str, Any], str]],
    dry_run: bool,
) -> int:
    """Re-index documents whose _id derives from a country value.

    Affected documents are written under their recomputed ``_id`` and the
    old document is deleted, so future cron ingests dedup correctly.

    Args:
        es: Elasticsearch client.
        index: Index name.
        fields: Country fields that participate in the _id.
        rebuild: Function producing ``(new_source, new_id)`` from a
            source dict (mutating the country fields to canonical form).
        dry_run: If True, only report what would change.

    Returns:
        Number of documents re-indexed (or that would be).
    """
    if not es.indices.exists(index=index):
        logger.info("[%s] %s — index absent, skipped.", index, fields)
        return 0

    divergent: dict[str, str] = {}
    for field in fields:
        divergent.update(find_divergent_values(es, index, field))
    if not divergent:
        logger.info("[%s] %s — all values already canonical.", index, fields)
        return 0

    for old, new in sorted(divergent.items()):
        logger.info("[%s] %s: %r -> %r", index, fields, old, new)

    query = {
        "bool": {
            "should": [{"terms": {f: list(divergent.keys())}} for f in fields],
            "minimum_should_match": 1,
        }
    }
    affected = es.count(index=index, query=query)["count"]

    if dry_run:
        logger.info(
            "[%s] — DRY RUN: %d document(s) would be re-indexed "
            "(old _id deleted, canonical _id written).",
            index, affected,
        )
        return affected

    actions: list[dict[str, Any]] = []
    for hit in helpers.scan(es, index=index, query={"query": query}):
        old_id = hit["_id"]
        new_src, new_id = rebuild(dict(hit["_source"]))
        actions.append({
            "_op_type": "index",
            "_index": hit["_index"],
            "_id": new_id,
            "_source": new_src,
        })
        if new_id != old_id:
            actions.append({
                "_op_type": "delete",
                "_index": hit["_index"],
                "_id": old_id,
            })

    success, errors = helpers.bulk(es, actions, raise_on_error=False)
    if errors:
        logger.error("[%s] — %d bulk error(s): %s", index, len(errors), errors[:3])
    es.indices.refresh(index=index)
    logger.info("[%s] — %d document(s) re-indexed.", index, affected)
    return affected


def main() -> None:
    """CLI entry point."""
    # Configure the ROOT logger: under ``python -m`` this module's logger is
    # named "__main__", so a named setup_logging() call would leave every
    # INFO line invisible (only the last-resort >=WARNING handler fires).
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Normalize country values in existing GEON indices."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report divergent values without rewriting documents.",
    )
    args = parser.parse_args()

    es = get_es_client()
    total = 0
    for index, field, kind, extra_map in UPDATE_TARGETS:
        try:
            total += migrate_field(es, index, field, kind, extra_map, args.dry_run)
        except Exception:
            logger.exception("[%s] %s — migration failed.", index, field)
    for index, fields, rebuild in REINDEX_TARGETS:
        try:
            total += migrate_reindex(es, index, fields, rebuild, args.dry_run)
        except Exception:
            logger.exception("[%s] %s — migration failed.", index, fields)

    verb = "would be updated" if args.dry_run else "updated"
    logger.info("Migration complete: %d document(s) %s.", total, verb)
    if not args.dry_run and total:
        logger.info(
            "Risk scores will pick up the canonical values at the next "
            "daily run (05:00) — or run: python -m risk_score.calculator"
        )


if __name__ == "__main__":
    main()
    sys.exit(0)
