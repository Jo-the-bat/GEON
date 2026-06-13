#!/usr/bin/env bash
# GEON — Backup Script
# Creates Elasticsearch snapshots and exports OpenCTI data.
# Retains the last 7 daily backups.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load environment
if [ -f "${PROJECT_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
fi

# --- Configuration ---
BACKUP_DIR="${PROJECT_DIR}/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_PATH="${BACKUP_DIR}/${TIMESTAMP}"
RETENTION_DAYS=7

# Elasticsearch is reached through the container (see es_* helpers below), not
# via a host port — on the shared host ES isn't published. Override the
# container name with ES_CONTAINER if needed.
ES_CONTAINER="${ES_CONTAINER:-geon-elasticsearch}"
SNAPSHOT_REPO="geon_backup"
SNAPSHOT_RETENTION="${SNAPSHOT_RETENTION:-7}"  # keep the last N manual (geon_*) snapshots

OPENCTI_URL="${OPENCTI_URL:-http://localhost:8080}"
OPENCTI_TOKEN="${OPENCTI_ADMIN_TOKEN:-}"

# --- Colors ---
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${CYAN}[INFO]${NC}  $(date '+%Y-%m-%d %H:%M:%S') $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $(date '+%Y-%m-%d %H:%M:%S') $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $(date '+%Y-%m-%d %H:%M:%S') $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- Elasticsearch REST helpers (executed INSIDE the ES container) -----------
# On the shared host ES isn't published, so a host-side curl to localhost:9200
# can't reach it. The ES container always has curl + $ELASTIC_PASSWORD, and this
# script already relies on docker (cp / volume inspect) — so route every ES REST
# call through `docker exec`. Works on standalone and shared topologies alike.
es_put_code() {  # <path> <json-body> -> prints HTTP status code
    printf '%s' "$2" | docker exec -i "$ES_CONTAINER" sh -c \
        "curl -sS -o /dev/null -w '%{http_code}' -u \"elastic:\$ELASTIC_PASSWORD\" -X PUT \"http://localhost:9200$1\" -H 'Content-Type: application/json' --data-binary @-"
}
es_get() {  # <path> -> prints response body
    docker exec "$ES_CONTAINER" sh -c \
        "curl -sS -u \"elastic:\$ELASTIC_PASSWORD\" \"http://localhost:9200$1\""
}
es_delete_code() {  # <path> -> prints HTTP status code
    docker exec "$ES_CONTAINER" sh -c \
        "curl -sS -o /dev/null -w '%{http_code}' -u \"elastic:\$ELASTIC_PASSWORD\" -X DELETE \"http://localhost:9200$1\""
}

info "Starting GEON backup: ${TIMESTAMP}"

mkdir -p "$BACKUP_PATH"

# --- 1. Elasticsearch Snapshot ---
info "Registering Elasticsearch snapshot repository..."

# Register the snapshot repository (idempotent — PUT on an existing repo with
# the same settings returns 200). Requires path.repo set in elasticsearch.yml
# and the geon_es_backups volume mounted + writable by the ES uid (1000).
REGISTER_RESULT=$(es_put_code "/_snapshot/${SNAPSHOT_REPO}" \
    '{"type":"fs","settings":{"location":"/usr/share/elasticsearch/backups","compress":true}}')

if [[ "$REGISTER_RESULT" = "200" || "$REGISTER_RESULT" = "201" ]]; then
    ok "Snapshot repository registered."
else
    fail "Could not register snapshot repository (HTTP ${REGISTER_RESULT})."
    echo "     Ensure path.repo is set in elasticsearch.yml, the geon_es_backups"
    echo "     volume is mounted, and its directory is writable by the ES uid."
    exit 1
fi

# Create a snapshot of all geon-* indices
SNAPSHOT_NAME="geon_${TIMESTAMP}"
info "Creating Elasticsearch snapshot: ${SNAPSHOT_NAME}"

SNAPSHOT_RESULT=$(es_put_code "/_snapshot/${SNAPSHOT_REPO}/${SNAPSHOT_NAME}?wait_for_completion=true" \
    '{"indices":"geon-*","ignore_unavailable":true,"include_global_state":false}')

if [[ "$SNAPSHOT_RESULT" = "200" || "$SNAPSHOT_RESULT" = "201" ]]; then
    ok "Elasticsearch snapshot created: ${SNAPSHOT_NAME}"
else
    fail "Elasticsearch snapshot failed (HTTP ${SNAPSHOT_RESULT})."
    exit 1
fi

# Save snapshot metadata
es_get "/_snapshot/${SNAPSHOT_REPO}/${SNAPSHOT_NAME}" \
    > "${BACKUP_PATH}/es_snapshot_info.json" 2>/dev/null || true

# Export index list
es_get "/_cat/indices/geon-*?v&h=index,docs.count,store.size" \
    > "${BACKUP_PATH}/es_indices.txt" 2>/dev/null || true

ok "Elasticsearch index metadata saved."

# Prune old manual snapshots, keeping the most recent ${SNAPSHOT_RETENTION}.
# (SLM-managed snapshots — geon-snap-* — are pruned by their own retention.)
info "Pruning old ES snapshots (keeping last ${SNAPSHOT_RETENTION})..."
TO_DELETE=$(es_get "/_cat/snapshots/${SNAPSHOT_REPO}?h=id&s=id" 2>/dev/null \
    | grep '^geon_' | head -n "-${SNAPSHOT_RETENTION}" || true)
if [ -n "$TO_DELETE" ]; then
    while IFS= read -r SNAP; do
        [ -z "$SNAP" ] && continue
        DEL_CODE=$(es_delete_code "/_snapshot/${SNAPSHOT_REPO}/${SNAP}" 2>/dev/null || echo "000")
        if [ "$DEL_CODE" = "200" ]; then
            info "  Deleted old snapshot: ${SNAP}"
        else
            warn "  Could not delete snapshot ${SNAP} (HTTP ${DEL_CODE})."
        fi
    done <<< "$TO_DELETE"
fi
ok "ES snapshot retention applied."

# --- 2. n8n Backup (SQLite database) ---
info "Backing up n8n data..."

N8N_VOLUME="geon_n8n_data"
N8N_CONTAINER=$(docker ps --filter "name=n8n" --format "{{.Names}}" 2>/dev/null | head -1)

if [ -n "$N8N_CONTAINER" ]; then
    # Copy the SQLite database from the n8n container/volume
    docker cp "${N8N_CONTAINER}:/home/node/.n8n/database.sqlite" \
        "${BACKUP_PATH}/n8n_database.sqlite" 2>/dev/null && \
        ok "n8n database backed up." || \
        warn "Could not copy n8n database from container."

    # Also back up n8n credentials encryption key if accessible
    docker cp "${N8N_CONTAINER}:/home/node/.n8n/.n8n-encryption-key" \
        "${BACKUP_PATH}/n8n_encryption_key" 2>/dev/null || true
else
    # Try via volume mount directly
    N8N_DATA_PATH=$(docker volume inspect "$N8N_VOLUME" --format '{{.Mountpoint}}' 2>/dev/null || echo "")
    if [ -n "$N8N_DATA_PATH" ] && [ -f "${N8N_DATA_PATH}/database.sqlite" ]; then
        cp "${N8N_DATA_PATH}/database.sqlite" "${BACKUP_PATH}/n8n_database.sqlite" 2>/dev/null && \
            ok "n8n database backed up from volume." || \
            warn "Could not copy n8n database from volume."
    else
        warn "n8n container not running and volume not accessible. Skipping n8n backup."
    fi
fi

# --- 3. OpenCTI Export ---
info "Exporting OpenCTI data..."

if [ -n "$OPENCTI_TOKEN" ]; then
    # Export reports via GraphQL API
    OPENCTI_EXPORT=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${OPENCTI_URL}/graphql" \
        -H "Authorization: Bearer ${OPENCTI_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{
            "query": "{ reports(first: 1000) { edges { node { id name description created published } } } }"
        }' \
        --output "${BACKUP_PATH}/opencti_reports.json" \
        2>/dev/null || echo "000")

    if [ "$OPENCTI_EXPORT" = "200" ]; then
        ok "OpenCTI reports exported."
    else
        fail "OpenCTI export failed (HTTP ${OPENCTI_EXPORT})."
        echo "     For a complete export, use the OpenCTI web interface: Administration > Data > Export."
    fi

    # Export indicators
    curl -s -X POST "${OPENCTI_URL}/graphql" \
        -H "Authorization: Bearer ${OPENCTI_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{
            "query": "{ indicators(first: 5000) { edges { node { id name pattern valid_from valid_until } } } }"
        }' \
        --output "${BACKUP_PATH}/opencti_indicators.json" \
        2>/dev/null || true
else
    warn "OPENCTI_ADMIN_TOKEN not set. Skipping OpenCTI export."
    echo "     Set the token in .env to enable automatic OpenCTI backups."
fi

# --- 4. Compress ---
info "Compressing backup..."

ARCHIVE="${BACKUP_DIR}/geon_backup_${TIMESTAMP}.tar.gz"
tar -czf "$ARCHIVE" -C "$BACKUP_DIR" "$TIMESTAMP" 2>/dev/null

# Remove the uncompressed directory
rm -rf "$BACKUP_PATH"
ok "Backup archived: ${ARCHIVE}"

# --- 5. Retention ---
info "Cleaning old backups (keeping last ${RETENTION_DAYS} days)..."

DELETED=0
find "$BACKUP_DIR" -name "geon_backup_*.tar.gz" -type f -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null | while read -r OLD; do
    info "  Deleted: $(basename "$OLD")"
    DELETED=$((DELETED + 1))
done

ok "Retention policy applied."

# --- Summary ---
ARCHIVE_SIZE=$(du -sh "$ARCHIVE" 2>/dev/null | cut -f1)
echo ""
ok "Backup complete."
info "  Archive: ${ARCHIVE}"
info "  Size:    ${ARCHIVE_SIZE:-unknown}"
info "  ES Snapshot: ${SNAPSHOT_NAME}"
echo ""
