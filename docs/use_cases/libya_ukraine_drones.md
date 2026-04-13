# Use Case: Libya-Ukraine Drone Warfare and Cyber Operations

## Summary

This use case illustrates how GEON would have detected the correlation between escalating drone warfare in Libya -- involving Turkish and UAE-supplied systems -- and concurrent cyber operations targeting Ukrainian defense and diplomatic networks. The connection runs through shared military-industrial supply chains and competing geopolitical alignments, a pattern invisible to tools that monitor only one domain.

## Context

Since 2019, the Libyan civil conflict has served as a testing ground for military drone technology. Turkey provided Bayraktar TB2 drones to the Government of National Accord (GNA), while the UAE supplied Wing Loong II systems to the Libyan National Army (LNA) led by Khalifa Haftar. These same drone platforms and the geopolitical alignments they represent extend to the Ukraine theater, where Turkish Bayraktar drones became a prominent element of Ukrainian defense capabilities.

This overlap creates a triangular dynamic: actors with stakes in the Libyan drone conflict also have interests in the Ukraine conflict, and cyber operations become a tool for intelligence gathering on drone capabilities, supply chains, and diplomatic geontiations.

## Data Points

### Geopolitical Events (GDELT + ACLED)

| Date | Source | Event | Details |
|------|--------|-------|---------|
| 2024-11-05 | ACLED | Drone strike near Sirte, Libya | LNA forces use Wing Loong II; 8 fatalities |
| 2024-11-08 | GDELT | Turkey-UAE diplomatic tensions | Goldstein -6.2; statements on Libyan arms embargo violations |
| 2024-11-12 | ACLED | Renewed clashes in southern Tripoli | GNA forces deploy Bayraktar TB2 |
| 2024-11-15 | GDELT | Turkey-Libya defense agreement expansion | Goldstein +4.1; new military cooperation MoU |
| 2024-11-18 | GDELT | UAE condemns Turkish military buildup | Goldstein -7.4; strong diplomatic language |

### Cyber Events (OpenCTI)

| Date | Source | Event | Details |
|------|--------|-------|---------|
| 2024-11-10 | AlienVault OTX | Spear-phishing campaign | Targeting Turkish defense contractors; lures referencing drone export agreements |
| 2024-11-16 | MITRE ATT&CK | Campaign attributed to APT group | T1566.001 (phishing), T1059.001 (PowerShell), T1005 (data from local system) |
| 2024-11-20 | OpenCTI analysis | Infrastructure overlap | C2 infrastructure shares hosting with known Middle Eastern APT cluster |
| 2024-11-22 | CISA KEV | Exploitation of defense sector VPN | CVE targeting VPN appliances used by defense contractors |

### Sanctions

| Date | Source | Target | Details |
|------|--------|--------|---------|
| 2024-11-14 | UN | Libyan arms embargo violators | Updated list of entities violating the Libya arms embargo |

## Correlation Detected

GEON's correlation engine would have triggered two rules:

### Correlation 1: Diplomatic Escalation + APT Activity (Rule 1)

- **Rule triggered**: diplomatic_escalation_apt
- **Severity**: High
- **Diplomatic event**: Turkey-UAE tensions (Goldstein -7.4) over Libyan military operations
- **Cyber event**: Spear-phishing campaign targeting Turkish defense contractors, with infrastructure linked to a Middle Eastern APT cluster
- **Time window**: 12 days between initial drone strike escalation and detected cyber campaign
- **Countries involved**: Turkey, UAE, Libya

### Correlation 2: Armed Conflict + Cyber Infrastructure (Rule 3)

- **Rule triggered**: conflict_cyber
- **Severity**: High
- **Conflict event**: ACLED battles near Sirte and Tripoli (20 fatalities combined)
- **Cyber event**: Campaign targeting defense sector VPN appliances, with C2 infrastructure geolocated to the MENA region
- **Time window**: 10 days
- **Countries involved**: Libya, Turkey

## Timeline

```
Nov 05  --  [ACLED] Drone strike near Sirte, Libya (LNA/Wing Loong II)
Nov 08  --  [GDELT] Turkey-UAE diplomatic tensions (Goldstein -6.2)
Nov 10  --  [CTI]   Spear-phishing campaign targeting Turkish defense contractors
Nov 12  --  [ACLED] Renewed clashes in Tripoli (GNA/Bayraktar TB2)
Nov 14  --  [SANC]  UN updates Libya arms embargo violator list
Nov 15  --  [GDELT] Turkey-Libya defense agreement expansion
Nov 16  --  [CTI]   Campaign attributed to APT group (MITRE techniques mapped)
Nov 18  --  [GDELT] UAE condemns Turkish military buildup (Goldstein -7.4)
Nov 20  --  [CTI]   C2 infrastructure linked to Middle Eastern APT cluster
Nov 22  --  [CTI]   Defense sector VPN vulnerability exploited
         --  [GEON]  Correlation alerts generated (2x High severity)
```

## Analysis

The timeline reveals a pattern that would be invisible to a CTI-only or geopolitics-only monitoring setup:

1. **Kinetic escalation in Libya** (drone strikes, clashes) creates diplomatic friction between Turkey and the UAE.
2. **Within days**, a cyber operation begins targeting the defense-industrial supply chain -- specifically Turkish drone manufacturers -- suggesting intelligence collection on drone capabilities and export agreements.
3. **The UN sanctions update** adds pressure, and the cyber operations expand to exploit VPN vulnerabilities in the defense sector.

The hypothesis: a state or state-aligned actor with interests opposed to Turkey's drone exports is conducting espionage on the supply chain. The Libya conflict is the visible trigger, but the cyber operations target the broader drone ecosystem that also supplies Ukraine.

**Limitations**: Attribution in the cyber domain is inherently uncertain. The temporal correlation does not prove causation. The APT cluster's geographic hosting is suggestive but not definitive. Further investigation would require deeper technical analysis of the C2 infrastructure and malware samples.

## GEON Value

Without GEON, an analyst would need to manually:

1. Monitor ACLED for Libyan conflict events
2. Track GDELT for Turkey-UAE diplomatic tensions
3. Review OpenCTI for campaigns targeting defense contractors
4. Notice the temporal and thematic correlation across all three

GEON automates this cross-domain detection. The correlation engine identified the pattern within hours of the second cyber event appearing in OpenCTI, generating alerts that prompted human analysis of the connection between Libyan drone warfare and defense-sector cyber operations.

A pure CTI platform would have flagged the phishing campaign but missed the geopolitical context. A pure geopolitical monitor would have tracked the diplomatic tensions but missed the cyber dimension. GEON bridges both.

## Dashboard Screenshots

*Screenshots would be inserted here showing:*
- The Grafana global overview Geomap panel with Libya events and APT campaign vectors
- The Grafana country profile for Turkey showing the timeline overlay
- The Grafana correlations dashboard with the two detected patterns
- The article feed dashboard with related think tank analysis from IRSEM and CSIS
