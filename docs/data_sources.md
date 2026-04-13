# HEGO Data Sources

## 1. GDELT (Global Database of Events, Language, and Tone)

### Overview

GDELT monitors news media worldwide and extracts structured event data, including actors, actions, locations, and sentiment. HEGO uses it as the primary source for diplomatic and military events.

- **Website**: [gdeltproject.org](https://www.gdeltproject.org/)
- **API**: `https://api.gdeltproject.org/api/v2/`
- **Cost**: Free, no API key required
- **Ingestion frequency**: Every 15 minutes

### API Endpoints

| Endpoint | URL | Use |
|----------|-----|-----|
| DOC API | `https://api.gdeltproject.org/api/v2/doc/doc` | Articles with tone, themes, entities |
| GEO API | `https://api.gdeltproject.org/api/v2/geo/geo` | Geolocated events |
| TV API | `https://api.gdeltproject.org/api/v2/tv/tv` | TV broadcast monitoring (optional) |

### CAMEO Event Codes

HEGO filters on the following CAMEO code families:

| Code Range | Category | Description |
|------------|----------|-------------|
| 04x | Material cooperation | Military/economic cooperation |
| 05x | Diplomatic cooperation | Diplomatic statements, agreements |
| 06x | Material cooperation | Aid, grants, loans |
| 13x | Threats | Threaten with force, sanctions |
| 16x | Sanctions | Impose sanctions, embargo |
| 17x | Coercion | Coercive actions |
| 18x | Assault | Physical assault, armed attack |
| 19x | Fight | Military engagement, armed conflict |
| 20x | Mass violence | Unconventional mass violence |

### Goldstein Scale

Events are scored on the Goldstein scale from -10 (maximum conflict) to +10 (maximum cooperation). HEGO uses this to detect tension spikes:

- **> +5**: Strong cooperation signal
- **-3 to +3**: Neutral range
- **< -5**: Significant tension (triggers correlation rules)
- **< -8**: Severe conflict/crisis

### Elasticsearch Index

- **Pattern**: `hego-gdelt-events-YYYY.MM`
- **Alias**: `hego-gdelt`

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | keyword | Unique GDELT event identifier |
| `date` | date | Event timestamp |
| `source_country` | keyword | ISO code of actor 1's country |
| `target_country` | keyword | ISO code of actor 2's country |
| `cameo_code` | keyword | CAMEO event code |
| `cameo_description` | text | Human-readable event description |
| `goldstein_scale` | float | Goldstein conflict/cooperation score |
| `tone` | float | Average tone of source articles |
| `num_articles` | integer | Number of articles covering this event |
| `geo_lat` / `geo_lon` | float | Event geolocation |
| `themes` | keyword[] | GDELT theme tags |
| `persons` | keyword[] | Named persons |
| `organizations` | keyword[] | Named organizations |

---

## 2. ACLED (Armed Conflict Location & Event Data)

### Overview

ACLED provides detailed, disaggregated data on political violence and protests worldwide. Each event is individually coded with actors, locations, and fatality counts.

- **Website**: [acleddata.com](https://acleddata.com/)
- **API**: `https://api.acleddata.com/acled/read/`
- **Cost**: Free for non-commercial use (requires registration)
- **API Key**: Required -- register at [acleddata.com/register](https://acleddata.com/register/)
- **Ingestion frequency**: Daily (2:00 AM)

### Event Types

| Event Type | Sub-types |
|------------|-----------|
| Battles | Armed clash, government regains territory, non-state actor overtakes territory |
| Violence against civilians | Sexual violence, attack, abduction/forced disappearance |
| Explosions/Remote violence | Chemical weapon, air/drone strike, suicide bomb, shelling |
| Riots | Violent demonstration, mob violence |
| Protests | Peaceful protest, protest with intervention, excessive force against protesters |
| Strategic developments | Agreement, arrests, change of group activity, headquarters established |

### API Parameters

| Parameter | Description |
|-----------|-------------|
| `key` | API key |
| `email` | Registered email |
| `event_date` | Date filter (e.g., `2025-01-01|2025-01-31`) |
| `event_type` | Filter by event type |
| `country` | Filter by country name |
| `limit` | Maximum results (default 5000) |

### Elasticsearch Index

- **Pattern**: `hego-acled-events-YYYY.MM`
- **Alias**: `hego-acled`

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | integer | ACLED event identifier |
| `event_date` | date | Event date |
| `event_type` | keyword | Type (battles, riots, etc.) |
| `sub_event_type` | keyword | Sub-type |
| `actor1` / `actor2` | keyword | Involved actors |
| `country` | keyword | Country name |
| `admin1` | keyword | First-level administrative division |
| `location` | keyword | Specific location name |
| `latitude` / `longitude` | float | Geolocation |
| `fatalities` | integer | Reported fatalities |
| `notes` | text | Event description |

---

## 3. Sanctions Lists

### Overview

HEGO ingests sanctions data from three major regulatory bodies to track sanctioned entities and detect correlations with cyber activity.

### Sources

#### OFAC (US Treasury)

- **API**: `https://sanctionslistservice.ofac.treas.gov/api/`
- **Lists**: SDN (Specially Designated Nationals), Consolidated Sanctions
- **Format**: JSON/XML
- **Update frequency**: Near-daily (HEGO checks weekly)

#### EU Consolidated Sanctions

- **Source**: European Council financial sanctions registry
- **URL**: `https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content`
- **Format**: XML
- **Update frequency**: Regular updates after Council decisions

#### UN Security Council Sanctions

- **Source**: UN consolidated list
- **URL**: `https://scsanctions.un.org/resources/xml/en/consolidated.xml`
- **Format**: XML
- **Update frequency**: After Security Council resolutions

### Elasticsearch Index

- **Index**: `hego-sanctions` (single index, no time rotation)
- **Alias**: `hego-sanctions`

### Key Fields

| Field | Type | Description |
|-------|------|-------------|
| `entity_id` | keyword | Unique identifier |
| `source` | keyword | OFAC, EU, or UN |
| `entity_type` | keyword | Individual or entity/organization |
| `name` | text | Primary name |
| `aliases` | text[] | Known aliases |
| `country` | keyword | Associated country |
| `program` | keyword | Sanctions program name |
| `listed_date` | date | Date added to sanctions list |
| `reasons` | text | Reason for listing |

---

## 4. RSS Feeds via Huginn

### Overview

Huginn agents aggregate, filter, and extract entities from RSS feeds published by think tanks, news agencies, defense publications, and cybersecurity organizations.

### Feed Categories

#### Think Tanks & Research Institutes

| Source | RSS URL | Focus |
|--------|---------|-------|
| IRSEM | `https://www.irsem.fr/feed/` | French defense/IR research |
| IFRI | `https://www.ifri.org/en/rss` | French international relations |
| CSIS | `https://www.csis.org/rss.xml` | US strategic studies |
| Brookings | `https://www.brookings.edu/feed/` | US policy research |
| Chatham House | `https://www.chathamhouse.org/rss` | UK international affairs |
| War on the Rocks | `https://warontherocks.com/feed/` | US defense analysis |
| Lawfare | `https://www.lawfaremedia.org/feed` | National security law |
| The Diplomat | `https://thediplomat.com/feed/` | Asia-Pacific affairs |
| Carnegie | `https://carnegieendowment.org/rss/feeds` | Global policy |
| RAND | `https://www.rand.org/content/rand/pubs.xml` | Defense research |

#### News Agencies

| Source | RSS URL | Focus |
|--------|---------|-------|
| Reuters World | `https://feeds.reuters.com/reuters/worldNews` | Global news |
| AFP | Via Huginn scraping | French/global news |
| AP News | `https://apnews.com/rss` | US/global news |

#### Cybersecurity & Defense

| Source | RSS URL | Focus |
|--------|---------|-------|
| ANSSI | `https://www.cert.ssi.gouv.fr/feed/` | French CERT alerts |
| CERT-FR | `https://www.cert.ssi.gouv.fr/avis/feed/` | French CERT advisories |
| CISA | `https://www.cisa.gov/news.xml` | US CISA alerts |
| ENISA | `https://www.enisa.europa.eu/rss.xml` | EU cybersecurity |
| Defense One | `https://www.defenseone.com/rss/` | US defense news |

#### Regional

| Source | RSS URL | Focus |
|--------|---------|-------|
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` | Middle East / global |
| SCMP | `https://www.scmp.com/rss/91/feed` | Asia / China |
| Moscow Times | `https://www.themoscowtimes.com/rss/news` | Russia |

### Huginn Pipeline

1. **RSS Agent**: Fetches articles from each feed
2. **Filter Agent**: Keeps articles matching keywords (geopolitics, cyber, defense, sanctions, conflict, etc.)
3. **Extraction Agent**: Identifies country names, organization names, and person names via pattern matching
4. **Elasticsearch Agent**: Indexes filtered articles into `hego-articles-YYYY.MM`
5. **OpenCTI Agent**: Creates reports in OpenCTI for articles with CTI relevance

### Elasticsearch Index

- **Pattern**: `hego-articles-YYYY.MM`
- **Alias**: `hego-articles`

---

## 5. OpenCTI Connectors (Cyber Threat Intelligence)

### Overview

OpenCTI aggregates structured CTI from multiple sources into a STIX2 knowledge graph. HEGO exports relevant data to Elasticsearch for cross-correlation with geopolitical events.

### Active Connectors

| Connector | Type | Description |
|-----------|------|-------------|
| MITRE ATT&CK | External import | Tactics, techniques, and procedures |
| AlienVault OTX | External import | Indicators, pulses, threat intelligence |
| CISA KEV | External import | Known exploited vulnerabilities |
| CVE / NVD | External import | Common vulnerabilities and exposures |
| OpenCTI Datasets | External import | Geographic, sector, and standard datasets |
| AbuseIPDB | External import | IP reputation data |

### Export to Elasticsearch

The `opencti_export/exporter.py` script queries the OpenCTI GraphQL API and indexes relevant objects into Elasticsearch:

- **Index**: `hego-cti-*`
- **Objects exported**: Intrusion sets (APT groups), campaigns, indicators, malware, attack patterns
- **Frequency**: Every 6 hours

### Key Fields in Elasticsearch

| Field | Type | Description |
|-------|------|-------------|
| `stix_id` | keyword | STIX2 identifier |
| `type` | keyword | STIX object type (intrusion-set, campaign, indicator, etc.) |
| `name` | text | Object name |
| `description` | text | Object description |
| `created` | date | Creation date |
| `modified` | date | Last modification date |
| `country` | keyword | Attributed country (for intrusion sets) |
| `aliases` | keyword[] | Known aliases |
| `techniques` | keyword[] | Associated MITRE ATT&CK techniques |
| `confidence` | integer | Confidence level (0-100) |
