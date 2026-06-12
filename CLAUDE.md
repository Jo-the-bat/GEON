# GEON -- n8n, Elasticsearch, GDELT, OpenCTI

## Identite du projet

GEON est une plateforme d'intelligence geopolitique et cyber qui correle automatiquement les evenements diplomatiques/militaires avec l'activite des menaces cyber (APT, campagnes, IoC). Le nom est un acronyme de la stack technique (**n**8n, **E**lasticsearch, **G**DELT, **O**penCTI) et evoque la geonciation, concept central des relations internationales.

**Positionnement** : Aucun outil open source existant ne fait la convergence entre CTI structuree (OpenCTI/STIX2) et donnees geopolitiques (GDELT/ACLED). World Monitor fait du geopolitique sans CTI. Les integrations OpenCTI/Elastic existantes font de la CTI sans geopolitique. GEON comble ce gap.

**Contexte** : Projet personnel de GEON, professionnel en cybersecurite (analyste SOC, administration Linux/Docker), destine a servir de portfolio pour une candidature en Master Relations Internationales. Le projet doit demontrer la capacite a croiser analyse technique et comprehension geopolitique.

---

## Architecture generale

```
Internet
   |
   v
+---------------------------+
|   Reverse proxy HOTE      |  :443 passthrough SNI (ssl_preread) --
|   (odetowar-nginx,        |  ne dechiffre PAS le trafic GEON
|    hors de ce compose)    |  :80 http -> ACME geon + autres sites
+------------+--------------+
             | TCP 443 brut + proxy_protocol, reseau externe `geon_net`
             v
+----------------+
|   Nginx GEON   |  TLS termine ICI (certs Let's Encrypt du sidecar certbot)
| (TLS + forward |  :8443 ssl proxy_protocol  <- passthrough du proxy hote
|  auth Authelia)|  :443  ssl                 <- mode standalone (hote dedie)
|                |  :80   ACME challenges + compat ancien montage
+-------+--------+
        +---> /                -->  Landing page GEON (statique)
        +---> /opencti         -->  opencti:8080
        +---> /grafana         -->  grafana:3000
        +---> /n8n             -->  n8n:5678
        +---> /auth            -->  authelia:9091

+-----------------------------------------------------------+
|               Reseau `geon_net` (externe)                  |
|                                                            |
|  +-----------------+  +-----------------+  +------------+  |
|  | Elasticsearch   |  |    OpenCTI      |  |    n8n     |  |
|  | (stockage,      |  | (+ 3 workers,   |  | (workflow  |  |
|  |  indexation)    |  |  5 connecteurs) |  |  engine)   |  |
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
|  |      Conteneur geon-ingestor (scheduler.py)        |   |
|  |  GDELT, GKG, ACLED, Sanctions, OpenCTI export,     |   |
|  |  Polymarket, Cloudflare Radar, consensus           |   |
|  |  predictif, SIPRI, risk scores,                    |   |
|  |  moteur de correlation (10 regles)                 |   |
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
| **Nginx (GEON)** | Termine le TLS de GEON (certs Let's Encrypt), forward-auth Authelia, routage des sous-chemins. Listeners : 8443 (proxy_protocol, cible du passthrough SNI du proxy hote), 443 (standalone), 80 (ACME + compat) | `nginx:alpine` |
| **Certbot** | Sidecar : emission et renouvellement des certificats Let's Encrypt (webroot HTTP-01 via geon-nginx:80). Premiere emission via `scripts/init_tls.sh` | `certbot/certbot` |
| **Elasticsearch** | Stockage, indexation, recherche, agregations (partage par OpenCTI, Grafana et les ingestors) | `docker.elastic.co/elasticsearch/elasticsearch:${ELASTIC_VERSION}` (8.17.0) |
| **Grafana** | Dashboards geopolitiques, visualisations, cartes, timelines, monitoring. Auth anonyme (role Admin) derriere Authelia, formulaire de login desactive | `grafana/grafana:10.4.15` |
| **OpenCTI** | Graphe de connaissances CTI, relations STIX2 + 3 workers + 5 connecteurs | `opencti/platform` + `opencti/worker` + `opencti/connector-*` |
| **n8n** | Automatisation de workflows : veille RSS, enrichissement, declencheurs, webhooks | `docker.n8n.io/n8nio/n8n:1.70.3` |
| **Redis** | Cache pour OpenCTI | `redis:7-alpine` |
| **RabbitMQ** | Message broker pour OpenCTI | `rabbitmq:3-management-alpine` |
| **MinIO** | Stockage objet pour OpenCTI | `minio/minio` |
| **Authelia** | Authentification centralisee + MFA devant Nginx | `authelia/authelia` |
| **Prometheus** | Collecte de metriques pour le monitoring | `prom/prometheus` |
| **Ingestor** | Conteneur unique executant tous les ingestors Python + moteur de correlation via `scheduler.py` (PID 1) | build local `ingestors/Dockerfile` (`python:3.11-slim`) |

---

## Contraintes techniques imperatives

### Rootless Docker

L'ensemble du stack DOIT tourner en Docker rootless. Raisons : securite (surface d'attaque reduite), coherence avec le positionnement secu du projet.

**Configuration requise sur le host (seules commandes root necessaires) :**
```bash
# Pour Elasticsearch
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.d/99-elasticsearch.conf
```

Note : le compose GEON ne publie aucun port sur l'hote ; `net.ipv4.ip_unprivileged_port_start=80` n'est necessaire que pour le reverse proxy hote s'il tourne lui-meme en rootless.

**Toute la suite (docker compose, builds, etc.) s'execute en tant qu'utilisateur non-root.**

### Reseau

- GEON termine son propre TLS : `geon-nginx` ecoute en 443 (TLS direct) et 8443 (TLS + proxy_protocol), certificats Let's Encrypt geres par le sidecar `geon-certbot` (renouvellement automatique, reload nginx toutes les 6h)
- Sur le VPS partage, le compose ne publie AUCUN port : le proxy hote (`odetowar-nginx`, hors de ce repo) fait du passthrough SNI (`ssl_preread`) et forwarde le TCP 443 brut vers `geon-nginx:8443` avec proxy_protocol (conservation des IP clientes). Patch du proxy hote : `docs/host_proxy_sni_passthrough.md`
- Sur un hote dedie : `docker-compose.standalone.yml` publie 80/443 et GEON est totalement autonome
- `geon_net` est un reseau Docker EXTERNE (`docker network create geon_net` avant le premier `up`), partage avec le proxy hote
- Le Nginx GEON fait le forward-auth Authelia et route les sous-chemins (`/`, `/opencti`, `/grafana`, `/n8n`, `/auth`) ; les blocs location sont factorises dans `conf.d/geon-locations.inc`
- Les services internes communiquent par leurs noms de service Docker (DNS interne)

### Securite

- Authelia devant tous les services web (Grafana, OpenCTI, n8n)
- MFA active (TOTP)
- Toutes les credentials dans un fichier `.env` hors du repo (gitignore)
- Aucun mot de passe en dur dans le docker-compose ou les scripts
- Healthchecks Docker sur tous les services critiques

### Volumes et persistance

- Tous les services avec etat ont des volumes nommes Docker
- Convention de nommage : `geon_<service>_data` ; les volumes historiques sont declares `external: true` dans le compose et mappes sur les noms `docker_geon_<service>_data` (heritage d'un rename du projet — ne pas "corriger"). Les volumes TLS (`geon_certbot_certs`, `geon_certbot_webroot`) sont posterieurs au rename et geres normalement par compose
- `scripts/backup.sh` snapshot les index Elasticsearch et exporte la base OpenCTI

---

## Conteneur ingestor et ordonnancement

Tous les ingestors Python et le moteur de correlation tournent dans un conteneur unique `geon-ingestor` (build local `ingestors/Dockerfile`). `scheduler.py` tourne en PID 1 et ordonnance les jobs avec la bibliotheque `schedule`. Il n'y a PAS de cron host (`scripts/crontab.example` est un vestige documentaire).

| Job | Frequence |
|-----|-----------|
| GDELT Events | toutes les 15 min |
| GDELT GKG | toutes les 15 min |
| Export OpenCTI -> ES | toutes les heures |
| ACLED | quotidien 03:00 |
| Sanctions (OFAC + EU + UN) | hebdomadaire, dimanche 04:00 |
| Moteur de correlation | toutes les 30 min |
| Polymarket | toutes les heures |
| Polymarket enrichissement | toutes les 2 h |
| Cloudflare Radar | toutes les 30 min |
| Consensus predictif (Metaculus/Manifold) | toutes les 2 h |
| SIPRI | hebdomadaire, lundi 02:00 |
| Risk scores | quotidien 05:00 |

Chaque job (sauf l'enrichissement Polymarket) est aussi execute une fois au demarrage du conteneur. Mode seed : `python scheduler.py --seed N` ingere N jours d'historique GDELT + ACLED avant de demarrer le cron.

Apres toute modification du code Python, rebuilder l'image : `docker compose -f docker/docker-compose.yml build ingestor && docker compose -f docker/docker-compose.yml up -d ingestor`.

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

**Ingestion** : GDELT v2 Events Export CSV (pas DOC API). Cron toutes les 15 minutes.
- Telecharge le dernier fichier CSV ZIP depuis `data.gdeltproject.org/gdeltv2/lastupdate.txt`
- Parse les 61 colonnes tab-separated (CAMEO codes, Goldstein, acteurs, geo, tone)
- Filtre par CAMEO codes pertinents (04-06, 13-20)
- Resout les codes pays CAMEO/ISO vers noms lisibles (150 pays)
- Indexe dans Elasticsearch (index `geon-gdelt-events-YYYY.MM`)

**Index Elasticsearch** :
```json
{
  "event_id": "string",
  "date": "datetime",
  "source_country": "string",
  "target_country": "string",
  "actor1_name": "string",
  "actor1_country": "string",
  "actor1_type": "string",
  "actor2_name": "string",
  "actor2_country": "string",
  "actor2_type": "string",
  "quad_class": "integer (1=Verbal Coop, 2=Material Coop, 3=Verbal Conflict, 4=Material Conflict)",
  "cameo_code": "string",
  "cameo_description": "string",
  "goldstein_scale": "float",
  "tone": "float",
  "num_articles": "integer",
  "geo_lat": "float",
  "geo_lon": "float",
  "geo_location": "geo_point",
  "source_url": "string",
  "severity": "string (low|medium|high|critical)"
}
```

### 1b. GDELT GKG (Global Knowledge Graph)

**Role** : Enrichissement thematique des evenements — themes, personnes, organisations, analyse de tonalite detaillee, GCAM.

**Source** : Fichier GKG CSV ZIP depuis `data.gdeltproject.org/gdeltv2/lastupdate.txt` (3e fichier liste).

**Ingestion** : Cron toutes les 15 minutes, juste apres les Events.

**Index** : `geon-gkg-YYYY.MM`

**Champs** : date, source_url, source_name, themes, persons, organizations, locations (nested avec lat/lon), tone (6 composantes), gcam_scores

### 2. ACLED (Armed Conflict Location & Event Data)

**Role** : Donnees de terrain sur les conflits armes, violences politiques, manifestations.

**API** : `https://api.acleddata.com/acled/read/`
- Necessite une cle API (gratuite pour usage non-commercial)
- Evenements geolocalises avec types (batailles, violences contre civils, emeutes, etc.)

**Ingestion** : Cron quotidien (03:00), incremental.
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

**Sources** (les trois sont implementees dans `sanctions/ingestor.py`) :
- OFAC SDN (US Treasury) : `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML`
- EU Consolidated Financial Sanctions (FSF) : `https://webgate.ec.europa.eu/fsd/fsf/public/files/` — attention, dans ce XML `wholeName` est un ATTRIBUT, pas un element
- UN Security Council Consolidated List : `https://scsanctions.un.org/resources/xml/en/consolidated.xml`

**Ingestion** : Cron hebdomadaire (dimanche 04:00).
- Index : `geon-sanctions`
- Enrichit les entites dans OpenCTI (organisations sanctionnees) si OpenCTI est disponible

### 4. Flux RSS via n8n

5 workflows generes par `n8n/workflows/generate_workflows.py` (la liste a jour des sources et URLs vit dans ce script). Voir `n8n/WORKFLOWS.md` pour l'import via le CLI n8n, le tableau des feeds RSS morts et leurs remplacements (beaucoup de sources passent par un proxy Google News).

| Workflow | Sources | Fichier |
|----------|---------|---------|
| News Agencies | Reuters, France 24, AP News, BBC World | `rss_agencies.json` |
| Think Tanks | IRSEM, IFRI, CSIS, Brookings, Chatham House, Carnegie, RAND | `rss_think_tanks.json` |
| Defense & Security | War on the Rocks, Lawfare, Defense One, The Diplomat, CERT-FR | `rss_defense.json` |
| Regional Media | Al Jazeera, SCMP, Moscow Times, The Africa Report, Middle East Eye | `rss_regional.json` |
| World News | BBC World (toutes les 30 min) | `rss_world_news.json` |

**Pipeline n8n** (identique pour chaque workflow) :
```
Schedule Trigger (1 h) -> RSS Feed Read (par source) -> Set (tag source)
  -> Filter Geopolitical (noeud Code, filtre par mots-cles)
  -> Index to ES (HTTP Request, auth via $env.ELASTIC_PASSWORD)
```

- Index : `geon-articles-YYYY.MM` (mapping de reference : `ingestors/articles/mapping.json`)
- Filtre : au moins un mot-cle parmi war, conflict, sanctions, military, nato, cyber attack, apt, etc. (liste complete dans `WORKFLOWS.md`)
- Workflows additionnels (alertes Discord, enrichissement OpenCTI) : setup manuel, voir `WORKFLOWS.md`

### 5. OpenCTI Feeds (CTI technique)

**Connecteurs OpenCTI deployes (dans le compose)** :
- MITRE ATT&CK
- AlienVault OTX
- CISA Known Exploited Vulnerabilities
- CVE (NVD)
- OpenCTI Datasets (secteurs, geographie, companies)

(AbuseIPDB : variables `.env` reservees mais connecteur non deploye.)

**Export vers Elasticsearch** : Script Python `opencti_export/exporter.py` qui interroge l'API GraphQL pycti et indexe intrusion sets, campaigns, malware, indicators dans `geon-cti-threats`, `geon-cti-campaigns`, `geon-cti-indicators`. Cron horaire (incremental ; `--full` pour un export complet).

### 6. Polymarket (Marches predictifs geopolitiques)

**Role** : Dimension predictive — les marches de prediction refletent le consensus des participants sur la probabilite d'evenements geopolitiques futurs.

**API** : `https://gamma-api.polymarket.com/markets`

**Ingestion** : Cron horaire.
- Filtre les marches geopolitiques par tags et mots-cles (war, sanctions, election, NATO, etc.)
- Exclut sports, crypto, entertainment
- Extrait les pays impliques depuis la question
- Indexe dans `geon-polymarket-cases`

**Enrichissement** : Cron toutes les 2h.
- Pour chaque case active, requete GDELT, correlations, sanctions, APT pour les pays concernes
- Detection de mouvements significatifs (>10% en 24h) → alerte dans `geon-correlations`

### 7. Score de risque composite par pays

**Module** : `risk_score/calculator.py`

Score 0-100 par pays, agregant 7 facteurs ponderes :
- Evenements GDELT negatifs (Goldstein < 0, 30j) — 25%
- Conflits ACLED (si disponible) — 15%
- Sanctions actives — 10%
- Groupes APT attribues (via `country_apt_mapping.json`) — 15%
- Correlations detectees — 20%
- Hausse depenses militaires (YoY) — 10%
- Importations d'armes recentes (TIV) — 5%

Index : `geon-risk-scores` (1 doc/pays, mis a jour quotidiennement a 05:00)

### 8. Cloudflare Radar (Coupures internet)

**Role** : Detection des coupures internet par pays — indicateur de censure, conflit, ou instabilite.

**API** : `https://api.cloudflare.com/client/v4/radar/annotations/outages`
- Necessite un token API Cloudflare (gratuit, permissions Radar:Read)
- `CLOUDFLARE_RADAR_TOKEN` dans `.env`

**Ingestion** : Cron toutes les 30 minutes.
- Index : `geon-outages`

**Champs** : outage_id, date, country, country_code, asn, asn_name, type (country-level|asn-level|region), scope (national|regional|local), duration_hours, severity (partial|major|total), status (ongoing|resolved), start_time, end_time

### 9. Metaculus + Manifold Markets (Consensus predictif)

**Role** : Croisement de plateformes de prediction pour detecter les divergences de consensus.

**APIs** :
- Manifold Markets (public) : `https://api.manifold.markets/v0/search-markets`
- Metaculus (token requis) : `https://www.metaculus.com/api/questions/`
- `METACULUS_API_TOKEN` dans `.env`

**Ingestion** : Cron toutes les 2 heures.
- Index marches externes : `geon-predictions`
- Enrichissement des cases Polymarket existantes avec objet `consensus` (polymarket_yes, metaculus_median, manifold_yes, consensus_score, divergence, platforms_count)
- Alerte de type `prediction_divergence` dans `geon-correlations` si divergence > 0.15

### 10. SIPRI (Transferts d'armes et depenses militaires)

**Role** : Donnees strategiques sur les flux d'armement et l'evolution des budgets de defense.

**Donnees** : Pas d'API publique. Donnees embarquees dans `sipri/ingestor.py` (seed cure depuis les bases SIPRI) + mise a jour via CSV optionnels dans `ingestors/sipri/data/` (repertoire non versionne, lu s'il existe).
- Sources : SIPRI Arms Transfers Database, SIPRI Military Expenditure Database

**Ingestion** : Cron hebdomadaire (lundi 02:00).
- Index transferts : `geon-arms-transfers` (supplier_country, recipient_country, weapon_type, tiv_value, etc.)
- Index depenses : `geon-military-spending` (country, spending_usd_millions, spending_pct_gdp, spending_change_yoy_pct)

---

## Moteur de correlation

C'est le coeur de la valeur ajoutee de GEON. Le moteur (`correlation/engine.py`) tourne toutes les 30 minutes dans le conteneur ingestor :

1. Execute les 10 regles (chaque regle est isolee — une exception n'arrete pas les autres)
2. Deduplique par `correlation_id` (mget contre l'index existant)
3. Indexe les nouvelles correlations dans `geon-correlations`
4. Alerte (Discord + email) pour les correlations de severite >= high

CLI : `python -m correlation.engine [--rules 1 2 ...] [--dry-run]`

Regles ES uniquement : 4, 5, 7, 8, 10. Regles necessitant OpenCTI : 1, 2, 3, 6, 9 (degradent proprement si OpenCTI est indisponible). Les regles APT (1, 6, 9) s'appuient sur `common/country_apt_mapping.json` (attribution pays -> groupes APT, validation stricte pour eviter la cross-contamination).

### Regles de correlation

**Regle 1 : Escalade diplomatique + activite APT** (`diplomatic_escalation_apt`)
- Declencheur : score Goldstein < -5 (tension forte) sur une paire de pays dans GDELT
- ET : campagne APT attribuee a l'un des deux pays dans OpenCTI dans une fenetre de +/-30 jours
- Action : creer une alerte dans `geon-correlations`, enrichir le rapport OpenCTI

**Regle 2 : Sanction + pic cyber** (`sanction_cyber_spike`)
- Declencheur : nouvelle sanction contre un pays/entite
- ET : augmentation > 200% des IoC lies a ce pays dans les 60 jours suivants
- Action : alerte + timeline automatique

**Regle 3 : Conflit arme + infrastructure cyber** (`conflict_cyber_infrastructure`)
- Declencheur : evenement ACLED de type "bataille" ou "violence contre civils"
- ET : activite cyber attribuee a un acteur de la meme zone dans OpenCTI
- Action : alerte + correlation geographique

**Regle 4 : Changement de rhetorique** (`rhetoric_shift`)
- Declencheur : variation de tonalite GDELT > 2 ecarts-types sur 7 jours pour une paire de pays
- Action : alerte "signal faible" dans `geon-correlations`

**Regle 5 : Coupure internet + escalade diplomatique/militaire** (`internet_outage_escalation`)
- Declencheur : coupure internet nationale ou majeure dans `geon-outages`
- ET : evenements GDELT avec Goldstein < -5 ou conflits ACLED dans le meme pays dans ±48h
- Action : correlation `internet_outage_escalation` dans `geon-correlations`
- Severite : critical si coupure totale + conflit, high si partielle + tension

**Regle 6 : Hausse depenses militaires + activite APT** (`military_buildup_cyber`)
- Declencheur : pays avec spending_change_yoy_pct > 10% dans `geon-military-spending`
- ET : groupe APT attribue a ce pays actif dans OpenCTI
- Action : correlation `military_buildup_cyber` dans `geon-correlations`
- Correlation lente (donnees annuelles) mais strategiquement pertinente

**Regle 7 : Transfert d'armes + escalade regionale** (`arms_transfer_escalation`)
- Declencheur : livraison d'armes recente dans `geon-arms-transfers`
- ET : hausse > 50% des evenements GDELT negatifs (Goldstein < -3) impliquant le destinataire et ses voisins (`common/country_neighbors.json`) sur une fenetre de 90 jours
- Action : correlation `arms_transfer_escalation` dans `geon-correlations`

**Regle 8 : Mouvement de marche predictif + evenement reel** (`prediction_event_match`)
- Declencheur : mouvement de prix Polymarket > 10% en 72h
- ET : evenement GDELT de forte severite (|Goldstein| > 7) impliquant les memes pays
- Action : correlation `prediction_event_match` — mesure si les marches anticipent ou reagissent aux crises

**Regle 9 : Coupure internet + activite APT** (`outage_apt_activity`)
- Declencheur : coupure internet recente dans `geon-outages`
- ET : activite APT dans les 30 jours — soit un groupe attribue au pays (suggere un shutdown etatique), soit un groupe ciblant le pays (suggere une disruption liee a une attaque)
- Action : correlation `outage_apt_activity` (via `country_apt_mapping.json` + OpenCTI)

**Regle 10 : Convergence multi-signaux** (`multi_signal_convergence`) — la regle la plus importante
- Point de depart : pays avec un risk score >= 40 dans `geon-risk-scores`
- Verifie 7 signaux independants : >100 evenements GDELT negatifs (Goldstein < -3, 7j), nouvelle sanction (30j), coupure internet (7j), mouvement Polymarket > 5% (7j), correlation `diplomatic_escalation_apt` (30j), conflits ACLED (7j), hausse des depenses militaires (>10% YoY)
- Declencheur : >= 3 signaux convergent sur le meme pays
- Action : alerte de fusion (severite elevee) dans `geon-correlations`

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

### Notifications (implementation principale)

`correlation/alerting.py` envoie directement les alertes pour toute correlation de severite >= high :
- **Discord webhook** — embeds colores par severite (retry avec backoff via tenacity)
- **Email SMTP** — si les variables `ALERT_EMAIL_*` sont configurees

Format de notification :
```
[GEON ALERT] Correlation detectee
Regle: Escalade diplomatique + activite APT
Pays: Russie <-> Ukraine
Evenement diplo: Goldstein -8.3 -- "Military force deployment"
Evenement cyber: APT28 -- Campagne phishing ciblant infrastructure energetique
Fenetre: 12 jours
Dashboard: https://geon.example.com/grafana/d/correlations
```

### n8n Alerting Workflows (optionnel, setup manuel)

n8n peut relayer les alertes via un webhook entrant + noeud de decision sur la severite (voir `n8n/WORKFLOWS.md`, section workflows additionnels).

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

### Dashboard 6 : Prediction Markets (Polymarket)
- Nombre de marches geopolitiques actifs
- Mouvements de prix significatifs (>10%) cette semaine
- Volume total USD
- Table des marches actifs avec prix YES/NO, volume, trend, pays impliques, evenements GEON lies
- Alertes de mouvements de prix recents

---

## Structure du repository

```
geon/
+-- CLAUDE.md                          # Ce fichier
+-- README.md                          # Documentation publique
+-- LICENSE                            # MIT
+-- .env.example                       # Template des variables d'environnement
+-- .gitignore
|
+-- docker/
|   +-- docker-compose.yml             # Compose principal (tous les services)
|   +-- docker-compose.standalone.yml  # Override hote dedie : publie 80/443
|   +-- nginx/
|   |   +-- nginx.conf                 # Config principale Nginx
|   |   +-- conf.d/
|   |       +-- geon.conf              # Serveurs :80 (ACME+compat) et :443/:8443 (TLS)
|   |       +-- geon-locations.inc     # Blocs location partages (forward-auth + routage)
|   +-- authelia/
|   |   +-- configuration.yml.example  # Les .yml reels sont gitignores
|   |   +-- users_database.yml.example
|   +-- elasticsearch/
|   |   +-- elasticsearch.yml.example
|   +-- prometheus/
|   |   +-- prometheus.yml             # + prometheus.yml.example
|   +-- grafana/
|       +-- provisioning/
|           +-- datasources/
|           |   +-- datasources.yml    # Elasticsearch + Prometheus
|           +-- dashboards/
|               +-- dashboards.yml     # Provisioning config
|               +-- json/              # 6 dashboards provisionnes
|                   +-- global_overview.json
|                   +-- country_profile.json
|                   +-- correlations.json
|                   +-- articles.json
|                   +-- monitoring.json
|                   +-- prediction_markets.json
|
+-- ingestors/
|   +-- Dockerfile                     # Image geon-ingestor (python:3.11-slim)
|   +-- requirements.txt               # Dependances Python communes
|   +-- scheduler.py                   # PID 1 du conteneur : ordonnance tous les jobs
|   +-- common/
|   |   +-- config.py                  # Chargement .env, constantes
|   |   +-- es_client.py               # Client Elasticsearch partage
|   |   +-- opencti_client.py          # Client OpenCTI GraphQL partage
|   |   +-- country_apt_mapping.json   # Attribution pays -> groupes APT
|   |   +-- country_neighbors.json     # Pays -> voisins (regle 7)
|   +-- gdelt/                         # ingestor.py, parser.py, mapping.json
|   +-- gkg/                           # ingestor.py, parser.py, mapping.json
|   +-- acled/                         # ingestor.py, mapping.json
|   +-- sanctions/                     # ingestor.py (OFAC + EU + UN), mapping.json
|   +-- opencti_export/                # exporter.py, mapping.json
|   +-- polymarket/                    # ingestor.py, parser.py, mapping.json
|   +-- cloudflare_radar/              # ingestor.py, parser.py, mapping.json
|   +-- prediction_consensus/          # ingestor.py, parser.py, matcher.py, mapping.json
|   +-- sipri/                         # ingestor.py, parser.py, mapping.json, mapping_spending.json
|   +-- risk_score/                    # calculator.py, mapping.json
|   +-- articles/                      # mapping.json (index geon-articles, alimente par n8n)
|   +-- correlation/
|       +-- engine.py                  # Moteur de correlation principal
|       +-- alerting.py                # Envoi des alertes (Discord, email)
|       +-- rules/
|           +-- diplomatic_apt.py           # Regle 1
|           +-- sanction_cyber.py           # Regle 2
|           +-- conflict_cyber.py           # Regle 3
|           +-- rhetoric_shift.py           # Regle 4
|           +-- internet_outage.py          # Regle 5
|           +-- military_buildup.py         # Regle 6
|           +-- arms_escalation.py          # Regle 7
|           +-- prediction_validated.py     # Regle 8
|           +-- outage_apt.py               # Regle 9
|           +-- multi_signal_convergence.py # Regle 10
|
+-- n8n/
|   +-- WORKFLOWS.md                   # Doc : import CLI, feeds morts, filtre keywords
|   +-- workflows/
|       +-- generate_workflows.py      # Genere les 5 JSON ci-dessous
|       +-- rss_agencies.json
|       +-- rss_think_tanks.json
|       +-- rss_defense.json
|       +-- rss_regional.json
|       +-- rss_world_news.json
|
+-- scripts/
|   +-- setup.sh                       # Verification des prerequis + preparation
|   +-- init_tls.sh                    # Bootstrap TLS : cert factice -> vrai cert LE
|   +-- backup.sh                      # Backup Elasticsearch + OpenCTI
|   +-- restore.sh                     # Restauration
|   +-- crontab.example                # Legacy — l'ordonnancement reel est scheduler.py
|
+-- landing/                           # Page d'accueil statique GEON
|   +-- index.html
|   +-- style.css
|   +-- assets/
|       +-- logo.svg
|
+-- docs/
|   +-- architecture.md                # Documentation architecture detaillee
|   +-- installation.md                # Guide d'installation pas a pas
|   +-- data_sources.md                # Documentation des sources de donnees
|   +-- correlation_rules.md           # Documentation des regles de correlation
|   +-- host_proxy_sni_passthrough.md  # Patch passthrough SNI du proxy hote (VPS partage)
|   +-- use_cases/
|       +-- libya_ukraine_drones.md    # Cas d'usage : affaire Libye/Ukraine
|       +-- template.md                # Template pour nouveaux cas d'usage
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
GEON_DOMAIN=geon.example.com
GEON_EMAIL=contact@example.com

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
ALERT_EMAIL_FROM=geon@example.com
ALERT_EMAIL_TO=alerts@example.com
ALERT_EMAIL_PASSWORD=<SMTP_PASSWORD>

# === CLOUDFLARE RADAR ===
CLOUDFLARE_RADAR_TOKEN=<YOUR_CLOUDFLARE_TOKEN>

# === PREDICTION MARKETS (METACULUS) ===
METACULUS_API_TOKEN=<YOUR_METACULUS_TOKEN>

# === OPENCTI CONNECTORS ===
MITRE_CONNECTOR_ID=<GENERATE_UUID>
ALIENVAULT_API_KEY=<YOUR_OTX_KEY>
ALIENVAULT_CONNECTOR_ID=<GENERATE_UUID>
ABUSEIPDB_API_KEY=<YOUR_ABUSEIPDB_KEY>      # reserve, connecteur non deploye
ABUSEIPDB_CONNECTOR_ID=<GENERATE_UUID>
CISA_KEV_CONNECTOR_ID=<GENERATE_UUID>
CVE_CONNECTOR_ID=<GENERATE_UUID>
OPENCTI_DATASETS_CONNECTOR_ID=<GENERATE_UUID>
```

Notes :
- Pas de variables `GF_*` ni `N8N_BASIC_AUTH_*` : Grafana reutilise `ELASTIC_PASSWORD` comme mot de passe admin et tourne en auth anonyme (role Admin) derriere Authelia ; n8n est protege par Authelia.
- Les ingestors lisent leur config via `ingestors/common/config.py` (variables `ES_HOST`, `ES_SCHEME`, `OPENCTI_URL`, etc. injectees par le compose dans le conteneur).

---

## Etat du projet

Les 8 phases initiales sont TERMINEES : socle infrastructure, OpenCTI (+ connecteurs + export ES), ingestion GDELT, ACLED + sanctions, n8n + RSS, moteur de correlation, monitoring + backup, documentation + cas d'usage (Libye/Ukraine/drones).

Extensions livrees depuis :
- GDELT GKG (enrichissement thematique)
- Sanctions EU + UN (en plus d'OFAC)
- Polymarket + enrichissement contexte GEON
- Consensus predictif Metaculus/Manifold
- Cloudflare Radar (coupures internet)
- SIPRI (transferts d'armes, depenses militaires)
- Score de risque composite par pays (v2, 7 facteurs)
- Regles de correlation 5 a 10 (dont la fusion multi-signaux)
- Dashboard Prediction Markets (6 dashboards au total)
- Workflows RSS generes par script (5 categories)
- TLS rapatrie dans GEON : geon-nginx termine le TLS (sidecar certbot), passthrough SNI cote proxy hote (`docs/host_proxy_sni_passthrough.md`), profil standalone pour hote dedie

Le projet est en phase d'exploitation : les evolutions typiques sont l'ajout de sources, de regles de correlation, de panels Grafana, et la fiabilisation des parsers (les flux externes cassent regulierement — voir l'historique git).

---

## Conventions de code

### Python (ingestors)
- Python 3.11+
- Type hints systematiques
- Docstrings Google style
- Logging via le module `logging` (pas de print)
- Configuration via variables d'environnement (python-dotenv)
- Gestion d'erreurs robuste : retry avec backoff exponentiel pour les appels API
- Bibliotheques : `elasticsearch[async]`, `requests`, `pycti` (client OpenCTI), `python-dotenv`, `tenacity` (retry), `schedule` (ordonnancement), `python-dateutil`

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
# Demarrer le stack complet (reseau geon_net externe requis au prealable)
docker compose -f docker/docker-compose.yml up -d

# Sur hote dedie (GEON publie lui-meme 80/443)
docker compose -f docker/docker-compose.yml -f docker/docker-compose.standalone.yml up -d

# Premiere emission du certificat Let's Encrypt (une seule fois)
./scripts/init_tls.sh            # --staging pour tester contre l'env de staging LE

# Verifier la sante
docker compose -f docker/docker-compose.yml ps
curl -sk https://geon.example.com/grafana/api/health | jq .

# Rebuild de l'image ingestor apres modification du code Python
docker compose -f docker/docker-compose.yml build ingestor
docker compose -f docker/docker-compose.yml up -d ingestor

# Lancer une ingestion manuellement (dans le conteneur)
docker exec geon-ingestor python -m gdelt.ingestor
docker exec geon-ingestor python -m acled.ingestor
docker exec geon-ingestor python -m sanctions.ingestor
docker exec geon-ingestor python -m correlation.engine --dry-run

# Voir les logs
docker compose -f docker/docker-compose.yml logs -f --tail=100 ingestor
docker compose -f docker/docker-compose.yml logs -f --tail=100 opencti
docker compose -f docker/docker-compose.yml logs -f --tail=100 n8n

# Backup
./scripts/backup.sh

# Consulter les index (Elasticsearch n'est pas expose sur l'hote)
docker exec geon-elasticsearch curl -s -u "elastic:$ELASTIC_PASSWORD" "localhost:9200/_cat/indices?v" | grep geon
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
