#!/usr/bin/env bash
# GEON — Install ILM retention policies + index templates for the
# volume-heavy time-series indices (GDELT events, GDELT GKG, ACLED events).
#
# WHY this exists: these are monthly indices (geon-<src>-YYYY.MM) that otherwise
# grow forever. GKG alone adds ~2 GB/month, GDELT events ~0.8 GB/month. Without a
# delete phase nothing ever reclaims disk.
#
# Single-node reality: hot/warm/cold *tiers* don't relocate data on one node
# (that needs multiple nodes with data-tier roles). The warm phase here only
# force-merges + marks old monthly indices read-only on the SAME disk. The only
# action that actually frees space is the `delete` phase — i.e. retention.
#
# Per-family retention (the warm phase @30d is shared; only delete min_age differs):
#   GKG    — delete @ 90d   (biggest, rarely re-queried beyond recent enrichment)
#   GDELT  — delete @ 180d  (correlation baselines read a 90d histogram → keep margin)
#   ACLED  — delete @ 365d  (low volume, long-lived ground truth)
#
# Run ONCE after the first `docker compose up`, once Elasticsearch is healthy.
# Idempotent — PUT on an existing policy/template/index-setting just replaces it.
#
# ES access: defaults to http://localhost:9200 (standalone / dev). On the shared
# host ES isn't published — run the equivalent PUTs from inside the container, e.g.
#   docker exec geon-elasticsearch sh -c 'curl ... -u "elastic:$ELASTIC_PASSWORD" ...'

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

# --- ILM policies: hot → warm (30d, forcemerge+readonly) → delete (per-family) ---
# $1 = policy name, $2 = delete min_age
apply_policy() {
    local name="$1"
    local delete_age="$2"
    info "Installing ILM policy ${name} (delete @ ${delete_age}) ..."
    put_es "/_ilm/policy/${name}" "$(cat <<EOF
{
    "policy": {
        "phases": {
            "hot":  { "min_age": "0ms", "actions": { "set_priority": { "priority": 100 } } },
            "warm": {
                "min_age": "30d",
                "actions": {
                    "set_priority": { "priority": 50 },
                    "forcemerge":   { "max_num_segments": 1 },
                    "readonly":     {}
                }
            },
            "delete": { "min_age": "${delete_age}", "actions": { "delete": {} } }
        }
    }
}
EOF
)" "ILM policy ${name}"
}

apply_policy "geon-retention-gkg"   "90d"
apply_policy "geon-retention-gdelt"  "180d"
apply_policy "geon-retention-acled"  "365d"

# --- Index templates: attach the right policy at index creation ---
# $1 = template name, $2 = index pattern, $3 = policy name
apply_template() {
    local name="$1"
    local pattern="$2"
    local policy="$3"
    info "Installing index template ${name} (pattern ${pattern} → ${policy}) ..."
    put_es "/_index_template/${name}" "$(cat <<EOF
{
    "index_patterns": ["${pattern}"],
    "priority": 200,
    "template": {
        "settings": {
            "index.lifecycle.name": "${policy}",
            "number_of_shards": 1,
            "number_of_replicas": 0
        }
    }
}
EOF
)" "Index template ${name}"
}

apply_template "geon-gdelt-events" "geon-gdelt-events-*" "geon-retention-gdelt"
apply_template "geon-gkg"          "geon-gkg-*"          "geon-retention-gkg"
apply_template "geon-acled-events" "geon-acled-events-*" "geon-retention-acled"

# --- Adopt EXISTING indices (templates only apply at creation) ---
# $1 = index pattern, $2 = policy name
adopt_existing() {
    local pattern="$1"
    local policy="$2"
    local existing
    existing=$(curl -sS -u "${ES_USER}:${ES_PASS}" \
        "${ES_URL}/_cat/indices/${pattern}?h=index" 2>/dev/null | tr -d ' ' || true)
    [ -z "${existing}" ] && return 0
    while IFS= read -r idx; do
        [ -z "${idx}" ] && continue
        put_es "/${idx}/_settings" "{\"index.lifecycle.name\":\"${policy}\"}" \
            "Adopt ${idx} → ${policy}"
    done <<< "${existing}"
}

info "Attaching existing indices to their retention policy ..."
adopt_existing "geon-gdelt-events-*" "geon-retention-gdelt"
adopt_existing "geon-gkg-*"          "geon-retention-gkg"
adopt_existing "geon-acled-events-*" "geon-retention-acled"

echo ""
ok "ILM retention policies + templates installed and existing indices adopted."
echo ""
echo "  Retention (from index creation date): GKG 90d · GDELT 180d · ACLED 365d."
echo "  ILM re-evaluates every ~10 min. Indices already past delete min_age will"
echo "  force-merge (warm), then be removed on the next delete check."
echo ""
