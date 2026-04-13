# NEGO n8n Workflows

This document describes the n8n workflows used by the NEGO platform for RSS
aggregation, alert dispatching, and OpenCTI enrichment. Each workflow should
be recreated manually in the n8n web UI at `https://hego.joranbatty.fr/n8n/`.

---

## Workflow 1 -- RSS Think Tanks & Agencies

**Purpose**: Aggregates articles from major think tanks, news agencies, and
cybersecurity sources. Filters for geopolitical and cyber keywords, then
indexes relevant articles into Elasticsearch.

**Trigger**: Schedule node -- runs every hour.

### Sources (RSS Feed Read nodes)

Create one **RSS Feed Read** node per source. Set each to fetch the latest
articles since the last run.

| Source                  | Feed URL                                                         |
|-------------------------|------------------------------------------------------------------|
| IRSEM                   | `https://www.irsem.fr/feed/`                                    |
| IFRI                    | `https://www.ifri.org/en/rss.xml`                               |
| CSIS                    | `https://www.csis.org/analysis/feed`                             |
| Brookings               | `https://www.brookings.edu/feed/`                                |
| Chatham House            | `https://www.chathamhouse.org/rss/all`                           |
| War on the Rocks        | `https://warontherocks.com/feed/`                                |
| Lawfare                 | `https://www.lawfaremedia.org/rss.xml`                           |
| The Diplomat            | `https://thediplomat.com/feed/`                                  |
| Reuters World           | `https://www.reutersagency.com/feed/`                            |
| AFP                     | `https://www.afp.com/en/rss-feeds`                               |
| Defense One             | `https://www.defenseone.com/rss/`                                |
| ANSSI Alertes           | `https://www.cert.ssi.gouv.fr/feed/`                             |
| CERT-FR                 | `https://www.cert.ssi.gouv.fr/avis/feed/`                       |

> **Note**: Some feeds may require adjusting the URL. Verify each feed URL is
> active before deploying.

### Node chain

```
Schedule (every 1h)
   |
   v
RSS Feed Read (IRSEM) ─┐
RSS Feed Read (IFRI)  ──┤
RSS Feed Read (CSIS)  ──┤
...                     ──┤
RSS Feed Read (CERT-FR) ─┘
   |
   v
Merge (Append mode -- combine all articles into one stream)
   |
   v
IF node (Filter)
   Condition: title OR description contains any of:
     military, conflict, sanction, diplomacy, threat, cyber,
     defense, geopolitical, APT, malware, espionage, NATO,
     nuclear, missile, drone, intelligence, war, ceasefire,
     embargo, coalition, alliance, weapon, attack, campaign,
     vulnerability, infrastructure, sovereignty, deterrence
   |
   v
Set node (Normalize fields)
   Map to:
     - title:        {{ $json.title }}
     - url:          {{ $json.link }}
     - published:    {{ $json.pubDate }}  (parse to ISO 8601)
     - source:       {{ $json._feedName }} or hardcoded source name
     - description:  {{ $json.contentSnippet }} or {{ $json.description }}
     - themes:       [] (extracted keywords from title)
     - ingested_at:  {{ $now.toISO() }}
   |
   v
Elasticsearch Bulk node
   Index:  nego-articles-{{ $now.format('yyyy.MM') }}
   Action: index
   ID:     SHA256 of (url + published)
```

### Step-by-step setup

1. Open n8n UI. Click **Add workflow**.
2. Name it `RSS Think Tanks & Agencies`.
3. Add a **Schedule Trigger** node. Set interval to `1 hour`.
4. For each source in the table above, add an **RSS Feed Read** node:
   - Set **URL** to the feed URL.
5. Add a **Merge** node. Set mode to **Append**. Connect all RSS Feed Read
   outputs to this node.
6. Add an **IF** node. Set condition to check if `{{$json.title}}` or
   `{{$json.description}}` contains any geopolitical/cyber keyword (use
   the `contains` operator with multiple OR conditions).
7. Add a **Set** node to normalize the fields into the target schema.
8. Add an **Elasticsearch** node:
   - **Operation**: Index
   - **Index**: `nego-articles-` followed by the current year-month
   - **Document ID**: Use an expression to generate a SHA256 hash
   - **Connection**: Configure with Elasticsearch host credentials from
     the `.env` file.
9. Connect: Schedule -> RSS nodes -> Merge -> IF -> Set -> Elasticsearch.
10. **Activate** the workflow.

---

## Workflow 2 -- RSS Regional Sources

**Purpose**: Same pattern as Workflow 1, but focused on regional news sources
for broader geographic coverage.

**Trigger**: Schedule node -- runs every hour.

### Sources (RSS Feed Read nodes)

| Source              | Feed URL                                              |
|---------------------|-------------------------------------------------------|
| Al Jazeera          | `https://www.aljazeera.com/xml/rss/all.xml`           |
| SCMP                | `https://www.scmp.com/rss/91/feed`                    |
| Moscow Times        | `https://www.themoscowtimes.com/rss/news`             |
| The Africa Report   | `https://www.theafricareport.com/feed/`               |

### Node chain

Identical to Workflow 1:

```
Schedule (every 1h) -> RSS nodes -> Merge -> Filter -> Normalize -> Elasticsearch
```

Index target: `nego-articles-YYYY.MM` (same index as Workflow 1).

### Step-by-step setup

1. Create a new workflow named `RSS Regional Sources`.
2. Add a **Schedule Trigger** (1 hour interval).
3. Add one **RSS Feed Read** per regional source.
4. Add **Merge** (Append), **IF** (same keywords), **Set** (same schema),
   and **Elasticsearch** (same index) nodes.
5. Connect and activate.

---

## Workflow 3 -- Correlation Alerts to Discord

**Purpose**: Receives correlation alerts from the Python correlation engine
via webhook and forwards them as formatted Discord embeds.

**Trigger**: Webhook node (POST).

### Node chain

```
Webhook (POST /webhook/correlation-alert)
   |
   v
Set node (Build Discord embed)
   Map to Discord embed format:
     - title:       "NEGO Correlation Detected"
     - description: {{ $json.description }}
     - color:       based on severity (critical=0xFF0000, high=0xFF6600,
                    medium=0xFFCC00, low=0x00CC00)
     - fields:
         - Rule:        {{ $json.rule_name }}
         - Severity:    {{ $json.severity }}
         - Countries:   {{ $json.countries_involved.join(' <-> ') }}
         - Dashboard:   https://hego.joranbatty.fr/grafana/d/correlations
     - timestamp:   {{ $json.timestamp }}
   |
   v
HTTP Request node (Discord webhook)
   Method:  POST
   URL:     {{ $env.DISCORD_WEBHOOK_URL }}
   Body:    { "embeds": [ <embed object> ] }
   Headers: Content-Type: application/json
```

### Step-by-step setup

1. Create a new workflow named `Correlation Alerts -> Discord`.
2. Add a **Webhook** node:
   - **HTTP Method**: POST
   - **Path**: `correlation-alert`
   - Note the generated webhook URL (e.g.,
     `https://hego.joranbatty.fr/n8n/webhook/correlation-alert`).
3. Add a **Set** node to build the Discord embed JSON structure:
   - Use expressions to map `severity` to a color code.
   - Build the `fields` array from the incoming correlation data.
4. Add an **HTTP Request** node:
   - **Method**: POST
   - **URL**: Use the `DISCORD_WEBHOOK_URL` environment variable or paste
     the Discord webhook URL directly.
   - **Body Content Type**: JSON
   - **Body**: The embed payload from the Set node.
5. Connect: Webhook -> Set -> HTTP Request.
6. Activate the workflow.

### Integration with the correlation engine

Update `ingestors/correlation/alerting.py` to POST to the n8n webhook URL
in addition to (or instead of) the direct Discord call. Add the webhook URL
to `.env`:

```bash
N8N_CORRELATION_WEBHOOK_URL=https://hego.joranbatty.fr/n8n/webhook/correlation-alert
```

The correlation engine can then call:

```python
requests.post(
    os.getenv("N8N_CORRELATION_WEBHOOK_URL"),
    json=correlation,
    timeout=10,
)
```

---

## Workflow 4 -- OpenCTI Enrichment

**Purpose**: After an article is ingested (from Workflows 1 or 2), this
workflow extracts entities from the article and creates corresponding
reports and entities in OpenCTI via its GraphQL API.

**Trigger**: Webhook node (POST), called after article ingestion.

### Node chain

```
Webhook (POST /webhook/enrich-article)
   |
   v
Set node (Extract entities)
   Use expressions/regex to extract:
     - Country names (match against a list of ~200 country names)
     - Organization names (NATO, UN, EU, ASEAN, etc.)
     - Person names (basic NER via regex: capitalized word sequences)
     - APT group names (match against known APT naming patterns)
   |
   v
IF node (Has entities?)
   Condition: extracted countries or organizations list is not empty
   |
   v (true branch)
HTTP Request node (OpenCTI GraphQL -- Create Report)
   Method:  POST
   URL:     http://opencti:8080/graphql
   Headers:
     Authorization: Bearer {{ $env.OPENCTI_ADMIN_TOKEN }}
     Content-Type:  application/json
   Body:
     {
       "query": "mutation CreateReport($input: ReportAddInput!) { reportAdd(input: $input) { id name } }",
       "variables": {
         "input": {
           "name": "{{ $json.title }}",
           "description": "{{ $json.description }}",
           "published": "{{ $json.published }}",
           "report_types": ["external-report"],
           "externalReferences": [{
             "source_name": "{{ $json.source }}",
             "url": "{{ $json.url }}"
           }]
         }
       }
     }
   |
   v
HTTP Request node (OpenCTI GraphQL -- Create/Link Entities)
   For each extracted country:
     Create a Location (Country) entity if it does not exist.
     Create a relationship linking the report to the country.
   |
   v
NoOp (end)
```

### Step-by-step setup

1. Create a new workflow named `OpenCTI Enrichment`.
2. Add a **Webhook** node:
   - **HTTP Method**: POST
   - **Path**: `enrich-article`
3. Add a **Set** node for entity extraction:
   - Define expressions that scan `title` and `description` for known
     country names, organization acronyms, and APT group patterns.
   - Output arrays: `countries`, `organizations`, `persons`.
4. Add an **IF** node:
   - Condition: `{{ $json.countries.length > 0 || $json.organizations.length > 0 }}`
5. On the **true** branch, add an **HTTP Request** node:
   - Configure it to call OpenCTI's GraphQL endpoint.
   - Use the `reportAdd` mutation to create a report.
   - Pass the article title, description, published date, and URL.
6. Optionally add a loop (**SplitInBatches**) over extracted countries:
   - For each country, send a GraphQL mutation to create a Location entity
     and a relationship (`related-to`) linking it to the report.
7. Connect: Webhook -> Set -> IF -> HTTP Request (report) -> Loop (countries).
8. Activate the workflow.

### Integration with RSS workflows

In Workflows 1 and 2, add an additional **HTTP Request** node after the
Elasticsearch indexing step that POSTs the article to this webhook:

```
Elasticsearch node
   |
   v
HTTP Request (POST to /webhook/enrich-article)
   Body: {{ $json }}
```

This way, every indexed article is automatically sent for OpenCTI enrichment.

---

## Environment Variables for n8n

Ensure these are set in the n8n container environment (via docker-compose or
`.env`):

| Variable                | Description                              |
|-------------------------|------------------------------------------|
| `DISCORD_WEBHOOK_URL`   | Discord channel webhook URL              |
| `OPENCTI_ADMIN_TOKEN`   | OpenCTI API token for GraphQL mutations  |
| `ELASTIC_PASSWORD`      | Elasticsearch password                   |

These can be referenced in n8n nodes using `{{ $env.VARIABLE_NAME }}`.

---

## Maintenance

- **Monitoring**: Check the n8n execution log regularly for failed runs.
  Access via the n8n UI under **Executions**.
- **Feed updates**: RSS feed URLs may change. Review and update the
  URLs in Workflows 1 and 2 quarterly.
- **Backup**: The n8n SQLite database is backed up automatically by
  `scripts/backup.sh`. Workflows can also be exported as JSON from the
  n8n UI (Settings -> Export).
- **Import/Export**: Workflows can be shared as JSON. Use the n8n UI
  to import/export workflow definitions for version control.
