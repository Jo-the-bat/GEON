#!/usr/bin/env bash
# =============================================================================
# GEON — TLS bootstrap (premiere emission du certificat Let's Encrypt)
#
# Resout le probleme d'oeuf et de poule : nginx refuse de demarrer sans
# certificat, et certbot (webroot) a besoin de nginx pour servir le challenge.
#
#   1. Genere un certificat auto-signe factice si aucun cert n'existe
#   2. (Re)demarre geon-nginx, qui peut alors charger sa config TLS
#   3. Demande le vrai certificat a Let's Encrypt (webroot HTTP-01)
#   4. Recharge nginx avec le vrai certificat
#
# Le challenge HTTP-01 doit pouvoir atteindre geon-nginx:80 via le chemin
# public http://$DOMAIN/.well-known/acme-challenge/ (le proxy hote doit
# forwarder ce chemin — voir docs/host_proxy_sni_passthrough.md).
#
# Usage : ./scripts/init_tls.sh [--staging]
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
COMPOSE=(docker compose -f "$PROJECT_DIR/docker/docker-compose.yml")

# --- Charger .env pour GEON_DOMAIN / GEON_EMAIL ---
if [[ -f "$PROJECT_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$PROJECT_DIR/.env"; set +a
fi
DOMAIN="${GEON_DOMAIN:-geon.example.com}"
EMAIL="${GEON_EMAIL:-contact@example.com}"

STAGING_FLAG=""
if [[ "${1:-}" == "--staging" ]]; then
    STAGING_FLAG="--staging"
    echo "[INFO] Mode staging Let's Encrypt (certificat de test)."
fi

LIVE_DIR="/etc/letsencrypt/live/$DOMAIN"

# --- 1. Certificat factice si aucun cert present ---
echo "[INFO] Verification du certificat existant pour $DOMAIN ..."
"${COMPOSE[@]}" run --rm --entrypoint sh certbot -c "
    if [ -f '$LIVE_DIR/fullchain.pem' ]; then
        echo '[INFO] Un certificat existe deja, pas de cert factice.'
    else
        echo '[INFO] Generation d un certificat auto-signe temporaire ...'
        mkdir -p '$LIVE_DIR'
        openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
            -keyout '$LIVE_DIR/privkey.pem' \
            -out '$LIVE_DIR/fullchain.pem' \
            -subj '/CN=$DOMAIN'
    fi
"

# --- 2. (Re)demarrer nginx pour qu'il charge la config TLS ---
echo "[INFO] Demarrage de geon-nginx ..."
"${COMPOSE[@]}" up -d nginx

# --- 3. Vrai certificat via webroot ---
echo "[INFO] Demande du certificat Let's Encrypt pour $DOMAIN ..."
"${COMPOSE[@]}" run --rm --entrypoint sh certbot -c "
    # Supprimer le cert factice (auto-signe, pas de lineage certbot)
    if [ ! -d '/etc/letsencrypt/renewal' ] || [ ! -f '/etc/letsencrypt/renewal/$DOMAIN.conf' ]; then
        rm -rf '$LIVE_DIR'
    fi
    certbot certonly --webroot -w /var/www/certbot \
        -d '$DOMAIN' \
        --email '$EMAIL' \
        --agree-tos --no-eff-email \
        --non-interactive \
        $STAGING_FLAG
"

# --- 4. Recharger nginx avec le vrai certificat ---
echo "[INFO] Rechargement de geon-nginx ..."
"${COMPOSE[@]}" exec nginx nginx -s reload

echo "[OK] Certificat installe. Le sidecar geon-certbot renouvellera automatiquement."
echo "     Verifier : docker compose -f docker/docker-compose.yml up -d certbot"
