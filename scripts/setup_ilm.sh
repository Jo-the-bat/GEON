#!/usr/bin/env bash
# GEON — Install ILM policy + monthly-rollover index templates for the
# volume-heavy indices (GDELT events, GDELT GKG, ACLED events).
#
# Retention policy:
#   hot   — indices younger than 30 days (actively written)
#   warm  — indices 30–60 days old (read-only, force-merged)
#   delete — indices older than 90 days (removed to cap disk)
#
# Run this script ONCE after the first `docker compose up`, once Elasticsearch
# is healthy. It is idempotent — PUT on an existing policy/template just
# replaces it, so re-runs are safe.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "${PROJECT_DIR}/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    . "${PROJECT_DIR}/.env"
    set +a
fi

ES_URL="${ES_URL:-http://localhost:9200}"
ES_USER="${ES_USER:-elastic}"
ES_PASS="${ELASTIC_PASSWORD:-changeme}"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
fail() { echo -e "${RED}[FAIL]${NC}  $*" >&2; }

# --- Helper: PUT a JSON body and accept 200/201/409 as "success" ---
put_es() {
    local path="$1"
    local payload="$2"
    local label="$3"

    local http_code
    http_code=$(curl -sS -u "${ES_USER}:${ES_PASS}" \
        -o /tmp/geon_ilm.out \
        -w '%{http_code}' \
        -X PUT "${ES_URL}${path}" \
        -H 'Content-Type: application/json' \
        -d "${payload}" || true)

    case "${http_code}" in
        2??|409)
            ok "${label} (HTTP ${http_code})"
            ;;
        *)
            fail "${label} failed (HTTP ${http_code}): $(cat /tmp/geon_ilm.out)"
            exit 1
            ;;
    esac
}

# --- Wait for ES ---
info "Waiting for Elasticsearch at ${ES_URL} ..."
for i in $(seq 1 30); do
    if curl -sf -u "${ES_USER}:${ES_PASS}" "${ES_URL}/_cluster/health" >/dev/null 2>&1; then
        ok "Elasticsearch is reachable."
        break
    fi
    if [ "$i" -eq 30 ]; then
        fail "Elasticsearch did not become reachable after 30 retries."
        exit 1
    fi
    sleep 2
done

# --- ILM policy: hot → warm (30d) → delete (90d) ---
info "Installing ILM policy geon-monthly-rollover ..."
put_es "/_ilm/policy/geon-monthly-rollover" '{
    "policy": {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "set_priority": { "priority": 100 }
                }
            },
            "warm": {
                "min_age": "30d",
                "actions": {
                    "set_priority": { "priority": 50 },
                    "forcemerge": { "max_num_segments": 1 },
                    "readonly": {}
                }
            },
            "delete": {
                "min_age": "90d",
                "actions": { "delete": {} }
            }
        }
    }
}' "ILM policy geon-monthly-rollover"

# --- Index templates ---
# Each template attaches the ILM policy and sets single-shard / zero-replica
# defaults, matching the rest of the GEON index defaults.

apply_template() {
    local name="$1"
    local pattern="$2"
    info "Installing index template ${name} (pattern ${pattern}) ..."
    put_es "/_index_template/${name}" "$(cat <<EOF
{
    "index_patterns": ["${pattern}"],
    "priority": 200,
    "template": {
        "settings": {
            "index.lifecycle.name": "geon-monthly-rollover",
            "number_of_shards": 1,
            "number_of_replicas": 0
        }
    }
}
EOF
)" "Index template ${name}"
}

apply_template "geon-gdelt-events" "geon-gdelt-events-*"
apply_template "geon-gdelt-gkg"    "geon-gkg-*"
apply_template "geon-acled-events" "geon-acled-events-*"

echo ""
ok "ILM policy and index templates installed."
echo ""
echo "  Existing indices that match the patterns above will adopt the policy"
echo "  on their next rollover / the next phase transition check (~10 min)."
echo ""
