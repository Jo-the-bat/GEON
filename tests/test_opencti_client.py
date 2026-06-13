"""Tests for the OpenCTI client wrapper's request-timeout clamp.

pycti 6.6 dropped the ``requests_timeout`` constructor kwarg and hardcodes a
300s per-request timeout. The single-threaded scheduler can't afford that
during per-country OpenCTI loops, so ``_clamp_request_timeout`` wraps the
underlying ``requests`` session to enforce a shorter cap on every pycti
version, overriding pycti's explicit per-call ``timeout=300``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

from common.opencti_client import OPENCTI_REQUEST_TIMEOUT, _clamp_request_timeout


def _client_with_recorder() -> tuple[SimpleNamespace, dict]:
    recorded: dict[str, object] = {}

    def make(verb):
        def fn(*args, **kwargs):
            recorded[verb] = kwargs.get("timeout")
            return "ok"
        return fn

    client = SimpleNamespace(session=SimpleNamespace(get=make("get"), post=make("post")))
    return client, recorded


def test_clamp_overrides_pycti_per_call_timeout():
    """An explicit timeout=300 (what pycti passes) is overridden to the cap."""
    client, recorded = _client_with_recorder()
    _clamp_request_timeout(client, 60)
    assert client.session.post("url", json={}, timeout=300) == "ok"
    assert client.session.get("url", stream=True, timeout=300) == "ok"
    assert recorded["post"] == 60
    assert recorded["get"] == 60


def test_clamp_sets_timeout_when_absent():
    client, recorded = _client_with_recorder()
    _clamp_request_timeout(client, 45)
    client.session.post("url")
    assert recorded["post"] == 45


def test_clamp_tolerates_missing_session():
    """Defensive branch: a client without .session must not raise."""
    _clamp_request_timeout(SimpleNamespace(), 60)  # no exception


def test_default_timeout_constant_is_sane():
    assert 0 < OPENCTI_REQUEST_TIMEOUT <= 120
