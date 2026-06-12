"""Tests for the central settings loader (common/settings.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
from common import settings
from common.settings import setting


class TestSetting:
    def test_reads_committed_config(self):
        # Values from the versioned ingestors/config.yaml.
        assert setting("correlation.engine.reactivation_days", 99) == 14
        assert setting("correlation.diplomatic_apt.goldstein_threshold", 0.0) == -5.0

    def test_missing_path_returns_default(self):
        assert setting("correlation.nope.missing", 42) == 42
        assert setting("nope", "fallback") == "fallback"

    def test_int_yaml_for_float_default_is_coerced(self):
        value = setting("correlation.military_buildup.yoy_threshold", 10.0)
        assert isinstance(value, float)

    def test_lists_pass_through(self):
        types = setting("correlation.conflict_cyber.conflict_event_types", [])
        assert "Battles" in types

    def test_missing_file_falls_back_to_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "CONFIG_PATH", tmp_path / "absent.yaml")
        settings.reload()
        try:
            assert setting("correlation.engine.reactivation_days", 99) == 99
        finally:
            settings.reload()

    def test_invalid_yaml_falls_back_to_defaults(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a\n- list\n")
        monkeypatch.setattr(settings, "CONFIG_PATH", bad)
        settings.reload()
        try:
            assert setting("anything.at.all", "default") == "default"
        finally:
            settings.reload()

    def test_weights_sum_to_one(self):
        weights = setting("risk_score.weights", {})
        assert abs(sum(weights.values()) - 1.0) < 1e-9


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
