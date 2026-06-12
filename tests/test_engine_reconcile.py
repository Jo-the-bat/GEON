"""Tests for the correlation engine's situation reconciliation (Phase 0)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest

from correlation.engine import (
    CORRELATIONS_INDEX,
    REACTIVATION_DAYS,
    TIMELINE_MAX_ENTRIES,
    CorrelationEngine,
)


def _engine_with_mocked_es():
    engine = object.__new__(CorrelationEngine)
    engine.es = MagicMock()
    engine.octi = None
    engine.rules = []
    engine.dry_run = False
    return engine


def _candidate(cid="abc123", severity="high", description="d1"):
    return {
        "correlation_id": cid,
        "timestamp": "2026-06-12T10:00:00+00:00",
        "date": "2026-06-12T10:00:00+00:00",
        "rule_name": "diplomatic_escalation_apt",
        "severity": severity,
        "countries_involved": ["RUSSIA", "UKRAINE"],
        "description": description,
        "timeline": [{"date": "2026-06-12", "type": "diplomatic",
                      "description": "event A"}],
    }


def _mget_response(found_docs):
    docs = []
    for cid, src in found_docs.items():
        docs.append({"_id": cid, "found": True, "_source": src})
    return {"docs": docs}


class TestReconcileNew:
    def test_unknown_id_is_new(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        engine.es.mget.return_value = {"docs": [{"_id": "abc123", "found": False}]}

        new, updated, alertable = engine._reconcile([_candidate()])

        assert len(new) == 1 and not updated
        assert new[0]["times_seen"] == 1
        assert new[0]["first_seen"]
        assert new[0]["last_seen"]
        assert alertable[0]["alert_context"] == "new"

    def test_no_index_all_new(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = False

        new, updated, alertable = engine._reconcile([_candidate()])
        assert len(new) == 1 and not updated and len(alertable) == 1

    def test_in_run_duplicates_collapsed(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = False

        new, _, _ = engine._reconcile([_candidate(), _candidate()])
        assert len(new) == 1


class TestReconcileUpdate:
    def test_known_id_updates_not_realerts(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        stored = _candidate()
        stored["times_seen"] = 3
        stored["first_seen"] = "2026-06-01T00:00:00+00:00"
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        new, updated, alertable = engine._reconcile([_candidate()])

        assert not new and len(updated) == 1
        assert updated[0]["times_seen"] == 4
        assert updated[0]["first_seen"] == "2026-06-01T00:00:00+00:00"
        assert not alertable  # same severity, recent: silent refresh

    def test_escalation_realerts(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        stored = _candidate(severity="medium")
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        cand = _candidate(severity="critical", description="worse now")
        new, updated, alertable = engine._reconcile([cand])

        assert not new and len(updated) == 1
        assert updated[0]["severity"] == "critical"
        assert updated[0]["description"] == "worse now"
        assert alertable[0]["alert_context"] == "escalation"

    def test_severity_never_deescalates(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        stored = _candidate(severity="critical", description="old peak")
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        cand = _candidate(severity="medium", description="cooling")
        _, updated, alertable = engine._reconcile([cand])

        assert updated[0]["severity"] == "critical"
        assert updated[0]["description"] == "old peak"
        assert not alertable

    def test_reactivation_realerts(self):
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        dormant = (
            datetime.now(timezone.utc)
            - timedelta(days=REACTIVATION_DAYS + 1)
        ).isoformat()
        stored = _candidate()
        stored["last_seen"] = dormant
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        _, updated, alertable = engine._reconcile([_candidate()])

        assert len(updated) == 1
        assert alertable[0]["alert_context"] == "reactivation"

    def test_mget_failure_fails_closed(self):
        """A transient ES error must NOT wipe stored situation state by
        re-indexing every candidate as new — the run is skipped."""
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        engine.es.mget.side_effect = RuntimeError("es down")

        new, updated, alertable = engine._reconcile([_candidate()])
        assert not new and not updated and not alertable

    def test_merge_refreshes_activity_date(self):
        """`date` must track the latest firing or active situations vanish
        from every 30-day window (risk score, rule 10, dashboards)."""
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        stored = _candidate()
        stored["date"] = "2026-01-01T00:00:00+00:00"
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        _, updated, _ = engine._reconcile([_candidate()])
        assert updated[0]["date"] > "2026-06-01"
        assert updated[0]["timestamp"] == "2026-06-12T10:00:00+00:00"

    def test_escalation_refreshes_rule_specific_fields(self):
        """Rule-specific payload (e.g. rule 10's signals_active) must be
        refreshed on escalation, not just the four common fields."""
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        stored = _candidate(severity="high")
        stored["signals_active"] = 3
        stored["last_seen"] = datetime.now(timezone.utc).isoformat()
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        cand = _candidate(severity="critical")
        cand["signals_active"] = 5
        _, updated, _ = engine._reconcile([cand])
        assert updated[0]["signals_active"] == 5

    def test_reactivation_refreshes_payload_even_if_lower_severity(self):
        """A reactivation alert must describe the fresh firing, not the
        weeks-old stored event (severity still keeps the historic max)."""
        engine = _engine_with_mocked_es()
        engine.es.indices.exists.return_value = True
        dormant = (
            datetime.now(timezone.utc)
            - timedelta(days=REACTIVATION_DAYS + 1)
        ).isoformat()
        stored = _candidate(severity="critical", description="old peak")
        stored["last_seen"] = dormant
        engine.es.mget.return_value = _mget_response({"abc123": stored})

        cand = _candidate(severity="medium", description="fresh firing")
        _, updated, alertable = engine._reconcile([cand])
        assert alertable[0]["alert_context"] == "reactivation"
        assert updated[0]["description"] == "fresh firing"
        assert updated[0]["severity"] == "critical"  # never de-escalates


class TestTimelineMerge:
    def test_dedup_and_sort(self):
        merged = CorrelationEngine._merge_timeline(
            [{"date": "2026-06-02", "type": "cyber", "description": "b"}],
            [{"date": "2026-06-01", "type": "diplomatic", "description": "a"},
             {"date": "2026-06-02", "type": "cyber", "description": "b"}],
        )
        assert len(merged) == 2
        assert merged[0]["date"] == "2026-06-01"

    def test_dedup_ignores_run_relative_dates(self):
        """Rules emit now-derived timeline dates: the same logical entry
        with a drifting date must not accumulate on every run."""
        merged = CorrelationEngine._merge_timeline(
            [{"date": "2026-06-12T10:00:00", "type": "cyber",
              "description": "IoC spike for RUSSIA"}],
            [{"date": "2026-06-12T11:00:00", "type": "cyber",
              "description": "IoC spike for RUSSIA"}],
        )
        assert len(merged) == 1

    def test_cap_keeps_oldest_and_newest(self):
        entries = [
            {"date": f"2026-{m:02d}-{d:02d}", "type": "x",
             "description": f"{m}-{d}"}
            for m in range(1, 4) for d in range(1, 29)
        ]
        merged = CorrelationEngine._merge_timeline(entries, [])
        assert len(merged) <= TIMELINE_MAX_ENTRIES
        # Origin events survive the cap, and so do the most recent ones.
        assert merged[0]["date"] == "2026-01-01"
        assert merged[-1]["date"] == "2026-03-28"


class TestDispatch:
    def test_only_high_and_above(self, monkeypatch):
        engine = _engine_with_mocked_es()
        sent = []
        monkeypatch.setattr(
            "correlation.engine.send_alerts", lambda cs: sent.append(cs)
        )
        engine._dispatch_alerts([
            {**_candidate(cid="a", severity="critical"), "alert_context": "new"},
            {**_candidate(cid="b", severity="medium"), "alert_context": "new"},
            {**_candidate(cid="c", severity="high"), "alert_context": "escalation"},
        ])
        assert len(sent) == 1  # one batch
        assert {c["correlation_id"] for c in sent[0]} == {"a", "c"}

    def test_no_alertable_no_call(self, monkeypatch):
        engine = _engine_with_mocked_es()
        sent = []
        monkeypatch.setattr(
            "correlation.engine.send_alerts", lambda cs: sent.append(cs)
        )
        engine._dispatch_alerts([
            {**_candidate(severity="low"), "alert_context": "new"},
        ])
        assert not sent


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
