"""Tests for correlation Rule 9 (internet outage + APT activity).

Covers the three APT lookup paths: OpenCTI campaigns (offensive), the ES
CTI index (offensive + targeting), and the static-mapping fallback.
The ES mock routes by index, and CTI queries are told apart by shape
(offensive uses a top-level ``should``; targeting uses ``filter``).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.rules.outage_apt import (
    _COUNTRY_APT_MAP,
    APT_WINDOW_DAYS,
    CTI_INDEX,
    OUTAGES_INDEX,
    OutageAPTRule,
)

OUTAGE_START = "2026-06-12T00:00:00+00:00"


def _hits(docs):
    return {"hits": {"hits": [{"_id": f"id-{i}", "_source": d} for i, d in enumerate(docs)]}}


def _outage(country="RUSSIA", type_="country-level", scope="national",
            severity="total", start_time=OUTAGE_START, outage_id="out-9",
            duration_hours=6):
    return {"country": country, "type": type_, "scope": scope,
            "severity": severity, "start_time": start_time,
            "outage_id": outage_id, "duration_hours": duration_hours}


def _assert_scoring_contract(corr):
    """Every correlation carries auditable confidence + evidence refs."""
    assert isinstance(corr["confidence"], int)
    assert 5 <= corr["confidence"] <= 95
    assert isinstance(corr["confidence_factors"], dict)
    assert "base" in corr["confidence_factors"]
    assert corr["evidence"]
    for ev in corr["evidence"]:
        assert {"index", "doc_id", "kind", "summary"} <= set(ev)


def _make_rule(outages, cti_offensive=(), cti_targeting=(), octi=None):
    rule = OutageAPTRule(es=MagicMock(), octi=octi)
    calls = []

    def _search(**kwargs):
        calls.append(kwargs)
        index = kwargs["index"]
        if index == OUTAGES_INDEX:
            return _hits(outages)
        if index == CTI_INDEX:
            if "filter" in kwargs["query"]["bool"]:
                return _hits(list(cti_targeting))
            return _hits(list(cti_offensive))
        raise AssertionError(f"unexpected index queried: {index}")

    rule.es.search.side_effect = _search
    return rule, calls


class TestOffensiveOpenCTIPath:
    def test_national_outage_with_attributed_campaign_is_critical(self, monkeypatch):
        seen = {}

        def fake_campaigns(_octi, country, days_back=30):
            seen["country"] = country
            seen["days_back"] = days_back
            return [{"name": "APT28", "id": "campaign--1"}]

        monkeypatch.setattr(
            "correlation.rules.outage_apt.get_campaigns_by_country", fake_campaigns
        )
        rule, _ = _make_rule([_outage()], octi=MagicMock())

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        assert corr["rule_name"] == "outage_apt_activity"
        assert corr["severity"] == "critical"  # national + offensive APT
        assert corr["countries_involved"] == ["RUSSIA"]
        assert corr["correlation_id"]
        assert corr["cyber_event"]["apt_group"] == "APT28"
        assert corr["cyber_event"]["campaign_id"] == "campaign--1"
        assert "attributed to RUSSIA: APT28" in corr["description"]
        assert corr["diplomatic_event"]["event_id"] == "out-9"
        assert corr["timeline"][0]["type"] == "internet_outage"
        assert any(
            e["type"] == "apt_activity" and "APT28 (offensive)" in e["description"]
            for e in corr["timeline"]
        )
        # The configured APT activity window is forwarded to OpenCTI.
        assert seen == {"country": "RUSSIA", "days_back": APT_WINDOW_DAYS}
        # Confidence/evidence contract: outage ref + STIX-validated APT.
        _assert_scoring_contract(corr)
        assert corr["evidence"][0]["kind"] == "outage"
        assert corr["evidence"][0]["index"] == OUTAGES_INDEX
        assert corr["evidence"][0]["doc_id"] == "id-0"  # ES _id propagated
        cyber_evs = [e for e in corr["evidence"] if e["kind"] == "cyber"]
        assert [e["index"] for e in cyber_evs] == ["opencti"]
        assert cyber_evs[0]["doc_id"] == "campaign--1"
        # OpenCTI is the strongest provenance present.
        assert corr["confidence_factors"]["attribution"] == 30.0
        assert corr["confidence_factors"]["volume"] > 0


class TestOffensiveEsCtiPath:
    def test_cti_index_hits_preferred_over_static(self):
        rule, _ = _make_rule(
            [_outage()], cti_offensive=[{"name": "Sandworm Team"}]
        )

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        assert corr["cyber_event"]["apt_group"] == "Sandworm Team"
        assert corr["cyber_event"]["campaign_id"] == "id-0"  # ES _id propagated
        assert corr["severity"] == "critical"
        # Indexed-CTI provenance scores between opencti and static.
        _assert_scoring_contract(corr)
        assert corr["confidence_factors"]["attribution"] == 20.0
        cyber_evs = [e for e in corr["evidence"] if e["kind"] == "cyber"]
        assert all(e["index"] == CTI_INDEX for e in cyber_evs)


class TestStaticFallbackPath:
    def test_no_octi_no_cti_hits_uses_static_attribution(self):
        rule, _ = _make_rule(
            [_outage(type_="asn-level", scope="regional", severity="partial")]
        )

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        # Non-national outage: no critical escalation, offensive APT => high.
        assert corr["severity"] == "high"
        assert corr["cyber_event"]["apt_group"] == _COUNTRY_APT_MAP["RUSSIA"][0]
        assert corr["cyber_event"]["campaign_id"] == ""
        # Static fallback is capped at 3 groups, all flagged offensive.
        apt_entries = [e for e in corr["timeline"] if e["type"] == "apt_activity"]
        assert len(apt_entries) == 3
        assert all("(offensive)" in e["description"] for e in apt_entries)
        # Static attribution is the weakest provenance.
        _assert_scoring_contract(corr)
        assert corr["confidence_factors"]["attribution"] == 15.0
        cyber_evs = [e for e in corr["evidence"] if e["kind"] == "cyber"]
        assert cyber_evs and all(e["index"] == "static" for e in cyber_evs)
        # Without an ES doc, the APT name itself is the reference.
        assert cyber_evs[0]["doc_id"] == _COUNTRY_APT_MAP["RUSSIA"][0]


class TestTargetingPath:
    def test_targeting_only_is_high_even_for_national_outage(self):
        assert "UKRAINE" not in _COUNTRY_APT_MAP  # fixture sanity
        rule, _ = _make_rule(
            [_outage(country="UKRAINE")],
            cti_targeting=[{"name": "Sandworm Team"}],
        )

        out = rule.run()

        assert len(out) == 1
        corr = out[0]
        # Critical is reserved for offensive (state-shutdown) attribution.
        assert corr["severity"] == "high"
        assert corr["cyber_event"]["apt_group"] == "Sandworm Team"
        assert "targeting UKRAINE: Sandworm Team" in corr["description"]


class TestNoAptSignal:
    def test_outage_without_any_apt_match_is_skipped(self):
        assert "MOLDOVA" not in _COUNTRY_APT_MAP  # fixture sanity
        rule, calls = _make_rule([_outage(country="MOLDOVA")])
        assert rule.run() == []
        # No static mapping => only the targeting CTI query is issued.
        cti_calls = [c for c in calls if c["index"] == CTI_INDEX]
        assert len(cti_calls) == 1
        assert "filter" in cti_calls[0]["query"]["bool"]


class TestEmptyData:
    def test_no_outages_returns_empty(self):
        rule, calls = _make_rule([])
        assert rule.run() == []
        assert len(calls) == 1

    def test_es_error_returns_empty(self):
        rule = OutageAPTRule(es=MagicMock())
        rule.es.search.side_effect = RuntimeError("es down")
        assert rule.run() == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
