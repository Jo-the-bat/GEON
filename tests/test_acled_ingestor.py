"""Tests for the ACLED event ingestor.

We test the static helpers directly — _normalise_event and
_index_name_for_date — so we don't have to stand up an ACLED account or an
Elasticsearch cluster.
"""

from __future__ import annotations

import json
from pathlib import Path

from acled.ingestor import ACLEDIngestor

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file."""
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# _normalise_event
# ---------------------------------------------------------------------------


class TestAcledNormalise:
    def setup_method(self) -> None:
        self.sample = load_fixture("acled_sample.json")

    def test_normalise_event_complete(self) -> None:
        """A complete ACLED record should round-trip into the ES schema with
        all primary fields populated."""
        raw = self.sample["data"][0]  # Libya battle event
        doc = ACLEDIngestor._normalise_event(raw)

        assert doc["event_id"] == str(raw["data_id"])
        assert doc["event_type"] == "Battles"
        # Country is normalized to the GEON canonical dimension at write.
        assert doc["country"] == "LIBYA"
        assert doc["fatalities"] == 8
        assert doc["latitude"] == 31.2089
        assert doc["longitude"] == 16.5887
        assert doc["geo_location"] == {"lat": 31.2089, "lon": 16.5887}
        assert doc["actor1"].startswith("Libyan National Army")

    def test_normalise_event_missing_coords(self) -> None:
        """An event without coordinates should still normalise; geo_location
        is None rather than {"lat": None, "lon": None}."""
        raw = {"data_id": 1, "event_date": "2025-01-01", "event_type": "Riots"}
        doc = ACLEDIngestor._normalise_event(raw)
        assert doc["latitude"] is None
        assert doc["longitude"] is None
        assert doc["geo_location"] is None
        assert doc["fatalities"] == 0  # default

    def test_normalise_event_invalid_fatalities(self) -> None:
        """Non-numeric ``fatalities`` should not raise — fall back to 0."""
        raw = {
            "data_id": 2,
            "event_date": "2025-01-02",
            "fatalities": "not-a-number",
        }
        doc = ACLEDIngestor._normalise_event(raw)
        assert doc["fatalities"] == 0


# ---------------------------------------------------------------------------
# _index_name_for_date
# ---------------------------------------------------------------------------


class TestAcledIndexNaming:
    def test_index_name_for_date(self) -> None:
        name = ACLEDIngestor._index_name_for_date("2025-06-15")
        assert name == "geon-acled-events-2025.06"

    def test_index_name_for_malformed_date_falls_back(self) -> None:
        name = ACLEDIngestor._index_name_for_date("not-a-date")
        assert name.startswith("geon-acled-events-")
        # 4-digit year + "." + 2-digit month
        tail = name.split("-")[-1]
        assert len(tail) == 7 and tail[4] == "."

    def test_index_name_preserves_day_prefix(self) -> None:
        """Index name is derived only from YYYY-MM; day is irrelevant."""
        a = ACLEDIngestor._index_name_for_date("2026-04-16")
        b = ACLEDIngestor._index_name_for_date("2026-04-01")
        assert a == b == "geon-acled-events-2026.04"


# ---------------------------------------------------------------------------
# End-to-end normalisation of the fixture
# ---------------------------------------------------------------------------


class TestAcledBatchNormalise:
    def test_normalise_empty_response(self) -> None:
        """Nothing to normalise should produce nothing (and not crash)."""
        payload = {"status": 200, "success": True, "count": 0, "data": []}
        out = [ACLEDIngestor._normalise_event(e) for e in payload["data"]]
        assert out == []

    def test_normalise_all_sample_events(self) -> None:
        sample = load_fixture("acled_sample.json")
        normalised = [ACLEDIngestor._normalise_event(e) for e in sample["data"]]
        assert len(normalised) == 3
        # Fatalities sum across the sample = 8 + 12 + 5 = 25
        assert sum(d["fatalities"] for d in normalised) == 25
        # All events have a country set
        assert all(d["country"] for d in normalised)


# ---------------------------------------------------------------------------
# Mapping file sanity
# ---------------------------------------------------------------------------


class TestAcledMapping:
    def test_mapping_valid_json(self) -> None:
        mapping_path = (
            Path(__file__).resolve().parent.parent
            / "ingestors"
            / "acled"
            / "mapping.json"
        )
        with open(mapping_path) as f:
            mapping = json.load(f)
        assert "mappings" in mapping
        assert "properties" in mapping["mappings"]
