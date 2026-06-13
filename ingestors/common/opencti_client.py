"""GEON OpenCTI client wrapper.

Provides helper functions for interacting with the OpenCTI platform via its
Python SDK (pycti).  Used by ingestors to create entities and by the
correlation engine to query CTI data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pycti import OpenCTIApiClient
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.config import (
    OPENCTI_TOKEN,
    OPENCTI_URL,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
)
from common.countries import ALIASES, CANONICAL_COUNTRIES, normalize_country

logger = logging.getLogger(__name__)


def _location_search_terms(canonical: str) -> list[str]:
    """Search terms likely to hit the OpenCTI geography name for a country.

    OpenCTI's dataset uses UN long forms ("Viet Nam", "Russian
    Federation", "Democratic People's Republic of Korea") — our alias
    table maps exactly those forms to the canonical name, so reversing it
    gives the right search phrases.
    """
    terms = [canonical.title()]
    terms += [
        alias for alias, target in ALIASES.items()
        if target == canonical and len(alias) > 3
    ][:4]
    return terms


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

# Per-request timeout (seconds) imposed on the pycti HTTP session. The
# scheduler is single-threaded (one PID running every job in turn), so a hung
# OpenCTI during the per-country correlation loops would stall every
# downstream job. pycti 6.6 dropped the ``requests_timeout`` constructor kwarg
# and hardcodes 300s per call, so we enforce the cap on the session itself —
# which works regardless of the pycti version.
OPENCTI_REQUEST_TIMEOUT = 60


def _clamp_request_timeout(client: OpenCTIApiClient, seconds: int) -> None:
    """Force a per-request timeout on pycti's underlying ``requests`` session.

    pycti issues every GraphQL call as ``session.post(..., timeout=300)`` (and
    file fetches as ``session.get(..., timeout=300)``). An explicit per-call
    timeout overrides any session-level default, so the only version-stable way
    to shorten it is to wrap the session verbs and override the kwarg.
    """
    session = getattr(client, "session", None)
    if session is None:  # pragma: no cover - defensive, pycti always sets it
        logger.warning("OpenCTI client exposes no .session; cannot clamp timeout.")
        return
    for verb in ("get", "post"):
        original = getattr(session, verb)

        def capped(*args, _original=original, **kwargs):
            kwargs["timeout"] = seconds
            return _original(*args, **kwargs)

        setattr(session, verb, capped)


def get_opencti_client() -> OpenCTIApiClient:
    """Create and return a configured OpenCTI API client.

    Connection parameters are read from environment variables via
    :mod:`common.config`.

    Returns:
        Authenticated :class:`~pycti.OpenCTIApiClient` instance.

    Raises:
        ValueError: If ``OPENCTI_TOKEN`` is not set.
    """
    if not OPENCTI_TOKEN:
        raise ValueError(
            "OPENCTI_ADMIN_TOKEN is not set.  "
            "Please add it to your .env file."
        )

    logger.info("Connecting to OpenCTI at %s", OPENCTI_URL)
    client = OpenCTIApiClient(
        url=OPENCTI_URL,
        token=OPENCTI_TOKEN,
        log_level="warning",
    )
    _clamp_request_timeout(client, OPENCTI_REQUEST_TIMEOUT)
    logger.info("Connected to OpenCTI.")
    return client


# ---------------------------------------------------------------------------
# Entity creation helpers
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def create_country(
    client: OpenCTIApiClient,
    name: str,
    description: str = "",
) -> dict[str, Any] | None:
    """Create a Country entity in OpenCTI (STIX ``location`` with type country).

    If a country with the same *name* already exists the existing entity is
    returned without modification.

    Args:
        client: Authenticated OpenCTI client.
        name: Country name (e.g. ``"Ukraine"``).
        description: Optional longer description.

    Returns:
        Dict with at least ``"id"`` and ``"name"`` keys, or ``None`` on
        failure.
    """
    try:
        country = client.location.create(
            name=name,
            description=description,
            type="Country",
        )
        if country:
            logger.info("OpenCTI country entity ensured: %s (id=%s)", name, country.get("id"))
        return country
    except Exception:
        logger.exception("Failed to create country '%s' in OpenCTI.", name)
        return None


@retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def create_organization(
    client: OpenCTIApiClient,
    name: str,
    description: str = "",
) -> dict[str, Any] | None:
    """Create an Organization entity in OpenCTI (STIX ``identity``).

    If an organization with the same *name* already exists the existing
    entity is returned without modification.

    Args:
        client: Authenticated OpenCTI client.
        name: Organization name.
        description: Optional longer description.

    Returns:
        Dict with at least ``"id"`` and ``"name"`` keys, or ``None`` on
        failure.
    """
    try:
        org = client.identity.create(
            name=name,
            description=description,
            type="Organization",
        )
        if org:
            logger.info("OpenCTI organization entity ensured: %s (id=%s)", name, org.get("id"))
        return org
    except Exception:
        logger.exception("Failed to create organization '%s' in OpenCTI.", name)
        return None


# ---------------------------------------------------------------------------
# Query helpers for the correlation engine
# ---------------------------------------------------------------------------

def _resolve_country_location(
    client: OpenCTIApiClient,
    country_name: str,
) -> dict[str, Any] | None:
    """Resolve a country name to its STIX ``Location`` entity in OpenCTI.

    The Location entity is typically imported by the ``opencti-datasets``
    connector (``geography.json``). Returns ``None`` if not found.

    Args:
        client: Authenticated OpenCTI client.
        country_name: Country name (English, as in OpenCTI geography dataset).

    Returns:
        Location dict with at least ``id`` / ``standard_id``, or ``None``.
    """
    canonical = normalize_country(country_name)
    if canonical not in CANONICAL_COUNTRIES:
        # GDELT actor pseudo-codes (SEA, AFR, GOV...) regularly reach the
        # rules — not resolvable, not worth a warning per run.
        logger.debug("'%s' is not a country — skipping Location lookup.", country_name)
        return None

    # OpenCTI's geography dataset stores UN long forms ("Viet Nam",
    # "Russian Federation"), not the GEON canonical uppercase form — try
    # cheap exact reads first.
    candidates = list(dict.fromkeys(
        [country_name, canonical.title(), canonical]
    ))
    try:
        for candidate in candidates:
            location = client.location.read(
                filters={
                    "mode": "and",
                    "filters": [
                        {"key": "name", "values": [candidate], "operator": "eq"},
                        {"key": "entity_type", "values": ["Country"], "operator": "eq"},
                    ],
                    "filterGroups": [],
                }
            )
            if location:
                return location

        # Fuzzy fallback: search with the canonical name AND its known
        # alias phrases (UN long forms), then accept a result only when
        # its name/aliases map back to the same canonical country.
        for term in _location_search_terms(canonical):
            results = client.location.list(
                search=term,
                first=10,
                filters={
                    "mode": "and",
                    "filters": [
                        {"key": "entity_type", "values": ["Country"], "operator": "eq"},
                    ],
                    "filterGroups": [],
                },
            )
            for location in results or []:
                names = [location.get("name", "")]
                names += location.get("x_opencti_aliases") or []
                names += location.get("aliases") or []
                if any(normalize_country(n) == canonical for n in names if n):
                    return location
    except Exception:
        logger.exception(
            "Failed to resolve Location for country '%s'.", country_name
        )
        return None


def _relationship_from_id(rel: dict[str, Any]) -> str | None:
    """Extract the source entity ID from a STIX relationship dict."""
    from_obj = rel.get("from") or {}
    return from_obj.get("id") or from_obj.get("standard_id")


@retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def get_campaigns_by_country(
    client: OpenCTIApiClient,
    country_name: str,
    days_back: int = 30,
) -> list[dict[str, Any]]:
    """Returns campaigns and intrusion sets linked to *country_name* via STIX
    ``originates-from`` or ``attributed-to`` relationships.

    Requires the Location entity for the country to exist in OpenCTI (imported
    by the ``opencti-datasets`` connector). If the Location cannot be resolved,
    returns an empty list rather than falling back on a full-text search — the
    full-text fallback produced massive false positives (e.g. the intrusion set
    ``APT28`` matching on an article about "Russian dissidents targeted by
    APT28" and being mis-attributed to Russia).

    Args:
        client: Authenticated OpenCTI client.
        country_name: Country name (e.g. ``"Russia"``).
        days_back: Only include relationships whose ``start_time`` is within
            this many days. Defaults to 30.

    Returns:
        List of Intrusion-Set / Campaign dicts, each tagged with
        ``_geon_type`` for downstream callers. May be empty.
    """
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days_back)).isoformat()

    location = _resolve_country_location(client, country_name)
    if not location:
        if normalize_country(country_name) in CANONICAL_COUNTRIES:
            logger.warning(
                "Location '%s' not found in OpenCTI — rule cannot attribute APTs by relationship",
                country_name,
            )
        return []

    location_id = location.get("id") or location.get("standard_id")
    if not location_id:
        logger.warning(
            "Location entity for '%s' has no usable id/standard_id; skipping.",
            country_name,
        )
        return []

    try:
        relationships = client.stix_core_relationship.list(
            filters={
                "mode": "and",
                "filters": [
                    {
                        "key": "relationship_type",
                        "values": ["originates-from", "attributed-to"],
                        "operator": "eq",
                    },
                    {"key": "toId", "values": [location_id], "operator": "eq"},
                    {
                        "key": "fromTypes",
                        "values": ["Intrusion-Set", "Campaign"],
                        "operator": "eq",
                    },
                    {"key": "start_time", "values": [since], "operator": "gte"},
                ],
                "filterGroups": [],
            },
        )
    except Exception:
        logger.exception(
            "Failed to query STIX relationships for country '%s' (location=%s).",
            country_name, location_id,
        )
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel in relationships or []:
        src_id = _relationship_from_id(rel)
        if not src_id or src_id in seen:
            continue
        seen.add(src_id)

        # pycti usually includes ``from`` already partially populated; only call
        # stix_domain_object.read() when we're missing the name/type.
        from_obj = rel.get("from") or {}
        entity_type = (from_obj.get("entity_type") or "").strip()
        if not from_obj.get("name") or not entity_type:
            try:
                fetched = client.stix_domain_object.read(id=src_id)
            except Exception:
                logger.debug("Could not resolve source entity %s", src_id)
                continue
            if not fetched:
                continue
            from_obj = fetched
            entity_type = (from_obj.get("entity_type") or "").strip()

        # Normalise _geon_type to lower-kebab-case ("intrusion-set", "campaign")
        from_obj["_geon_type"] = entity_type.lower().replace(" ", "-") or "unknown"
        results.append(from_obj)

    logger.info(
        "Found %d campaigns/intrusion-sets linked to '%s' via STIX relationships "
        "(days_back=%d).",
        len(results), country_name, days_back,
    )
    return results


@retry(
    retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def get_indicators_by_country(
    client: OpenCTIApiClient,
    country_name: str,
    days_back: int = 60,
) -> list[dict[str, Any]]:
    """Returns indicators linked to *country_name* via STIX ``located-at`` or
    ``targets`` relationships.

    Same semantics as :func:`get_campaigns_by_country`: the Location must
    exist in OpenCTI (via the ``opencti-datasets`` connector). Full-text
    search was removed to eliminate false positives. If the Location cannot
    be resolved, returns an empty list.

    Args:
        client: Authenticated OpenCTI client.
        country_name: Country name.
        days_back: Only include relationships whose ``start_time`` is within
            this many days. Defaults to 60.

    Returns:
        List of Indicator dicts. May be empty.
    """
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days_back)).isoformat()

    location = _resolve_country_location(client, country_name)
    if not location:
        if normalize_country(country_name) in CANONICAL_COUNTRIES:
            logger.warning(
                "Location '%s' not found in OpenCTI — rule cannot attribute IoCs by relationship",
                country_name,
            )
        return []

    location_id = location.get("id") or location.get("standard_id")
    if not location_id:
        return []

    try:
        relationships = client.stix_core_relationship.list(
            filters={
                "mode": "and",
                "filters": [
                    {
                        "key": "relationship_type",
                        "values": ["located-at", "targets"],
                        "operator": "eq",
                    },
                    {"key": "toId", "values": [location_id], "operator": "eq"},
                    {"key": "fromTypes", "values": ["Indicator"], "operator": "eq"},
                    {"key": "start_time", "values": [since], "operator": "gte"},
                ],
                "filterGroups": [],
            },
        )
    except Exception:
        logger.exception(
            "Failed to query STIX relationships (indicators) for country '%s'.",
            country_name,
        )
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel in relationships or []:
        src_id = _relationship_from_id(rel)
        if not src_id or src_id in seen:
            continue
        seen.add(src_id)

        from_obj = rel.get("from") or {}
        if not from_obj.get("pattern") and not from_obj.get("name"):
            try:
                fetched = client.stix_domain_object.read(id=src_id)
            except Exception:
                continue
            if not fetched:
                continue
            from_obj = fetched
        results.append(from_obj)

    logger.info(
        "Found %d indicators linked to '%s' via STIX relationships (days_back=%d).",
        len(results), country_name, days_back,
    )
    return results
