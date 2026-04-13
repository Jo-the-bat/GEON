# GEON Installation Guide

## Prerequisites

### Hardware

- **Minimum**: 8 GB RAM, 4 CPU cores, 50 GB disk
- **Recommended**: 16 GB RAM, 8 CPU cores, 100 GB SSD
- Elasticsearch and OpenCTI are the most resource-intensive components.

### Software

- Linux host (Debian/Ubuntu recommended)
- Docker Engine 24+ in rootless mode
- Docker Compose v2
- Git
- Python 3.11+ (for running ingestors outside Docker)
- A registered domain name with DNS pointing to your server

### API Keys (optional, for data ingestion)

- **ACLED**: Free API key from [acleddata.com](https://acleddata.com/register/)
- **AlienVault OTX**: Free API key from [otx.alienvault.com](https://otx.alienvault.com/)
- **AbuseIPDB**: Free API key from [abuseipdb.com](https://www.abuseipdb.com/register)
- GDELT does not require an API key.

---

## Step 1: Host Configuration

These are the only commands that require root privileges.

### Kernel Parameters

Elasticsearch requires a higher virtual memory map count:

```bash
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.d/99-elasticsearch.conf
```

For rootless Docker to bind ports 80 and 443:

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee -a /etc/sysctl.d/99-unprivileged-ports.conf
```

### Docker Rootless Mode

If Docker is not yet installed in rootless mode:

```bash
# Install Docker Engine first (if not already installed)
# See: https://docs.docker.com/engine/install/

# Then set up rootless mode
dockerd-rootless-setuptool.sh install

# Add to your shell profile
export PATH=$HOME/bin:$PATH
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock
```

Verify rootless mode:

```bash
docker info 2>/dev/null | grep -i rootless
# Should output: rootless
```

---

## Step 2: Clone the Repository

```bash
git clone https://github.com/Jo-the-bat/GEON.git
cd GEON
```

---

## Step 3: Configure Environment

```bash
cp .env.example .env
nano .env
```

At minimum, set the following:

1. **GEON_DOMAIN** -- your domain name (e.g., `geon.joranbatty.fr`)
2. **GEON_EMAIL** -- email for Let's Encrypt certificate notifications
3. **ELASTIC_PASSWORD** -- a strong password for Elasticsearch
4. **OPENCTI_ADMIN_PASSWORD** -- a strong password for the OpenCTI admin
5. **OPENCTI_ADMIN_TOKEN** -- generate a UUID: `python3 -c "import uuid; print(uuid.uuid4())"`
6. **N8N_ENCRYPTION_KEY** -- generate a 32-character key for n8n workflow encryption
7. **GF_SECURITY_ADMIN_PASSWORD** -- a strong password for the Grafana admin

Generate strong passwords for the other services (RabbitMQ, MinIO, Authelia).

All fields marked `<GENERATE>` need unique random values. All fields marked `<YOUR_*>` need your actual API keys.

---

## Step 4: Run Setup

```bash
chmod +x scripts/setup.sh scripts/backup.sh scripts/restore.sh
./scripts/setup.sh
```

The setup script will:

- Verify Docker and Docker Compose are installed
- Check kernel parameter settings
- Check available system memory
- Create the `.env` file from the template if it does not exist
- Create required directories (`logs/`, `backups/`, `docker/nginx/ssl/`)
- Pull Docker images

---

## Step 5: Start the Stack

```bash
docker compose -f docker/docker-compose.yml up -d
```

Watch the logs to monitor startup:

```bash
docker compose -f docker/docker-compose.yml logs -f --tail=50
```

### Startup Order

Services start in dependency order:

1. Redis, RabbitMQ, MinIO (no dependencies)
2. Elasticsearch (waits for healthy state)
3. Grafana, OpenCTI (wait for Elasticsearch)
4. n8n (waits for Redis)
5. Authelia
6. Nginx (waits for all upstream services)

Initial startup may take several minutes as Elasticsearch creates its cluster state and OpenCTI initializes its schema.

---

## Step 6: Verify Services

```bash
# Check all containers are running and healthy
docker compose -f docker/docker-compose.yml ps

# Test Elasticsearch
curl -sk -u elastic:${ELASTIC_PASSWORD} https://localhost:9200/_cluster/health | python3 -m json.tool

# Test Grafana
curl -sk https://geon.joranbatty.fr/grafana/api/health | python3 -m json.tool

# Test OpenCTI
curl -sk https://geon.joranbatty.fr/opencti/health
```

---

## Step 7: Set Up Grafana Datasources

Grafana datasources are provisioned automatically via `docker/grafana/datasources.yml`. If you need to verify or adjust them:

1. Open Grafana at `https://geon.joranbatty.fr/grafana`
2. Navigate to **Configuration > Data sources**
3. Verify the Elasticsearch datasource points to `http://elasticsearch:9200` and the Prometheus datasource points to `http://prometheus:9090`

The Elasticsearch datasource should be configured with index patterns matching `geon-*` to discover all GEON indices.

---

## Step 8: Configure Ingestors

### Python Environment

```bash
cd ingestors
python3 -m venv ../venv
source ../venv/bin/activate
pip install -r requirements.txt
```

### Manual Test Run

```bash
# Test GDELT ingestion
python -m gdelt.ingestor

# Test ACLED ingestion (requires ACLED_API_KEY in .env)
python -m acled.ingestor

# Test correlation engine
python -m correlation.engine
```

### Set Up Cron

```bash
# Edit the crontab example with your actual paths
nano scripts/crontab.example

# Install it
crontab scripts/crontab.example
```

---

## Step 9: Import n8n Workflows

1. Open n8n at `https://geon.joranbatty.fr/n8n`
2. Log in with the credentials set in `.env` (`N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD`)
3. Go to **Workflows > Import from File**
4. Import each JSON file from `n8n/workflows/`:
   - `rss_think_tanks.json` -- RSS feeds from research institutes
   - `rss_agencies.json` -- News agency RSS feeds
   - `rss_defense.json` -- Defense and cybersecurity RSS feeds
   - `enrichment_pipeline.json` -- Entity extraction and OpenCTI enrichment
5. Activate each workflow after verifying its configuration

---

## Step 10: TLS Certificate

If using Let's Encrypt with Certbot:

```bash
# Initial certificate request (Nginx must be running and port 80 reachable)
docker compose -f docker/docker-compose.yml run --rm certbot certonly \
    --webroot -w /var/www/certbot \
    -d geon.joranbatty.fr \
    --email contact@joranbatty.fr \
    --agree-tos --no-eff-email

# Reload Nginx to pick up the new certificate
docker compose -f docker/docker-compose.yml exec nginx nginx -s reload
```

Certbot auto-renewal is handled by the Certbot sidecar container.

---

## Troubleshooting

### Elasticsearch fails to start

- Check `vm.max_map_count`: `cat /proc/sys/vm/max_map_count`
- Check disk space: `df -h`
- Check logs: `docker compose logs elasticsearch`

### OpenCTI shows "Waiting for dependencies"

- Ensure Elasticsearch, Redis, RabbitMQ, and MinIO are all healthy
- OpenCTI can take 3-5 minutes on first boot to initialize

### Nginx returns 502 Bad Gateway

- The upstream service is not yet ready. Wait and retry.
- Check the specific service logs: `docker compose logs <service>`

### Grafana shows "No data" on dashboards

- Verify the Elasticsearch datasource is configured and reachable
- Ensure at least one ingestor has run to populate the `geon-*` indices
- Check that the index pattern in the dashboard panels matches your indices

### n8n workflows fail to execute

- Check n8n logs: `docker compose logs n8n`
- Verify Elasticsearch and OpenCTI URLs are reachable from within the Docker network (use service names, not localhost)
- Ensure the `N8N_ENCRYPTION_KEY` has not changed since workflows were created

### Permission denied errors

- Ensure Docker is running in rootless mode
- Check that volume directories are owned by your user
