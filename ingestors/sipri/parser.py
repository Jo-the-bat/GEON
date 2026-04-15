"""Parser for SIPRI arms transfers and military spending data.

SIPRI does not expose a public REST API. Data comes from:
- Embedded seed datasets (curated from SIPRI public databases)
- Optional CSV files placed in data/ directory for updates

The parser normalizes records into the geon-arms-transfers and
geon-military-spending schemas.
"""

from __future__ import annotations

import csv
import hashlib
import logging
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def normalize_transfer(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize an arms transfer row into index schema."""
    now = datetime.now(timezone.utc).isoformat()
    year = int(row.get("year", 0) or 0)
    supplier = str(row.get("supplier_country", row.get("supplier", ""))).upper()
    recipient = str(row.get("recipient_country", row.get("recipient", ""))).upper()
    weapon_type = str(row.get("weapon_type", row.get("armament_category", "")))
    weapon_desc = str(row.get("weapon_description", row.get("designation", "")))
    quantity = int(row.get("quantity", row.get("number_ordered", 0)) or 0)
    tiv = float(row.get("tiv_value", row.get("tiv_delivery_values", 0)) or 0)
    order_date = str(row.get("order_date", row.get("year_of_order", "")))
    delivery_date = str(row.get("delivery_date", row.get("year_of_deliveries", "")))
    status = str(row.get("status", row.get("deal_status", "unknown"))).lower()

    transfer_id = hashlib.sha256(
        f"{year}:{supplier}:{recipient}:{weapon_desc}:{quantity}".encode()
    ).hexdigest()[:20]

    return {
        "transfer_id": transfer_id,
        "year": year,
        "supplier_country": supplier,
        "recipient_country": recipient,
        "weapon_type": weapon_type,
        "weapon_description": weapon_desc,
        "quantity": quantity,
        "tiv_value": tiv,
        "order_date": order_date,
        "delivery_date": delivery_date,
        "status": status,
        "date": f"{year}-06-15T00:00:00Z" if year else now,
        "ingested_at": now,
    }


def normalize_spending(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a military spending row into index schema."""
    now = datetime.now(timezone.utc).isoformat()
    year = int(row.get("year", 0) or 0)
    country = str(row.get("country", "")).upper()
    cc = str(row.get("country_code", "")).upper()
    usd = float(row.get("spending_usd_millions", 0) or 0)
    pct_gdp = float(row.get("spending_pct_gdp", 0) or 0)
    yoy = float(row.get("spending_change_yoy_pct", 0) or 0)

    return {
        "year": year,
        "country": country,
        "country_code": cc,
        "spending_usd_millions": usd,
        "spending_pct_gdp": pct_gdp,
        "spending_change_yoy_pct": yoy,
        "date": f"{year}-01-01T00:00:00Z" if year else now,
        "ingested_at": now,
    }


def parse_transfers_csv(content: str) -> list[dict[str, Any]]:
    """Parse a CSV of arms transfers into normalized documents."""
    docs = []
    reader = csv.DictReader(StringIO(content))
    for row in reader:
        doc = normalize_transfer(row)
        if doc["year"] > 0 and doc["supplier_country"]:
            docs.append(doc)
    return docs


def parse_spending_csv(content: str) -> list[dict[str, Any]]:
    """Parse a CSV of military spending into normalized documents."""
    docs = []
    reader = csv.DictReader(StringIO(content))
    for row in reader:
        doc = normalize_spending(row)
        if doc["year"] > 0 and doc["country"]:
            docs.append(doc)
    return docs
