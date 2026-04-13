# GEON -- n8n, Elasticsearch, GDELT, OpenCTI

## Identite du projet

GEON est une plateforme d'intelligence geopolitique et cyber qui correle automatiquement les evenements diplomatiques/militaires avec l'activite des menaces cyber (APT, campagnes, IoC). Le nom est un acronyme de la stack technique (**n**8n, **E**lasticsearch, **G**DELT, **O**penCTI) et evoque la geonciation, concept central des relations internationales.

**Positionnement** : Aucun outil open source existant ne fait la convergence entre CTI structuree (OpenCTI/STIX2) et donnees geopolitiques (GDELT/ACLED). World Monitor fait du geopolitique sans CTI. Les integrations OpenCTI/Elastic existantes font de la CTI sans geopolitique. GEON comble ce gap.

**Contexte** : Projet personnel de Joran Batty, professionnel en cybersecurite (analyste SOC, administration Linux/Docker), destine a servir de portfolio pour une candidature en Master Relations Internationales. Le projet doit demontrer la capacite a croiser analyse technique et comprehension geopolitique.

---

## Architecture generale

```
Internet
   |
   v
+----------------+
|     Nginx      | :443 (TLS via Certbot / Let's Encrypt)
|  reverse proxy | :80  (redirect -> 443)
+-------+--------+
        | reseau Docker interne uniquement
        +---> /                -->  Landing page GEON (statique)
        +---> /opencti         -->  opencti:8080
        +---> /grafana         -->  grafana:3000
        +---> /n8n             -->  n8n:5678
        +---> /auth            -->  authelia:9091

+-----------------------------------------------------------+
|               Reseau interne Docker                        |
|                                                            |
|  +-----------------+  +-----------------+  +------------+  |
|  | Elasticsearch   |  |    OpenCTI      |  |    n8n     |  |
|  | (stockage,      |  |   (GraphQL,     |  | (workflow  |  |
|  |  indexation)     |  |    STIX2)       |  |  engine)   |  |
|  +--------+--------+  +--------+--------+  +-----+------+  |
|           |                    |                  |          |
|  +--------+--------+  +-------+--------+  +------+------+  |
|  |    Grafana      |  |   RabbitMQ     |  |    Redis    |  |
|  | (dashboards,    |  |  (msg broker)  |  |   (cache)   |  |
|  |  visualisation) |  +----------------+  +-------------+  |
|  +-----------------+                                        |
|                        +----------------+                   |
|                        |     MinIO      |                   |
|                        | (object store) |                   |
|                        +----------------+                   |
|                                                            |
|  +----------------------------------------------------+   |
|  |           Scripts d'ingestion Python                |   |
|  |  - GDELT ingestor (cron)                            |   |
|  |  - ACLED ingestor (cron)                            |   |
|  |  - Sanctions ingestor (cron)                        |   |
|  |  - OpenCTI exporter (cron)                          |   |
|  |  - Correlation engine (cron)                        |   |
|  +----------------------------------------------------+   |
|                                                            |
|  +-----------------+  +-----------------+                  |
|  |   Authelia      |  |   Prometheus    |                  |
|  |   (MFA)         |  |  (monitoring)   |                  |
|  +-----------------+  +-----------------+                  |
+------------------------------------------------------------+
```

**Flux de donnees** : Elasticsearch est le point central partage par trois consommateurs :
- **Grafana** interroge Elasticsearch pour les dashboards geopolitiques et CTI
- **OpenCTI** utilise Elasticsearch comme backend de stockage pour le graphe STIX2
- **Les ingestors Python** ecrivent dans Elasticsearch (index `geon-*`)
- **n8n** orchestre les workflows d'automatisation (RSS, enrichissement, webhooks)

---

## Stack technique

| Composant | Role | Image Docker |
|-----------|------|--------------|
| **Nginx** | Reverse proxy, TLS termination, Let's Encrypt | `nginx:alpine` + certbot sidecar |
| **Elasticsearch** | Stockage, indexation, recherche, agregations (partage par OpenCTI, Grafana et les ingestors) | `docker.elastic.co/elasticsearch/elasticsearch:8.x` |
| **Grafana** | Dashboards geopolitiques, visualisations, cartes, timelines, monitoring | `grafana/grafana` |
| **OpenCTI** | Graphe de connaissances CTI, relations STIX2 | `opencti/platform:latest` |
| **n8n** | Automatisation de workflows : veille RSS, enrichissement, declencheurs, webhooks | `docker.n8n.io/n8nio/n8n` |
| **Redis** | Cache pour OpenCTI | `redis:7-alpine` |
| **RabbitMQ** | Message broker pour OpenCTI | `rabbitmq:3-management-alpine` |
| **MinIO** | Stockage objet pour OpenCTI | `minio/minio` |
| **Authelia** | Authentification centralisee + MFA devant Nginx | `authelia/authelia` |
| **Prometheus** | Collecte de metriques pour le monitoring | `prom/prometheus` |
| **Certbot** | Renouvellement automatique Let's Encrypt | `certbot/certbot` |

---

## Contraintes techniques imperatives

### Rootless Docker

L'ensemble du stack DOIT tourner en Docker rootless. Raisons : securite (surface d'attaque reduite), coherence avec le positionnement secu du projet.

**Configuration requise sur le host (seules commandes root necessaires) :**
```bash
# Pour Elasticsearch
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.d/99-elasticsearch.conf

# Pour les ports privilegies (80/443)
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee -a /etc/sysctl.d/99-unprivileged-ports.conf
```

**Toute la suite (docker compose, builds, etc.) s'execute en tant qu'utilisateur non-root.**

### Reseau

- AUCUN service interne ne doit exposer de port sur l'interface publique, sauf Nginx (80/443)
- Toute communication inter-services passe par le reseau Docker interne
- Les services internes communiquent par leurs noms de service Docker (DNS interne)
- Le domaine cible est `geon.joranbatty.fr` avec un certificat Let's Encrypt

### Securite

- Authelia devant tous les services web (Grafana, OpenCTI, n8n)
- MFA active (TOTP)
- Toutes les credentials dans un fichier `.env` hors du repo (gitignore)
- Aucun mot de passe en dur dans le docker-compose ou les scripts
- Healthchecks Docker sur tous les services critiques

### Volumes et persistance

- Tous les services avec etat doivent avoir des volumes nommes Docker
- Convention de nommage : `geon_<service>_data` (ex: `geon_elasticsearch_data`)
- Un script de backup qui snapshot les index Elasticsearch et exporte la base OpenCTI

---

## Sources de donnees

### 1. GDELT (Global Database of Events, Language, and Tone)

**Role** : Source principale d'evenements geopolitiques mondiaux.

**API** : `https://api.gdeltproject.org/api/v2/`
- GDELT DOC API : articles, tonalite, themes, geolocalisation
- GDELT GEO API : evenements geolocalises
- GDELT TV API : monitoring medias TV (optionnel)

**Filtres a appliquer** :
- Categories CAMEO pertinentes : conflits armes (19x), menaces (13x), sanctions (16x), cooperation militaire (04x), diplomatie (05x, 06x)
- Filtrage geographique par pays/regions d'interet
- Score de tonalite (Goldstein scale) pour detecter les pics negatifs

**Ingestion** : Script Python avec cron toutes les 15 minutes.
- Requete l'API GDELT
- Parse et structure les evenements
- Indexe dans Elasticsearch (index `geon-gdelt-events-YYYY.MM`)
- Cree des entites dans OpenCTI si pertinent (pays, organisations)

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

**Role** : Donnees de terrain sur les conflits armes, violences politiques, manifestations.

**API** : `https://api.acleddata.com/acled/read/`
- Necessite une cle API (gratuite pour usage non-commercial)
- Evenements geolocalises avec types (batailles, violences contre civils, emeutes, etc.)

**Ingestion** : Script Python avec cron quotidien.
- Index : `geon-acled-events-YYYY.MM`

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
- EU Consolidated Sanctions : via le site du Conseil europeen
- UN Security Council Sanctions : via l'API UN

**Ingestion** : Script Python avec cron hebdomadaire.
- Index : `geon-sanctions`
- Enrichit les entites dans OpenCTI (personnes, organisations, pays sanctionnes)

### 4. Flux RSS via n8n

**Sources a agreger** (non exhaustif) :
- **Think tanks** : IRSEM, IFRI, CSIS, Brookings, Chatham House, War on the Rocks, Lawfare, The Diplomat, Carnegie Endowment, RAND
- **Agences** : Reuters, AFP, AP
- **Institutionnel** : ANSSI (alertes), CERT-FR, CISA, ENISA
- **Defense** : Revue Defense Nationale, Jane's (si accessible), Defense One
- **Regional** : Al Jazeera, SCMP, Moscow Times

**Pipeline n8n** :
1. Noeud RSS Feed Trigger -> recupere les articles a intervalles reguliers
2. Noeud Function (filtrage) -> garde uniquement les articles pertinents (mots-cles geopolitique/cyber/defense)
3. Noeud Function (extraction) -> extrait entites (pays, organisations, personnes) via regex/NLP basique
4. Noeud Elasticsearch -> pousse dans Elasticsearch (index `geon-articles-YYYY.MM`)
5. Noeud HTTP Request -> cree des entites/rapports dans OpenCTI via l'API GraphQL si c'est du CTI

### 5. OpenCTI Feeds (CTI technique)

**Connecteurs OpenCTI a activer** :
- MITRE ATT&CK
- AlienVault OTX
- Abuse IPDB
- OpenCTI Datasets (secteurs, geographie)
- CISA Known Exploited Vulnerabilities
- CVE (NVD)
- Red Flag Domains (optionnel, deja configure sur le serveur SOC)

**Export vers Elasticsearch** : Utiliser le connecteur Elastic officiel ou un script custom qui interroge l'API GraphQL d'OpenCTI et indexe dans `geon-cti-*`.

---

## Moteur de correlation

C'est le coeur de la valeur ajoutee de GEON. Un script Python (ou ensemble de scripts) qui tourne en cron et cherche des patterns inter-sources.

### Regles de correlation

**Regle 1 : Escalade diplomatique + activite APT**
- Declencheur : score Goldstein < -5 (tension forte) sur une paire de pays dans GDELT
- ET : campagne APT attribuee a l'un des deux pays dans OpenCTI dans une fenetre de +/-30 jours
- Action : creer une alerte dans `geon-correlations`, enrichir le rapport OpenCTI

**Regle 2 : Sanction + pic cyber**
- Declencheur : nouvelle sanction contre un pays/entite
- ET : augmentation > 200% des IoC lies a ce pays dans les 60 jours suivants
- Action : alerte + timeline automatique

**Regle 3 : Conflit arme + infrastructure cyber**
- Declencheur : evenement ACLED de type "bataille" ou "violence contre civils"
- ET : activite cyber attribuee a un acteur de la meme zone dans OpenCTI
- Action : alerte + correlation geographique

**Regle 4 : Changement de rhetorique**
- Declencheur : variation de tonalite GDELT > 2 ecarts-types sur 7 jours pour une paire de pays
- Action : alerte "signal faible" dans `geon-correlations`

### Index de correlation

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

### Grafana Alerting

Configurer des regles d'alerte Grafana qui se declenchent sur :
- Nouvelle entree dans `geon-correlations` avec severity >= high
- Plus de N articles negatifs (tone < -5) sur un pays en 24h
- Nouveau groupe APT detecte dans OpenCTI lie a un pays en conflit actif dans ACLED
- Nouvelle sanction ingeree

Les alertes Grafana interrogent directement Elasticsearch via la datasource configuree.

### n8n Alerting Workflows

n8n peut egalement declencher des alertes via des workflows dedies :
- Webhook entrant depuis le moteur de correlation Python
- Noeud de decision sur la severite
- Envoi vers Discord ou email selon le niveau

### Notifications

Les alertes sont envoyees via :
- **Discord webhook** (canal dedie GEON)
- **Email** (via n8n workflows ou Grafana contact points)

Format de notification :
```
[GEON ALERT] Correlation detectee
Regle: Escalade diplomatique + activite APT
Pays: Russie <-> Ukraine
Evenement diplo: Goldstein -8.3 -- "Military force deployment"
Evenement cyber: APT28 -- Campagne phishing ciblant infrastructure energetique
Fenetre: 12 jours
Dashboard: https://geon.joranbatty.fr/grafana/d/correlations
```

---

## Dashboards Grafana

Grafana se connecte a Elasticsearch en tant que datasource pour visualiser toutes les donnees indexees. Il remplace Kibana dans cette architecture, offrant un point unique pour les dashboards geopolitiques, CTI et monitoring.

### Dashboard 1 : Vue globale (landing)
- Carte mondiale avec les evenements GDELT (points) + conflits ACLED (zones) + campagnes APT (vecteurs) via le panel Geomap
- Timeline des 30 derniers jours
- Top 10 pays par nombre d'evenements
- Score de tonalite moyen par region
- Dernieres correlations detectees

### Dashboard 2 : Fiche pays
- Variable template pour selectionner un pays
- Timeline des evenements (GDELT + ACLED + CTI) pour ce pays
- Groupes APT attribues (via OpenCTI)
- Sanctions actives
- Score de risque composite
- Articles recents (RSS)

### Dashboard 3 : Correlations cyber/geopolitique
- Liste des correlations detectees par le moteur
- Filtres par severite, pays, type de regle via variables
- Vue detaillee avec timeline croisee (evenement diplo + evenement cyber sur le meme axe)

### Dashboard 4 : Veille articles
- Flux des articles ingeres via n8n
- Filtres par source, pays, theme
- Nuage de mots-cles (via panel Word cloud)
- Tendances sur 7/30 jours

### Dashboard 5 : Monitoring GEON
- Sante des services (via Prometheus datasource)
- Derniere ingestion par source (GDELT, ACLED, RSS, OpenCTI)
- Volume d'index Elasticsearch
- Alertes de monitoring

---

## Structure du repository

```
geon/
+-- CLAUDE.md                          # Ce fichier
+-- README.md                          # Documentation publique
+-- LICENSE                            # MIT ou Apache 2.0
+-- .env.example                       # Template des variables d'environnement
+-- .gitignore
|
+-- docker/
|   +-- docker-compose.yml             # Compose principal
|   +-- docker-compose.override.yml    # Overrides dev local (optionnel)
|   +-- nginx/
|   |   +-- nginx.conf                 # Config principale Nginx
|   |   +-- conf.d/
|   |   |   +-- geon.conf              # Vhost GEON avec reverse proxy
|   |   +-- ssl/                       # Certificats (gitignore, genere par certbot)
|   +-- authelia/
|   |   +-- configuration.yml
|   |   +-- users_database.yml
|   +-- elasticsearch/
|   |   +-- elasticsearch.yml
|   +-- opencti/
|   |   +-- opencti.env
|   +-- n8n/
|   |   +-- n8n.env                    # Variables d'environnement n8n
|   +-- prometheus/
|   |   +-- prometheus.yml
|   +-- grafana/
|       +-- datasources.yml            # Elasticsearch + Prometheus datasources
|       +-- dashboards.yml             # Dashboard provisioning config
|       +-- dashboards/                # Fichiers JSON de dashboards provisionnes
|
+-- ingestors/
|   +-- requirements.txt               # Dependances Python communes
|   +-- common/
|   |   +-- __init__.py
|   |   +-- es_client.py               # Client Elasticsearch partage
|   |   +-- opencti_client.py           # Client OpenCTI GraphQL partage
|   |   +-- config.py                   # Chargement .env, constantes
|   +-- gdelt/
|   |   +-- __init__.py
|   |   +-- ingestor.py                 # Script principal d'ingestion GDELT
|   |   +-- parser.py                   # Parsing des reponses GDELT
|   |   +-- mapping.json                # Mapping Elasticsearch pour l'index GDELT
|   +-- acled/
|   |   +-- __init__.py
|   |   +-- ingestor.py
|   |   +-- mapping.json
|   +-- sanctions/
|   |   +-- __init__.py
|   |   +-- ingestor.py
|   |   +-- mapping.json
|   +-- opencti_export/
|   |   +-- __init__.py
|   |   +-- exporter.py                 # Export OpenCTI -> Elasticsearch
|   |   +-- mapping.json
|   +-- correlation/
|       +-- __init__.py
|       +-- engine.py                   # Moteur de correlation principal
|       +-- rules/
|       |   +-- __init__.py
|       |   +-- diplomatic_apt.py       # Regle 1
|       |   +-- sanction_cyber.py       # Regle 2
|       |   +-- conflict_cyber.py       # Regle 3
|       |   +-- rhetoric_shift.py       # Regle 4
|       +-- alerting.py                 # Envoi des alertes (Discord, email)
|
+-- n8n/
|   +-- workflows/                      # Exports JSON des workflows n8n
|       +-- rss_think_tanks.json
|       +-- rss_agencies.json
|       +-- rss_defense.json
|       +-- enrichment_pipeline.json
|
+-- grafana/
|   +-- dashboards/                     # Exports JSON des dashboards Grafana
|   |   +-- global_overview.json
|   |   +-- country_profile.json
|   |   +-- correlations.json
|   |   +-- articles.json
|   |   +-- monitoring.json
|   +-- datasources/                    # Datasource provisioning
|       +-- setup.sh
|
+-- scripts/
|   +-- setup.sh                        # Script d'installation initial
|   +-- backup.sh                       # Backup Elasticsearch + OpenCTI
|   +-- restore.sh                      # Restauration
|   +-- crontab.example                 # Exemple de crontab pour les ingestors
|
+-- landing/                            # Page d'accueil statique GEON
|   +-- index.html
|   +-- style.css
|   +-- assets/
|       +-- logo.svg
|
+-- docs/
|   +-- architecture.md                 # Documentation architecture detaillee
|   +-- installation.md                 # Guide d'installation pas a pas
|   +-- data_sources.md                 # Documentation des sources de donnees
|   +-- correlation_rules.md            # Documentation des regles de correlation
|   +-- use_cases/
|       +-- libya_ukraine_drones.md     # Cas d'usage : affaire Libye/Ukraine
|       +-- template.md                 # Template pour nouveaux cas d'usage
|
+-- tests/
    +-- test_gdelt_parser.py
    +-- test_acled_ingestor.py
    +-- test_correlation_engine.py
    +-- fixtures/
        +-- gdelt_sample.json
        +-- acled_sample.json
        +-- opencti_sample.json
```

---

## Variables d'environnement (.env)

```bash
# === DOMAINE ===
GEON_DOMAIN=geon.joranbatty.fr
GEON_EMAIL=contact@joranbatty.fr

# === ELASTICSEARCH ===
ELASTIC_VERSION=8.17.0
ELASTIC_PASSWORD=<GENERATE>
ELASTIC_CLUSTER_NAME=geon
ELASTIC_HEAP_SIZE=2g

# === OPENCTI ===
OPENCTI_VERSION=latest
OPENCTI_ADMIN_EMAIL=admin@geon.local
OPENCTI_ADMIN_PASSWORD=<GENERATE>
OPENCTI_ADMIN_TOKEN=<GENERATE_UUID>
OPENCTI_HEALTHCHECK_KEY=<GENERATE_UUID>

# === RABBITMQ ===
RABBITMQ_DEFAULT_USER=opencti
RABBITMQ_DEFAULT_PASS=<GENERATE>

# === MINIO ===
MINIO_ROOT_USER=opencti
MINIO_ROOT_PASSWORD=<GENERATE>

# === N8N ===
N8N_ENCRYPTION_KEY=<GENERATE_32_CHARS>
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=<GENERATE>

# === GRAFANA ===
GF_SECURITY_ADMIN_USER=admin
GF_SECURITY_ADMIN_PASSWORD=<GENERATE>
GF_SERVER_ROOT_URL=https://geon.joranbatty.fr/grafana
GF_SERVER_SERVE_FROM_SUB_PATH=true

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
ALERT_EMAIL_FROM=geon@joranbatty.fr
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

## Phases de developpement

### Phase 1 -- Socle infrastructure
1. Docker Compose avec Nginx + Elasticsearch + Grafana + Authelia
2. Configuration rootless Docker
3. TLS via Certbot pour geon.joranbatty.fr
4. Verifier que tout boot proprement et que Grafana est accessible derriere Authelia
5. Landing page GEON

### Phase 2 -- OpenCTI
1. Ajouter OpenCTI + Redis + RabbitMQ + MinIO au compose
2. Configurer les connecteurs de base (MITRE ATT&CK, AlienVault, CISA KEV, OpenCTI Datasets)
3. Verifier que les donnees CTI remontent dans OpenCTI
4. Mettre en place l'export OpenCTI -> Elasticsearch

### Phase 3 -- Ingestion GDELT
1. Script Python d'ingestion GDELT
2. Mapping Elasticsearch
3. Cron toutes les 15 minutes
4. Premier dashboard Grafana (carte + timeline)

### Phase 4 -- Ingestion ACLED + Sanctions
1. Script Python ACLED
2. Script Python sanctions (OFAC, EU, UN)
3. Enrichissement croise avec OpenCTI (entites pays, organisations)

### Phase 5 -- n8n + RSS
1. Deployer n8n
2. Configurer les workflows RSS pour les think tanks et agences
3. Pipeline de filtrage et d'extraction d'entites via noeuds Function
4. Indexation dans Elasticsearch

### Phase 6 -- Moteur de correlation
1. Implementer les 4 regles de correlation
2. Index `geon-correlations`
3. Alerting Discord + email (via n8n workflows et Grafana alerting)
4. Dashboard correlations dans Grafana

### Phase 7 -- Monitoring + backup
1. Prometheus + Grafana (monitoring unifie avec les dashboards geopolitiques)
2. Dashboard monitoring GEON
3. Script de backup automatise
4. Crontab complete

### Phase 8 -- Documentation + cas d'usage
1. README.md complet
2. Documentation d'installation
3. Cas d'usage documente : affaire Libye/Ukraine/drones
4. Captures d'ecran des dashboards

---

## Conventions de code

### Python (ingestors)
- Python 3.11+
- Type hints systematiques
- Docstrings Google style
- Logging via le module `logging` (pas de print)
- Configuration via variables d'environnement (python-dotenv)
- Gestion d'erreurs robuste : retry avec backoff exponentiel pour les appels API
- Bibliotheques : `elasticsearch[async]`, `requests`, `pycti` (client OpenCTI), `python-dotenv`

### Docker
- Images Alpine quand disponibles
- Multi-stage builds si custom
- Healthchecks sur tous les services
- Labels clairs sur chaque service
- Pas de `privileged: true` ni de `network_mode: host`
- Tous les services sur un reseau bridge custom (`geon_net`)

### Elasticsearch
- Index Lifecycle Management (ILM) pour la rotation des index
- Convention de nommage : `geon-<source>-<type>-YYYY.MM`
- Alias pour les requetes : `geon-gdelt` -> pointe vers tous les `geon-gdelt-events-*`
- Mapping explicite (pas de dynamic mapping en production)
- Shards : 1 primary, 0 replica (single node)

### Git
- Commits conventionnels : `feat:`, `fix:`, `docs:`, `infra:`, `ingest:`, `corr:`
- Branches : `main` (stable), `dev` (developpement), `feature/<nom>`
- `.env` et tous les secrets dans `.gitignore`
- GitHub repo : `Jo-the-bat/GEON`

---

## Commandes utiles

```bash
# Demarrer le stack complet
docker compose -f docker/docker-compose.yml up -d

# Verifier la sante
docker compose -f docker/docker-compose.yml ps
curl -sk https://geon.joranbatty.fr/grafana/api/health | jq .

# Lancer une ingestion manuellement
cd ingestors && python -m gdelt.ingestor
cd ingestors && python -m acled.ingestor
cd ingestors && python -m correlation.engine

# Voir les logs
docker compose -f docker/docker-compose.yml logs -f --tail=100 opencti
docker compose -f docker/docker-compose.yml logs -f --tail=100 elasticsearch
docker compose -f docker/docker-compose.yml logs -f --tail=100 n8n

# Backup
./scripts/backup.sh

# Consulter les index
curl -s localhost:9200/_cat/indices?v | grep geon
```

---

## Ressources et references

- **GDELT** : https://www.gdeltproject.org/ | API Doc : https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
- **ACLED** : https://acleddata.com/ | API Doc : https://apidocs.acleddata.com/
- **OpenCTI** : https://docs.opencti.io/ | GitHub : https://github.com/OpenCTI-Platform/opencti
- **Elasticsearch** : https://www.elastic.co/guide/
- **Grafana** : https://grafana.com/docs/ | Elasticsearch datasource : https://grafana.com/docs/grafana/latest/datasources/elasticsearch/
- **n8n** : https://docs.n8n.io/ | GitHub : https://github.com/n8n-io/n8n
- **Authelia** : https://www.authelia.com/configuration/
- **Docker rootless** : https://docs.docker.com/engine/security/rootless/
- **World Monitor** (etat de l'art, concurrent) : https://worldmonitor.app | https://github.com/koala73/worldmonitor
- **PizzINT GDELT Dashboard** (etat de l'art) : https://www.pizzint.watch/gdelt
- **Elastic OpenCTI connector** : https://www.elastic.co/guide/en/integrations/current/ti_opencti.html
- **CAMEO Codes** (classification GDELT) : https://www.gdeltproject.org/data/lookups/CAMEO.eventcodes.txt
