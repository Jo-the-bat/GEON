# Passthrough SNI — patch du proxy hote (odetowar-nginx)

GEON termine desormais son propre TLS : `geon-nginx` ecoute en 443 (TLS
direct) et 8443 (TLS + proxy_protocol), avec un sidecar certbot pour les
certificats Let's Encrypt.

Sur le VPS partage, les ports 80/443 de l'hote sont tenus par le proxy
partage (`odetowar-nginx`), qui sert aussi d'autres sites
(ex. `saheleye.example.com`). Pour que GEON possede son TLS sans casser les
autres sites, le proxy hote doit passer en **passthrough SNI** : il route le
TCP 443 brut selon le SNI, sans dechiffrer le trafic GEON.

```
Internet :443
   |
   v
odetowar-nginx  (stream + ssl_preread, ne dechiffre plus GEON)
   |-- SNI geon.example.com --> geon-nginx:8443  (TLS GEON, proxy_protocol)
   +-- SNI autres             --> 127.0.0.1:<port> (vhosts https locaux, inchanges)
```

## Prerequis

- `odetowar-nginx` est attache au reseau Docker `geon_net` (deja le cas — il
  joint actuellement `geon-nginx:80`).
- L'image nginx officielle embarque le module `stream` (statique) : aucun
  rebuild necessaire.
- Choisir un port local libre pour le listener https interne du proxy
  (exemples ci-dessous avec `4445` — verifier avec `ss -tln`, 8443 est
  deja pris sur ce VPS).

## 1. Bloc `stream` (dans nginx.conf, au meme niveau que `http {}`)

```nginx
stream {
    # Resolution dynamique du nom de conteneur via le DNS Docker
    resolver 127.0.0.11 valid=30s;

    map $ssl_preread_server_name $tls_upstream {
        geon.example.com  "geon-nginx:8443";
        default             "127.0.0.1:4445";
    }

    server {
        listen 443;
        ssl_preread on;
        proxy_protocol on;          # transmet l'IP cliente reelle
        proxy_pass $tls_upstream;
    }
}
```

## 2. Deplacer les vhosts https existants vers le listener interne

Dans chaque vhost qui avait `listen 443 ssl ...` (saheleye, etc.) :

```nginx
server {
    # AVANT : listen 443 ssl;
    listen 127.0.0.1:4445 ssl proxy_protocol;

    # Restaurer l'IP cliente reelle transmise par le bloc stream
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;

    # ... le reste du vhost est inchange (certs, locations, ...)
}
```

## 3. Vhost :80 de geon — challenges ACME

Le port 80 reste en mode `http`. Le vhost `geon.example.com:80` du proxy
hote doit forwarder les challenges certbot vers geon-nginx et rediriger le
reste :

```nginx
server {
    listen 80;
    server_name geon.example.com;

    location ^~ /.well-known/acme-challenge/ {
        proxy_pass http://geon-nginx:80;
        proxy_set_header Host $host;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
```

L'ancien vhost https `geon.example.com` du proxy hote (celui qui terminait
le TLS avec le cert geon et proxyfiait vers `geon-nginx:80`) peut etre
supprime : le cert est desormais gere par le sidecar certbot de GEON.

## 4. Migration (sans coupure)

```bash
# Cote GEON (compte de deploiement) — AVANT de toucher au proxy hote :
cd <repo GEON>
docker compose -f docker/docker-compose.yml up -d certbot
./scripts/init_tls.sh          # cert factice -> nginx up -> vrai cert -> reload
# Le site continue de fonctionner via l'ancien montage pendant cette etape.

# Cote proxy hote :
# 1. Appliquer les changements 1-3 ci-dessus
# 2. nginx -t puis reload
docker exec odetowar-nginx nginx -t
docker exec odetowar-nginx nginx -s reload

# Verification :
curl -sv https://geon.example.com/ -o /dev/null 2>&1 | grep -E "subject|issuer"
#  -> le certificat doit etre celui emis par le certbot GEON
curl -s  https://saheleye.example.com/ -o /dev/null -w "%{http_code}\n"
#  -> les autres sites repondent normalement
```

## Rollback

Retirer le bloc `stream`, remettre `listen 443 ssl;` dans les vhosts,
recreer le vhost https geon du proxy hote, reload. Le serveur :80 de
geon-nginx reste compatible avec l'ancien montage (X-Forwarded-Proto), donc
le rollback est immediat.

## Notes

- `proxy_protocol on` dans le bloc stream est indispensable pour conserver
  les IP clientes reelles (logs nginx, rate-limiting et anti-bruteforce
  Authelia). `geon-nginx` ne l'accepte que sur son listener 8443 ; le
  listener 443 reste en TLS classique pour le mode standalone
  (`docker-compose.standalone.yml`).
- Le renouvellement certbot passe par HTTP-01 sur le port 80 (etape 3),
  qui reste en mode http sur le proxy hote — aucun impact du passthrough.
