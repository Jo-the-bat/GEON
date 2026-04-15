#!/usr/bin/env python3
"""Generate n8n RSS workflow JSON files for GEON."""

import json
import uuid

KEYWORDS_JS = """const keywords = [
  'war', 'conflict', 'sanctions', 'military', 'nato', 'ceasefire',
  'invasion', 'nuclear', 'treaty', 'diplomacy', 'geopolitical',
  'cyber attack', 'apt', 'espionage', 'intelligence', 'defense',
  'missile', 'drone', 'territorial', 'sovereignty', 'alliance',
  'embargo', 'coup', 'insurgency', 'peacekeeping', 'un security council',
  'arms deal'
];

const filtered = [];
for (const item of $input.all()) {
  const text = `${item.json.title || ''} ${item.json.contentSnippet || item.json.content || ''}`.toLowerCase();
  const matched = keywords.filter(k => text.includes(k));
  if (matched.length > 0) {
    filtered.push({
      json: {
        title: item.json.title || '',
        source: item.json.source || 'Unknown',
        source_category: item.json.source_category || 'unknown',
        url: item.json.link || item.json.url || '',
        published_date: new Date(item.json.isoDate || item.json.pubDate || Date.now()).toISOString(),
        date: new Date(item.json.isoDate || item.json.pubDate || Date.now()).toISOString(),
        summary: (item.json.contentSnippet || item.json.content || '').substring(0, 500),
        keywords_matched: matched,
        ingested_at: new Date().toISOString()
      }
    });
  }
}
return filtered;"""


def uid() -> str:
    return str(uuid.uuid4())


def build_workflow(name: str, sources: list[dict], category: str) -> dict:
    """Build a complete n8n workflow JSON.

    Each source dict: {"name": "BBC World", "url": "https://...", "note": "optional"}
    """
    nodes = []
    connections: dict[str, dict] = {}

    # 1. Schedule Trigger (every 1 hour)
    trigger_name = "Every 1 hour"
    nodes.append({
        "parameters": {
            "rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}
        },
        "id": uid(),
        "name": trigger_name,
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [0, 0],
    })

    # Calculate vertical spacing
    n = len(sources)
    y_start = -(n - 1) * 120
    rss_names = []
    set_names = []

    for i, src in enumerate(sources):
        y = y_start + i * 240
        rss_name = f"RSS {src['name']}"
        set_name = f"Tag {src['name']}"
        rss_names.append(rss_name)
        set_names.append(set_name)

        # RSS Feed Read node
        nodes.append({
            "parameters": {"url": src["url"]},
            "id": uid(),
            "name": rss_name,
            "type": "n8n-nodes-base.rssFeedRead",
            "typeVersion": 1,
            "position": [250, y],
        })

        # Set node (add source + source_category)
        nodes.append({
            "parameters": {
                "assignments": {
                    "assignments": [
                        {"id": uid(), "name": "source", "value": src["name"], "type": "string"},
                        {"id": uid(), "name": "source_category", "value": category, "type": "string"},
                    ]
                },
                "options": {},
            },
            "id": uid(),
            "name": set_name,
            "type": "n8n-nodes-base.set",
            "typeVersion": 3.4,
            "position": [500, y],
        })

    # Filter Geopolitical (Code node)
    filter_name = "Filter Geopolitical"
    filter_y = 0
    nodes.append({
        "parameters": {"jsCode": KEYWORDS_JS},
        "id": uid(),
        "name": filter_name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [750, filter_y],
    })

    # Index to ES (HTTP Request)
    es_name = "Index to ES"
    nodes.append({
        "parameters": {
            "method": "POST",
            "url": "=http://elastic:{{ $env.ELASTIC_PASSWORD }}@elasticsearch:9200/geon-articles-{{ $now.format('yyyy.MM') }}/_doc",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify($json) }}",
            "options": {},
        },
        "id": uid(),
        "name": es_name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [1000, filter_y],
    })

    # Wire connections
    # Trigger → all RSS nodes
    connections[trigger_name] = {
        "main": [[{"node": rss_name, "type": "main", "index": 0} for rss_name in rss_names]]
    }

    # Each RSS → its Set node
    for rss_name, set_name in zip(rss_names, set_names):
        connections[rss_name] = {
            "main": [[{"node": set_name, "type": "main", "index": 0}]]
        }

    # Each Set → Filter
    for set_name in set_names:
        connections[set_name] = {
            "main": [[{"node": filter_name, "type": "main", "index": 0}]]
        }

    # Filter → ES
    connections[filter_name] = {
        "main": [[{"node": es_name, "type": "main", "index": 0}]]
    }

    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "active": True,
        "settings": {"executionOrder": "v1"},
        "tags": [{"name": "geon"}],
    }


# ─── Workflow definitions ────────────────────────────────────────────

AGENCIES = [
    {"name": "Reuters", "url": "https://news.google.com/rss/search?q=site:reuters.com+world&hl=en-US&gl=US&ceid=US:en", "note": "Google News proxy — original feed dead"},
    {"name": "France 24", "url": "https://www.france24.com/en/rss"},
    {"name": "AP News", "url": "https://feedx.net/rss/ap.xml", "note": "feedx mirror — RSSHub returns 403"},
    {"name": "BBC World", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
]

THINK_TANKS = [
    {"name": "IRSEM", "url": "https://news.google.com/rss/search?q=site:irsem.fr&hl=fr&gl=FR&ceid=FR:fr", "note": "Google News proxy — no native RSS"},
    {"name": "IFRI", "url": "https://www.ifri.org/en/rss.xml", "note": "Updated URL from /en/rss"},
    {"name": "CSIS", "url": "https://www.csis.org/rss.xml", "note": "Updated URL from /analysis/feed"},
    {"name": "Brookings", "url": "https://news.google.com/rss/search?q=site:brookings.edu&hl=en-US&gl=US&ceid=US:en", "note": "Google News proxy — native feed removed"},
    {"name": "Chatham House", "url": "https://news.google.com/rss/search?q=site:chathamhouse.org&hl=en-US&gl=US&ceid=US:en", "note": "Google News proxy — 403 on native"},
    {"name": "Carnegie", "url": "https://news.google.com/rss/search?q=site:carnegieendowment.org&hl=en-US&gl=US&ceid=US:en", "note": "Google News proxy — native returns HTML"},
    {"name": "RAND", "url": "https://www.rand.org/pubs/commentary.xml", "note": "Updated from /news.xml (403)"},
]

DEFENSE = [
    {"name": "War on the Rocks", "url": "https://warontherocks.com/feed/"},
    {"name": "Lawfare", "url": "https://news.google.com/rss/search?q=site:lawfaremedia.org&hl=en-US&gl=US&ceid=US:en", "note": "Google News proxy — Cloudflare blocking"},
    {"name": "Defense One", "url": "https://www.defenseone.com/rss/all/", "note": "Updated from /rss/ (404)"},
    {"name": "The Diplomat", "url": "https://thediplomat.com/feed/"},
    {"name": "ANSSI / CERT-FR", "url": "https://www.cert.ssi.gouv.fr/feed/"},
    {"name": "CERT-FR Alertes", "url": "https://www.cert.ssi.gouv.fr/alerte/feed/"},
]

REGIONAL = [
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "SCMP", "url": "https://www.scmp.com/rss/91/feed"},
    {"name": "Moscow Times", "url": "https://www.themoscowtimes.com/rss/news"},
    {"name": "The Africa Report", "url": "https://news.google.com/rss/search?q=site:theafricareport.com&hl=en-US&gl=US&ceid=US:en", "note": "Google News proxy — WAF blocking"},
    {"name": "Middle East Eye", "url": "https://www.middleeasteye.net/rss"},
]

WORKFLOWS = {
    "rss_agencies.json": ("GEON \u2014 News Agencies RSS \u2192 Elasticsearch", AGENCIES, "agency"),
    "rss_think_tanks.json": ("GEON \u2014 Think Tanks RSS \u2192 Elasticsearch", THINK_TANKS, "think_tank"),
    "rss_defense.json": ("GEON \u2014 Defense & Security RSS \u2192 Elasticsearch", DEFENSE, "defense"),
    "rss_regional.json": ("GEON \u2014 Regional Media RSS \u2192 Elasticsearch", REGIONAL, "regional"),
}

if __name__ == "__main__":
    for filename, (name, sources, category) in WORKFLOWS.items():
        wf = build_workflow(name, sources, category)
        path = f"/home/docker-user/geon/n8n/workflows/{filename}"
        with open(path, "w") as f:
            json.dump(wf, f, indent=2)
        print(f"Generated {filename}: {len(sources)} sources, {len(wf['nodes'])} nodes")
