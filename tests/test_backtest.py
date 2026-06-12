"""Tests for the backtesting harness (episodes + ground-truth scoring)."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from backtest.runner import (
    Detection,
    collapse_episodes,
    load_ground_truth,
    score_against_ground_truth,
)


def _d(day, key="RUSSIA||UKRAINE", detector="escalation", strength=2.5):
    countries = tuple(key.split("||"))
    return Detection(day=date.fromisoformat(day), detector=detector,
                     key=key, countries=countries, strength=strength)


class TestCollapseEpisodes:
    def test_consecutive_days_one_episode(self):
        eps = collapse_episodes([_d("2026-05-01"), _d("2026-05-02"),
                                 _d("2026-05-03")])
        assert len(eps) == 1
        assert eps[0].first_day == date(2026, 5, 1)
        assert eps[0].last_day == date(2026, 5, 3)
        assert eps[0].days_active == 3

    def test_gap_splits_episodes(self):
        eps = collapse_episodes([_d("2026-05-01"), _d("2026-05-10")])
        assert len(eps) == 2

    def test_small_gap_tolerated(self):
        eps = collapse_episodes([_d("2026-05-01"), _d("2026-05-03")],
                                gap_days=2)
        assert len(eps) == 1

    def test_distinct_subjects_distinct_episodes(self):
        eps = collapse_episodes([
            _d("2026-05-01"),
            _d("2026-05-01", key="CHINA||TAIWAN"),
            _d("2026-05-01", detector="rhetoric"),
        ])
        assert len(eps) == 3

    def test_max_strength_tracked(self):
        eps = collapse_episodes([
            _d("2026-05-01", strength=2.1),
            _d("2026-05-02", strength=4.7),
            _d("2026-05-03", strength=3.0),
        ])
        assert eps[0].max_strength == 4.7


class TestGroundTruthScoring:
    EVENT = {
        "name": "Crise X",
        "date": date(2026, 5, 10),
        "countries": {"RUSSIA", "UKRAINE"},
        "window_before": 7,
        "window_after": 3,
    }

    def test_hit_with_lead(self):
        eps = collapse_episodes([_d("2026-05-07")])
        results = score_against_ground_truth(eps, [self.EVENT])
        assert results[0]["detected"] is True
        assert results[0]["lead_days"] == -3  # detected 3 days BEFORE
        assert eps[0].matched_event == "Crise X"

    def test_miss_outside_window(self):
        eps = collapse_episodes([_d("2026-04-20")])
        results = score_against_ground_truth(eps, [self.EVENT])
        assert results[0]["detected"] is False

    def test_miss_wrong_countries(self):
        eps = collapse_episodes([_d("2026-05-09", key="CHINA||TAIWAN")])
        results = score_against_ground_truth(eps, [self.EVENT])
        assert results[0]["detected"] is False

    def test_earliest_episode_wins(self):
        eps = collapse_episodes([
            _d("2026-05-12"),
            _d("2026-05-04", detector="rhetoric"),
        ])
        results = score_against_ground_truth(eps, [self.EVENT])
        assert results[0]["first_detection"] == "2026-05-04"
        assert results[0]["detector"] == "rhetoric"


class TestGroundTruthFile:
    def test_committed_template_loads(self):
        # The repo template has no events yet but must parse cleanly.
        assert load_ground_truth() == []

    def test_parses_entries(self, tmp_path):
        f = tmp_path / "gt.yaml"
        f.write_text(
            "events:\n"
            "  - name: Test\n"
            "    date: 2026-05-10\n"
            "    countries: [russia, UKRAINE]\n"
        )
        events = load_ground_truth(f)
        assert events[0]["countries"] == {"RUSSIA", "UKRAINE"}
        assert events[0]["window_before"] == 7


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
