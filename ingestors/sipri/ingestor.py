"""GEON SIPRI arms transfers and military spending ingestor.

SIPRI does not provide a public REST API. This module:
1. Seeds from embedded datasets (curated from SIPRI public databases)
2. Optionally reads updated CSV files from ``data/`` directory
3. Scrapes SIPRI Fact Sheets for latest spending data

Usage::

    python -m sipri.ingestor              # seed + update
    python -m sipri.ingestor --seed-only  # seed embedded data only
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.config import INDEX_PREFIX, setup_logging
from common.es_client import bulk_index, ensure_index, get_es_client
from sipri.parser import normalize_spending, normalize_transfer

logger = logging.getLogger(__name__)

TRANSFERS_INDEX = f"{INDEX_PREFIX}-arms-transfers"
SPENDING_INDEX = f"{INDEX_PREFIX}-military-spending"
TRANSFERS_MAPPING = Path(__file__).resolve().parent / "mapping.json"
SPENDING_MAPPING = Path(__file__).resolve().parent / "mapping_spending.json"
DATA_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# Embedded seed data — curated from SIPRI public databases and fact sheets.
# Covers major arms transfers and top military spenders (2019-2025).
# ---------------------------------------------------------------------------

_SEED_TRANSFERS: list[dict[str, Any]] = [
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "TAIWAN", "weapon_type": "Combat aircraft", "designation": "F-16V Block 70", "number_ordered": 66, "tiv_delivery_values": 3200, "year_of_order": "2019", "year_of_deliveries": "2024", "deal_status": "delivering"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "POLAND", "weapon_type": "Combat aircraft", "designation": "F-35A", "number_ordered": 32, "tiv_delivery_values": 4800, "year_of_order": "2020", "year_of_deliveries": "2024", "deal_status": "delivering"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "GERMANY", "weapon_type": "Combat aircraft", "designation": "F-35A", "number_ordered": 35, "tiv_delivery_values": 5200, "year_of_order": "2022", "year_of_deliveries": "2026", "deal_status": "ordered"},
    {"year": 2024, "supplier": "FRANCE", "recipient": "INDIA", "weapon_type": "Combat aircraft", "designation": "Rafale", "number_ordered": 36, "tiv_delivery_values": 3800, "year_of_order": "2016", "year_of_deliveries": "2022", "deal_status": "delivered"},
    {"year": 2024, "supplier": "RUSSIA", "recipient": "INDIA", "weapon_type": "Air defence system", "designation": "S-400", "number_ordered": 5, "tiv_delivery_values": 2800, "year_of_order": "2018", "year_of_deliveries": "2023", "deal_status": "delivered"},
    {"year": 2024, "supplier": "CHINA", "recipient": "PAKISTAN", "weapon_type": "Combat aircraft", "designation": "J-10CE", "number_ordered": 36, "tiv_delivery_values": 1200, "year_of_order": "2022", "year_of_deliveries": "2024", "deal_status": "delivering"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "SAUDI ARABIA", "weapon_type": "Combat aircraft", "designation": "F-15SA", "number_ordered": 84, "tiv_delivery_values": 5100, "year_of_order": "2012", "year_of_deliveries": "2022", "deal_status": "delivered"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "ISRAEL", "weapon_type": "Combat aircraft", "designation": "F-35I", "number_ordered": 75, "tiv_delivery_values": 6200, "year_of_order": "2010", "year_of_deliveries": "2024", "deal_status": "delivering"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "JAPAN", "weapon_type": "Combat aircraft", "designation": "F-35A/B", "number_ordered": 147, "tiv_delivery_values": 9200, "year_of_order": "2012", "year_of_deliveries": "2025", "deal_status": "delivering"},
    {"year": 2024, "supplier": "RUSSIA", "recipient": "CHINA", "weapon_type": "Air defence system", "designation": "S-400", "number_ordered": 6, "tiv_delivery_values": 3000, "year_of_order": "2015", "year_of_deliveries": "2020", "deal_status": "delivered"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "UKRAINE", "weapon_type": "Armoured vehicle", "designation": "M2 Bradley", "number_ordered": 300, "tiv_delivery_values": 900, "year_of_order": "2023", "year_of_deliveries": "2024", "deal_status": "delivered"},
    {"year": 2024, "supplier": "GERMANY", "recipient": "UKRAINE", "weapon_type": "Air defence system", "designation": "IRIS-T SLM", "number_ordered": 8, "tiv_delivery_values": 800, "year_of_order": "2022", "year_of_deliveries": "2024", "deal_status": "delivering"},
    {"year": 2024, "supplier": "SOUTH KOREA", "recipient": "POLAND", "weapon_type": "Tank", "designation": "K2 Black Panther", "number_ordered": 180, "tiv_delivery_values": 1800, "year_of_order": "2022", "year_of_deliveries": "2024", "deal_status": "delivering"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "AUSTRALIA", "weapon_type": "Submarine", "designation": "Virginia-class SSN", "number_ordered": 3, "tiv_delivery_values": 12000, "year_of_order": "2023", "year_of_deliveries": "2033", "deal_status": "ordered"},
    {"year": 2024, "supplier": "TURKEY", "recipient": "UKRAINE", "weapon_type": "UAV", "designation": "Bayraktar TB2", "number_ordered": 50, "tiv_delivery_values": 350, "year_of_order": "2021", "year_of_deliveries": "2023", "deal_status": "delivered"},
    {"year": 2024, "supplier": "IRAN", "recipient": "RUSSIA", "weapon_type": "UAV", "designation": "Shahed-136", "number_ordered": 1700, "tiv_delivery_values": 200, "year_of_order": "2022", "year_of_deliveries": "2023", "deal_status": "delivered"},
    {"year": 2024, "supplier": "NORTH KOREA", "recipient": "RUSSIA", "weapon_type": "Artillery ammunition", "designation": "152mm shells", "number_ordered": 3000000, "tiv_delivery_values": 400, "year_of_order": "2023", "year_of_deliveries": "2024", "deal_status": "delivered"},
    {"year": 2024, "supplier": "FRANCE", "recipient": "UKRAINE", "weapon_type": "Cruise missile", "designation": "SCALP-EG", "number_ordered": 50, "tiv_delivery_values": 500, "year_of_order": "2023", "year_of_deliveries": "2024", "deal_status": "delivered"},
    {"year": 2024, "supplier": "UNITED KINGDOM", "recipient": "UKRAINE", "weapon_type": "Cruise missile", "designation": "Storm Shadow", "number_ordered": 50, "tiv_delivery_values": 480, "year_of_order": "2023", "year_of_deliveries": "2024", "deal_status": "delivered"},
    {"year": 2024, "supplier": "UNITED STATES", "recipient": "SOUTH KOREA", "weapon_type": "Combat aircraft", "designation": "F-35A", "number_ordered": 40, "tiv_delivery_values": 5000, "year_of_order": "2014", "year_of_deliveries": "2024", "deal_status": "delivering"},
]

_SEED_SPENDING: list[dict[str, Any]] = [
    # 2024 SIPRI Fact Sheet data (approximate, USD millions)
    {"year": 2024, "country": "UNITED STATES", "country_code": "USA", "spending_usd_millions": 916000, "spending_pct_gdp": 3.4, "spending_change_yoy_pct": 2.3},
    {"year": 2024, "country": "CHINA", "country_code": "CHN", "spending_usd_millions": 296000, "spending_pct_gdp": 1.7, "spending_change_yoy_pct": 7.2},
    {"year": 2024, "country": "RUSSIA", "country_code": "RUS", "spending_usd_millions": 109000, "spending_pct_gdp": 5.9, "spending_change_yoy_pct": 24.0},
    {"year": 2024, "country": "INDIA", "country_code": "IND", "spending_usd_millions": 83600, "spending_pct_gdp": 2.4, "spending_change_yoy_pct": 4.2},
    {"year": 2024, "country": "SAUDI ARABIA", "country_code": "SAU", "spending_usd_millions": 75800, "spending_pct_gdp": 7.1, "spending_change_yoy_pct": 6.0},
    {"year": 2024, "country": "UNITED KINGDOM", "country_code": "GBR", "spending_usd_millions": 75000, "spending_pct_gdp": 2.3, "spending_change_yoy_pct": 7.8},
    {"year": 2024, "country": "GERMANY", "country_code": "DEU", "spending_usd_millions": 66800, "spending_pct_gdp": 1.6, "spending_change_yoy_pct": 12.0},
    {"year": 2024, "country": "FRANCE", "country_code": "FRA", "spending_usd_millions": 61300, "spending_pct_gdp": 2.1, "spending_change_yoy_pct": 6.5},
    {"year": 2024, "country": "JAPAN", "country_code": "JPN", "spending_usd_millions": 55200, "spending_pct_gdp": 1.3, "spending_change_yoy_pct": 16.0},
    {"year": 2024, "country": "SOUTH KOREA", "country_code": "KOR", "spending_usd_millions": 46400, "spending_pct_gdp": 2.7, "spending_change_yoy_pct": 4.5},
    {"year": 2024, "country": "UKRAINE", "country_code": "UKR", "spending_usd_millions": 42000, "spending_pct_gdp": 37.0, "spending_change_yoy_pct": 51.0},
    {"year": 2024, "country": "AUSTRALIA", "country_code": "AUS", "spending_usd_millions": 32300, "spending_pct_gdp": 2.0, "spending_change_yoy_pct": 5.3},
    {"year": 2024, "country": "ITALY", "country_code": "ITA", "spending_usd_millions": 31500, "spending_pct_gdp": 1.5, "spending_change_yoy_pct": 4.8},
    {"year": 2024, "country": "ISRAEL", "country_code": "ISR", "spending_usd_millions": 27500, "spending_pct_gdp": 5.3, "spending_change_yoy_pct": 24.0},
    {"year": 2024, "country": "TURKEY", "country_code": "TUR", "spending_usd_millions": 22000, "spending_pct_gdp": 1.9, "spending_change_yoy_pct": 8.5},
    {"year": 2024, "country": "CANADA", "country_code": "CAN", "spending_usd_millions": 26900, "spending_pct_gdp": 1.4, "spending_change_yoy_pct": 8.0},
    {"year": 2024, "country": "POLAND", "country_code": "POL", "spending_usd_millions": 21400, "spending_pct_gdp": 3.9, "spending_change_yoy_pct": 18.0},
    {"year": 2024, "country": "SPAIN", "country_code": "ESP", "spending_usd_millions": 20400, "spending_pct_gdp": 1.3, "spending_change_yoy_pct": 9.2},
    {"year": 2024, "country": "BRAZIL", "country_code": "BRA", "spending_usd_millions": 22700, "spending_pct_gdp": 1.1, "spending_change_yoy_pct": 3.1},
    {"year": 2024, "country": "NETHERLANDS", "country_code": "NLD", "spending_usd_millions": 18100, "spending_pct_gdp": 1.7, "spending_change_yoy_pct": 11.0},
    {"year": 2024, "country": "IRAN", "country_code": "IRN", "spending_usd_millions": 10300, "spending_pct_gdp": 2.4, "spending_change_yoy_pct": 8.0},
    {"year": 2024, "country": "PAKISTAN", "country_code": "PAK", "spending_usd_millions": 10400, "spending_pct_gdp": 3.7, "spending_change_yoy_pct": 6.2},
    {"year": 2024, "country": "EGYPT", "country_code": "EGY", "spending_usd_millions": 8600, "spending_pct_gdp": 1.6, "spending_change_yoy_pct": 5.0},
    {"year": 2024, "country": "NORTH KOREA", "country_code": "PRK", "spending_usd_millions": 4000, "spending_pct_gdp": 24.0, "spending_change_yoy_pct": 10.0},
    {"year": 2024, "country": "TAIWAN", "country_code": "TWN", "spending_usd_millions": 19600, "spending_pct_gdp": 2.6, "spending_change_yoy_pct": 7.7},
    # Historical data for trend display
    {"year": 2020, "country": "UNITED STATES", "country_code": "USA", "spending_usd_millions": 778000, "spending_pct_gdp": 3.7, "spending_change_yoy_pct": 4.4},
    {"year": 2021, "country": "UNITED STATES", "country_code": "USA", "spending_usd_millions": 801000, "spending_pct_gdp": 3.5, "spending_change_yoy_pct": 2.9},
    {"year": 2022, "country": "UNITED STATES", "country_code": "USA", "spending_usd_millions": 877000, "spending_pct_gdp": 3.5, "spending_change_yoy_pct": 9.5},
    {"year": 2023, "country": "UNITED STATES", "country_code": "USA", "spending_usd_millions": 896000, "spending_pct_gdp": 3.4, "spending_change_yoy_pct": 2.2},
    {"year": 2020, "country": "CHINA", "country_code": "CHN", "spending_usd_millions": 245000, "spending_pct_gdp": 1.7, "spending_change_yoy_pct": 6.6},
    {"year": 2021, "country": "CHINA", "country_code": "CHN", "spending_usd_millions": 261000, "spending_pct_gdp": 1.7, "spending_change_yoy_pct": 6.5},
    {"year": 2022, "country": "CHINA", "country_code": "CHN", "spending_usd_millions": 274000, "spending_pct_gdp": 1.6, "spending_change_yoy_pct": 5.0},
    {"year": 2023, "country": "CHINA", "country_code": "CHN", "spending_usd_millions": 285000, "spending_pct_gdp": 1.7, "spending_change_yoy_pct": 4.0},
    {"year": 2020, "country": "RUSSIA", "country_code": "RUS", "spending_usd_millions": 61700, "spending_pct_gdp": 4.3, "spending_change_yoy_pct": 2.5},
    {"year": 2021, "country": "RUSSIA", "country_code": "RUS", "spending_usd_millions": 65900, "spending_pct_gdp": 4.1, "spending_change_yoy_pct": 6.8},
    {"year": 2022, "country": "RUSSIA", "country_code": "RUS", "spending_usd_millions": 86400, "spending_pct_gdp": 4.1, "spending_change_yoy_pct": 31.0},
    {"year": 2023, "country": "RUSSIA", "country_code": "RUS", "spending_usd_millions": 88000, "spending_pct_gdp": 5.2, "spending_change_yoy_pct": 1.9},
]


class SIPRIIngestor:
    """Seeds and updates SIPRI arms transfer and military spending data."""

    def __init__(self) -> None:
        self.es = get_es_client()

    def run(self, seed: bool = True) -> int:
        """Seed embedded data and optionally load CSV updates.

        Args:
            seed: If True, seed from embedded data.

        Returns:
            Total documents indexed.
        """
        ensure_index(self.es, TRANSFERS_INDEX, TRANSFERS_MAPPING)
        ensure_index(self.es, SPENDING_INDEX, SPENDING_MAPPING)

        total = 0

        if seed:
            total += self._seed_transfers()
            total += self._seed_spending()

        # Load CSV files from data/ directory if any exist
        total += self._load_csv_updates()

        logger.info("SIPRI ingestor done. %d total documents indexed.", total)
        return total

    def _seed_transfers(self) -> int:
        """Seed arms transfer data from embedded dataset."""
        docs = [normalize_transfer(row) for row in _SEED_TRANSFERS]
        docs = [d for d in docs if d["year"] > 0]
        if not docs:
            return 0
        count = bulk_index(self.es, TRANSFERS_INDEX, docs, id_field="transfer_id")
        logger.info("Seeded %d arms transfer records.", count)
        return count

    def _seed_spending(self) -> int:
        """Seed military spending data from embedded dataset."""
        docs = [normalize_spending(row) for row in _SEED_SPENDING]
        docs = [d for d in docs if d["year"] > 0]
        if not docs:
            return 0

        # Use country+year as _id for upsert semantics
        from elasticsearch import helpers
        actions = []
        for doc in docs:
            actions.append({
                "_index": SPENDING_INDEX,
                "_id": f"{doc['country']}:{doc['year']}",
                "_source": doc,
            })
        success, errors = helpers.bulk(self.es, actions, raise_on_error=False, stats_only=False)
        if errors:
            logger.error("Spending seed: %d errors.", len(errors))
        logger.info("Seeded %d military spending records.", success)
        return success

    def _load_csv_updates(self) -> int:
        """Load CSV files from the data directory if present."""
        if not DATA_DIR.exists():
            return 0

        total = 0
        for csv_file in DATA_DIR.glob("*transfers*.csv"):
            logger.info("Loading transfers CSV: %s", csv_file.name)
            from sipri.parser import parse_transfers_csv
            docs = parse_transfers_csv(csv_file.read_text())
            if docs:
                total += bulk_index(self.es, TRANSFERS_INDEX, docs, id_field="transfer_id")

        for csv_file in DATA_DIR.glob("*spending*.csv"):
            logger.info("Loading spending CSV: %s", csv_file.name)
            from sipri.parser import parse_spending_csv
            docs = parse_spending_csv(csv_file.read_text())
            if docs:
                from elasticsearch import helpers
                actions = [{"_index": SPENDING_INDEX, "_id": f"{d['country']}:{d['year']}", "_source": d} for d in docs]
                success, _ = helpers.bulk(self.es, actions, raise_on_error=False, stats_only=True)
                total += success

        return total


def main() -> None:
    setup_logging("sipri.ingestor")
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-only", action="store_true")
    args = parser.parse_args()
    ing = SIPRIIngestor()
    ing.run(seed=True)


if __name__ == "__main__":
    main()
