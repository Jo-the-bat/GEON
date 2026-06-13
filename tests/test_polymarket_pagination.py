"""Tests for Polymarket pagination resilience.

The Gamma API rejects pagination past an offset cap with HTTP 422 once its
active-market catalogue exceeds ~10k. A naive ``while True`` loop calling
``raise_for_status()`` therefore crashed the whole job and indexed nothing —
Polymarket silently went stale for weeks. ``fetch_all_markets`` must treat
that 422 as a clean end-of-results, keep paging on real pages, still bubble up
genuine HTTP errors, and never loop unbounded.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest
import requests
from polymarket import ingestor as poly_mod
from polymarket.ingestor import PolymarketIngestor


def _ingestor() -> PolymarketIngestor:
    ing = object.__new__(PolymarketIngestor)
    ing.es = MagicMock()
    ing.logger = MagicMock()
    return ing


def _http_error(status: int) -> requests.HTTPError:
    resp = MagicMock()
    resp.status_code = status
    return requests.HTTPError(response=resp)


def _page(n: int) -> list[dict]:
    """A full page of ``n`` placeholder markets."""
    return [{"id": i} for i in range(n)]


class TestFetchAllMarkets:
    def test_offset_cap_422_is_clean_end(self, monkeypatch):
        """Two full pages then a 422 → 200 markets, no exception raised."""
        ing = _ingestor()
        calls = {"n": 0}

        def fake_fetch(limit, offset):
            calls["n"] += 1
            if offset >= 200:
                raise _http_error(422)
            return _page(limit)

        monkeypatch.setattr(ing, "_fetch_markets", fake_fetch)
        markets = ing.fetch_all_markets()
        assert len(markets) == 200
        assert calls["n"] == 3  # offsets 0, 100, 200(→422)

    def test_partial_page_ends_naturally(self, monkeypatch):
        """A short page signals the end before any 422."""
        ing = _ingestor()

        def fake_fetch(limit, offset):
            return _page(limit) if offset == 0 else _page(42)

        monkeypatch.setattr(ing, "_fetch_markets", fake_fetch)
        markets = ing.fetch_all_markets()
        assert len(markets) == 142

    def test_empty_first_page(self, monkeypatch):
        ing = _ingestor()
        monkeypatch.setattr(ing, "_fetch_markets", lambda limit, offset: [])
        assert ing.fetch_all_markets() == []

    def test_non_422_http_error_propagates(self, monkeypatch):
        """A real outage (500) must NOT be swallowed as end-of-results."""
        ing = _ingestor()

        def fake_fetch(limit, offset):
            if offset >= 100:
                raise _http_error(500)
            return _page(limit)

        monkeypatch.setattr(ing, "_fetch_markets", fake_fetch)
        with pytest.raises(requests.HTTPError):
            ing.fetch_all_markets()

    def test_page_cap_bounds_the_loop(self, monkeypatch):
        """Endless full pages stop at MAX_PAGES and warn (no silent truncation)."""
        ing = _ingestor()
        monkeypatch.setattr(poly_mod, "MAX_PAGES", 5)
        monkeypatch.setattr(ing, "_fetch_markets", lambda limit, offset: _page(limit))
        markets = ing.fetch_all_markets()
        assert len(markets) == 500  # exactly 5 pages of 100
        assert ing.logger.warning.called
