"""GEON alerting module.

Dispatches correlation alerts to Discord (webhook) and/or email (SMTP).
Alert format follows the GEON notification template specification.

Anti-spam: before sending, the module checks ``geon-alerts-sent`` in
Elasticsearch for a recent alert with the same (rule_name,
countries_involved, severity).  If one was sent in the last 7 days the
alert is silently skipped.  On successful send, a record is indexed so
future runs see it.
"""

from __future__ import annotations

import logging
import smtplib
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

DASHBOARD_BASE_URL = "https://geon.example.com/grafana/d/correlations"

ALERTS_SENT_INDEX = "geon-alerts-sent"
DEDUP_WINDOW_DAYS = 7


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

@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def send_discord_alert(correlation: dict[str, Any]) -> bool:
    """Send a formatted alert embed to the configured Discord webhook.

    Args:
        correlation: Correlation document dict.

    Returns:
        ``True`` if the message was sent successfully, ``False`` otherwise.
    """
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL is not configured — skipping Discord alert.")
        return False

    severity = correlation.get("severity", "medium")
    rule = correlation.get("rule_name", "Unknown rule")
    countries = _format_countries(correlation)
    description = correlation.get("description", "No description.")

    # Build the embed fields.
    fields: list[dict[str, Any]] = [
        {"name": "Rule", "value": rule, "inline": True},
        {"name": "Severity", "value": severity.upper(), "inline": True},
        {"name": "Countries", "value": countries, "inline": True},
    ]

    diplo = correlation.get("diplomatic_event", {})
    if diplo:
        goldstein = diplo.get("goldstein", "N/A")
        diplo_desc = diplo.get("description", "N/A")
        fields.append({
            "name": "Diplomatic Event",
            "value": f"Goldstein **{goldstein}** -- {diplo_desc}",
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
            "value": value,
            "inline": False,
        })

    fields.append({
        "name": "Dashboard",
        "value": f"[Open in Grafana]({DASHBOARD_BASE_URL})",
        "inline": False,
    })

    emoji = SEVERITY_EMOJI.get(severity, "\u26a0\ufe0f")
    embed = {
        "title": f"{emoji} GEON Correlation Detected",
        "description": description,
        "color": SEVERITY_COLORS.get(severity, 0xFFCC00),
        "fields": fields,
        "timestamp": correlation.get("timestamp", ""),
    }

    payload = {"embeds": [embed]}

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=15,
    )

    if response.ok:
        logger.info("Discord alert sent for correlation %s.", correlation.get("correlation_id"))
        return True
    else:
        logger.error(
            "Discord webhook returned HTTP %d: %s",
            response.status_code,
            response.text[:200],
        )
        return False


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((smtplib.SMTPException, ConnectionError, TimeoutError)),
    stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
    wait=wait_exponential(min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
    reraise=True,
)
def send_email_alert(correlation: dict[str, Any]) -> bool:
    """Send a correlation alert via email using SMTP.

    Args:
        correlation: Correlation document dict.

    Returns:
        ``True`` if the email was sent successfully, ``False`` otherwise.
    """
    if not all([ALERT_EMAIL_SMTP_HOST, ALERT_EMAIL_FROM, ALERT_EMAIL_TO]):
        logger.warning("Email SMTP settings are incomplete — skipping email alert.")
        return False

    severity = correlation.get("severity", "medium").upper()
    rule = correlation.get("rule_name", "Unknown rule")
    countries = _format_countries(correlation)

    subject = f"[GEON {severity}] {rule} -- {countries}"
    body_text = _format_plain_alert(correlation)

    # Build HTML body.
    body_html = _build_email_html(correlation)

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

        logger.info("Email alert sent to %s for correlation %s.",
                     ALERT_EMAIL_TO, correlation.get("correlation_id"))
        return True

    except smtplib.SMTPException:
        logger.exception("Failed to send email alert.")
        raise


def _build_email_html(correlation: dict[str, Any]) -> str:
    """Build an HTML email body for a correlation alert.

    Args:
        correlation: Correlation document dict.

    Returns:
        HTML string.
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

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="font-family:Arial,sans-serif;margin:0;padding:20px;background:#f4f4f4;">
  <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;">
    <div style="background:{color};padding:16px 20px;color:#fff;">
      <h2 style="margin:0;">GEON Correlation Alert</h2>
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
      <p style="margin-top:16px;">
        <a href="{DASHBOARD_BASE_URL}"
           style="background:#0066cc;color:#fff;padding:10px 20px;
                  text-decoration:none;border-radius:4px;">
          View in Grafana
        </a>
      </p>
    </div>
  </div>
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
        logger.debug("Failed to record sent alert — dedup may re-fire.", exc_info=True)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def send_alerts(correlation: dict[str, Any]) -> None:
    """Dispatch a correlation alert to all configured channels.

    Sends to Discord and email.  Failures in one channel do not prevent
    the other channel from being attempted.

    Anti-spam: skips dispatch if an equivalent alert (same rule, countries,
    severity) was sent in the last 7 days.

    Args:
        correlation: Correlation document dict.
    """
    cid = correlation.get("correlation_id", "unknown")

    if _was_recently_sent(correlation):
        logger.info(
            "Skipping alert for %s — equivalent alert sent within %d days.",
            cid, DEDUP_WINDOW_DAYS,
        )
        return

    logger.info("Dispatching alerts for correlation %s (severity=%s).",
                cid, correlation.get("severity", "?"))

    sent = False

    # --- Discord ---
    try:
        if send_discord_alert(correlation):
            sent = True
    except Exception:
        logger.exception("Failed to send Discord alert for %s.", cid)

    # --- Email ---
    try:
        if send_email_alert(correlation):
            sent = True
    except Exception:
        logger.exception("Failed to send email alert for %s.", cid)

    if sent:
        _record_sent(correlation)
