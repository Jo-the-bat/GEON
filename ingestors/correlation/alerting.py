"""GEON alerting module.

Dispatches correlation alerts to Discord (webhook) and/or email (SMTP).
Alert format follows the GEON notification template specification.

Alerts are sent in batch: one engine run produces a handful of Discord
messages (up to 10 embeds each, the webhook limit) and a single digest
email, regardless of how many correlations fired.

Anti-spam safety net: before sending, each correlation is checked against
``geon-alerts-sent`` in Elasticsearch for a recent alert with the same
(rule_name, countries_involved, severity). If one was sent in the last
7 days the alert is silently skipped. Escalations pass (the severity
changes the key) and reactivations pass (dormancy >= 14 days exceeds the
7-day window). On successful send, a record is indexed so future runs
see it.
"""

from __future__ import annotations

import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests
from common.config import (
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_PASSWORD,
    ALERT_EMAIL_SMTP_HOST,
    ALERT_EMAIL_SMTP_PORT,
    ALERT_EMAIL_TO,
    DISCORD_WEBHOOK_URL,
    RETRY_MAX_ATTEMPTS,
    RETRY_WAIT_MAX,
    RETRY_WAIT_MIN,
)
from common.es_client import get_es_client
from common.settings import setting
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity → colour mapping for Discord embeds
# ---------------------------------------------------------------------------
SEVERITY_COLORS: dict[str, int] = {
    "critical": 0xFF0000,  # Red
    "high": 0xFF6600,      # Orange
    "medium": 0xFFCC00,    # Yellow
    "low": 0x00CC00,       # Green
}

SEVERITY_EMOJI: dict[str, str] = {
    "critical": "\U0001f534",  # Red circle
    "high": "\U0001f7e0",      # Orange circle
    "medium": "\U0001f7e1",    # Yellow circle
    "low": "\U0001f7e2",       # Green circle
}

ALERT_CONTEXT_LABEL: dict[str, str] = {
    "new": "New",
    "escalation": "Escalation",
    "reactivation": "Reactivation",
}

DASHBOARD_BASE_URL = "https://geon.example.com/grafana/d/correlations"

ALERTS_SENT_INDEX = "geon-alerts-sent"
DEDUP_WINDOW_DAYS: int = setting("alerting.dedup_window_days", 7)

# Discord allows at most 10 embeds per webhook message AND at most 6000
# characters across all embeds of one message.
DISCORD_EMBEDS_PER_MESSAGE = 10
DISCORD_CHARS_PER_MESSAGE = 5500  # headroom under the 6000 hard limit
# Small pause between chunked webhook posts to stay clear of rate limits.
DISCORD_CHUNK_PAUSE_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_countries(correlation: dict[str, Any]) -> str:
    """Format the countries involved into a readable string.

    Args:
        correlation: Correlation document dict.

    Returns:
        Formatted country string, e.g. ``"Russia <-> Ukraine"``.
    """
    countries = correlation.get("countries_involved", [])
    if len(countries) >= 2:
        return f"{countries[0]} <-> {countries[1]}"
    elif countries:
        return countries[0]
    return "Unknown"


def _format_plain_alert(correlation: dict[str, Any]) -> str:
    """Format a correlation into a plain-text alert message.

    Args:
        correlation: Correlation document dict.

    Returns:
        Multi-line plain-text alert string.
    """
    severity = correlation.get("severity", "medium").upper()
    emoji = SEVERITY_EMOJI.get(correlation.get("severity", "medium"), "\u26a0\ufe0f")
    rule = correlation.get("rule_name", "Unknown rule")
    countries = _format_countries(correlation)
    description = correlation.get("description", "No description available.")

    # Diplomatic event details.
    diplo = correlation.get("diplomatic_event", {})
    diplo_line = ""
    if diplo:
        diplo_desc = diplo.get("description", "N/A")
        goldstein = diplo.get("goldstein", "N/A")
        diplo_line = f"Diplomatic event: Goldstein {goldstein} -- \"{diplo_desc}\""

    # Cyber event details.
    cyber = correlation.get("cyber_event", {})
    cyber_line = ""
    if cyber:
        apt = cyber.get("apt_group", "N/A")
        campaign = cyber.get("campaign_id", "")
        techniques = ", ".join(cyber.get("techniques", []))
        cyber_line = f"Cyber event: {apt}"
        if campaign:
            cyber_line += f" -- Campaign {campaign}"
        if techniques:
            cyber_line += f" -- Techniques: {techniques}"

    lines = [
        f"{emoji} [GEON ALERT] Correlation detected",
        f"Severity: {severity}",
        f"Rule: {rule}",
        f"Countries: {countries}",
    ]
    if correlation.get("confidence") is not None:
        lines.append(f"Confidence: {correlation['confidence']}/100")
    if diplo_line:
        lines.append(diplo_line)
    if cyber_line:
        lines.append(cyber_line)
    lines.append(f"Description: {description}")
    lines.append(f"Dashboard: {DASHBOARD_BASE_URL}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _build_discord_embed(correlation: dict[str, Any]) -> dict[str, Any]:
    """Build one Discord embed for a correlation.

    Args:
        correlation: Correlation document dict (may carry
            ``alert_context``).

    Returns:
        Discord embed dict.
    """
    severity = correlation.get("severity", "medium")
    rule = correlation.get("rule_name", "Unknown rule")
    countries = _format_countries(correlation)
    description = correlation.get("description", "No description.")[:1500]
    context = ALERT_CONTEXT_LABEL.get(
        correlation.get("alert_context", "new"), "New"
    )

    # Build the embed fields.
    fields: list[dict[str, Any]] = [
        {"name": "Rule", "value": rule, "inline": True},
        {"name": "Severity", "value": severity.upper(), "inline": True},
        {"name": "Countries", "value": countries, "inline": True},
    ]
    if correlation.get("confidence") is not None:
        fields.append({
            "name": "Confidence",
            "value": f"{correlation['confidence']}/100",
            "inline": True,
        })

    diplo = correlation.get("diplomatic_event", {})
    if diplo:
        goldstein = diplo.get("goldstein", "N/A")
        diplo_desc = diplo.get("description", "N/A")
        fields.append({
            "name": "Diplomatic Event",
            "value": f"Goldstein **{goldstein}** -- {diplo_desc}"[:1024],
            "inline": False,
        })

    cyber = correlation.get("cyber_event", {})
    if cyber:
        apt = cyber.get("apt_group", "N/A")
        techniques = ", ".join(cyber.get("techniques", []))
        value = f"**{apt}**"
        if techniques:
            value += f"\nTechniques: {techniques}"
        fields.append({
            "name": "Cyber Event",
            "value": value[:1024],
            "inline": False,
        })

    emoji = SEVERITY_EMOJI.get(severity, "\u26a0\ufe0f")
    return {
        "title": f"{emoji} GEON Correlation \u2014 {context}",
        "description": description,
        "color": SEVERITY_COLORS.get(severity, 0xFFCC00),
        "fields": fields,
        "timestamp": correlation.get("timestamp", ""),
    }


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def _post_discord(payload: dict[str, Any]) -> bool:
    """POST one payload to the Discord webhook (with retry)."""
    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=15,
    )
    if response.ok:
        return True
    logger.error(
        "Discord webhook returned HTTP %d: %s",
        response.status_code,
        response.text[:200],
    )
    return False


def _embed_size(embed: dict[str, Any]) -> int:
    """Character count Discord attributes to an embed (title, description,
    field names/values)."""
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    for field in embed.get("fields", []):
        size += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    return size


def _chunk_embeds(embeds: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split embeds into webhook messages respecting BOTH Discord limits:
    max 10 embeds per message and max ~6000 characters per message."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for embed in embeds:
        size = _embed_size(embed)
        if current and (
            len(current) >= DISCORD_EMBEDS_PER_MESSAGE
            or current_size + size > DISCORD_CHARS_PER_MESSAGE
        ):
            chunks.append(current)
            current, current_size = [], 0
        current.append(embed)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def send_discord_alerts(correlations: list[dict[str, Any]]) -> bool:
    """Send all correlation alerts to Discord in batched messages.

    One engine run produces ceil(n/10) webhook posts (Discord caps a
    message at 10 embeds) instead of one post per correlation.

    Args:
        correlations: Correlation document dicts.

    Returns:
        ``True`` if every chunk was sent successfully.
    """
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL is not configured \u2014 skipping Discord alerts.")
        return False
    if not correlations:
        return True

    embeds = [_build_discord_embed(c) for c in correlations]
    chunks = _chunk_embeds(embeds)

    all_ok = True
    for i, chunk in enumerate(chunks):
        payload: dict[str, Any] = {"embeds": chunk}
        if i == 0:
            payload["content"] = (
                f"**[GEON]** {len(correlations)} correlation alert(s) \u2014 "
                f"dashboard: {DASHBOARD_BASE_URL}"
            )
        if i > 0:
            time.sleep(DISCORD_CHUNK_PAUSE_SECONDS)
        try:
            ok = _post_discord(payload)
        except Exception:
            logger.exception(
                "Failed to post Discord chunk %d/%d.", i + 1, len(chunks)
            )
            ok = False
        all_ok = all_ok and ok

    if all_ok:
        logger.info(
            "Discord alerts sent: %d correlation(s) in %d message(s).",
            len(correlations),
            len(chunks),
        )
    return all_ok


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((smtplib.SMTPException, ConnectionError, TimeoutError)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def send_email_digest(correlations: list[dict[str, Any]]) -> bool:
    """Send ONE digest email covering all correlations of the run.

    Args:
        correlations: Correlation document dicts.

    Returns:
        ``True`` if the email was sent successfully, ``False`` otherwise.
    """
    if not all([ALERT_EMAIL_SMTP_HOST, ALERT_EMAIL_FROM, ALERT_EMAIL_TO]):
        logger.warning("Email SMTP settings are incomplete — skipping email alert.")
        return False
    if not correlations:
        return True

    severities = [c.get("severity", "medium") for c in correlations]
    rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    worst = max(severities, key=lambda s: rank.get(s, 1))

    if len(correlations) == 1:
        c = correlations[0]
        subject = (
            f"[GEON {worst.upper()}] {c.get('rule_name', 'Unknown rule')} "
            f"-- {_format_countries(c)}"
        )
    else:
        n_critical = sum(1 for s in severities if s == "critical")
        n_high = sum(1 for s in severities if s == "high")
        subject = (
            f"[GEON {worst.upper()}] {len(correlations)} correlations "
            f"({n_critical} critical, {n_high} high)"
        )

    separator = "\n\n" + "-" * 60 + "\n\n"
    body_text = separator.join(_format_plain_alert(c) for c in correlations)
    body_html = _build_email_digest_html(correlations)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO

    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(ALERT_EMAIL_SMTP_HOST, ALERT_EMAIL_SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if ALERT_EMAIL_PASSWORD:
                server.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASSWORD)
            server.sendmail(ALERT_EMAIL_FROM, [ALERT_EMAIL_TO], msg.as_string())

        logger.info(
            "Email digest sent to %s (%d correlation(s)).",
            ALERT_EMAIL_TO,
            len(correlations),
        )
        return True

    except smtplib.SMTPException:
        logger.exception("Failed to send email digest.")
        raise


def _build_email_html(correlation: dict[str, Any]) -> str:
    """Build one HTML card for a correlation (embedded in the digest).

    Args:
        correlation: Correlation document dict.

    Returns:
        HTML fragment string.
    """
    severity = correlation.get("severity", "medium")
    color = {
        "critical": "#FF0000",
        "high": "#FF6600",
        "medium": "#FFCC00",
        "low": "#00CC00",
    }.get(severity, "#FFCC00")

    rule = correlation.get("rule_name", "Unknown rule")
    countries = _format_countries(correlation)
    description = correlation.get("description", "No description.")

    diplo = correlation.get("diplomatic_event", {})
    cyber = correlation.get("cyber_event", {})

    diplo_html = ""
    if diplo:
        diplo_html = (
            f'<tr><td style="padding:6px;font-weight:bold;">Diplomatic Event</td>'
            f'<td style="padding:6px;">Goldstein {diplo.get("goldstein", "N/A")} '
            f'&mdash; {diplo.get("description", "N/A")}</td></tr>'
        )

    cyber_html = ""
    if cyber:
        apt = cyber.get("apt_group", "N/A")
        techniques = ", ".join(cyber.get("techniques", []))
        cyber_html = (
            f'<tr><td style="padding:6px;font-weight:bold;">Cyber Event</td>'
            f'<td style="padding:6px;">{apt}'
        )
        if techniques:
            cyber_html += f"<br/>Techniques: {techniques}"
        cyber_html += "</td></tr>"

    context = ALERT_CONTEXT_LABEL.get(
        correlation.get("alert_context", "new"), "New"
    )

    return f"""
  <div style="max-width:600px;margin:0 auto 20px;background:#fff;
              border-radius:8px;overflow:hidden;">
    <div style="background:{color};padding:16px 20px;color:#fff;">
      <h2 style="margin:0;">GEON Correlation — {context}</h2>
      <p style="margin:4px 0 0;">Severity: {severity.upper()}</p>
    </div>
    <div style="padding:20px;">
      <table style="width:100%;border-collapse:collapse;">
        <tr><td style="padding:6px;font-weight:bold;">Rule</td>
            <td style="padding:6px;">{rule}</td></tr>
        <tr><td style="padding:6px;font-weight:bold;">Countries</td>
            <td style="padding:6px;">{countries}</td></tr>
        {diplo_html}
        {cyber_html}
        <tr><td style="padding:6px;font-weight:bold;">Description</td>
            <td style="padding:6px;">{description}</td></tr>
      </table>
    </div>
  </div>"""


def _build_email_digest_html(correlations: list[dict[str, Any]]) -> str:
    """Build the full HTML body for the digest email (one card per
    correlation, single dashboard link at the bottom).

    Args:
        correlations: Correlation document dicts.

    Returns:
        HTML string.
    """
    cards = "\n".join(_build_email_html(c) for c in correlations)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="font-family:Arial,sans-serif;margin:0;padding:20px;background:#f4f4f4;">
  {cards}
  <p style="max-width:600px;margin:0 auto;text-align:center;">
    <a href="{DASHBOARD_BASE_URL}"
       style="background:#0066cc;color:#fff;padding:10px 20px;
              text-decoration:none;border-radius:4px;">
      View in Grafana
    </a>
  </p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Anti-spam deduplication via Elasticsearch
# ---------------------------------------------------------------------------

def _dedup_key(correlation: dict[str, Any]) -> str:
    """Build a deterministic key from (rule_name, sorted countries, severity)."""
    rule = correlation.get("rule_name", "")
    countries = sorted(correlation.get("countries_involved", []))
    severity = correlation.get("severity", "")
    return f"{rule}:{','.join(countries)}:{severity}"


def _was_recently_sent(correlation: dict[str, Any]) -> bool:
    """Check if an equivalent alert was sent in the last DEDUP_WINDOW_DAYS."""
    try:
        es = get_es_client()
        if not es.indices.exists(index=ALERTS_SENT_INDEX):
            return False
        key = _dedup_key(correlation)
        resp = es.count(
            index=ALERTS_SENT_INDEX,
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"dedup_key": key}},
                            {"range": {"sent_at": {"gte": f"now-{DEDUP_WINDOW_DAYS}d"}}},
                        ]
                    }
                }
            },
        )
        return resp.get("count", 0) > 0
    except Exception:
        logger.debug("Dedup check failed — will send alert anyway.", exc_info=True)
        return False


def _record_sent(correlation: dict[str, Any]) -> None:
    """Index a record in geon-alerts-sent after successful dispatch."""
    try:
        es = get_es_client()
        if not es.indices.exists(index=ALERTS_SENT_INDEX):
            es.indices.create(
                index=ALERTS_SENT_INDEX,
                body={
                    "mappings": {
                        "properties": {
                            "dedup_key": {"type": "keyword"},
                            "rule_name": {"type": "keyword"},
                            "countries": {"type": "keyword"},
                            "severity": {"type": "keyword"},
                            "sent_at": {"type": "date"},
                        }
                    }
                },
            )
        es.index(
            index=ALERTS_SENT_INDEX,
            body={
                "dedup_key": _dedup_key(correlation),
                "rule_name": correlation.get("rule_name", ""),
                "countries": sorted(correlation.get("countries_involved", [])),
                "severity": correlation.get("severity", ""),
                "sent_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        logger.debug("Could not record sent alert.", exc_info=True)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def send_alerts(correlations: list[dict[str, Any]]) -> None:
    """Dispatch the run's correlation alerts to all configured channels.

    Correlations already alerted in the last DEDUP_WINDOW_DAYS (same
    rule/countries/severity) are dropped, then ONE batch goes to Discord
    (chunked at 10 embeds/message) and ONE digest email is sent.
    Failures in one channel do not prevent the other channel from being
    attempted.

    Args:
        correlations: Alert-worthy correlation dicts for this run.
    """
    if not correlations:
        return

    fresh = [c for c in correlations if not _was_recently_sent(c)]
    suppressed = len(correlations) - len(fresh)
    if suppressed:
        logger.info(
            "%d alert(s) suppressed by the %d-day anti-spam window.",
            suppressed,
            DEDUP_WINDOW_DAYS,
        )
    if not fresh:
        return

    logger.info("Dispatching alerts for %d correlation(s).", len(fresh))

    # --- Discord ---
    discord_ok = False
    try:
        discord_ok = send_discord_alerts(fresh)
    except Exception:
        logger.exception("Failed to send Discord alerts.")

    # --- Email ---
    email_ok = False
    try:
        email_ok = send_email_digest(fresh)
    except Exception:
        logger.exception("Failed to send email digest.")

    if discord_ok or email_ok:
        for correlation in fresh:
            _record_sent(correlation)
