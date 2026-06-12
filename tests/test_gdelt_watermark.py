"""Tests for the GDELT watermark/backfill logic (Phase 1).

Before this, the ingestor only fetched "the latest" 15-minute window:
any downtime left silent, unrecoverable gaps in the event timeline.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from gdelt.ingestor import MAX_BACKFILL_WINDOWS, GDELTIngestor


def _ingestor():
    ing = object.__new__(GDELTIngestor)
    ing.es = MagicMock()
    ing.logger = MagicMock()
    return ing


W = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)


class TestMissedWindows:
    def test_no_gap(self):
        assert GDELTIngestor._missed_windows(W, W) == []

    def test_single_window(self):
        assert GDELTIngestor._missed_windows(W, W + timedelta(minutes=15)) == [
            W + timedelta(minutes=15)
        ]

    def test_one_hour_gap(self):
        missed = GDELTIngestor._missed_windows(W, W + timedelta(hours=1))
        assert len(missed) == 4
        assert missed[0] == W + timedelta(minutes=15)
        assert missed[-1] == W + timedelta(hours=1)

    def test_cap_keeps_most_recent(self):
        latest = W + timedelta(minutes=15 * (MAX_BACKFILL_WINDOWS + 50))
        missed = GDELTIngestor._missed_windows(W, latest)
        assert len(missed) == MAX_BACKFILL_WINDOWS
        assert missed[-1] == latest  # newest windows win


class TestIncrementalIngest:
    def test_first_run_anchors_at_latest(self, monkeypatch):
        ing = _ingestor()
        written = []
        monkeypatch.setattr(ing, "latest_window", lambda: (W, "http://x/csv.zip"))
        monkeypatch.setattr(ing, "_read_watermark", lambda: None)
        monkeypatch.setattr(ing, "_write_watermark", lambda dt: written.append(dt))
        monkeypatch.setattr(ing, "_download_csv_zip", lambda url: "csv")
        monkeypatch.setattr("gdelt.ingestor.parse_events_csv", lambda c: [{"e": 1}])
        monkeypatch.setattr(ing, "_process_and_index", lambda raw: 7)

        assert ing._ingest_incremental() == 7
        assert written == [W]

    def test_backfills_all_missed_windows(self, monkeypatch):
        ing = _ingestor()
        written, fetched = [], []
        latest = W + timedelta(minutes=45)
        monkeypatch.setattr(ing, "latest_window", lambda: (latest, "u"))
        monkeypatch.setattr(ing, "_read_watermark", lambda: W)
        monkeypatch.setattr(ing, "_write_watermark", lambda dt: written.append(dt))
        monkeypatch.setattr(
            ing, "fetch_csv_for_timestamp",
            lambda dt: fetched.append(dt) or [{"e": 1}],
        )
        monkeypatch.setattr(ing, "_process_and_index", lambda raw: 5)
        monkeypatch.setattr("gdelt.ingestor.time.sleep", lambda s: None)

        assert ing._ingest_incremental() == 15  # 3 windows x 5 docs
        assert len(fetched) == 3
        assert written[-1] == latest

    def test_404_window_advances_watermark(self, monkeypatch):
        """A 4xx window (GDELT skipped it) must not block the backfill."""
        ing = _ingestor()
        written = []
        latest = W + timedelta(minutes=30)
        monkeypatch.setattr(ing, "latest_window", lambda: (latest, "u"))
        monkeypatch.setattr(ing, "_read_watermark", lambda: W)
        monkeypatch.setattr(ing, "_write_watermark", lambda dt: written.append(dt))

        def fetch(dt):
            if dt == W + timedelta(minutes=15):
                raise ValueError("GDELT returned 404")
            return [{"e": 1}]

        monkeypatch.setattr(ing, "fetch_csv_for_timestamp", fetch)
        monkeypatch.setattr(ing, "_process_and_index", lambda raw: 5)
        monkeypatch.setattr("gdelt.ingestor.time.sleep", lambda s: None)

        assert ing._ingest_incremental() == 5
        assert written == [W + timedelta(minutes=15), latest]

    def test_transient_failure_keeps_watermark_for_retry(self, monkeypatch):
        """A network failure must stop the run BEFORE advancing the
        watermark, so the next run retries the same window."""
        ing = _ingestor()
        written = []
        latest = W + timedelta(minutes=30)
        monkeypatch.setattr(ing, "latest_window", lambda: (latest, "u"))
        monkeypatch.setattr(ing, "_read_watermark", lambda: W)
        monkeypatch.setattr(ing, "_write_watermark", lambda dt: written.append(dt))

        def fetch(dt):
            if dt == W + timedelta(minutes=30):
                raise RuntimeError("connection reset")
            return [{"e": 1}]

        monkeypatch.setattr(ing, "fetch_csv_for_timestamp", fetch)
        monkeypatch.setattr(ing, "_process_and_index", lambda raw: 5)
        monkeypatch.setattr("gdelt.ingestor.time.sleep", lambda s: None)

        assert ing._ingest_incremental() == 5
        # Watermark advanced past the good window only.
        assert written == [W + timedelta(minutes=15)]

    def test_up_to_date_is_noop(self, monkeypatch):
        ing = _ingestor()
        monkeypatch.setattr(ing, "latest_window", lambda: (W, "u"))
        monkeypatch.setattr(ing, "_read_watermark", lambda: W)
        called = []
        monkeypatch.setattr(ing, "fetch_csv_for_timestamp",
                            lambda dt: called.append(dt))
        assert ing._ingest_incremental() == 0
        assert not called


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
