"""NEGO Elasticsearch client wrapper.

Provides helper functions for connecting to Elasticsearch, managing indices,
bulk-indexing documents, and querying timestamps for incremental ingestion.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import NotFoundError, TransportError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from common.config import (
    ES_HOST,
    ES_PASSWORD,
    ES_PORT,
    ES_SCHEME,
    ES_USER,
    ES_VERIFY_CERTS,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def get_es_client() -> Elasticsearch:
    """Create and return a configured Elasticsearch client.

    Connection parameters are read from environment variables via
    :mod:`common.config`.

    Returns:
        A connected :class:`~elasticsearch.Elasticsearch` instance.
    """
    url = f"{ES_SCHEME}://{ES_HOST}:{ES_PORT}"
    logger.info("Connecting to Elasticsearch at %s", url)

    client = Elasticsearch(
        hosts=[url],
        basic_auth=(ES_USER, ES_PASSWORD) if ES_PASSWORD else None,
        verify_certs=ES_VERIFY_CERTS,
        request_timeout=30,
    )

    # Quick connectivity check — will raise on failure.
    info = client.info()
    logger.info(
        "Connected to Elasticsearch cluster '%s' (version %s)",
        info["cluster_name"],
        info["version"]["number"],
    )
    return client


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(TransportError),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def ensure_index(
    client: Elasticsearch,
    index_name: str,
    mapping_path: str | Path,
) -> None:
    """Create an Elasticsearch index with the given mapping if it does not exist.

    Args:
        client: Elasticsearch client instance.
        index_name: Name of the index to create.
        mapping_path: Filesystem path to a JSON file containing the index
            mapping and settings.
    """
    if client.indices.exists(index=index_name):
        logger.debug("Index '%s' already exists — skipping creation.", index_name)
        return

    mapping_path = Path(mapping_path)
    if not mapping_path.is_file():
        raise FileNotFoundError(f"Mapping file not found: {mapping_path}")

    with mapping_path.open("r", encoding="utf-8") as fh:
        body = json.load(fh)

    client.indices.create(index=index_name, body=body)
    logger.info("Created index '%s' with mapping from %s.", index_name, mapping_path)


# ---------------------------------------------------------------------------
# Bulk indexing
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(TransportError),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def bulk_index(
    client: Elasticsearch,
    index_name: str,
    documents: Sequence[dict[str, Any]],
    id_field: str = "event_id",
) -> int:
    """Bulk-index a sequence of documents into Elasticsearch.

    Documents are de-duplicated by using *id_field* as the ``_id``.

    Args:
        client: Elasticsearch client instance.
        index_name: Target index name.
        documents: Iterable of document dicts.
        id_field: Key within each document to use as the ``_id`` in
            Elasticsearch.  Defaults to ``"event_id"``.

    Returns:
        Number of successfully indexed documents.

    Raises:
        elasticsearch.helpers.BulkIndexError: When some documents fail to
            index (after logging the errors).
    """
    if not documents:
        logger.info("No documents to index into '%s'.", index_name)
        return 0

    def _actions():  # noqa: ANN202
        for doc in documents:
            action = {
                "_index": index_name,
                "_source": doc,
            }
            if id_field and id_field in doc:
                action["_id"] = doc[id_field]
            yield action

    success_count, errors = helpers.bulk(
        client,
        _actions(),
        stats_only=False,
        raise_on_error=False,
    )

    if errors:
        logger.error(
            "Bulk indexing into '%s': %d succeeded, %d errors.",
            index_name,
            success_count,
            len(errors),
        )
        for err in errors[:10]:
            logger.error("  Bulk error detail: %s", err)
    else:
        logger.info(
            "Bulk indexed %d documents into '%s'.",
            success_count,
            index_name,
        )

    return success_count


# ---------------------------------------------------------------------------
# Incremental ingestion helper
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(TransportError),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def get_latest_timestamp(
    client: Elasticsearch,
    index_name: str,
    timestamp_field: str = "date",
) -> str | None:
    """Return the most recent value of *timestamp_field* in *index_name*.

    Useful for incremental ingestion: fetch only events newer than the last
    ingested timestamp.

    Args:
        client: Elasticsearch client instance.
        index_name: Index (or alias / pattern) to query.
        timestamp_field: Document field that holds the event timestamp.

    Returns:
        ISO-8601 timestamp string of the most recent document, or ``None``
        if the index is empty or does not exist.
    """
    try:
        result = client.search(
            index=index_name,
            body={
                "size": 0,
                "aggs": {
                    "max_ts": {
                        "max": {"field": timestamp_field}
                    }
                },
            },
        )
    except NotFoundError:
        logger.debug("Index '%s' does not exist yet.", index_name)
        return None

    value = result["aggregations"]["max_ts"].get("value_as_string")
    if value:
        logger.debug("Latest timestamp in '%s': %s", index_name, value)
    else:
        logger.debug("No documents found in '%s'.", index_name)
    return value
