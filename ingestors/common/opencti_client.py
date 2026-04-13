"""HEGO OpenCTI client wrapper.

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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

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
    """Query campaigns and intrusion sets attributed to a country.

    Searches for :class:`Campaign` and :class:`IntrusionSet` objects whose
    ``originatesFrom`` or ``targets`` relationships reference *country_name*
    and that were created or modified within *days_back* days.

    Args:
        client: Authenticated OpenCTI client.
        country_name: Country name to search for (e.g. ``"Russia"``).
        days_back: How many days back to search.  Defaults to 30.

    Returns:
        List of campaign / intrusion-set dicts (may be empty).
    """
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days_back)).isoformat()
    results: list[dict[str, Any]] = []

    try:
        # --- Intrusion Sets ---
        intrusion_sets = client.intrusion_set.list(
            filters={
                "mode": "and",
                "filters": [
                    {
                        "key": "modified",
                        "values": [since],
                        "operator": "gte",
                    },
                ],
                "filterGroups": [],
            },
            search=country_name,
        )
        if intrusion_sets:
            for item in intrusion_sets:
                item["_hego_type"] = "intrusion-set"
            results.extend(intrusion_sets)

        # --- Campaigns ---
        campaigns = client.campaign.list(
            filters={
                "mode": "and",
                "filters": [
                    {
                        "key": "modified",
                        "values": [since],
                        "operator": "gte",
                    },
                ],
                "filterGroups": [],
            },
            search=country_name,
        )
        if campaigns:
            for item in campaigns:
                item["_hego_type"] = "campaign"
            results.extend(campaigns)

    except Exception:
        logger.exception(
            "Failed to query campaigns/intrusion-sets for country '%s'.",
            country_name,
        )

    logger.info(
        "Found %d campaigns/intrusion-sets linked to '%s' in the last %d days.",
        len(results),
        country_name,
        days_back,
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
    """Query indicators (IoC) linked to a country.

    Searches for :class:`Indicator` objects whose relationships reference
    *country_name* and that were created within *days_back* days.

    Args:
        client: Authenticated OpenCTI client.
        country_name: Country name to search for.
        days_back: How many days back to search.  Defaults to 60.

    Returns:
        List of indicator dicts (may be empty).
    """
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days_back)).isoformat()
    results: list[dict[str, Any]] = []

    try:
        indicators = client.indicator.list(
            filters={
                "mode": "and",
                "filters": [
                    {
                        "key": "created",
                        "values": [since],
                        "operator": "gte",
                    },
                ],
                "filterGroups": [],
            },
            search=country_name,
        )
        if indicators:
            results.extend(indicators)
    except Exception:
        logger.exception(
            "Failed to query indicators for country '%s'.",
            country_name,
        )

    logger.info(
        "Found %d indicators linked to '%s' in the last %d days.",
        len(results),
        country_name,
        days_back,
    )
    return results
