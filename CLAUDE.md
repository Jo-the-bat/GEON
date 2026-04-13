# HEGO — Huginn, Elasticsearch, GDELT, OpenCTI

## Identité du projet

HEGO est une plateforme d'intelligence géopolitique et cyber qui corrèle automatiquement les événements diplomatiques/militaires avec l'activité des menaces cyber (APT, campagnes, IoC). Le nom est un acronyme de la stack technique (Huginn, Elasticsearch, GDELT, OpenCTI) et fait écho au grec ἡγεμονία (hégémonie).

**Positionnement** : Aucun outil open source existant ne fait la convergence entre CTI structurée (OpenCTI/STIX2) et données géopolitiques (GDELT/ACLED). World Monitor fait du géopolitique sans CTI. Les intégrations OpenCTI/Elastic existantes font de la CTI sans géopolitique. HEGO comble ce gap.

**Contexte** : Projet personnel de Joran Batty, professionnel en cybersécurité (analyste SOC, administration Linux/Docker), destiné à servir de portfolio pour une candidature en Master Relations Internationales. Le projet doit démontrer la capacité à croiser analyse technique et compréhension géopolitique.

---

## Architecture générale

```
Internet
   │
   ▼
┌──────────────┐
│    Nginx     │ :443 (TLS via Certbot / Let's Encrypt)
│  reverse     │ :80  (redirect → 443)
│  proxy       │
└──────┬───────┘
       │ réseau Docker interne uniquement
       ├──→ /                →  Landing page HEGO (statique)
       ├──→ /opencti         →  opencti:8080
       ├──→ /kibana          →  kibana:5601
       ├──→ /huginn          →  huginn:3000
       └──→ /auth            →  authelia:9091

┌─────────────────────────────────────────────────────┐
│              Réseau interne Docker                   │
│                                                      │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │ Elasticsearch│  │   OpenCTI    │  │   Huginn   │ │
│  │  + Kibana    │  │  (GraphQL)   │  │  (agents)  │ │
│  └──────┬──────┘  └──────┬───────┘  └─────┬──────┘ │
│         │                │                 │         │
│  ┌──────┴──────┐  ┌──────┴───────┐  ┌─────┴──────┐ │
│  │   Redis     │  │  RabbitMQ    │  │  MinIO     │ │
│  └─────────────┘  └──────────────┘  └────────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │           Scripts d'ingestion Python            │ │
│  │  • GDELT ingestor (cron)                        │ │
│  │  • ACLED ingestor (cron)                        │ │
│  │  • Sanctions ingestor (cron)                    │ │
│  │  • RSS aggregator → Huginn                      │ │
│  │  • Corrélation engine (cron)                    │ │
│  └─────────────────────────────────────────────────┘ │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐                 │
│  │   Authelia   │  │  Prometheus  │                 │
│  │   (MFA)      │  │  + Grafana   │                 │
│  └──────────────┘  └──────────────┘                 │
└─────────────────────────────────────────────────────┘
```

---

## Stack technique

| Composant | Rôle | Image Docker |
|-----------|------|--------------|
| **Nginx** | Reverse proxy, TLS termination, Let's Encrypt | `nginx:alpine` + certbot sidecar |
| **Elasticsearch** | Stockage, indexation, recherche, agrégations | `docker.elastic.co/elasticsearch/elasticsearch:8.x` |
| **Kibana** | Dashboards, visualisations, cartes, timelines | `docker.elastic.co/kibana/kibana:8.x` |
| **OpenCTI** | Graphe de connaissances CTI, relations STIX2 | `opencti/platform:latest` |
| **Huginn** | Agents automatisés : veille RSS, enrichissement, déclencheurs | `ghcr.io/huginn/huginn` |
| **Redis** | Cache pour OpenCTI et Huginn | `redis:7-alpine` |
| **RabbitMQ** | Message broker pour OpenCTI | `rabbitmq:3-management-alpine` |
| **MinIO** | Stockage objet pour OpenCTI | `minio/minio` |
| **Authelia** | Authentification centralisée + MFA devant Nginx | `authelia/authelia` |
| **Prometheus** | Monitoring du stack | `prom/prometheus` |
| **Grafana** | Dashboards de monitoring | `grafana/grafana` |
| **Certbot** | Renouvellement automatique Let's Encrypt | `certbot/certbot` |

---

## Contraintes techniques impératives

### Rootless Docker

L'ensemble du stack DOIT tourner en Docker rootless. Raisons : sécurité (surface d'attaque réduite), cohérence avec le positionnement sécu du projet.

**Configuration requise sur le host (seules commandes root nécessaires) :**
```bash
# Pour Elasticsearch
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.d/99-elasticsearch.conf

# Pour les ports privilégiés (80/443)
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee -a /etc/sysctl.d/99-unprivileged-ports.conf
```

**Toute la suite (docker compose, builds, etc.) s'exécute en tant qu'utilisateur non-root.**

### Réseau

- AUCUN service interne ne doit exposer de port sur l'interface publique, sauf Nginx (80/443)
- Toute communication inter-services passe par le réseau Docker interne
- Les services internes communiquent par leurs noms de service Docker (DNS interne)
- Le domaine cible est `hego.joranbatty.fr` avec un certificat Let's Encrypt

### Sécurité

- Authelia devant tous les services web (Kibana, OpenCTI, Huginn, Grafana)
- MFA activé (TOTP)
- Toutes les credentials dans un fichier `.env` hors du repo (gitignore)
- Aucun mot de passe en dur dans le docker-compose ou les scripts
- Healthchecks Docker sur tous les services critiques

### Volumes et persistance

- Tous les services avec état doivent avoir des volumes nommés Docker
- Convention de nommage : `hego_<service>_data` (ex: `hego_elasticsearch_data`)
- Un script de backup qui snapshot les index Elasticsearch et exporte la base OpenCTI

---

## Sources de données

### 1. GDELT (Global Database of Events, Language, and Tone)

**Rôle** : Source principale d'événements géopolitiques mondiaux.

**API** : `https://api.gdeltproject.org/api/v2/`
- GDELT DOC API : articles, tonalité, thèmes, géolocalisation
- GDELT GEO API : événements géolocalisés
- GDELT TV API : monitoring médias TV (optionnel)

**Filtres à appliquer** :
- Catégories CAMEO pertinentes : conflits armés (19x), menaces (13x), sanctions (16x), coopération militaire (04x), diplomatie (05x, 06x)
- Filtrage géographique par pays/régions d'intérêt
- Score de tonalité (Goldstein scale) pour détecter les pics négatifs

**Ingestion** : Script Python avec cron toutes les 15 minutes.
- Requête l'API GDELT
- Parse et structure les événements
- Indexe dans Elasticsearch (index `hego-gdelt-events-YYYY.MM`)
- Crée des entités dans OpenCTI si pertinent (pays, organisations)

**Index Elasticsearch** :
```json
{
  "event_id": "string",
  "date": "datetime",
  "source_country": "string",
  "target_country": "string",
  "cameo_code": "string",
  "cameo_description": "string",
  "goldstein_scale": "float",
  "tone": "float",
  "num_articles": "integer",
  "geo_lat": "float",
  "geo_lon": "float",
  "source_url": "string",
  "themes": ["string"],
  "persons": ["string"],
  "organizations": ["string"]
}
```

### 2. ACLED (Armed Conflict Location & Event Data)

**Rôle** : Données de terrain sur les conflits armés, violences politiques, manifestations.

**API** : `https://api.acleddata.com/acled/read/`
- Nécessite une clé API (gratuite pour usage non-commercial)
- Événements géolocalisés avec types (batailles, violences contre civils, émeutes, etc.)

**Ingestion** : Script Python avec cron quotidien.
- Index : `hego-acled-events-YYYY.MM`

**Mapping** :
```json
{
  "event_id": "integer",
  "event_date": "datetime",
  "event_type": "string",
  "sub_event_type": "string",
  "actor1": "string",
  "actor2": "string",
  "country": "string",
  "admin1": "string",
  "location": "string",
  "latitude": "float",
  "longitude": "float",
  "fatalities": "integer",
  "notes": "text",
  "source": "string"
}
```

### 3. Listes de sanctions

**Sources** :
- OFAC (US Treasury) : `https://sanctionslistservice.ofac.treas.gov/api/`
- EU Consolidated Sanctions : via le site du Conseil européen
- UN Security Council Sanctions : via l'API UN

**Ingestion** : Script Python avec cron hebdomadaire.
- Index : `hego-sanctions`
- Enrichit les entités dans OpenCTI (personnes, organisations, pays sanctionnés)

### 4. Flux RSS via Huginn

**Sources à agréger** (non exhaustif) :
- **Think tanks** : IRSEM, IFRI, CSIS, Brookings, Chatham House, War on the Rocks, Lawfare, The Diplomat, Carnegie Endowment, RAND
- **Agences** : Reuters, AFP, AP
- **Institutionnel** : ANSSI (alertes), CERT-FR, CISA, ENISA
- **Défense** : Revue Défense Nationale, Jane's (si accessible), Defense One
- **Régional** : Al Jazeera, SCMP, Moscow Times

**Pipeline Huginn** :
1. Agent RSS → récupère les articles
2. Agent de filtrage → garde uniquement les articles pertinents (mots-clés géopolitique/cyber/défense)
3. Agent d'extraction → extrait entités (pays, organisations, personnes) via regex/NLP basique
4. Agent webhook → pousse dans Elasticsearch (index `hego-articles-YYYY.MM`)
5. Agent webhook → crée des entités/rapports dans OpenCTI si c'est du CTI

### 5. OpenCTI Feeds (CTI technique)

**Connecteurs OpenCTI à activer** :
- MITRE ATT&CK
- AlienVault OTX
- Abuse IPDB
- OpenCTI Datasets (secteurs, géographie)
- CISA Known Exploited Vulnerabilities
- CVE (NVD)
- Red Flag Domains (optionnel, déjà configuré sur le serveur SOC)

**Export vers Elasticsearch** : Utiliser le connecteur Elastic officiel ou un script custom qui interroge l'API GraphQL d'OpenCTI et indexe dans `hego-cti-*`.

---

## Moteur de corrélation

C'est le cœur de la valeur ajoutée de HEGO. Un script Python (ou ensemble de scripts) qui tourne en cron et cherche des patterns inter-sources.

### Règles de corrélation

**Règle 1 : Escalade diplomatique + activité APT**
- Déclencheur : score Goldstein < -5 (tension forte) sur une paire de pays dans GDELT
- ET : campagne APT attribuée à l'un des deux pays dans OpenCTI dans une fenêtre de ±30 jours
- Action : créer une alerte dans `hego-correlations`, enrichir le rapport OpenCTI

**Règle 2 : Sanction + pic cyber**
- Déclencheur : nouvelle sanction contre un pays/entité
- ET : augmentation > 200% des IoC liés à ce pays dans les 60 jours suivants
- Action : alerte + timeline automatique

**Règle 3 : Conflit armé + infrastructure cyber**
- Déclencheur : événement ACLED de type "bataille" ou "violence contre civils"
- ET : activité cyber attribuée à un acteur de la même zone dans OpenCTI
- Action : alerte + corrélation géographique

**Règle 4 : Changement de rhétorique**
- Déclencheur : variation de tonalité GDELT > 2 écarts-types sur 7 jours pour une paire de pays
- Action : alerte "signal faible" dans `hego-correlations`

### Index de corrélation

```json
{
  "correlation_id": "string",
  "timestamp": "datetime",
  "rule_name": "string",
  "severity": "string (low|medium|high|critical)",
  "countries_involved": ["string"],
  "diplomatic_event": { "event_id": "string", "description": "string", "goldstein": "float" },
  "cyber_event": { "campaign_id": "string", "apt_group": "string", "techniques": ["string"] },
  "description": "text",
  "timeline": [{ "date": "datetime", "type": "string", "description": "string" }]
}
```

---

## Alerting

### Elasticsearch Watcher / Rules

Configurer des alertes Kibana qui se déclenchent sur :
- Nouvelle entrée dans `hego-correlations` avec severity >= high
- Plus de N articles négatifs (tone < -5) sur un pays en 24h
- Nouveau groupe APT détecté dans OpenCTI lié à un pays en conflit actif dans ACLED
- Nouvelle sanction ingérée

### Notifications

Les alertes sont envoyées via :
- **Discord webhook** (canal dédié HEGO)
- **Email** (via Huginn ou Elasticsearch Watcher)

Format de notification :
```
🔴 [HEGO ALERT] Corrélation détectée
Règle: Escalade diplomatique + activité APT
Pays: Russie ↔ Ukraine
Événement diplo: Goldstein -8.3 — "Military force deployment"
Événement cyber: APT28 — Campagne phishing ciblant infrastructure énergétique
Fenêtre: 12 jours
Dashboard: https://hego.joranbatty.fr/kibana/app/dashboards#/view/correlations
```

---

## Dashboards Kibana

### Dashboard 1 : Vue globale (landing)
- Carte mondiale avec les événements GDELT (points) + conflits ACLED (zones) + campagnes APT (vecteurs)
- Timeline des 30 derniers jours
- Top 10 pays par nombre d'événements
- Score de tonalité moyen par région
- Dernières corrélations détectées

### Dashboard 2 : Fiche pays
- Sélecteur de pays
- Timeline des événements (GDELT + ACLED + CTI) pour ce pays
- Groupes APT attribués (via OpenCTI)
- Sanctions actives
- Score de risque composite
- Articles récents (RSS)

### Dashboard 3 : Corrélations cyber/géopolitique
- Liste des corrélations détectées par le moteur
- Filtres par sévérité, pays, type de règle
- Vue détaillée avec timeline croisée (événement diplo + événement cyber sur le même axe)

### Dashboard 4 : Veille articles
- Flux des articles ingérés via Huginn
- Filtres par source, pays, thème
- Nuage de mots-clés
- Tendances sur 7/30 jours

### Dashboard 5 : Monitoring HEGO
- Santé des services (via Prometheus)
- Dernière ingestion par source (GDELT, ACLED, RSS, OpenCTI)
- Volume d'index Elasticsearch
- Alertes de monitoring

---

## Structure du repository

```
hego/
├── CLAUDE.md                          # Ce fichier
├── README.md                          # Documentation publique
├── LICENSE                            # MIT ou Apache 2.0
├── .env.example                       # Template des variables d'environnement
├── .gitignore
│
├── docker/
│   ├── docker-compose.yml             # Compose principal
│   ├── docker-compose.override.yml    # Overrides dev local (optionnel)
│   ├── nginx/
│   │   ├── nginx.conf                 # Config principale Nginx
│   │   ├── conf.d/
│   │   │   └── hego.conf              # Vhost HEGO avec reverse proxy
│   │   └── ssl/                       # Certificats (gitignore, généré par certbot)
│   ├── authelia/
│   │   ├── configuration.yml
│   │   └── users_database.yml
│   ├── elasticsearch/
│   │   └── elasticsearch.yml
│   ├── kibana/
│   │   └── kibana.yml
│   ├── opencti/
│   │   └── opencti.env
│   ├── huginn/
│   │   └── .env
│   ├── prometheus/
│   │   └── prometheus.yml
│   └── grafana/
│       └── datasources.yml
│
├── ingestors/
│   ├── requirements.txt               # Dépendances Python communes
│   ├── common/
│   │   ├── __init__.py
│   │   ├── es_client.py               # Client Elasticsearch partagé
│   │   ├── opencti_client.py           # Client OpenCTI GraphQL partagé
│   │   └── config.py                   # Chargement .env, constantes
│   ├── gdelt/
│   │   ├── __init__.py
│   │   ├── ingestor.py                 # Script principal d'ingestion GDELT
│   │   ├── parser.py                   # Parsing des réponses GDELT
│   │   └── mapping.json                # Mapping Elasticsearch pour l'index GDELT
│   ├── acled/
│   │   ├── __init__.py
│   │   ├── ingestor.py
│   │   └── mapping.json
│   ├── sanctions/
│   │   ├── __init__.py
│   │   ├── ingestor.py
│   │   └── mapping.json
│   ├── opencti_export/
│   │   ├── __init__.py
│   │   ├── exporter.py                 # Export OpenCTI → Elasticsearch
│   │   └── mapping.json
│   └── correlation/
│       ├── __init__.py
│       ├── engine.py                   # Moteur de corrélation principal
│       ├── rules/
│       │   ├── __init__.py
│       │   ├── diplomatic_apt.py       # Règle 1
│       │   ├── sanction_cyber.py       # Règle 2
│       │   ├── conflict_cyber.py       # Règle 3
│       │   └── rhetoric_shift.py       # Règle 4
│       └── alerting.py                 # Envoi des alertes (Discord, email)
│
├── kibana/
│   ├── dashboards/                     # Exports JSON des dashboards Kibana
│   │   ├── global_overview.ndjson
│   │   ├── country_profile.ndjson
│   │   ├── correlations.ndjson
│   │   ├── articles.ndjson
│   │   └── monitoring.ndjson
│   └── index_patterns/                 # Index patterns pré-configurés
│       └── setup.sh
│
├── huginn/
│   └── scenarios/                      # Exports JSON des scénarios Huginn
│       ├── rss_think_tanks.json
│       ├── rss_agencies.json
│       ├── rss_defense.json
│       └── enrichment_pipeline.json
│
├── scripts/
│   ├── setup.sh                        # Script d'installation initial
│   ├── backup.sh                       # Backup Elasticsearch + OpenCTI
│   ├── restore.sh                      # Restauration
│   └── crontab.example                 # Exemple de crontab pour les ingestors
│
├── landing/                            # Page d'accueil statique HEGO
│   ├── index.html
│   ├── style.css
│   └── assets/
│       └── logo.svg
│
├── docs/
│   ├── architecture.md                 # Documentation architecture détaillée
│   ├── installation.md                 # Guide d'installation pas à pas
│   ├── data_sources.md                 # Documentation des sources de données
│   ├── correlation_rules.md            # Documentation des règles de corrélation
│   └── use_cases/
│       ├── libya_ukraine_drones.md     # Cas d'usage : affaire Libye/Ukraine
│       └── template.md                 # Template pour nouveaux cas d'usage
│
└── tests/
    ├── test_gdelt_parser.py
    ├── test_acled_ingestor.py
    ├── test_correlation_engine.py
    └── fixtures/
        ├── gdelt_sample.json
        ├── acled_sample.json
        └── opencti_sample.json
```

---

## Variables d'environnement (.env)

```bash
# === DOMAINE ===
HEGO_DOMAIN=hego.joranbatty.fr
HEGO_EMAIL=contact@joranbatty.fr

# === ELASTICSEARCH ===
ELASTIC_VERSION=8.17.0
ELASTIC_PASSWORD=<GENERATE>
ELASTIC_CLUSTER_NAME=hego
ELASTIC_HEAP_SIZE=2g
KIBANA_ENCRYPTION_KEY=<GENERATE_32_CHARS>

# === OPENCTI ===
OPENCTI_VERSION=latest
OPENCTI_ADMIN_EMAIL=admin@hego.local
OPENCTI_ADMIN_PASSWORD=<GENERATE>
OPENCTI_ADMIN_TOKEN=<GENERATE_UUID>
OPENCTI_HEALTHCHECK_KEY=<GENERATE_UUID>

# === RABBITMQ ===
RABBITMQ_DEFAULT_USER=opencti
RABBITMQ_DEFAULT_PASS=<GENERATE>

# === MINIO ===
MINIO_ROOT_USER=opencti
MINIO_ROOT_PASSWORD=<GENERATE>

# === HUGINN ===
HUGINN_DATABASE_PASSWORD=<GENERATE>
HUGINN_INVITATION_CODE=<GENERATE>

# === AUTHELIA ===
AUTHELIA_JWT_SECRET=<GENERATE>
AUTHELIA_SESSION_SECRET=<GENERATE>
AUTHELIA_STORAGE_ENCRYPTION_KEY=<GENERATE>

# === ACLED ===
ACLED_API_KEY=<YOUR_ACLED_KEY>
ACLED_EMAIL=<YOUR_ACLED_EMAIL>

# === ALERTING ===
DISCORD_WEBHOOK_URL=<YOUR_DISCORD_WEBHOOK>
ALERT_EMAIL_SMTP_HOST=<SMTP_HOST>
ALERT_EMAIL_SMTP_PORT=587
ALERT_EMAIL_FROM=hego@joranbatty.fr
ALERT_EMAIL_TO=joran@joranbatty.fr
ALERT_EMAIL_PASSWORD=<SMTP_PASSWORD>

# === OPENCTI CONNECTORS ===
MITRE_CONNECTOR_ID=<GENERATE_UUID>
ALIENVAULT_API_KEY=<YOUR_OTX_KEY>
ALIENVAULT_CONNECTOR_ID=<GENERATE_UUID>
ABUSEIPDB_API_KEY=<YOUR_ABUSEIPDB_KEY>
ABUSEIPDB_CONNECTOR_ID=<GENERATE_UUID>
CISA_KEV_CONNECTOR_ID=<GENERATE_UUID>
CVE_CONNECTOR_ID=<GENERATE_UUID>
OPENCTI_DATASETS_CONNECTOR_ID=<GENERATE_UUID>
```

---

## Phases de développement

### Phase 1 — Socle infrastructure
1. Docker Compose avec Nginx + Elasticsearch + Kibana + Authelia
2. Configuration rootless Docker
3. TLS via Certbot pour hego.joranbatty.fr
4. Vérifier que tout boot proprement et que Kibana est accessible derrière Authelia
5. Landing page HEGO

### Phase 2 — OpenCTI
1. Ajouter OpenCTI + Redis + RabbitMQ + MinIO au compose
2. Configurer les connecteurs de base (MITRE ATT&CK, AlienVault, CISA KEV, OpenCTI Datasets)
3. Vérifier que les données CTI remontent dans OpenCTI
4. Mettre en place l'export OpenCTI → Elasticsearch

### Phase 3 — Ingestion GDELT
1. Script Python d'ingestion GDELT
2. Mapping Elasticsearch
3. Cron toutes les 15 minutes
4. Premier dashboard Kibana (carte + timeline)

### Phase 4 — Ingestion ACLED + Sanctions
1. Script Python ACLED
2. Script Python sanctions (OFAC, EU, UN)
3. Enrichissement croisé avec OpenCTI (entités pays, organisations)

### Phase 5 — Huginn + RSS
1. Déployer Huginn
2. Configurer les agents RSS pour les think tanks et agences
3. Pipeline de filtrage et d'extraction d'entités
4. Indexation dans Elasticsearch

### Phase 6 — Moteur de corrélation
1. Implémenter les 4 règles de corrélation
2. Index `hego-correlations`
3. Alerting Discord + email
4. Dashboard corrélations dans Kibana

### Phase 7 — Monitoring + backup
1. Prometheus + Grafana
2. Dashboard monitoring HEGO
3. Script de backup automatisé
4. Crontab complète

### Phase 8 — Documentation + cas d'usage
1. README.md complet
2. Documentation d'installation
3. Cas d'usage documenté : affaire Libye/Ukraine/drones
4. Captures d'écran des dashboards

---

## Conventions de code

### Python (ingestors)
- Python 3.11+
- Type hints systématiques
- Docstrings Google style
- Logging via le module `logging` (pas de print)
- Configuration via variables d'environnement (python-dotenv)
- Gestion d'erreurs robuste : retry avec backoff exponentiel pour les appels API
- Bibliothèques : `elasticsearch[async]`, `requests`, `pycti` (client OpenCTI), `python-dotenv`

### Docker
- Images Alpine quand disponibles
- Multi-stage builds si custom
- Healthchecks sur tous les services
- Labels clairs sur chaque service
- Pas de `privileged: true` ni de `network_mode: host`
- Tous les services sur un réseau bridge custom (`hego_net`)

### Elasticsearch
- Index Lifecycle Management (ILM) pour la rotation des index
- Convention de nommage : `hego-<source>-<type>-YYYY.MM`
- Alias pour les requêtes : `hego-gdelt` → pointe vers tous les `hego-gdelt-events-*`
- Mapping explicite (pas de dynamic mapping en production)
- Shards : 1 primary, 0 replica (single node)

### Git
- Commits conventionnels : `feat:`, `fix:`, `docs:`, `infra:`, `ingest:`, `corr:`
- Branches : `main` (stable), `dev` (développement), `feature/<nom>`
- `.env` et tous les secrets dans `.gitignore`
- GitHub repo : sous le compte `Jo-the-bat`

---

## Commandes utiles

```bash
# Démarrer le stack complet
docker compose -f docker/docker-compose.yml up -d

# Vérifier la santé
docker compose -f docker/docker-compose.yml ps
curl -sk https://hego.joranbatty.fr/kibana/api/status | jq .status

# Lancer une ingestion manuellement
cd ingestors && python -m gdelt.ingestor
cd ingestors && python -m acled.ingestor
cd ingestors && python -m correlation.engine

# Voir les logs
docker compose -f docker/docker-compose.yml logs -f --tail=100 opencti
docker compose -f docker/docker-compose.yml logs -f --tail=100 elasticsearch

# Backup
./scripts/backup.sh

# Consulter les index
curl -s localhost:9200/_cat/indices?v | grep hego
```

---

## Ressources et références

- **GDELT** : https://www.gdeltproject.org/ | API Doc : https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- **ACLED** : https://acleddata.com/ | API Doc : https://apidocs.acleddata.com/
- **OpenCTI** : https://docs.opencti.io/ | GitHub : https://github.com/OpenCTI-Platform/opencti
- **Elastic/Kibana** : https://www.elastic.co/guide/
- **Huginn** : https://github.com/huginn/huginn
- **Authelia** : https://www.authelia.com/configuration/
- **Docker rootless** : https://docs.docker.com/engine/security/rootless/
- **World Monitor** (état de l'art, concurrent) : https://worldmonitor.app | https://github.com/koala73/worldmonitor
- **PizzINT GDELT Dashboard** (état de l'art) : https://www.pizzint.watch/gdelt
- **Elastic OpenCTI connector** : https://www.elastic.co/guide/en/integrations/current/ti_opencti.html
- **CAMEO Codes** (classification GDELT) : https://www.gdeltproject.org/data/lookups/CAMEO.eventcodes.txt
