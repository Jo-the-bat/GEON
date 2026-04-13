# GEON Architecture

## Overview

GEON is a multi-container Docker application that ingests, indexes, correlates, and visualizes geopolitical events alongside cyber threat intelligence. All services run in rootless Docker behind a single Nginx reverse proxy with TLS termination and Authelia MFA.

Elasticsearch serves as the shared data layer, accessed by Grafana for dashboards and visualization, by OpenCTI as its STIX2 knowledge graph backend, and by the Python ingestors for writing geopolitical and CTI data. n8n provides workflow automation for RSS ingestion, entity enrichment, and alert delivery.

## Network Topology

```
Internet
   |
   v
+----------------+
|     Nginx      |  :443 (TLS via Certbot / Let's Encrypt)
|  reverse proxy |  :80  (redirect to 443)
+-------+--------+
        | Docker internal network (geon_net)
        |
        +---> /                --> Landing page (static files)
        +---> /opencti         --> opencti:8080
        +---> /grafana         --> grafana:3000
        +---> /n8n             --> n8n:5678
        +---> /auth            --> authelia:9091
        |
+-------+--------------------------------------------------+
|               Docker network: geon_net                    |
|                                                           |
|  +---------------+  +----------------+  +-------------+  |
|  | Elasticsearch |  |    OpenCTI     |  |     n8n     |  |
|  |   (port 9200) |  |   (port 8080) |  | (port 5678) |  |
|  +-------+-------+  +-------+--------+  +------+------+  |
|          |                   |                  |         |
|  +-------+-------+  +-------+--------+  +------+------+  |
|  |    Grafana    |  |   RabbitMQ    |  |    Redis    |  |
|  |   (port 3000) |  |  (port 5672)  |  | (port 6379) |  |
|  +---------------+  +----------------+  +-------------+  |
|                                                           |
|  +---------------+  +----------------+                    |
|  |     MinIO     |  |   Authelia    |                    |
|  |  (port 9000)  |  |  (port 9091)  |                    |
|  +---------------+  +----------------+                    |
|                                                           |
|  +---------------------------------------------------+   |
|  |         Python Ingestors (cron-driven)             |   |
|  |  GDELT | ACLED | Sanctions | OpenCTI Export        |   |
|  |  Correlation Engine                                |   |
|  +---------------------------------------------------+   |
|                                                           |
|  +---------------+                                        |
|  |  Prometheus   |                                        |
|  |  (port 9090)  |                                        |
|  +---------------+                                        |
+-----------------------------------------------------------+
```

## Components

### Nginx (Reverse Proxy)

- **Image**: `nginx:alpine`
- **Role**: TLS termination, reverse proxy routing, static file serving
- **Ports**: 80 (redirect), 443 (HTTPS) -- the only ports exposed to the public
- **Configuration**: `docker/nginx/conf.d/geon.conf`
- **TLS**: Let's Encrypt certificates managed by a Certbot sidecar container

### Elasticsearch

- **Image**: `docker.elastic.co/elasticsearch/elasticsearch:8.x`
- **Role**: Primary data store for all ingested events, articles, and correlations. Shared by Grafana (dashboards), OpenCTI (STIX2 backend), and the Python ingestors (data writers).
- **Indices**: `geon-gdelt-*`, `geon-acled-*`, `geon-cti-*`, `geon-sanctions`, `geon-articles-*`, `geon-correlations`
- **Configuration**: Single-node deployment, 1 primary shard, 0 replicas
- **Persistence**: Named volume `geon_elasticsearch_data`
- **Requirement**: `vm.max_map_count >= 262144` on the host

### Grafana

- **Image**: `grafana/grafana`
- **Role**: Dashboards and visualization for geopolitical events, CTI data, correlations, and platform monitoring. Connects to Elasticsearch as a datasource to query all `geon-*` indices. Also connects to Prometheus for service health monitoring.
- **Access**: Via Nginx at `/grafana`, behind Authelia authentication
- **Dashboards**: Global overview (Geomap), country profiles (template variables), correlations, article feed, monitoring
- **Provisioning**: Datasources and dashboards provisioned via YAML and JSON files in `docker/grafana/`

### OpenCTI

- **Image**: `opencti/platform:latest`
- **Role**: STIX2 knowledge graph for structured cyber threat intelligence
- **Dependencies**: Redis (cache), RabbitMQ (message broker), MinIO (object storage), Elasticsearch (backend storage)
- **Connectors**: MITRE ATT&CK, AlienVault OTX, CISA KEV, CVE/NVD, OpenCTI Datasets
- **Access**: Via Nginx at `/opencti`, behind Authelia authentication

### n8n

- **Image**: `docker.n8n.io/n8nio/n8n`
- **Role**: Workflow automation engine for RSS feed aggregation, article filtering, entity extraction, Elasticsearch indexing, and OpenCTI report creation. Also handles alert delivery workflows triggered by the correlation engine.
- **Workflows**:
  - RSS Feed Trigger nodes fetch articles from think tanks, agencies, and defense publications
  - Function nodes filter articles by keyword relevance and extract entities (countries, organizations, persons)
  - Elasticsearch nodes write filtered articles to `geon-articles-YYYY.MM`
  - HTTP Request nodes create reports in OpenCTI via GraphQL for CTI-relevant articles
  - Webhook + notification workflows for alert delivery (Discord, email)
- **Access**: Via Nginx at `/n8n`, behind Authelia authentication

### Authelia

- **Image**: `authelia/authelia`
- **Role**: Centralized authentication with TOTP-based multi-factor authentication
- **Protection**: All web-facing services (Grafana, OpenCTI, n8n)
- **Storage**: SQLite or file-based user database

### Supporting Services

| Service | Image | Role |
|---------|-------|------|
| Redis | `redis:7-alpine` | Cache for OpenCTI |
| RabbitMQ | `rabbitmq:3-management-alpine` | Message broker for OpenCTI connectors |
| MinIO | `minio/minio` | S3-compatible object storage for OpenCTI |
| Prometheus | `prom/prometheus` | Metrics collection; Grafana queries it for monitoring dashboards |
| Certbot | `certbot/certbot` | Automatic Let's Encrypt certificate renewal |

## Data Flow

### Ingestion Pipeline

```
GDELT API -----> gdelt/ingestor.py -----> Elasticsearch (geon-gdelt-events-YYYY.MM)
                                     +--> OpenCTI (country/org entities)

ACLED API -----> acled/ingestor.py -----> Elasticsearch (geon-acled-events-YYYY.MM)

OFAC/EU/UN ----> sanctions/ingestor.py -> Elasticsearch (geon-sanctions)
                                     +--> OpenCTI (sanctioned entities)

RSS Feeds -----> n8n workflows ---------> Elasticsearch (geon-articles-YYYY.MM)
                                     +--> OpenCTI (reports, if CTI-relevant)

OpenCTI -------> opencti_export/exporter.py -> Elasticsearch (geon-cti-*)
```

### Visualization Layer

```
Elasticsearch (geon-*)
        |
        v
  Grafana Dashboards
  - Geomap panels for global event maps
  - Time series for trends and timelines
  - Table panels for correlation listings
  - Stat panels for KPIs and risk scores
  - Template variables for country/source filtering
```

### Correlation Pipeline

```
Elasticsearch (geon-gdelt-*, geon-acled-*, geon-cti-*, geon-sanctions)
        |
        v
  Correlation Engine (correlation/engine.py)
        |
        +--> Elasticsearch (geon-correlations)
        +--> n8n webhook (triggers alert workflows)
        +--> Discord webhook (direct alerts)
        +--> Email (alerts)
        +--> OpenCTI (enriched reports)
```

## Security Layers

1. **Network**: Only Nginx exposes ports (80/443). All inter-service traffic stays on the Docker bridge network.
2. **TLS**: All external traffic encrypted via Let's Encrypt certificates.
3. **Authentication**: Authelia enforces login + TOTP on all web interfaces.
4. **Docker**: Rootless mode -- the Docker daemon runs without root privileges.
5. **Secrets**: All credentials stored in `.env`, excluded from version control.
6. **Healthchecks**: Docker healthchecks detect and surface service failures.

## Volume Strategy

All stateful services use named Docker volumes:

| Volume | Service | Content |
|--------|---------|---------|
| `geon_elasticsearch_data` | Elasticsearch | Index data |
| `geon_opencti_data` | OpenCTI | Platform state |
| `geon_minio_data` | MinIO | Object storage |
| `geon_rabbitmq_data` | RabbitMQ | Message queues |
| `geon_redis_data` | Redis | Cache data |
| `geon_n8n_data` | n8n | Workflow definitions and execution history |
| `geon_authelia_data` | Authelia | User database |
| `geon_prometheus_data` | Prometheus | Metrics |
| `geon_grafana_data` | Grafana | Dashboard config and user preferences |
