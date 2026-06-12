#!/usr/bin/env bash
# GEON — Create dedicated Elasticsearch roles and users for Grafana + ingestors.
# Run this script ONCE after the first `docker compose up` (once Elasticsearch is
# healthy). It is idempotent: re-running it will PUT the roles/users again with
# the same configuration and will not error out.
#
# Required environment variables (loaded from .env at project root):
#   ELASTIC_PASSWORD         — superuser password for the `elastic` account
#   GRAFANA_ES_PASSWORD      — password to set for the geon_grafana_reader user
#   GEON_INGESTOR_PASSWORD   — password to set for the geon_ingestor user
#
# Optional:
#   ES_URL                   — defaults to http://localhost:9200 (host bind)
#                              For in-cluster use: http://elasticsearch:9200
#                              (pass via env or run inside the Docker network).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env if present
if [ -f "${PROJECT_DIR}/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    . "${PROJECT_DIR}/.env"
    set +a
fi

ES_URL="${ES_URL:-http://localhost:9200}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*" >&2; }

# --- Sanity checks ---
for var in ELASTIC_PASSWORD GRAFANA_ES_PASSWORD GEON_INGESTOR_PASSWORD; do
    if [ -z "${!var:-}" ]; then
        fail "$var is not set. Edit your .env file first."
        exit 1
    fi
done

# --- Wait for Elasticsearch to be reachable ---
info "Waiting for Elasticsearch at ${ES_URL} ..."
for i in $(seq 1 30); do
    if curl -sf -u "elastic:${ELASTIC_PASSWORD}" "${ES_URL}/_cluster/health" >/dev/null 2>&1; then
        ok "Elasticsearch is reachable."
        break
    fi
    if [ "$i" -eq 30 ]; then
        fail "Elasticsearch did not become reachable after 30 retries."
        exit 1
    fi
    sleep 2
done

# --- Helper: PUT via Security API and interpret status ---
put_security() {
    local path="$1"
    local payload="$2"
    local label="$3"
    local http_code
    http_code=$(curl -sf -u "elastic:${ELASTIC_PASSWORD}" \
        -o /tmp/geon_es_users.out \
        -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        -X POST "${ES_URL}${path}" \
        -d "${payload}" || true)
    if [[ "$http_code" == 2* ]]; then
        ok "${label} (HTTP ${http_code})"
    else
        fail "${label} failed (HTTP ${http_code}): $(cat /tmp/geon_es_users.out)"
        exit 1
    fi
}

# --- Role: geon_reader (read-only on geon-*) ---
info "Creating role geon_reader ..."
put_security "/_security/role/geon_reader" '{
    "cluster": ["monitor"],
    "indices": [
        {
            "names": ["geon-*"],
            "privileges": ["read", "view_index_metadata"]
        }
    ]
}' "Role geon_reader"

# --- Role: geon_writer (write on geon-*, plus read for self-consistency) ---
info "Creating role geon_writer ..."
put_security "/_security/role/geon_writer" '{
    "cluster": ["monitor"],
    "indices": [
        {
            "names": ["geon-*"],
            "privileges": [
                "create_index",
                "write",
                "view_index_metadata",
                "read",
                "manage"
            ]
        }
    ]
}' "Role geon_writer"

# --- User: geon_grafana_reader ---
info "Creating user geon_grafana_reader ..."
put_security "/_security/user/geon_grafana_reader" "$(cat <<EOF
{
    "password": "${GRAFANA_ES_PASSWORD}",
    "roles": ["geon_reader"],
    "full_name": "Grafana Datasource (read-only)",
    "email": "grafana@geon.local"
}
EOF
)" "User geon_grafana_reader"

# --- User: geon_ingestor ---
info "Creating user geon_ingestor ..."
put_security "/_security/user/geon_ingestor" "$(cat <<EOF
{
    "password": "${GEON_INGESTOR_PASSWORD}",
    "roles": ["geon_writer"],
    "full_name": "GEON Python Ingestor",
    "email": "ingestor@geon.local"
}
EOF
)" "User geon_ingestor"

echo ""
ok "All roles and users created."
echo ""
echo "  Grafana should now authenticate to Elasticsearch as:"
echo "    user:    geon_grafana_reader"
echo "    role:    geon_reader"
echo ""
echo "  Ingestors should now authenticate as:"
echo "    user:    geon_ingestor"
echo "    role:    geon_writer"
echo ""
echo "  If services were already running, restart them to pick up the new credentials:"
echo "    docker compose -f docker/docker-compose.yml restart grafana ingestor"
echo ""
