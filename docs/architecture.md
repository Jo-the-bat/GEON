# HEGO Architecture

## Overview

HEGO is a multi-container Docker application that ingests, indexes, correlates, and visualizes geopolitical events alongside cyber threat intelligence. All services run in rootless Docker behind a single Nginx reverse proxy with TLS termination and Authelia MFA.

## Network Topology

```
Internet
   |
   v
+----------------+
|     Nginx      |  :443 (TLS via Certbot / Let's Encrypt)
|  reverse proxy |  :80  (redirect to 443)
+-------+--------+
        | Docker internal network (hego_net)
        |
        +---> /                --> Landing page (static files)
        +---> /opencti         --> opencti:8080
        +---> /kibana          --> kibana:5601
        +---> /huginn          --> huginn:3000
        +---> /auth            --> authelia:9091
        |
+-------+--------------------------------------------------+
|               Docker network: hego_net                    |
|                                                           |
|  +---------------+  +----------------+  +-------------+  |
|  | Elasticsearch |  |    OpenCTI     |  |   Huginn    |  |
|  |   (port 9200) |  |   (port 8080) |  |  (port 3000)|  |
|  +-------+-------+  +-------+--------+  +------+------+  |
|          |                   |                  |         |
|  +-------+-------+  +-------+--------+  +------+------+  |
|  |    Kibana     |  |   RabbitMQ    |  |    Redis    |  |
|  |   (port 5601) |  |  (port 5672)  |  | (port 6379) |  |
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
|  +---------------+  +----------------+                    |
|  |  Prometheus   |  |    Grafana    |                    |
|  |  (port 9090)  |  |  (port 3001)  |                    |
|  +---------------+  +----------------+                    |
+-----------------------------------------------------------+
```

## Components

### Nginx (Reverse Proxy)

- **Image**: `nginx:alpine`
- **Role**: TLS termination, reverse proxy routing, static file serving
- **Ports**: 80 (redirect), 443 (HTTPS) -- the only ports exposed to the public
- **Configuration**: `docker/nginx/conf.d/hego.conf`
- **TLS**: Let's Encrypt certificates managed by a Certbot sidecar container

### Elasticsearch

- **Image**: `docker.elastic.co/elasticsearch/elasticsearch:8.x`
- **Role**: Primary data store for all ingested events, articles, and correlations
- **Indices**: `hego-gdelt-*`, `hego-acled-*`, `hego-cti-*`, `hego-sanctions`, `hego-articles-*`, `hego-correlations`
- **Configuration**: Single-node deployment, 1 primary shard, 0 replicas
- **Persistence**: Named volume `hego_elasticsearch_data`
- **Requirement**: `vm.max_map_count >= 262144` on the host

### Kibana

- **Image**: `docker.elastic.co/kibana/kibana:8.x`
- **Role**: Visualization dashboards -- maps, timelines, data tables, charts
- **Access**: Via Nginx at `/kibana`, behind Authelia authentication
- **Dashboards**: Global overview, country profiles, correlations, article feed, monitoring

### OpenCTI

- **Image**: `opencti/platform:latest`
- **Role**: STIX2 knowledge graph for structured cyber threat intelligence
- **Dependencies**: Redis (cache), RabbitMQ (message broker), MinIO (object storage)
- **Connectors**: MITRE ATT&CK, AlienVault OTX, CISA KEV, CVE/NVD, OpenCTI Datasets
- **Access**: Via Nginx at `/opencti`, behind Authelia authentication

### Huginn

- **Image**: `ghcr.io/huginn/huginn`
- **Role**: Automated agents for RSS feed aggregation, filtering, entity extraction, and webhook delivery
- **Pipeline**: RSS fetch -> keyword filter -> entity extraction -> Elasticsearch indexing + OpenCTI report creation
- **Access**: Via Nginx at `/huginn`, behind Authelia authentication

### Authelia

- **Image**: `authelia/authelia`
- **Role**: Centralized authentication with TOTP-based multi-factor authentication
- **Protection**: All web-facing services (Kibana, OpenCTI, Huginn, Grafana)
- **Storage**: SQLite or file-based user database

### Supporting Services

| Service | Image | Role |
|---------|-------|------|
| Redis | `redis:7-alpine` | Cache for OpenCTI and Huginn |
| RabbitMQ | `rabbitmq:3-management-alpine` | Message broker for OpenCTI connectors |
| MinIO | `minio/minio` | S3-compatible object storage for OpenCTI |
| Prometheus | `prom/prometheus` | Metrics collection and alerting |
| Grafana | `grafana/grafana` | Monitoring dashboards |
| Certbot | `certbot/certbot` | Automatic Let's Encrypt certificate renewal |

## Data Flow

### Ingestion Pipeline

```
GDELT API -----> gdelt/ingestor.py -----> Elasticsearch (hego-gdelt-events-YYYY.MM)
                                     +--> OpenCTI (country/org entities)

ACLED API -----> acled/ingestor.py -----> Elasticsearch (hego-acled-events-YYYY.MM)

OFAC/EU/UN ----> sanctions/ingestor.py -> Elasticsearch (hego-sanctions)
                                     +--> OpenCTI (sanctioned entities)

RSS Feeds -----> Huginn agents ---------> Elasticsearch (hego-articles-YYYY.MM)
                                     +--> OpenCTI (reports, if CTI-relevant)

OpenCTI -------> opencti_export/exporter.py -> Elasticsearch (hego-cti-*)
```

### Correlation Pipeline

```
Elasticsearch (hego-gdelt-*, hego-acled-*, hego-cti-*, hego-sanctions)
        |
        v
  Correlation Engine (correlation/engine.py)
        |
        +--> Elasticsearch (hego-correlations)
        +--> Discord webhook (alerts)
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
| `hego_elasticsearch_data` | Elasticsearch | Index data |
| `hego_opencti_data` | OpenCTI | Platform state |
| `hego_minio_data` | MinIO | Object storage |
| `hego_rabbitmq_data` | RabbitMQ | Message queues |
| `hego_redis_data` | Redis | Cache data |
| `hego_huginn_data` | Huginn | Agent state |
| `hego_authelia_data` | Authelia | User database |
| `hego_prometheus_data` | Prometheus | Metrics |
| `hego_grafana_data` | Grafana | Dashboard config |
