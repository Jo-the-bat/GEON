"""Tests for the GDELT event parser."""

from __future__ import annotations

import json
from pathlib import Path

from gdelt.parser import (
    calculate_severity,
    extract_cameo_info,
    normalize_event,
    resolve_country_name,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file."""
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CAMEO relevance — the ingestor's private _is_relevant_cameo delegates to
# RELEVANT_CAMEO_PREFIXES, so we test the underlying behaviour here.
# ---------------------------------------------------------------------------


class TestIsRelevantCameo:
    """Verify that the CAMEO filter keeps conflict-adjacent codes and drops
    the rest."""

    @staticmethod
    def _is_relevant(code: str) -> bool:
        # Re-implement the same check the ingestor uses, pulling the set from
        # the parser module to keep the source of truth in one place.
        from gdelt.parser import RELEVANT_CAMEO_PREFIXES

        if not code:
            return False
        return str(code)[:2] in RELEVANT_CAMEO_PREFIXES

    def test_is_relevant_cameo_conflict(self) -> None:
        assert self._is_relevant("130") is True  # Threaten (13x)

    def test_is_relevant_cameo_irrelevant(self) -> None:
        assert self._is_relevant("010") is False  # Public statement (01x)

    def test_is_relevant_cameo_empty(self) -> None:
        assert self._is_relevant("") is False


# ---------------------------------------------------------------------------
# Severity calculation
# ---------------------------------------------------------------------------


class TestSeverityCalculation:
    def test_severity_low(self) -> None:
        """Quiet event: no Goldstein, no articles, neutral tone → low."""
        assert calculate_severity(goldstein_scale=-1.0, num_articles=3, tone=0.0) == "low"

    def test_severity_medium(self) -> None:
        """Moderate conflict: goldstein -5 + 5 articles → score ~2.5 → medium."""
        assert calculate_severity(goldstein_scale=-5.0, num_articles=5, tone=0.0) == "medium"

    def test_severity_high(self) -> None:
        """Serious conflict: goldstein -5 + 50 articles + tone -5 → score 5.0 → high."""
        assert calculate_severity(goldstein_scale=-5.0, num_articles=50, tone=-5.0) == "high"

    def test_severity_critical(self) -> None:
        """Major event: goldstein -9 + 100 articles + tone -8 → score 8.0 → critical."""
        assert calculate_severity(goldstein_scale=-9.0, num_articles=100, tone=-8.0) == "critical"


# ---------------------------------------------------------------------------
# normalize_event — date formats, missing fields, deterministic id
# ---------------------------------------------------------------------------


class TestNormalizeEventDateFormats:
    def test_gdelt_long_format(self) -> None:
        """YYYYMMDDHHmmSS."""
        out = normalize_event({"seendate": "20250615120000"})
        assert out["date"].startswith("2025-06-15T12:00:00")

    def test_gdelt_short_format(self) -> None:
        """YYYYMMDD."""
        out = normalize_event({"Day": "20240101"})
        assert out["date"].startswith("2024-01-01T00:00:00")

    def test_iso_format(self) -> None:
        """Already ISO-formatted → preserved."""
        out = normalize_event({"date": "2025-06-15T12:00:00+00:00"})
        assert out["date"].startswith("2025-06-15T12:00:00")


class TestNormalizeEventMissingFields:
    def test_empty_input_does_not_crash(self) -> None:
        """normalize_event on an empty dict should return sensible defaults."""
        out = normalize_event({})
        assert out["source_country"] == ""
        assert out["target_country"] == ""
        assert out["goldstein_scale"] == 0.0
        assert out["tone"] == 0.0
        assert out["num_articles"] == 1
        assert out["cameo_code"] == ""
        assert out["severity"] == "low"
        # Must have an event_id even without input
        assert out["event_id"]

    def test_fixture_articles_all_normalise(self) -> None:
        """The three sample articles should all round-trip through normalize
        without raising and without emitting any None for core fields."""
        sample = load_fixture("gdelt_sample.json")
        for article in sample["articles"]:
            out = normalize_event(article)
            assert isinstance(out["goldstein_scale"], float)
            assert isinstance(out["tone"], float)
            assert isinstance(out["date"], str)


class TestEventIdDeterminism:
    def test_same_input_same_id(self) -> None:
        raw = {
            "date": "20250615120000",
            "url": "https://example.com/a",
            "source_country": "RUS",
            "target_country": "UKR",
        }
        out1 = normalize_event(raw)
        out2 = normalize_event(raw)
        assert out1["event_id"] == out2["event_id"]

    def test_different_url_different_id(self) -> None:
        base = {
            "date": "20250615120000",
            "source_country": "RUS",
            "target_country": "UKR",
        }
        a = normalize_event({**base, "url": "https://example.com/a"})
        b = normalize_event({**base, "url": "https://example.com/b"})
        assert a["event_id"] != b["event_id"]


# ---------------------------------------------------------------------------
# CAMEO/country helpers
# ---------------------------------------------------------------------------


class TestCameoAndCountryHelpers:
    def test_extract_cameo_info_known_subcode(self) -> None:
        info = extract_cameo_info("190")
        assert info["code"] == "190"
        assert info["category"] == "19"
        assert "military force" in info["description"].lower()

    def test_extract_cameo_info_unknown(self) -> None:
        info = extract_cameo_info("999")
        # Category "99" isn't in CAMEO_CODES → description stays "Unknown"
        assert info["description"] == "Unknown"

    def test_resolve_country_known_code(self) -> None:
        assert resolve_country_name("USA") == "UNITED STATES"
        assert resolve_country_name("rus") == "RUSSIA"

    def test_resolve_country_unknown_returns_upper(self) -> None:
        # Actor type codes (GOV, MIL, ...) are left as-is, uppercased.
        assert resolve_country_name("GOV") == "GOV"
