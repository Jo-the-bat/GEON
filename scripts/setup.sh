#!/usr/bin/env bash
# GEON — Initial Setup Script
# Checks prerequisites, prepares the environment, and pulls Docker images.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }

ERRORS=0

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}   GEON — Setup Script${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

# --- 1. Check Docker ---
info "Checking Docker installation..."

if ! command -v docker &>/dev/null; then
    fail "Docker is not installed. Install Docker Engine first."
    echo "  See: https://docs.docker.com/engine/install/"
    ERRORS=$((ERRORS + 1))
else
    ok "Docker is installed: $(docker --version)"

    # Check rootless mode
    if docker info 2>/dev/null | grep -q "rootless"; then
        ok "Docker is running in rootless mode."
    else
        warn "Docker does not appear to be running in rootless mode."
        echo "      GEON is designed for rootless Docker. See: https://docs.docker.com/engine/security/rootless/"
    fi
fi

# --- 2. Check Docker Compose ---
info "Checking Docker Compose..."

if docker compose version &>/dev/null; then
    ok "Docker Compose v2 is available: $(docker compose version --short 2>/dev/null || echo 'detected')"
elif command -v docker-compose &>/dev/null; then
    warn "Found docker-compose (v1). Docker Compose v2 (docker compose) is recommended."
else
    fail "Docker Compose is not installed."
    ERRORS=$((ERRORS + 1))
fi

# --- 3. Check sysctl settings ---
info "Checking kernel parameters..."

MAX_MAP_COUNT=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo "0")
if [ "$MAX_MAP_COUNT" -ge 262144 ]; then
    ok "vm.max_map_count = $MAX_MAP_COUNT (>= 262144)"
else
    warn "vm.max_map_count = $MAX_MAP_COUNT (needs >= 262144 for Elasticsearch)"
    echo "      Fix with: sudo sysctl -w vm.max_map_count=262144"
    echo "      Persist:  echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.d/99-elasticsearch.conf"
fi

UNPRIVILEGED_PORT=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo "1024")
if [ "$UNPRIVILEGED_PORT" -le 80 ]; then
    ok "net.ipv4.ip_unprivileged_port_start = $UNPRIVILEGED_PORT (<= 80)"
else
    warn "net.ipv4.ip_unprivileged_port_start = $UNPRIVILEGED_PORT (needs <= 80 for Nginx)"
    echo "      Fix with: sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80"
    echo "      Persist:  echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee -a /etc/sysctl.d/99-unprivileged-ports.conf"
fi

# --- 4. Check available memory ---
info "Checking system resources..."

TOTAL_MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
if [ -n "$TOTAL_MEM_KB" ]; then
    TOTAL_MEM_GB=$((TOTAL_MEM_KB / 1048576))
    if [ "$TOTAL_MEM_GB" -ge 8 ]; then
        ok "Available RAM: ${TOTAL_MEM_GB} GB (>= 8 GB)"
    else
        warn "Available RAM: ${TOTAL_MEM_GB} GB. GEON recommends at least 8 GB (16 GB ideal)."
    fi
fi

# --- 5. Environment file ---
info "Checking environment configuration..."

ENV_FILE="${PROJECT_DIR}/.env"
ENV_EXAMPLE="${PROJECT_DIR}/.env.example"

if [ -f "$ENV_FILE" ]; then
    ok ".env file exists."

    # Check for placeholder values
    if grep -q '<GENERATE>' "$ENV_FILE" 2>/dev/null || grep -q '<YOUR_' "$ENV_FILE" 2>/dev/null; then
        warn ".env contains placeholder values. Edit it before starting the stack:"
        echo "      nano ${ENV_FILE}"
    fi
else
    if [ -f "$ENV_EXAMPLE" ]; then
        info "Copying .env.example to .env..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        ok ".env created from template."
        warn "You MUST edit .env and set real passwords and API keys before starting:"
        echo "      nano ${ENV_FILE}"
    else
        fail ".env.example not found. Cannot create .env."
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- 6. Create required directories ---
info "Creating required directories..."

DIRS=(
    "${PROJECT_DIR}/logs"
    "${PROJECT_DIR}/docker/nginx/ssl"
    "${PROJECT_DIR}/backups"
)

for DIR in "${DIRS[@]}"; do
    mkdir -p "$DIR"
done

ok "Directories created: logs, ssl, backups"

# --- 7. Pull Docker images ---
COMPOSE_FILE="${PROJECT_DIR}/docker/docker-compose.yml"

if [ -f "$COMPOSE_FILE" ]; then
    info "Pulling Docker images (this may take a while)..."
    if docker compose -f "$COMPOSE_FILE" pull 2>/dev/null; then
        ok "Docker images pulled successfully."
    else
        warn "Could not pull images. They will be pulled on first 'docker compose up'."
    fi
else
    warn "docker-compose.yml not found at ${COMPOSE_FILE}. Skipping image pull."
fi

# --- Summary ---
echo ""
echo -e "${CYAN}============================================${NC}"

if [ "$ERRORS" -gt 0 ]; then
    fail "Setup completed with ${ERRORS} error(s). Fix the issues above before proceeding."
    exit 1
else
    ok "Setup completed successfully."
    echo ""
    echo -e "  ${CYAN}Next steps:${NC}"
    echo ""
    echo "  1. Edit your environment file:"
    echo "     nano .env"
    echo ""
    echo "  2. Start the stack:"
    echo "     docker compose -f docker/docker-compose.yml up -d"
    echo ""
    echo "  3. Verify services:"
    echo "     docker compose -f docker/docker-compose.yml ps"
    echo ""
    echo "  4. Access GEON:"
    echo "     https://geon.joranbatty.fr/"
    echo ""
fi
