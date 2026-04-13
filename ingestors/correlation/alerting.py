"""HEGO alerting module.

Dispatches correlation alerts to Discord (webhook) and/or email (SMTP).
Alert format follows the HEGO notification template specification.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

DASHBOARD_BASE_URL = "https://hego.joranbatty.fr/kibana/app/dashboards#/view/correlations"


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
        f"{emoji} [HEGO ALERT] Correlation detected",
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
        "value": f"[Open in Kibana]({DASHBOARD_BASE_URL})",
        "inline": False,
    })

    emoji = SEVERITY_EMOJI.get(severity, "\u26a0\ufe0f")
    embed = {
        "title": f"{emoji} HEGO Correlation Detected",
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

    subject = f"[HEGO {severity}] {rule} -- {countries}"
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
      <h2 style="margin:0;">HEGO Correlation Alert</h2>
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
           style="background:#0066cc;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px;">
          View in Kibana
        </a>
      </p>
    </div>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def send_alerts(correlation: dict[str, Any]) -> None:
    """Dispatch a correlation alert to all configured channels.

    Sends to Discord and email.  Failures in one channel do not prevent
    the other channel from being attempted.

    Args:
        correlation: Correlation document dict.
    """
    cid = correlation.get("correlation_id", "unknown")
    logger.info("Dispatching alerts for correlation %s (severity=%s).",
                cid, correlation.get("severity", "?"))

    # --- Discord ---
    try:
        send_discord_alert(correlation)
    except Exception:
        logger.exception("Failed to send Discord alert for %s.", cid)

    # --- Email ---
    try:
        send_email_alert(correlation)
    except Exception:
        logger.exception("Failed to send email alert for %s.", cid)
