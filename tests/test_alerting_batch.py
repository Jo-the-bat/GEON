"""Tests for batched alert notifications (Phase 0)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestors"))

import pytest

from correlation import alerting


def _correlation(i, severity="high"):
    return {
        "correlation_id": f"corr-{i}",
        "timestamp": "2026-06-12T10:00:00+00:00",
        "rule_name": "diplomatic_escalation_apt",
        "severity": severity,
        "countries_involved": ["RUSSIA", "UKRAINE"],
        "description": f"Correlation number {i}",
        "diplomatic_event": {"goldstein": -8.0, "description": "event"},
        "cyber_event": {"apt_group": "APT28", "techniques": []},
        "alert_context": "new",
    }


@pytest.fixture
def discord(monkeypatch):
    """Configure a fake webhook and capture posts."""
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json)
        resp = MagicMock()
        resp.ok = True
        return resp

    monkeypatch.setattr(alerting, "DISCORD_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(alerting.requests, "post", fake_post)
    monkeypatch.setattr(alerting.time, "sleep", lambda s: None)
    return posts


class TestDiscordBatching:
    def test_single_message_for_few(self, discord):
        assert alerting.send_discord_alerts([_correlation(i) for i in range(3)])
        assert len(discord) == 1
        assert len(discord[0]["embeds"]) == 3

    def test_chunked_at_ten_embeds(self, discord):
        assert alerting.send_discord_alerts([_correlation(i) for i in range(25)])
        assert len(discord) == 3
        assert [len(p["embeds"]) for p in discord] == [10, 10, 5]

    def test_chunked_by_char_budget(self, discord):
        big = [_correlation(i) for i in range(6)]
        for c in big:
            c["description"] = "x" * 1400  # 6 embeds x ~1500 chars > 5500
        assert alerting.send_discord_alerts(big)
        assert len(discord) > 1
        for post in discord:
            total = sum(alerting._embed_size(e) for e in post["embeds"])
            assert total <= alerting.DISCORD_CHARS_PER_MESSAGE

    def test_summary_content_on_first_chunk(self, discord):
        alerting.send_discord_alerts([_correlation(i) for i in range(12)])
        assert "12 correlation" in discord[0].get("content", "")
        assert "content" not in discord[1]

    def test_no_webhook_configured(self, monkeypatch):
        monkeypatch.setattr(alerting, "DISCORD_WEBHOOK_URL", "")
        assert alerting.send_discord_alerts([_correlation(1)]) is False

    def test_empty_list_is_noop(self, discord):
        assert alerting.send_discord_alerts([]) is True
        assert not discord

    def test_alert_context_in_title(self, discord):
        c = _correlation(1)
        c["alert_context"] = "escalation"
        alerting.send_discord_alerts([c])
        assert "Escalation" in discord[0]["embeds"][0]["title"]


class TestEmailDigest:
    def test_single_email_for_many(self, monkeypatch):
        sent = []

        class FakeSMTP:
            def __init__(self, *a, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def starttls(self): pass
            def login(self, *a): pass
            def sendmail(self, frm, to, msg): sent.append(msg)

        monkeypatch.setattr(alerting, "ALERT_EMAIL_SMTP_HOST", "smtp.test")
        monkeypatch.setattr(alerting, "ALERT_EMAIL_FROM", "geon@test")
        monkeypatch.setattr(alerting, "ALERT_EMAIL_TO", "joran@test")
        monkeypatch.setattr(alerting, "ALERT_EMAIL_PASSWORD", "")
        monkeypatch.setattr(alerting.smtplib, "SMTP", FakeSMTP)

        assert alerting.send_email_digest(
            [_correlation(i, severity=s) for i, s in
             enumerate(["critical", "high", "high"])]
        )
        assert len(sent) == 1
        assert "3 correlations" in sent[0]
        assert "CRITICAL" in sent[0]

    def test_incomplete_settings_skips(self, monkeypatch):
        monkeypatch.setattr(alerting, "ALERT_EMAIL_SMTP_HOST", "")
        assert alerting.send_email_digest([_correlation(1)]) is False


class TestDispatcher:
    def test_send_alerts_calls_both_channels_once(self, monkeypatch):
        discord_calls, email_calls = [], []
        monkeypatch.setattr(
            alerting, "send_discord_alerts", lambda cs: discord_calls.append(cs)
        )
        monkeypatch.setattr(
            alerting, "send_email_digest", lambda cs: email_calls.append(cs)
        )
        alerting.send_alerts([_correlation(i) for i in range(5)])
        assert len(discord_calls) == 1 and len(discord_calls[0]) == 5
        assert len(email_calls) == 1 and len(email_calls[0]) == 5

    def test_empty_is_noop(self, monkeypatch):
        called = []
        monkeypatch.setattr(alerting, "send_discord_alerts", lambda cs: called.append(1))
        alerting.send_alerts([])
        assert not called


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
