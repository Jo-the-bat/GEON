# Changelog

All notable changes to GEON are documented in this file.

## [0.1.0] - 2026-04-17

First stable release. The full ingestion-correlation-alerting pipeline is
operational.

### Data Sources (10 ingestors)
- GDELT Events (15-min CSV export)
- GDELT GKG (Global Knowledge Graph)
- ACLED (Armed Conflict Location & Event Data)
- Sanctions (OFAC SDN, EU Consolidated, UN Security Council)
- OpenCTI export (intrusion sets, campaigns, indicators)
- Polymarket (geopolitical prediction markets)
- Cloudflare Radar (internet outages)
- Metaculus + Manifold Markets (prediction consensus)
- SIPRI (arms transfers and military spending)
- RSS feeds via n8n (think tanks, agencies, defense, CERT)

### Correlation Engine (10 rules)
1. Diplomatic escalation + APT activity
2. Sanction + cyber spike
3. Armed conflict + cyber infrastructure
4. Rhetoric shift (weak signal)
5. Internet outage + escalation
6. Military spending increase + APT activity
7. Arms transfer escalation
8. Prediction market validation
9. Internet outage + APT
10. Multi-signal convergence fusion

### Dashboards (6 Grafana dashboards)
- Global Overview (Geomap, timeline, top countries)
- Country Profile (risk score, APT groups, sanctions, spending trend)
- Correlations (severity filters, dual timeline)
- Article Feed (RSS keyword trends)
- Monitoring (Prometheus service health, ingestion timestamps)
- Prediction Markets (Polymarket cases, consensus scores)

### Alerting
- Discord webhook alerts with severity-colored embeds
- Email alerts (SMTP) with HTML formatting
- Anti-spam dedup (7-day window per rule/country/severity)

### Infrastructure
- Docker rootless with Nginx reverse proxy + TLS (Let's Encrypt)
- Authelia MFA (TOTP) on all web services
- Elasticsearch RBAC (dedicated reader/writer roles)
- ILM policy for monthly index rotation (hot 30d / warm 30d / delete 90d)
- Prometheus monitoring
- Automated ES snapshot backups
