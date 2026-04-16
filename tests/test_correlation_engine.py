"""Tests for the GEON correlation engine and individual rules.

Pure logic only — we do not spin up a real Elasticsearch or OpenCTI.
Dependencies are injected (ES via mock) or accessed via static methods.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from correlation.rules.diplomatic_apt import DiplomaticAPTRule
from correlation.rules.sanction_cyber import MIN_BASELINE_COUNT, SanctionCyberRule


# ---------------------------------------------------------------------------
# DiplomaticAPTRule._compute_severity — severity matrix
# ---------------------------------------------------------------------------


class TestDiplomaticAptSeverity:
    """Severity derives from (goldstein, max APT confidence)."""

    @pytest.mark.parametrize(
        ("goldstein", "confidence", "expected"),
        [
            # base=3 (critical), confidence boost clamped to 3 → critical
            (-9.0, 80, "critical"),
            # base=2 (high), boosted → critical
            (-7.0, 80, "critical"),
            # base=2 (high), no boost → high
            (-7.0, 50, "high"),
            # base=1 (medium), boosted → high
            (-5.0, 80, "high"),
            # base=1 (medium), no boost → medium
            (-5.0, 50, "medium"),
            # Above the threshold for any base bump
            (-3.0, 0, "medium"),
        ],
    )
    def test_severity_matrix(
        self, goldstein: float, confidence: int, expected: str
    ) -> None:
        apts = [{"confidence": confidence}]
        assert DiplomaticAPTRule._compute_severity(goldstein, apts) == expected

    def test_severity_with_no_apts(self) -> None:
        """Empty APT list should still produce a severity based on Goldstein alone."""
        assert DiplomaticAPTRule._compute_severity(-9.0, []) == "critical"
        assert DiplomaticAPTRule._compute_severity(-2.0, []) == "medium"


# ---------------------------------------------------------------------------
# SanctionCyberRule — baseline + severity
# ---------------------------------------------------------------------------


class _StubES:
    """Minimal ES double for rule constructors."""

    def search(self, **kwargs):  # pragma: no cover - not exercised here
        return {"hits": {"hits": []}}

    def count(self, **kwargs):  # pragma: no cover - overridden per-test
        return {"count": 0}

    class _Indices:
        def exists(self, **kwargs):
            return False

    indices = _Indices()

    def mget(self, **kwargs):  # pragma: no cover - overridden per-test
        return {"docs": []}


class TestSanctionCyberBaseline:
    """Rule 2 must refuse to compute a spike on an insufficient baseline."""

    def _rule(self) -> SanctionCyberRule:
        return SanctionCyberRule(es=_StubES(), octi=None)  # type: ignore[arg-type]

    def test_insufficient_baseline_returns_none(self) -> None:
        """baseline < MIN_BASELINE_COUNT should skip (return None).

        _compute_ioc_spike calls _count_iocs_in_window twice: once for the
        post-sanction window, once for the baseline. We stub both with
        side_effect.
        """
        rule = self._rule()
        rule._count_iocs_in_window = MagicMock(side_effect=[100, 3])  # type: ignore[method-assign]
        assert rule._compute_ioc_spike("Iran") is None

    def test_sufficient_baseline_returns_ratio(self) -> None:
        rule = self._rule()
        # Post=40, baseline=10 → ratio 4.0
        rule._count_iocs_in_window = MagicMock(side_effect=[40, 10])  # type: ignore[method-assign]
        assert rule._compute_ioc_spike("Iran") == pytest.approx(4.0)

    def test_zero_post_with_solid_baseline_is_zero(self) -> None:
        """A solid baseline with zero post is now 0.0 (not an auto-spike)."""
        rule = self._rule()
        rule._count_iocs_in_window = MagicMock(side_effect=[0, 20])  # type: ignore[method-assign]
        assert rule._compute_ioc_spike("Iran") == pytest.approx(0.0)

    def test_min_baseline_constant_is_exposed(self) -> None:
        """Sanity check: regression guard if someone accidentally removes the
        baseline gate."""
        assert MIN_BASELINE_COUNT >= 10


class TestSanctionCyberSeverity:
    """_build_correlation severity tiers depend on the spike ratio."""

    def _rule(self) -> SanctionCyberRule:
        return SanctionCyberRule(es=_StubES(), octi=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [
            (5.5, "critical"),
            (5.0, "critical"),
            (3.5, "high"),
            (3.0, "high"),
            (2.5, "medium"),
            (2.0, "medium"),
            (1.5, "low"),
        ],
    )
    def test_severity_matrix(self, ratio: float, expected: str) -> None:
        rule = self._rule()
        doc = rule._build_correlation(
            country="Iran",
            sanction_docs=[{"name": "x", "sanctions_source": "OFAC", "programs": []}],
            spike_ratio=ratio,
        )
        assert doc["severity"] == expected


# ---------------------------------------------------------------------------
# CorrelationEngine._deduplicate
# ---------------------------------------------------------------------------


class TestCorrelationDedup:
    """The deduplicator drops correlations whose ID already exists in ES."""

    def _engine(self) -> "CorrelationEngine":
        # Import here so conftest has already patched sys.path / env vars.
        from correlation.engine import CorrelationEngine

        # Bypass __init__: we only need ``es`` and the real _deduplicate method.
        engine = CorrelationEngine.__new__(CorrelationEngine)
        engine.es = MagicMock()
        return engine

    def test_removes_existing_ids(self) -> None:
        engine = self._engine()
        engine.es.indices.exists.return_value = True
        # ``b`` is already indexed, ``a`` and ``c`` are new.
        engine.es.mget.return_value = {
            "docs": [
                {"_id": "a", "found": False},
                {"_id": "b", "found": True},
                {"_id": "c", "found": False},
            ]
        }
        candidates = [
            {"correlation_id": "a", "rule_name": "r"},
            {"correlation_id": "b", "rule_name": "r"},
            {"correlation_id": "c", "rule_name": "r"},
        ]
        new = engine._deduplicate(candidates)
        assert [c["correlation_id"] for c in new] == ["a", "c"]

    def test_no_existing_index_returns_all(self) -> None:
        engine = self._engine()
        engine.es.indices.exists.return_value = False
        candidates = [{"correlation_id": "x"}]
        assert engine._deduplicate(candidates) == candidates

    def test_empty_input_returns_empty(self) -> None:
        assert self._engine()._deduplicate([]) == []
