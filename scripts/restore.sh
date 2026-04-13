#!/usr/bin/env bash
# GEON — Restore Script
# Restores an Elasticsearch snapshot and OpenCTI data from a backup archive.
#
# Usage: ./scripts/restore.sh <backup_archive.tar.gz>
# Example: ./scripts/restore.sh backups/geon_backup_20250615_040000.tar.gz

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
ES_HOST="${ES_HOST:-http://localhost:9200}"
ES_USER="${ES_USER:-elastic}"
ES_PASS="${ELASTIC_PASSWORD:-changeme}"
SNAPSHOT_REPO="geon_backup"

OPENCTI_URL="${OPENCTI_URL:-http://localhost:8080}"
OPENCTI_TOKEN="${OPENCTI_ADMIN_TOKEN:-}"

# --- Colors ---
CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }

# --- Argument check ---
if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup_archive.tar.gz>"
    echo ""
    echo "Available backups:"
    ls -1t "${PROJECT_DIR}/backups"/geon_backup_*.tar.gz 2>/dev/null || echo "  (none found)"
    exit 1
fi

ARCHIVE="$1"

if [ ! -f "$ARCHIVE" ]; then
    # Try relative to project dir
    if [ -f "${PROJECT_DIR}/${ARCHIVE}" ]; then
        ARCHIVE="${PROJECT_DIR}/${ARCHIVE}"
    else
        fail "Backup archive not found: ${ARCHIVE}"
    fi
fi

info "Restoring from: ${ARCHIVE}"

# --- Extract archive ---
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

info "Extracting archive..."
tar -xzf "$ARCHIVE" -C "$TEMP_DIR"

# Find the extracted directory
BACKUP_DIR=$(find "$TEMP_DIR" -maxdepth 1 -mindepth 1 -type d | head -1)
if [ -z "$BACKUP_DIR" ]; then
    fail "Archive does not contain a backup directory."
fi

ok "Archive extracted to: ${BACKUP_DIR}"

# --- 1. Restore Elasticsearch Snapshot ---
info "Restoring Elasticsearch snapshot..."

# Read snapshot info to determine the snapshot name
if [ -f "${BACKUP_DIR}/es_snapshot_info.json" ]; then
    SNAPSHOT_NAME=$(python3 -c "
import json, sys
with open('${BACKUP_DIR}/es_snapshot_info.json') as f:
    data = json.load(f)
snapshots = data.get('snapshots', [])
if snapshots:
    print(snapshots[0].get('snapshot', ''))
" 2>/dev/null || echo "")
fi

if [ -z "${SNAPSHOT_NAME:-}" ]; then
    # Fall back: list available snapshots
    info "Could not determine snapshot name from metadata. Listing available snapshots..."
    curl -s -u "${ES_USER}:${ES_PASS}" \
        "${ES_HOST}/_snapshot/${SNAPSHOT_REPO}/_all" 2>/dev/null | python3 -m json.tool 2>/dev/null || true

    echo ""
    warn "Please specify the snapshot name to restore."
    read -rp "Snapshot name: " SNAPSHOT_NAME

    if [ -z "$SNAPSHOT_NAME" ]; then
        fail "No snapshot name provided."
    fi
fi

info "Snapshot to restore: ${SNAPSHOT_NAME}"

# Close indices that will be restored (to avoid conflicts)
warn "This will close and overwrite existing geon-* indices."
read -rp "Continue? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    info "Restore cancelled."
    exit 0
fi

info "Closing existing geon-* indices..."
curl -s -u "${ES_USER}:${ES_PASS}" \
    -X POST "${ES_HOST}/geon-*/_close?ignore_unavailable=true" 2>/dev/null || true

# Restore the snapshot
info "Restoring snapshot ${SNAPSHOT_NAME}..."
RESTORE_RESULT=$(curl -s -w "\n%{http_code}" \
    -u "${ES_USER}:${ES_PASS}" \
    -X POST "${ES_HOST}/_snapshot/${SNAPSHOT_REPO}/${SNAPSHOT_NAME}/_restore?wait_for_completion=true" \
    -H "Content-Type: application/json" \
    -d '{
        "indices": "geon-*",
        "ignore_unavailable": true,
        "include_global_state": false
    }' 2>/dev/null)

HTTP_CODE=$(echo "$RESTORE_RESULT" | tail -1)
BODY=$(echo "$RESTORE_RESULT" | head -n -1)

if [ "$HTTP_CODE" = "200" ]; then
    ok "Elasticsearch snapshot restored successfully."
else
    fail "Elasticsearch restore failed (HTTP ${HTTP_CODE}): ${BODY}"
fi

# --- 2. Restore n8n Data ---
info "Checking for n8n database backup..."

if [ -f "${BACKUP_DIR}/n8n_database.sqlite" ]; then
    N8N_CONTAINER=$(docker ps --filter "name=n8n" --format "{{.Names}}" 2>/dev/null | head -1)
    if [ -n "$N8N_CONTAINER" ]; then
        warn "This will overwrite the current n8n database."
        read -rp "Restore n8n database? [y/N] " N8N_CONFIRM
        if [[ "$N8N_CONFIRM" =~ ^[Yy]$ ]]; then
            # Stop n8n before restoring
            info "Stopping n8n container..."
            docker stop "$N8N_CONTAINER" 2>/dev/null || true

            docker cp "${BACKUP_DIR}/n8n_database.sqlite" \
                "${N8N_CONTAINER}:/home/node/.n8n/database.sqlite" 2>/dev/null && \
                ok "n8n database restored." || \
                fail "Could not restore n8n database."

            # Restore encryption key if present
            if [ -f "${BACKUP_DIR}/n8n_encryption_key" ]; then
                docker cp "${BACKUP_DIR}/n8n_encryption_key" \
                    "${N8N_CONTAINER}:/home/node/.n8n/.n8n-encryption-key" 2>/dev/null || true
            fi

            info "Restarting n8n container..."
            docker start "$N8N_CONTAINER" 2>/dev/null || true
        else
            info "Skipping n8n restore."
        fi
    else
        warn "n8n container not running. Copy manually:"
        echo "      docker cp ${BACKUP_DIR}/n8n_database.sqlite <n8n_container>:/home/node/.n8n/database.sqlite"
    fi
else
    info "No n8n database found in this backup."
fi

# --- 3. Restore OpenCTI Data ---
info "Checking for OpenCTI export data..."

if [ -f "${BACKUP_DIR}/opencti_reports.json" ] && [ -n "$OPENCTI_TOKEN" ]; then
    info "OpenCTI export data found."
    warn "Automatic OpenCTI import via GraphQL is limited."
    echo "      For a full restore, use the OpenCTI web interface:"
    echo "        1. Go to Administration > Data > Import"
    echo "        2. Upload the STIX bundle or use the JSON export"
    echo ""
    echo "      Export files available in the backup:"
    ls -la "${BACKUP_DIR}"/opencti_*.json 2>/dev/null || echo "      (none)"
    echo ""

    # Copy exports to a persistent location
    RESTORE_EXPORTS="${PROJECT_DIR}/backups/restore_opencti_${SNAPSHOT_NAME}"
    mkdir -p "$RESTORE_EXPORTS"
    cp "${BACKUP_DIR}"/opencti_*.json "$RESTORE_EXPORTS/" 2>/dev/null || true
    ok "OpenCTI exports copied to: ${RESTORE_EXPORTS}"
else
    if [ ! -f "${BACKUP_DIR}/opencti_reports.json" ]; then
        info "No OpenCTI export data found in this backup."
    fi
    if [ -z "$OPENCTI_TOKEN" ]; then
        warn "OPENCTI_ADMIN_TOKEN not set. Cannot verify OpenCTI restore."
    fi
fi

# --- Summary ---
echo ""
ok "Restore complete."
echo ""
info "  Elasticsearch snapshot: ${SNAPSHOT_NAME}"
info "  Verify indices: curl -s -u ${ES_USER}:*** ${ES_HOST}/_cat/indices/geon-*?v"
echo ""
