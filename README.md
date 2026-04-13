# HEGO

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-rootless-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/engine/security/rootless/)

**Geopolitical & Cyber Threat Intelligence Platform**

HEGO automatically correlates diplomatic events, armed conflicts, and sanctions with cyber threat activity from APT groups and malware campaigns. It bridges the gap between structured CTI (OpenCTI/STIX2) and geopolitical event data (GDELT/ACLED) that no existing open-source tool connects.

The name is an acronym of the core stack -- **H**uginn, **E**lasticsearch, **G**DELT, **O**penCTI -- and echoes the Greek word for hegemony.

---

![HEGO Dashboard](docs/screenshots/dashboard.png)

---

## Why HEGO?

Existing tools address one side of the picture but never both:

| Tool | Geopolitical events | Cyber threat intelligence | Correlation |
|------|:---:|:---:|:---:|
| [World Monitor](https://worldmonitor.app) | Yes | No | No |
| [PizzINT GDELT](https://www.pizzint.watch/gdelt) | Yes | No | No |
| OpenCTI + Elastic connector | No | Yes | No |
| **HEGO** | **Yes** | **Yes** | **Yes** |

HEGO is the first platform that detects patterns like "diplomatic escalation between two countries followed by an APT campaign attributed to one of them" -- automatically.

---

## Architecture

```
Internet --> Nginx (TLS) --> Kibana / OpenCTI / Huginn
                  |
      Elasticsearch  <--  Ingestors (GDELT, ACLED, Sanctions)
                  |
           OpenCTI (STIX2)  <--  Connectors (MITRE, AlienVault, CISA)
                  |
          Correlation Engine  -->  Alerts (Discord, Email)
```

All services run behind Nginx with TLS termination and Authelia MFA. No internal port is exposed to the public interface. See [docs/architecture.md](docs/architecture.md) for the full diagram and component descriptions.

---

## Quick Start

### Prerequisites

- Docker Engine (rootless mode) with Docker Compose v2
- Git
- 8 GB+ RAM (16 GB recommended)
- A domain name with DNS pointing to your server (for TLS)

### Installation

```bash
# Clone the repository
git clone https://github.com/Jo-the-bat/HEGO.git
cd HEGO

# Copy the environment template and fill in your secrets
cp .env.example .env
nano .env  # Set passwords, API keys, domain

# Run the setup script (checks prerequisites, prepares directories)
chmod +x scripts/setup.sh
./scripts/setup.sh

# Start the stack
docker compose -f docker/docker-compose.yml up -d

# Verify services are healthy
docker compose -f docker/docker-compose.yml ps
```

See [docs/installation.md](docs/installation.md) for the complete step-by-step guide.

---

## Data Sources

| Source | Type | Frequency | Description |
|--------|------|-----------|-------------|
| **GDELT** | Geopolitical events | Every 15 min | Global diplomatic/military events, CAMEO-coded and geolocated |
| **ACLED** | Armed conflicts | Daily | Battles, protests, violence against civilians |
| **OFAC / EU / UN** | Sanctions | Weekly | Sanctioned persons, organizations, countries |
| **RSS via Huginn** | Articles | Continuous | Think tanks, agencies, defense publications |
| **OpenCTI Connectors** | Cyber threats | Continuous | MITRE ATT&CK, AlienVault OTX, CISA KEV, CVE |

See [docs/data_sources.md](docs/data_sources.md) for API details and configuration.

---

## Correlation Rules

The correlation engine runs hourly and applies four detection rules:

1. **Diplomatic Escalation + APT Activity** -- Goldstein score < -5 between two countries combined with an APT campaign attributed to one of them within 30 days.
2. **Sanction + Cyber Spike** -- New sanction against a country followed by a >200% increase in related indicators of compromise within 60 days.
3. **Armed Conflict + Cyber Infrastructure** -- ACLED battle or civilian violence event coinciding with cyber operations from the same geographic area.
4. **Rhetoric Shift** -- Tone variation exceeding 2 standard deviations over 7 days for a country pair, flagged as a weak signal.

See [docs/correlation_rules.md](docs/correlation_rules.md) for trigger conditions, severity calculation, and examples.

---

## Dashboards

| Dashboard | Description |
|-----------|-------------|
| **Global Overview** | World map with GDELT events, ACLED conflicts, and APT campaigns. 30-day timeline. |
| **Country Profile** | Per-country timeline, attributed APT groups, active sanctions, risk score. |
| **Correlations** | Detected cross-domain patterns with severity filters and dual timelines. |
| **Article Feed** | Ingested articles from RSS sources with keyword trends. |
| **Monitoring** | Service health, ingestion timestamps, index volumes. |

---

## Roadmap

- [x] Phase 1 -- Infrastructure: Nginx, Elasticsearch, Kibana, Authelia, TLS
- [x] Phase 2 -- OpenCTI: platform, connectors, STIX2 graph
- [ ] Phase 3 -- GDELT ingestion and first dashboards
- [ ] Phase 4 -- ACLED and sanctions ingestion
- [ ] Phase 5 -- Huginn RSS pipeline
- [ ] Phase 6 -- Correlation engine and alerting
- [ ] Phase 7 -- Monitoring, backups, crontab
- [ ] Phase 8 -- Documentation and use cases

---

## Security

- **Rootless Docker** -- the entire stack runs without root privileges
- **Authelia MFA** -- TOTP-based multi-factor authentication on all web services
- **No exposed ports** -- only Nginx (80/443) is reachable from the network
- **TLS everywhere** -- Let's Encrypt certificates via Certbot with automatic renewal
- **Secrets management** -- all credentials in `.env`, excluded from version control
- **Healthchecks** -- Docker healthchecks on every critical service

---

## Contributing

Contributions are welcome. Please open an issue first to discuss the change you would like to make.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit using conventional commits (`feat:`, `fix:`, `docs:`, `infra:`)
4. Push and open a pull request

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

## Credits

Built by [Joran Batty](https://github.com/Jo-the-bat) as a portfolio project for a Master's candidacy in International Relations, demonstrating the convergence of technical cybersecurity skills and geopolitical analysis.

### Upstream Projects

- [Elasticsearch & Kibana](https://www.elastic.co/) -- Search and visualization
- [OpenCTI](https://github.com/OpenCTI-Platform/opencti) -- Cyber threat intelligence platform
- [GDELT Project](https://www.gdeltproject.org/) -- Global event database
- [ACLED](https://acleddata.com/) -- Armed conflict data
- [Huginn](https://github.com/huginn/huginn) -- Agent automation
- [Authelia](https://www.authelia.com/) -- Authentication and MFA
- [Nginx](https://nginx.org/) -- Reverse proxy
- [Prometheus](https://prometheus.io/) & [Grafana](https://grafana.com/) -- Monitoring
