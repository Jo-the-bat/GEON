"""Shared confidence scoring and evidence helpers for correlation rules.

Every correlation document carries two analyst-facing dimensions beyond
severity:

- ``confidence`` (0-100): how much the detection itself can be trusted.
  Severity says "how bad if true"; confidence says "how likely true".
- ``evidence``: references to the source documents that triggered the
  rule (index + doc id + date + a one-line summary), so an analyst can
  verify the claim instead of taking the alert on faith.

Confidence convention (factors are additive bonuses on a base, clamped
to [5, 95] — never 0 "impossible" nor 100 "certain"):

- attribution quality: STIX-relationship-validated APT +30,
  static-map-validated +15 (use :func:`attribution_bonus`)
- corroboration volume: more independent source events = higher
  (use :func:`volume_bonus`)
- statistical strength: z-score above threshold scales up
  (use :func:`zscore_bonus`)
- temporal proximity between the correlated events scales up
  (use :func:`proximity_bonus`)

Each rule records the factors it used in ``confidence_factors`` so the
number is auditable.
"""

from __future__ import annotations

from typing import Any

CONFIDENCE_FLOOR = 5
CONFIDENCE_CEILING = 95


def clamp_confidence(value: float) -> int:
    """Clamp a raw confidence score into [FLOOR, CEILING]."""
    return int(max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, round(value))))


def confidence(base: float, factors: dict[str, float]) -> tuple[int, dict[str, float]]:
    """Combine a base score and named bonus factors.

    Args:
        base: Rule-specific starting score (typically 20-40).
        factors: Named additive bonuses (negative values allowed for
            penalties). Recorded verbatim in the document.

    Returns:
        Tuple of (clamped confidence, factors dict including the base).
    """
    total = base + sum(factors.values())
    recorded = {"base": float(base), **{k: float(v) for k, v in factors.items()}}
    return clamp_confidence(total), recorded


def attribution_bonus(source: str) -> float:
    """Bonus for the quality of an APT attribution.

    Args:
        source: ``"opencti"`` (STIX relationship, validated),
            ``"static"`` (attribution map only), ``"es_cti"`` (indexed
            CTI doc, validated) or anything else.
    """
    return {"opencti": 30.0, "es_cti": 20.0, "static": 15.0}.get(source, 0.0)


def volume_bonus(count: int, saturation: int = 20, max_bonus: float = 20.0) -> float:
    """Bonus scaling with corroborating event volume, saturating.

    Args:
        count: Number of independent corroborating events.
        saturation: Count at which the bonus reaches *max_bonus*.
        max_bonus: Ceiling of the bonus.
    """
    if count <= 0:
        return 0.0
    return max_bonus * min(1.0, count / saturation)


def zscore_bonus(z: float | None, threshold: float = 2.0, max_bonus: float = 20.0) -> float:
    """Bonus for statistical strength above the z-score threshold.

    ``None`` (no baseline available) yields 0 — absence of statistics is
    neither evidence for nor against.
    """
    if z is None or z < threshold:
        return 0.0
    # threshold..(threshold+3) maps to 50%..100% of the bonus.
    return max_bonus * min(1.0, 0.5 + (z - threshold) / 6.0)


def proximity_bonus(days_apart: float | None, window_days: float, max_bonus: float = 15.0) -> float:
    """Bonus for temporal proximity of the correlated events.

    Args:
        days_apart: Absolute gap between the two correlated events.
        window_days: The rule's correlation window (gap == window -> 0).
        max_bonus: Bonus when the events coincide (gap == 0).
    """
    if days_apart is None or window_days <= 0:
        return 0.0
    closeness = max(0.0, 1.0 - min(days_apart, window_days) / window_days)
    return max_bonus * closeness


def evidence_entry(
    *,
    index: str = "",
    doc_id: str = "",
    date: str = "",
    kind: str = "",
    summary: str = "",
) -> dict[str, str]:
    """Build one evidence reference.

    Args:
        index: Elasticsearch index (or "opencti" for STIX entities).
        doc_id: Document/entity identifier.
        date: Source event date (ISO).
        kind: Evidence type (diplomatic, cyber, sanction, outage,
            conflict, market, spending, transfer, signal).
        summary: One-line human-readable description.

    Returns:
        Evidence dict (truncated summary).
    """
    return {
        "index": index,
        "doc_id": str(doc_id),
        "date": date,
        "kind": kind,
        "summary": summary[:300],
    }


def evidence_from_hit(
    hit: dict[str, Any], kind: str, summary: str, date_field: str = "date"
) -> dict[str, str]:
    """Build an evidence reference from a raw ES hit (with _id/_index).

    Args:
        hit: Raw ES hit dict (``_index``, ``_id``, ``_source``).
        kind: Evidence type.
        summary: One-line description.
        date_field: Source field holding the event date.
    """
    src = hit.get("_source", {})
    return evidence_entry(
        index=hit.get("_index", ""),
        doc_id=hit.get("_id", ""),
        date=str(src.get(date_field, "")),
        kind=kind,
        summary=summary,
    )
