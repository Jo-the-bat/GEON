# GEON Review Response

Systematic response to the 67-point code review. Each point lists its status
and the commit that addresses it.

Legend: done = fixed, deferred = planned for a future pass, n/a = not applicable.

---

## Group 1 -- Security Critical

| # | Description | Status | Commit |
|---|-------------|--------|--------|
| 1.1 | Grafana: remove anonymous admin, add auth proxy | done | `35cd0a1` |
| 1.2 | Grafana datasource: dedicated ES reader user | done | `35cd0a1` |
| 1.3 | Ingestors: use dedicated ES writer, not superuser | done | `35cd0a1` |
| 1.4 | n8n: ES credentials via n8n credential store | done | `35cd0a1` |
| 1.5 | defusedxml for sanctions XML parsing | done | `35cd0a1` |

## Group 2 -- Business Logic Bugs

| # | Description | Status | Commit |
|---|-------------|--------|--------|
| 2.1 | OpenCTI client: STIX relationship resolution instead of full-text search | done | `3053423` |
| 2.2 | sanction_cyber: use valid_from, require baseline >= 10 | done | `3053423` |
| 2.3 | Risk score: historize per (country, day) instead of overwriting | done | `3053423` |
| 2.4 | military_buildup: filter on current/previous year only | done | `3053423` |
| 2.5 | Polymarket: compute real price_change_7d from history samples | done | `3053423` |
| 2.6 | conflict_cyber: remove "Strategic developments" from filter | done | `3053423` |

## Group 3 -- Tests

| # | Description | Status | Commit |
|---|-------------|--------|--------|
| 3.1 | Implement GDELT parser tests (5 test cases) | done | `49dfa2c` |
| 3.2 | Implement correlation engine tests (4 test cases) | done | `49dfa2c` |
| 3.3 | Implement ACLED ingestor tests (5 test cases) | done | `49dfa2c` |
| 3.4 | CI: GitHub Actions workflow (pytest + ruff) | done | `49dfa2c` |

## Group 4 -- Documentation / Code Coherence

| # | Description | Status | Commit |
|---|-------------|--------|--------|
| 4.1 | Eradicate all Kibana references | done | `0c63b6c` |
| 4.2 | Sanctions: fix "stubs" docstring | done | `0c63b6c` |
| 4.3 | Risk score: harmonize 7-component documentation | done | `0c63b6c` |
| 4.4 | SIPRI: align docstring with actual behavior | done | `0c63b6c` |
| 4.5 | Libya/Ukraine use case: add illustrative disclaimer, SIPRI section | done | `0c63b6c` |
| 4.6 | "geontiation": explicit neologism disclaimer in README | done | `0c63b6c` |

## Group 5 -- Infrastructure / Reliability

| # | Description | Status | Commit |
|---|-------------|--------|--------|
| 5.1 | Backup ES: add volume + path.repo | done | `617829b` |
| 5.2 | ILM: add setup_ilm.sh (hot/warm/delete policy) | done | `617829b` |
| 5.3 | Scheduler: remove unconditional boot runs, add --bootstrap | done | `617829b` |
| 5.4 | Factor OpenCTI workers via YAML anchor | done | `617829b` |
| 5.5 | Alert anti-spam dedup (7-day window in geon-alerts-sent) | done | `617829b` |

## Group 6 -- Portfolio / Jury Visibility

| # | Description | Status | Commit |
|---|-------------|--------|--------|
| 6.1 | Landing page: sample correlation section | done | (this commit) |
| 6.2 | Demo page for unauthenticated visitors | done | (this commit) |
| 6.3 | CHANGELOG.md + README badges (CI, Python, ruff) | done | (this commit) |
| 6.4 | This file (REVIEW_RESPONSE.md) | done | (this commit) |

---

## Notes

- Git tag `v0.1.0` should be created manually after merging this branch:
  `git tag -a v0.1.0 -m "First stable release"` then create the GitHub
  release from the UI.
- The `country_apt_mapping.json` and `country_neighbors.json` files were
  explicitly excluded from this review pass per instructions.
- See `REVIEW_NOTES.md` for additional observations discovered during the pass.
