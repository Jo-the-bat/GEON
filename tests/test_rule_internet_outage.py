"""Tests for correlation Rule 5 (internet outage + diplomatic/military escalation).

Elasticsearch is mocked with a router keyed on the queried index so the
three different searches (outages, GDELT, ACLED) can be shaped independently.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.internet_outage import (
    ACLED_INDEX_PATTERN,
    GDELT_INDEX_PATTERN,
    GOLDSTEIN_THRESHOLD,
    OUTAGES_INDEX,
    WINDOW_HOURS,
    InternetOutageRule,
)

OUTAGE_START = "2026-06-11T00:00:00+00:00"


def _hits(docs):
    return {"hits": {"hits": [{"_id": f"id-{i}", "_source": d} for i, d in enumerate(docs)]}}


def _outage(country="RUSSIA", severity="total", type_="country-level",
            scope="national", start_time=OUTAGE_START, outage_id="out-1"):
    return {"country": country, "severity": severity, "type": type_,
            "scope": scope, "start_time": start_time, "outage_id": outage_id}


def _gdelt_event(goldstein):
    return {"event_id": "777", "date": "2026-06-11T06:00:00+00:00",
            "goldstein_scale": goldstein,
            "cameo_description": "Military force deployment"}


def _acled_event():
    return {"event_date": "2026-06-10T12:00:00+00:00", "event_type": "Battles",
            "notes": "Clashes near the border"}


def _make_rule(outages, gdelt, acled):
    """Rule with an ES mock routing searches by target index."""
    rule = InternetOutageRule(es=MagicMock(), octi=None)
    calls = []

    def _search(**kwargs):
        calls.append(kwargs)
        index = kwargs["index"]
        if index == OUTAGES_INDEX:
            return _hits(outages)
        if index == GDELT_INDEX_PATTERN:
            return _hits(gdelt)
        if index == ACLED_INDEX_PATTERN:
            return _hits(acled)
        raise AssertionError(f"unexpected index queried: {index}")

    rule.es.search.side_effect = _search
    return rule, calls


class TestRunHappyPath:
    def test_total_outage_with_conflict_and_escalation_is_critical(self):
        gdelt = _gdelt_event(GOLDSTEIN_THRESHOLD - 1.0)
        rule, _ = _make_rule([_outage()], [gdelt], [_acled_event()])

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        assert corr["rule_name"] == "internet_outage_escalation"
        assert corr["severity"] == "critical"
        assert corr["countries_involved"] == ["RUSSIA"]
        assert corr["correlation_id"]
        assert "Internet outage (total) in RUSSIA" in corr["description"]
        assert corr["diplomatic_event"]["goldstein"] == GOLDSTEIN_THRESHOLD - 1.0
        assert corr["diplomatic_event"]["event_id"] == "777"
        # The outage id rides in the cyber_event slot (current behaviour).
        assert corr["cyber_event"]["campaign_id"] == "out-1"
        assert [e["type"] for e in corr["timeline"]] == [
            "internet_outage", "diplomatic", "conflict",
        ]


class TestQueryShape:
    def test_acled_query_uses_event_date_within_window(self):
        """ACLED docs carry ``event_date`` (not ``date``) — pin the field
        name and the +/- WINDOW_HOURS bounds around the outage start."""
        rule, calls = _make_rule([_outage()], [], [_acled_event()])
        rule.run()

        acled_calls = [c for c in calls if c["index"] == ACLED_INDEX_PATTERN]
        assert len(acled_calls) == 1
        filters = acled_calls[0]["query"]["bool"]["filter"]
        date_range = filters[0]["range"]["event_date"]
        ref = datetime.fromisoformat(OUTAGE_START)
        assert date_range["gte"] == (ref - timedelta(hours=WINDOW_HOURS)).isoformat()
        assert date_range["lte"] == (ref + timedelta(hours=WINDOW_HOURS)).isoformat()
        assert filters[1] == {"term": {"country": "RUSSIA"}}

    def test_gdelt_query_uses_goldstein_threshold(self):
        rule, calls = _make_rule([_outage()], [_gdelt_event(GOLDSTEIN_THRESHOLD - 1.0)], [])
        rule.run()

        gdelt_calls = [c for c in calls if c["index"] == GDELT_INDEX_PATTERN]
        assert len(gdelt_calls) == 1
        filters = gdelt_calls[0]["query"]["bool"]["filter"]
        assert {"range": {"goldstein_scale": {"lt": GOLDSTEIN_THRESHOLD}}} in filters


class TestNoCorroboration:
    def test_outage_alone_produces_no_correlation(self):
        """An outage with neither GDELT escalation nor ACLED conflict in
        the window must not fire."""
        rule, calls = _make_rule([_outage()], [], [])
        assert rule.run() == []
        # Both corroboration sources were checked.
        assert {c["index"] for c in calls} == {
            OUTAGES_INDEX, GDELT_INDEX_PATTERN, ACLED_INDEX_PATTERN,
        }


class TestEmptyData:
    def test_no_outages_returns_empty(self):
        rule, calls = _make_rule([], [], [])
        assert rule.run() == []
        assert len(calls) == 1  # stops after the outage query

    def test_es_error_returns_empty(self):
        rule = InternetOutageRule(es=MagicMock(), octi=None)
        rule.es.search.side_effect = RuntimeError("es down")
        assert rule.run() == []


class TestCountryNormalization:
    def test_legacy_spelling_normalized_before_querying(self):
        """Pre-migration outage docs may carry old Cloudflare spellings;
        they must be normalized for both the output and the sub-queries."""
        rule, calls = _make_rule(
            [_outage(country="IVORY COAST")],
            [_gdelt_event(GOLDSTEIN_THRESHOLD - 1.0)],
            [],
        )
        out = rule.run()

        assert out[0]["countries_involved"] == ["COTE D'IVOIRE"]
        gdelt_calls = [c for c in calls if c["index"] == GDELT_INDEX_PATTERN]
        should = gdelt_calls[0]["query"]["bool"]["filter"][2]["bool"]["should"]
        assert {"term": {"source_country": "COTE D'IVOIRE"}} in should
        assert {"term": {"target_country": "COTE D'IVOIRE"}} in should


class TestSeverityMatrix:
    """_build_correlation: total+conflict=critical; total or conflict or
    diplomatic alone=high; nothing=medium (unreachable from run())."""

    @pytest.mark.parametrize(
        ("outage_severity", "gdelt", "acled", "expected"),
        [
            ("total", [], [_acled_event()], "critical"),
            ("total", [_gdelt_event(-6.0)], [], "high"),
            ("partial", [], [_acled_event()], "high"),
            ("partial", [_gdelt_event(-6.0)], [], "high"),
            ("partial", [], [], "medium"),
        ],
    )
    def test_matrix(self, outage_severity, gdelt, acled, expected):
        rule = object.__new__(InternetOutageRule)
        corr = rule._build_correlation(_outage(severity=outage_severity), gdelt, acled)
        assert corr["severity"] == expected


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
