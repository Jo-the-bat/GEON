"""Tests for the analyst lifecycle: triage CLI + engine integration."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from correlation.engine import CorrelationEngine
from correlation.triage import STATUSES, list_correlations, set_status


def _engine():
    engine = object.__new__(CorrelationEngine)
    engine.es = MagicMock()
    engine.octi = None
    engine.rules = []
    engine.dry_run = False
    return engine


def _candidate(cid="abc123", severity="high"):
    return {
        "correlation_id": cid,
        "timestamp": "2026-06-12T10:00:00+00:00",
        "rule_name": "diplomatic_escalation_apt",
        "severity": severity,
        "countries_involved": ["RUSSIA", "UKRAINE"],
        "description": "d",
        "timeline": [],
    }


def _mget(found):
    return {"docs": [{"_id": k, "found": True, "_source": v}
                     for k, v in found.items()]}


class TestEngineLifecycle:
    def test_new_situations_open(self):
        engine = _engine()
        engine.es.indices.exists.return_value = False
        new, _, _ = engine._reconcile([_candidate()])
        assert new[0]["status"] == "open"

    def test_false_positive_never_realerts(self):
        engine = _engine()
        engine.es.indices.exists.return_value = True
        stored = _candidate(severity="medium")
        stored["status"] = "false_positive"
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget({"abc123": stored})

        # Escalation would normally re-alert — the analyst verdict wins.
        cand = _candidate(severity="critical")
        _, updated, alertable = engine._reconcile([cand])
        assert updated[0]["status"] == "false_positive"
        assert alertable == []

    def test_engine_never_clobbers_triage(self):
        engine = _engine()
        engine.es.indices.exists.return_value = True
        stored = _candidate(severity="medium")
        stored.update({
            "status": "acknowledged",
            "triage_note": "looking into it",
            "triaged_at": "2026-06-10T00:00:00+00:00",
            "last_seen": datetime.now(timezone.utc).isoformat(),
        })
        engine.es.mget.return_value = _mget({"abc123": stored})

        cand = _candidate(severity="critical")  # payload refresh path
        _, updated, _ = engine._reconcile([cand])
        assert updated[0]["status"] == "acknowledged"
        assert updated[0]["triage_note"] == "looking into it"

    def test_resolved_reopens_on_escalation(self):
        engine = _engine()
        engine.es.indices.exists.return_value = True
        stored = _candidate(severity="medium")
        stored["status"] = "resolved"
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget({"abc123": stored})

        cand = _candidate(severity="critical")
        _, updated, alertable = engine._reconcile([cand])
        assert updated[0]["status"] == "open"
        assert alertable and alertable[0]["alert_context"] == "escalation"


class TestReviewRegressions:
    """Fixes from the phase-3 adversarial review."""

    def test_fp_date_not_refreshed(self):
        """False positives must age OUT of the 30-day windows: the
        engine stops bumping their activity date."""
        engine = _engine()
        engine.es.indices.exists.return_value = True
        stored = _candidate()
        stored["status"] = "false_positive"
        stored["date"] = "2026-01-01T00:00:00+00:00"
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget({"abc123": stored})

        _, updated, _ = engine._reconcile([_candidate()])
        assert updated[0]["date"] == "2026-01-01T00:00:00+00:00"

    def test_acknowledged_suppresses_reactivation_not_escalation(self):
        from datetime import timedelta

        from correlation.engine import REACTIVATION_DAYS
        engine = _engine()
        engine.es.indices.exists.return_value = True
        dormant = (datetime.now(timezone.utc)
                   - timedelta(days=REACTIVATION_DAYS + 1)).isoformat()

        stored = _candidate()
        stored.update({"status": "acknowledged", "last_seen": dormant})
        engine.es.mget.return_value = _mget({"abc123": stored})
        _, _, alertable = engine._reconcile([_candidate()])
        assert alertable == []  # reactivation muted: analyst engaged

        stored2 = _candidate(severity="medium")
        stored2.update({"status": "acknowledged",
                        "last_seen": datetime.now(timezone.utc).isoformat()})
        engine.es.mget.return_value = _mget({"abc123": stored2})
        _, _, alertable = engine._reconcile([_candidate(severity="critical")])
        assert alertable and alertable[0]["alert_context"] == "escalation"

    def test_legacy_doc_gets_status_on_merge(self):
        engine = _engine()
        engine.es.indices.exists.return_value = True
        stored = _candidate()  # no status field (pre-lifecycle doc)
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget({"abc123": stored})

        _, updated, _ = engine._reconcile([_candidate()])
        assert updated[0]["status"] == "open"

    def test_stale_optional_payload_keys_removed(self):
        engine = _engine()
        engine.es.indices.exists.return_value = True
        stored = _candidate()
        stored.update({"flag": "intelligence_fusion_alert",
                       "baseline": {"zscore": 3.4},
                       "last_seen": datetime.now(timezone.utc).isoformat()})
        engine.es.mget.return_value = _mget({"abc123": stored})

        cand = _candidate()  # same severity, carries neither key
        _, updated, _ = engine._reconcile([cand])
        assert "flag" not in updated[0]
        assert "baseline" not in updated[0]

    def test_signal_evidence_refreshes_in_place(self):
        """Synthetic signal entries must refresh (candidate wins) instead
        of freezing at first detection or collapsing together."""
        from correlation.engine import CorrelationEngine
        stored = [
            {"index": "geon-gdelt-events-*", "doc_id": "signal:gdelt",
             "kind": "signal", "date": "2026-06-01", "summary": "OLD 100 events"},
            {"index": "geon-sanctions", "doc_id": "signal:sanctions",
             "kind": "signal", "date": "2026-06-01", "summary": "sanctions"},
        ]
        cand = [
            {"index": "geon-gdelt-events-*", "doc_id": "signal:gdelt",
             "kind": "signal", "date": "2026-06-12", "summary": "NEW 400 events"},
        ]
        merged = CorrelationEngine._merge_evidence(stored, cand)
        assert len(merged) == 2  # distinct signals never collapse
        gdelt = next(e for e in merged if e["doc_id"] == "signal:gdelt")
        assert gdelt["summary"] == "NEW 400 events"


class TestEvidenceMerge:
    def test_union_dedup_by_index_and_id(self):
        merged = CorrelationEngine._merge_evidence(
            [{"index": "i", "doc_id": "1", "date": "2026-06-01"}],
            [{"index": "i", "doc_id": "1", "date": "2026-06-01"},
             {"index": "i", "doc_id": "2", "date": "2026-06-02"}],
        )
        assert len(merged) == 2

    def test_cap_keeps_oldest_and_newest(self):
        from correlation.engine import EVIDENCE_MAX_ENTRIES
        entries = [{"index": "i", "doc_id": str(i), "date": f"2026-01-{i:02d}"}
                   for i in range(1, 29)]
        merged = CorrelationEngine._merge_evidence(entries, [])
        assert len(merged) <= EVIDENCE_MAX_ENTRIES
        assert merged[0]["doc_id"] == "1"
        assert merged[-1]["doc_id"] == "28"


class TestTriage:
    def test_set_status_updates_doc(self):
        es = MagicMock()
        assert set_status(es, "abc", "acknowledged", note="checking")
        kwargs = es.update.call_args.kwargs
        assert kwargs["id"] == "abc"
        assert kwargs["doc"]["status"] == "acknowledged"
        assert kwargs["doc"]["triage_note"] == "checking"
        assert "triaged_at" in kwargs["doc"]

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            set_status(MagicMock(), "abc", "bogus")

    def test_statuses_cover_lifecycle(self):
        assert set(STATUSES) == {
            "open", "acknowledged", "resolved", "false_positive"}

    def test_list_open_includes_legacy_docs_without_status(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": []}}
        list_correlations(es, status="open")
        query = es.search.call_args.kwargs["query"]
        # The open filter must match docs with status=open OR no status.
        clause = query["bool"]["filter"][0]["bool"]["should"]
        assert any("must_not" in c.get("bool", {}) for c in clause)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
