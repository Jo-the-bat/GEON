# Use Case: [Title]

## Summary

One paragraph describing the scenario and what GEON detected.

## Context

Background information on the geopolitical and/or cyber situation.

## Data Points

### Geopolitical Events

| Date | Source | Event | Details |
|------|--------|-------|---------|
| YYYY-MM-DD | GDELT/ACLED | Event description | Goldstein score, location, actors |

### Cyber Events

| Date | Source | Event | Details |
|------|--------|-------|---------|
| YYYY-MM-DD | OpenCTI | Campaign/indicator | APT group, techniques, targets |

### Sanctions (if applicable)

| Date | Source | Target | Details |
|------|--------|--------|---------|
| YYYY-MM-DD | OFAC/EU/UN | Entity name | Program, reason |

## Correlation Detected

- **Rule triggered**: Which correlation rule matched
- **Severity**: low / medium / high / critical
- **Time window**: Gap between geopolitical and cyber events
- **Countries involved**: List

## Timeline

Chronological sequence of events showing how the geopolitical and cyber dimensions interrelate.

```
[Date 1] -- Geopolitical event A
[Date 2] -- Geopolitical event B
[Date 3] -- Cyber event detected
[Date 4] -- Correlation alert generated
```

## Analysis

Interpretation of the correlation. What does it suggest? What are the limitations? What additional investigation would be warranted?

## GEON Value

How did GEON add value compared to monitoring each domain separately? What would have been missed without cross-domain correlation?

## Dashboard Screenshots

Include relevant Grafana dashboard screenshots showing the correlation visualization.
