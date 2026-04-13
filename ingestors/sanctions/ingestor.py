"""GEON sanctions ingestor.

Fetches sanctioned entities from the OFAC SDN list (and optionally EU/UN
lists) and indexes them into Elasticsearch.  Entities are also created or
updated in OpenCTI for cross-referencing by the correlation engine.

Usage::

    python -m sanctions.ingestor
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import xml.etree.ElementTree as ET
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

from common.config import (
    INDEX_PREFIX,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
    setup_logging,
)
from common.es_client import bulk_index, ensure_index, get_es_client
from common.opencti_client import (
    create_organization,
    get_opencti_client,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OFAC_SDN_XML_URL = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
)
MAPPING_PATH = Path(__file__).resolve().parent / "mapping.json"
INDEX_NAME = f"{INDEX_PREFIX}-sanctions"

# XML namespace used by the OFAC SDN schema.
NS = {"sdn": "http://tempuri.org/sdnList.xsd"}


class SanctionsIngestor:
    """Fetches sanctions data and indexes it in Elasticsearch / OpenCTI.

    Currently implements the OFAC SDN list.  EU and UN sources are marked
    with TODOs for future implementation.

    Attributes:
        es: Elasticsearch client instance.
        octi: OpenCTI client (may be ``None`` if unavailable).
    """

    def __init__(self) -> None:
        self.es = get_es_client()
        try:
            self.octi = get_opencti_client()
        except Exception:
            logger.warning(
                "Could not connect to OpenCTI — sanctions will be indexed "
                "in Elasticsearch only."
            )
            self.octi = None

    # ------------------------------------------------------------------
    # OFAC SDN fetching
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        reraise=True,
    )
    def _fetch_ofac_xml(self) -> bytes:
        """Download the OFAC SDN XML file.

        Returns:
            Raw XML content as bytes.
        """
        logger.info("Downloading OFAC SDN list from %s", OFAC_SDN_XML_URL)
        response = requests.get(OFAC_SDN_XML_URL, timeout=120)
        response.raise_for_status()
        logger.info("Downloaded %d bytes of OFAC SDN XML.", len(response.content))
        return response.content

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    def _parse_ofac_xml(self, xml_data: bytes) -> list[dict[str, Any]]:
        """Parse the OFAC SDN XML into normalised document dicts.

        Args:
            xml_data: Raw XML bytes.

        Returns:
            List of normalised sanctions entity dicts.
        """
        root = ET.fromstring(xml_data)  # noqa: S314 — trusted source
        entries = root.findall(".//sdn:sdnEntry", NS)
        logger.info("Found %d SDN entries in XML.", len(entries))

        documents: list[dict[str, Any]] = []
        for entry in entries:
            doc = self._parse_sdn_entry(entry)
            if doc:
                documents.append(doc)

        logger.info("Parsed %d valid sanctions entities.", len(documents))
        return documents

    def _parse_sdn_entry(self, entry: ET.Element) -> dict[str, Any] | None:
        """Parse a single ``<sdnEntry>`` element.

        Args:
            entry: XML element for one SDN entry.

        Returns:
            Normalised dict, or ``None`` if the entry cannot be parsed.
        """
        uid = self._text(entry, "sdn:uid")
        if not uid:
            return None

        # Determine entity type from sdnType.
        sdn_type_raw = self._text(entry, "sdn:sdnType") or ""
        entity_type = self._map_sdn_type(sdn_type_raw)

        # Build name from firstName + lastName.
        first_name = self._text(entry, "sdn:firstName") or ""
        last_name = self._text(entry, "sdn:lastName") or ""
        name = f"{first_name} {last_name}".strip() or f"SDN-{uid}"

        # Aliases.
        aliases: list[str] = []
        aka_list = entry.find("sdn:akaList", NS)
        if aka_list is not None:
            for aka in aka_list.findall("sdn:aka", NS):
                aka_name = self._text(aka, "sdn:lastName")
                if aka_name:
                    aliases.append(aka_name)

        # Programs.
        programs: list[str] = []
        program_list = entry.find("sdn:programList", NS)
        if program_list is not None:
            for prog in program_list.findall("sdn:program", NS):
                if prog.text:
                    programs.append(prog.text.strip())

        # Country — attempt to extract from address list.
        country = ""
        address_list = entry.find("sdn:addressList", NS)
        if address_list is not None:
            for addr in address_list.findall("sdn:address", NS):
                c = self._text(addr, "sdn:country")
                if c:
                    country = c
                    break

        # Listed date — from dateOfBirth list or remarks.
        # TODO: Parse the publish date from the XML header for more accurate
        # listing dates.  The individual entries sometimes lack explicit dates.
        listed_date = self._extract_date(entry)

        # Notes / remarks.
        remarks = self._text(entry, "sdn:remarks") or ""

        # Deterministic entity_id.
        entity_id = hashlib.sha256(
            f"OFAC-{uid}-{name}".encode()
        ).hexdigest()[:24]

        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "name": name,
            "aliases": aliases,
            "country": country,
            "programs": programs,
            "sanctions_source": "OFAC",
            "listed_date": listed_date,
            "notes": remarks,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text(element: ET.Element, tag: str) -> str | None:
        """Extract text from a child element, returning ``None`` if absent."""
        child = element.find(tag, NS)
        return child.text.strip() if child is not None and child.text else None

    @staticmethod
    def _map_sdn_type(raw: str) -> str:
        """Map OFAC sdnType to GEON entity_type vocabulary.

        Args:
            raw: Raw value like ``"Individual"``, ``"Entity"``, ``"Vessel"``.

        Returns:
            One of ``"person"``, ``"organization"``, ``"vessel"``, or
            ``"unknown"``.
        """
        mapping = {
            "individual": "person",
            "entity": "organization",
            "vessel": "vessel",
            "aircraft": "vessel",
        }
        return mapping.get(raw.strip().lower(), "unknown")

    @staticmethod
    def _extract_date(entry: ET.Element) -> str | None:
        """Attempt to extract a date from an SDN entry.

        Tries the ``dateOfBirthList`` first, then falls back to ``None``.

        Args:
            entry: SDN entry XML element.

        Returns:
            ISO-format date string or ``None``.
        """
        # TODO: Improve date extraction — check nationality, ID documents,
        # and other sub-elements for additional date hints.
        dob_list = entry.find("sdn:dateOfBirthList", NS)
        if dob_list is not None:
            for item in dob_list.findall("sdn:dateOfBirthItem", NS):
                dob = item.find("sdn:dateOfBirth", NS)
                if dob is not None and dob.text:
                    text = dob.text.strip()
                    # OFAC uses various date formats.
                    for fmt in ("%d %b %Y", "%Y", "%m/%d/%Y", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
                        except ValueError:
                            continue
        return None

    # ------------------------------------------------------------------
    # OpenCTI enrichment
    # ------------------------------------------------------------------

    def _push_to_opencti(self, documents: list[dict[str, Any]]) -> None:
        """Create or update sanctioned entities in OpenCTI.

        Currently only creates Organization entities for ``entity_type ==
        "organization"``.

        Args:
            documents: List of normalised sanctions dicts.
        """
        if self.octi is None:
            return

        created = 0
        for doc in documents:
            if doc["entity_type"] == "organization":
                result = create_organization(
                    self.octi,
                    name=doc["name"],
                    description=(
                        f"Sanctioned by {doc['sanctions_source']}. "
                        f"Programs: {', '.join(doc.get('programs', []))}. "
                        f"{doc.get('notes', '')}"
                    ),
                )
                if result:
                    created += 1

            # TODO: Create Person entities in OpenCTI for individuals.
            # pycti does not have a direct "person" creation helper — use
            # client.identity.create(type="Individual", ...) when needed.

            # TODO: Create Relationship objects linking sanctioned entities
            # to their countries (originatesFrom, locatedAt).

        logger.info("Created/updated %d entities in OpenCTI.", created)

    # ------------------------------------------------------------------
    # EU / UN sources (stubs)
    # ------------------------------------------------------------------

    def _fetch_eu_sanctions(self) -> list[dict[str, Any]]:
        """Fetch the EU Consolidated Sanctions list.

        Returns:
            List of normalised sanctions entity dicts.
        """
        # TODO: Implement EU sanctions ingestion.
        # The EU publishes a consolidated XML at:
        #   https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content
        # XML schema differs significantly from OFAC — needs dedicated parser.
        logger.info("EU sanctions ingestion not yet implemented.")
        return []

    def _fetch_un_sanctions(self) -> list[dict[str, Any]]:
        """Fetch the UN Security Council Consolidated Sanctions list.

        Returns:
            List of normalised sanctions entity dicts.
        """
        # TODO: Implement UN sanctions ingestion.
        # The UN publishes XML at:
        #   https://scsanctions.un.org/resources/xml/en/consolidated.xml
        # Uses its own XML schema — needs dedicated parser.
        logger.info("UN sanctions ingestion not yet implemented.")
        return []

    # ------------------------------------------------------------------
    # Main ingestion flow
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Execute the full sanctions ingestion pipeline.

        1. Fetch OFAC SDN XML.
        2. Parse and normalise entries.
        3. Ensure index exists and bulk-index.
        4. Push relevant entities to OpenCTI.

        Returns:
            Number of documents indexed.
        """
        # --- OFAC ---
        xml_data = self._fetch_ofac_xml()
        documents = self._parse_ofac_xml(xml_data)

        # --- EU (stub) ---
        documents.extend(self._fetch_eu_sanctions())

        # --- UN (stub) ---
        documents.extend(self._fetch_un_sanctions())

        if not documents:
            logger.info("No sanctions entities to index.")
            return 0

        # --- Index into Elasticsearch ---
        ensure_index(self.es, INDEX_NAME, MAPPING_PATH)
        count = bulk_index(self.es, INDEX_NAME, documents, id_field="entity_id")

        # --- Push to OpenCTI ---
        self._push_to_opencti(documents)

        logger.info("Sanctions ingestion complete: %d entities indexed.", count)
        return count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for the sanctions ingestor."""
    setup_logging("sanctions.ingestor")

    parser = argparse.ArgumentParser(description="GEON sanctions ingestor")
    parser.parse_args()

    try:
        ingestor = SanctionsIngestor()
        ingestor.run()
    except Exception:
        logger.exception("Sanctions ingestion failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
