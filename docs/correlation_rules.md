# HEGO Correlation Rules

## Overview

The HEGO correlation engine runs hourly and evaluates four rules that detect patterns spanning geopolitical events and cyber threat activity. When a rule matches, an alert is created in the `hego-correlations` Elasticsearch index and notifications are sent via Discord and email.

Each correlation includes:
- A severity level (low, medium, high, critical)
- The triggering events from both domains
- A cross-referenced timeline
- Links to the relevant Kibana dashboard

---

## Rule 1: Diplomatic Escalation + APT Activity

### Purpose

Detect when a diplomatic crisis between two countries coincides with a cyber campaign attributed to one of them.

### Trigger Conditions

1. **Diplomatic event**: GDELT event with Goldstein score < -5 involving a specific country pair (source_country, target_country)
2. **Cyber event**: OpenCTI intrusion set (APT group) attributed to one of the two countries with an active campaign created or modified within a +/- 30 day window around the diplomatic event

### Data Sources

- `hego-gdelt-events-*` (Goldstein score, country pair)
- `hego-cti-*` (intrusion sets, campaigns, country attribution)

### Severity Calculation

| Goldstein Score | APT Confidence | Severity |
|-----------------|----------------|----------|
| < -3 | Any | low |
| < -5 | < 50 | medium |
| < -5 | >= 50 | high |
| < -8 | >= 70 | critical |

### Example

> **Scenario**: GDELT records a Goldstein -8.3 event coded as "Military force deployment" between Russia and Ukraine. Within 12 days, OpenCTI shows APT28 (attributed to Russia) launching a phishing campaign targeting Ukrainian energy infrastructure.
>
> **Alert**: Critical severity. Timeline shows the diplomatic escalation preceded the cyber operation by 12 days, suggesting potential state-directed retaliation or preparation.

---

## Rule 2: Sanction + Cyber Spike

### Purpose

Detect when the imposition of sanctions against a country or entity is followed by an increase in cyber threat indicators associated with that country.

### Trigger Conditions

1. **Sanction event**: New entry in `hego-sanctions` targeting a country or entity
2. **Cyber spike**: More than 200% increase in the count of indicators of compromise (IoC) attributed to that country in `hego-cti-*` within 60 days after the sanction date, compared to the 60-day baseline before the sanction

### Data Sources

- `hego-sanctions` (new sanctions, country, date)
- `hego-cti-*` (indicators, country attribution, creation date)

### Severity Calculation

| IoC Increase | Sanction Source | Severity |
|--------------|-----------------|----------|
| 200-300% | Single source | medium |
| 200-300% | Multiple sources | high |
| > 300% | Any | high |
| > 500% | Any | critical |

### Example

> **Scenario**: The EU imposes new sanctions on Iranian entities linked to drone production. Over the following 45 days, the number of IoCs attributed to Iranian APT groups in OpenCTI increases by 340% compared to the prior 60 days.
>
> **Alert**: High severity. The correlation suggests a possible retaliatory cyber campaign following the sanctions announcement.

---

## Rule 3: Armed Conflict + Cyber Infrastructure

### Purpose

Detect when physical armed conflict in a region coincides with cyber operations from threat actors in the same geographic area.

### Trigger Conditions

1. **Conflict event**: ACLED event of type "Battles" or "Violence against civilians" in a specific country or region
2. **Cyber event**: Active campaign or indicator activity in OpenCTI attributed to a threat actor operating from or targeting the same country/region, within a +/- 14 day window

### Data Sources

- `hego-acled-events-*` (event type, country, location, date)
- `hego-cti-*` (campaigns, intrusion sets, country attribution)

### Severity Calculation

| Conflict Fatalities | Cyber Activity Type | Severity |
|---------------------|---------------------|----------|
| < 10 | Indicators only | low |
| < 10 | Active campaign | medium |
| >= 10 | Indicators only | medium |
| >= 10 | Active campaign | high |
| >= 50 | Active campaign | critical |

### Example

> **Scenario**: ACLED records a series of battles in eastern Libya with 23 fatalities. Within 10 days, OpenCTI logs a campaign attributed to a North African threat actor deploying wiper malware against regional telecommunications infrastructure.
>
> **Alert**: High severity. Geographic and temporal correlation between kinetic operations and cyber attacks targeting supporting infrastructure.

---

## Rule 4: Rhetoric Shift (Weak Signal)

### Purpose

Detect early warning signals by identifying unusual changes in media tone about a country pair, which may precede escalation.

### Trigger Conditions

1. **Tone variation**: The average GDELT tone score for a country pair over the last 7 days deviates by more than 2 standard deviations from the 90-day rolling mean for that same pair
2. No minimum cyber component required -- this is a standalone geopolitical signal

### Data Sources

- `hego-gdelt-events-*` (tone, country pair, date)

### Severity Calculation

| Standard Deviations | Direction | Severity |
|----------------------|-----------|----------|
| > 2 sigma | Negative (deterioration) | low |
| > 3 sigma | Negative | medium |
| > 2 sigma | Positive (sudden improvement) | low |
| > 3 sigma | Positive | low |

Negative shifts receive higher severity because sudden tone deterioration more reliably precedes crises.

### Example

> **Scenario**: GDELT tone for the China-Taiwan pair averages -1.2 over 90 days with a standard deviation of 0.8. Over the past 7 days, the average drops to -4.1 -- a deviation of 3.6 sigma.
>
> **Alert**: Medium severity (weak signal). The rhetoric shift may indicate an approaching diplomatic or military escalation worth monitoring.

---

## Correlation Index Schema

All detected correlations are stored in `hego-correlations`:

```json
{
    "correlation_id": "corr-20250615-001",
    "timestamp": "2025-06-15T14:30:00Z",
    "rule_name": "diplomatic_escalation_apt",
    "severity": "critical",
    "countries_involved": ["RUS", "UKR"],
    "diplomatic_event": {
        "event_id": "gdelt-12345678",
        "description": "Military force deployment",
        "goldstein": -8.3
    },
    "cyber_event": {
        "campaign_id": "opencti-uuid-here",
        "apt_group": "APT28",
        "techniques": ["T1566.001", "T1059.001"]
    },
    "description": "Diplomatic escalation between Russia and Ukraine (Goldstein -8.3) correlated with APT28 phishing campaign targeting energy infrastructure. 12-day gap.",
    "timeline": [
        {"date": "2025-06-03T00:00:00Z", "type": "diplomatic", "description": "Military force deployment detected"},
        {"date": "2025-06-15T00:00:00Z", "type": "cyber", "description": "APT28 campaign detected targeting energy sector"}
    ]
}
```

---

## Alert Format

When a correlation with severity >= high is detected, notifications are sent:

### Discord

```
[HEGO ALERT] Correlation detected
Rule: Diplomatic Escalation + APT Activity
Severity: CRITICAL
Countries: Russia <-> Ukraine
Diplomatic: Goldstein -8.3 -- "Military force deployment"
Cyber: APT28 -- Phishing campaign targeting energy infrastructure
Time gap: 12 days
Dashboard: https://hego.joranbatty.fr/kibana/app/dashboards#/view/correlations
```

### Email

Subject: `[HEGO] CRITICAL: Diplomatic Escalation + APT Activity -- RUS/UKR`

Body includes the same information as the Discord notification plus direct links to the relevant OpenCTI campaign and GDELT event details.
