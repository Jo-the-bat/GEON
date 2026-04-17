# GEON Review Notes

Post-review observations and status from the hardening/review-pass branch.

## Summary

- **6 commits** on `hardening/review-pass` covering all 6 review groups
- **48 tests** passing (GDELT parser, ACLED ingestor, correlation engine)
- **0 ruff F-category errors** (unused imports, undefined names all fixed)
- **81 ruff E501** (line length > 100) remain in pre-existing code (HTML email
  templates, long correlation rule strings). These are cosmetic and were not
  in scope for this pass.
- **docker compose config** validates cleanly

## Points Treated

All 28+ explicit points from the review were addressed:
- Group 1 (Security): 5/5
- Group 2 (Business logic): 6/6
- Group 3 (Tests): 4/4
- Group 4 (Documentation): 6/6
- Group 5 (Infrastructure): 5/5
- Group 6 (Portfolio): 4/4

See REVIEW_RESPONSE.md for per-point commit mapping.

## Deferred Items

| Item | Reason |
|------|--------|
| `country_apt_mapping.json` improvements | Explicitly excluded per review instructions |
| `country_neighbors.json` maritime additions | Explicitly excluded per review instructions |
| E501 line-length fixes in pre-existing code | Cosmetic, not in scope |
| Demo page screenshot placeholders | Requires live instance for captures |
| Git tag v0.1.0 | Requires human validation before tagging |

## Discoveries During the Pass

1. **cloudflare_radar/ingestor.py** had unused imports (`sys`, `datetime`,
   `timezone`) — cleaned up by ruff autofix.
2. **polymarket/ingestor.py** had unused `sys` and `extract_countries` imports
   — cleaned up by ruff autofix.
3. **correlation/rules/outage_apt.py** had unused local variable `apt_names`
   — removed.
4. **prediction_consensus/ingestor.py** had unused `sys` and
   `extract_countries` imports — cleaned up by ruff autofix.
5. The opencti_export/exporter.py had an unused `timedelta` import — cleaned.
6. The YAML anchor for OpenCTI workers saved ~45 lines of duplication in
   docker-compose.yml.

## Recommended for a Second Pass

- Fix the 81 remaining E501 line-length violations (mostly HTML templates
  and long strings in correlation rules).
- Add integration tests that exercise the ES client layer (currently only
  unit tests exist).
- Add typing stubs or mypy configuration for stricter type checking.
- Replace demo page screenshot placeholders with actual captures once the
  platform is running.
- Consider adding a `--dry-run` flag to the correlation engine for safe
  testing without alert dispatch.
