# n8n RSS Workflows

## Overview

4 RSS workflow pipelines feed geopolitical articles into Elasticsearch (`geon-articles-YYYY.MM`).

Each workflow follows the same pattern:
```
Schedule Trigger (1h) → RSS Feed Read (per source) → Set (tag source) → Filter Geopolitical (Code) → Index to ES (HTTP)
```

## Workflows

| Workflow | Sources | File |
|----------|---------|------|
| News Agencies | Reuters, France 24, AP News, BBC World | `rss_agencies.json` |
| Think Tanks | IRSEM, IFRI, CSIS, Brookings, Chatham House, Carnegie, RAND | `rss_think_tanks.json` |
| Defense & Security | War on the Rocks, Lawfare, Defense One, The Diplomat, CERT-FR (x2) | `rss_defense.json` |
| Regional Media | Al Jazeera, SCMP, Moscow Times, The Africa Report, Middle East Eye | `rss_regional.json` |

## Dead RSS feeds (replaced as of 2026-04-15)

Several original RSS URLs no longer work. Alternatives used:

| Source | Original URL | Replacement | Reason |
|--------|-------------|-------------|--------|
| Reuters | `feeds.reuters.com/reuters/worldNews` | Google News proxy | Feed discontinued |
| AP News | `rsshub.app/apnews/topics/world-news` | feedx.net mirror | RSSHub returns 403 |
| IRSEM | `www.irsem.fr/feed/` | Google News proxy | No native RSS |
| IFRI | `www.ifri.org/en/rss` | `www.ifri.org/en/rss.xml` | URL changed |
| CSIS | `www.csis.org/analysis/feed` | `www.csis.org/rss.xml` | URL changed |
| Brookings | `www.brookings.edu/feed/` | Google News proxy | Feed removed |
| Chatham House | `www.chathamhouse.org/rss` | Google News proxy | 403 blocked |
| Carnegie | `carnegieendowment.org/rss/solr` | Google News proxy | Returns HTML |
| RAND | `www.rand.org/news.xml` | `www.rand.org/pubs/commentary.xml` | 403 blocked |
| Lawfare | `www.lawfaremedia.org/rss.xml` | Google News proxy | Cloudflare WAF |
| Defense One | `www.defenseone.com/rss/` | `www.defenseone.com/rss/all/` | URL changed |
| The Africa Report | `www.theafricareport.com/feed/` | Google News proxy | WAF blocking |

## Import / Update

Workflows are managed via the n8n CLI:

```bash
# Import a workflow (deactivates on import)
docker cp n8n/workflows/rss_agencies.json geon-n8n:/tmp/wf.json
docker exec geon-n8n sh -c "echo '[' > /tmp/wf_arr.json && cat /tmp/wf.json >> /tmp/wf_arr.json && echo ']' >> /tmp/wf_arr.json"
docker exec geon-n8n n8n import:workflow --input=/tmp/wf_arr.json

# Activate (requires restart)
docker exec geon-n8n n8n list:workflow  # get ID
docker exec geon-n8n n8n update:workflow --id=<ID> --active=true
docker restart geon-n8n
```

## Regenerating workflows

The workflows are generated from `generate_workflows.py`:

```bash
python3 n8n/workflows/generate_workflows.py
```

Edit source lists and URLs in that script, then regenerate and reimport.

## ES credentials

The workflows authenticate to Elasticsearch via `$env.ELASTIC_PASSWORD` (set in the n8n container's environment via docker-compose).

## Geopolitical keyword filter

Articles are filtered by the presence of at least one keyword:
war, conflict, sanctions, military, nato, ceasefire, invasion, nuclear, treaty, diplomacy, geopolitical, cyber attack, apt, espionage, intelligence, defense, missile, drone, territorial, sovereignty, alliance, embargo, coup, insurgency, peacekeeping, un security council, arms deal

## Additional workflows (manual setup)

### Correlation Alerts to Discord

Receives correlation alerts from the Python correlation engine via webhook and forwards them as formatted Discord embeds. See the previous version of this file in git history for full setup instructions.

### OpenCTI Enrichment

Extracts entities from ingested articles and creates corresponding reports in OpenCTI via GraphQL API. See the previous version of this file in git history for full setup instructions.
