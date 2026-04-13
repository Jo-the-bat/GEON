"""Tests for the ACLED event ingestor."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file."""
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


class TestAcledEventParsing:
    """Tests for parsing ACLED API responses."""

    def setup_method(self) -> None:
        self.sample_data = load_fixture("acled_sample.json")

    def test_parse_single_event(self) -> None:
        """Should parse a single ACLED event into the expected schema."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import parse_event
        #
        # events = self.sample_data["data"]
        # result = parse_event(events[0])
        #
        # assert result["event_id"] is not None
        # assert isinstance(result["event_date"], str)
        # assert result["event_type"] in (
        #     "Battles", "Violence against civilians",
        #     "Explosions/Remote violence", "Riots", "Protests",
        #     "Strategic developments"
        # )
        pass

    def test_parse_batch(self) -> None:
        """Should parse a batch of events."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import parse_events
        #
        # results = parse_events(self.sample_data)
        # assert isinstance(results, list)
        # assert len(results) == len(self.sample_data["data"])
        pass

    def test_fatalities_is_integer(self) -> None:
        """Fatalities field should be a non-negative integer."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import parse_events
        #
        # results = parse_events(self.sample_data)
        # for event in results:
        #     assert isinstance(event["fatalities"], int)
        #     assert event["fatalities"] >= 0
        pass

    def test_geolocation_present(self) -> None:
        """Each event should have valid latitude and longitude."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import parse_events
        #
        # results = parse_events(self.sample_data)
        # for event in results:
        #     assert -90 <= event["latitude"] <= 90
        #     assert -180 <= event["longitude"] <= 180
        pass

    def test_actor_extraction(self) -> None:
        """Should extract actor1 and actor2 fields."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import parse_event
        #
        # event = self.sample_data["data"][0]
        # result = parse_event(event)
        # assert "actor1" in result
        # assert isinstance(result["actor1"], str)
        pass

    def test_empty_response(self) -> None:
        """Should handle empty data array."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import parse_events
        #
        # result = parse_events({"data": [], "count": 0, "status": 200})
        # assert result == []
        pass


class TestAcledApiClient:
    """Tests for the ACLED API client."""

    def test_api_key_required(self) -> None:
        """Should raise an error if ACLED_API_KEY is not set."""
        # TODO: Import ingestor once implemented
        # with patch.dict(os.environ, {}, clear=True):
        #     from ingestors.acled.ingestor import get_api_key
        #     with pytest.raises(ValueError):
        #         get_api_key()
        pass

    @patch("requests.get")
    def test_fetch_events_success(self, mock_get: MagicMock) -> None:
        """Should fetch and return events on success."""
        # TODO: Import ingestor once implemented
        # mock_get.return_value.status_code = 200
        # mock_get.return_value.json.return_value = load_fixture("acled_sample.json")
        #
        # from ingestors.acled.ingestor import fetch_events
        # result = fetch_events()
        # assert len(result) > 0
        pass

    @patch("requests.get")
    def test_fetch_events_rate_limit(self, mock_get: MagicMock) -> None:
        """Should handle rate limit responses with backoff."""
        # TODO: Import ingestor once implemented
        # mock_get.return_value.status_code = 429
        #
        # from ingestors.acled.ingestor import fetch_events
        # result = fetch_events()
        # assert result == []
        pass

    def test_date_range_construction(self) -> None:
        """Should construct correct date range for daily ingestion."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import build_date_range
        #
        # start, end = build_date_range(lookback_days=1)
        # assert start < end
        pass


class TestAcledElasticsearchIndexing:
    """Tests for indexing ACLED events into Elasticsearch."""

    def test_index_name_format(self) -> None:
        """Index name should follow geon-acled-events-YYYY.MM pattern."""
        # TODO: Import ingestor once implemented
        # from ingestors.acled.ingestor import get_index_name
        #
        # name = get_index_name("2025-06-15")
        # assert name == "geon-acled-events-2025.06"
        pass

    def test_mapping_valid_json(self) -> None:
        """The ACLED mapping file should be valid JSON."""
        mapping_path = Path(__file__).parent.parent / "ingestors" / "acled" / "mapping.json"
        if mapping_path.exists():
            with open(mapping_path) as f:
                mapping = json.load(f)
            assert "mappings" in mapping or "properties" in mapping
