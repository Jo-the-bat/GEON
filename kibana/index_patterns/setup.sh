#!/usr/bin/env bash
# HEGO — Kibana Index Pattern Setup
# Creates data views (index patterns) for all HEGO indices via the Kibana API.
#
# Prerequisites:
#   - Kibana must be running and accessible
#   - Elasticsearch must have the indices created (or at least the templates)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

# Load environment
if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
fi

KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
KIBANA_BASE_PATH="${KIBANA_BASE_PATH:-/kibana}"
ES_USER="${ES_USER:-elastic}"
ES_PASS="${ELASTIC_PASSWORD:-changeme}"

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $*"; }

FULL_URL="${KIBANA_URL}${KIBANA_BASE_PATH}"

info "Creating Kibana data views (index patterns)..."
info "Kibana URL: ${FULL_URL}"

# Function to create a data view
create_data_view() {
    local name="$1"
    local pattern="$2"
    local time_field="${3:-@timestamp}"
    local id="${4:-$name}"

    info "Creating data view: ${name} (${pattern})"

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -u "${ES_USER}:${ES_PASS}" \
        -X POST "${FULL_URL}/api/data_views/data_view" \
        -H "kbn-xsrf: true" \
        -H "Content-Type: application/json" \
        -d "{
            \"data_view\": {
                \"id\": \"${id}\",
                \"title\": \"${pattern}\",
                \"name\": \"${name}\",
                \"timeFieldName\": \"${time_field}\"
            },
            \"override\": true
        }" 2>/dev/null || echo "000")

    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
        ok "  Created: ${name}"
    else
        fail "  Failed to create ${name} (HTTP ${HTTP_CODE})"
    fi
}

echo ""

# GDELT events
create_data_view \
    "HEGO - GDELT Events" \
    "hego-gdelt-events-*" \
    "date" \
    "hego-gdelt"

# ACLED events
create_data_view \
    "HEGO - ACLED Events" \
    "hego-acled-events-*" \
    "event_date" \
    "hego-acled"

# CTI data (from OpenCTI)
create_data_view \
    "HEGO - CTI" \
    "hego-cti-*" \
    "created" \
    "hego-cti"

# Correlations
create_data_view \
    "HEGO - Correlations" \
    "hego-correlations" \
    "timestamp" \
    "hego-correlations"

# Sanctions
create_data_view \
    "HEGO - Sanctions" \
    "hego-sanctions" \
    "listed_date" \
    "hego-sanctions"

# Articles (from Huginn RSS)
create_data_view \
    "HEGO - Articles" \
    "hego-articles-*" \
    "published_date" \
    "hego-articles"

echo ""
ok "Data view setup complete."
info "Open Kibana > Stack Management > Data Views to verify."
