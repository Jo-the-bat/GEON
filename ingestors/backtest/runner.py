"""GEON backtesting harness.

Replays clock-injectable detectors over the historical data already in
Elasticsearch, day by day, and scores the detections against a curated
ground truth (``backtest/ground_truth.yaml``). This is how thresholds
get tuned honestly: instead of guessing, measure what each configuration
would have detected, how early, and at what noise cost.

Detectors:

- ``rhetoric`` — Rule 4 (rhetoric_shift) replayed with ``as_of``.
- ``escalation`` — the geopolitical layer of Rule 1: diplomatic
  escalations gated by the pair's own statistical baseline. The APT
  half is deliberately skipped (OpenCTI has no historical state to
  replay) — this measures the GDELT detection layer.

Outputs: detection episodes (consecutive daily firings of the same
subject collapse into one episode with its first-detection date),
ground-truth hits/misses with lead/lag, and the total alert volume
(noise proxy).

Usage (inside the ingestor container)::

    python -m backtest.runner --start 2026-04-15 --end 2026-06-10
    python -m backtest.runner --start ... --end ... --step-days 1 \
        --detectors escalation rhetoric --output /tmp/backtest.json
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from common.config import setup_logging
from common.es_client import get_es_client
from correlation.rules.diplomatic_apt import (
    ZSCORE_THRESHOLD,
    DiplomaticAPTRule,
)
from correlation.rules.rhetoric_shift import RhetoricShiftRule

logger = logging.getLogger(__name__)

GROUND_TRUTH_PATH = Path(__file__).resolve().parent / "ground_truth.yaml"


# ---------------------------------------------------------------------------
# Detections and episodes
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """One detector firing at one replay step."""

    day: date
    detector: str
    key: str                      # situation key (sorted countries)
    countries: tuple[str, ...]
    strength: float               # z-score / deviation — detector-specific


@dataclass
class Episode:
    """Consecutive daily firings of the same (detector, key) subject."""

    detector: str
    key: str
    countries: tuple[str, ...]
    first_day: date
    last_day: date
    days_active: int = 1
    max_strength: float = 0.0
    matched_event: str | None = field(default=None)


def collapse_episodes(
    detections: list[Detection], gap_days: int = 2
) -> list[Episode]:
    """Collapse daily detections into episodes.

    Firings of the same (detector, key) with gaps <= *gap_days* belong
    to one episode — mirroring the engine's situation semantics.

    Args:
        detections: All detections, any order.
        gap_days: Maximum silent gap inside one episode.

    Returns:
        Episodes ordered by first detection day.
    """
    episodes: list[Episode] = []
    open_episodes: dict[tuple[str, str], Episode] = {}

    for det in sorted(detections, key=lambda d: (d.day, d.detector, d.key)):
        ep = open_episodes.get((det.detector, det.key))
        if ep is not None and (det.day - ep.last_day).days <= gap_days:
            ep.last_day = det.day
            ep.days_active = (ep.last_day - ep.first_day).days + 1
            ep.max_strength = max(ep.max_strength, det.strength)
            continue
        ep = Episode(
            detector=det.detector,
            key=det.key,
            countries=det.countries,
            first_day=det.day,
            last_day=det.day,
            max_strength=det.strength,
        )
        open_episodes[(det.detector, det.key)] = ep
        episodes.append(ep)

    return sorted(episodes, key=lambda e: e.first_day)


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def load_ground_truth(path: Path = GROUND_TRUTH_PATH) -> list[dict[str, Any]]:
    """Load ground-truth events (empty list when the file has none)."""
    try:
        data = yaml.safe_load(path.open()) or {}
    except FileNotFoundError:
        return []
    events = data.get("events") or []
    parsed = []
    for ev in events:
        parsed.append({
            "name": ev["name"],
            "date": ev["date"] if isinstance(ev["date"], date)
            else date.fromisoformat(str(ev["date"])),
            "countries": {str(c).upper() for c in ev.get("countries", [])},
            "window_before": int(ev.get("window_before", 7)),
            "window_after": int(ev.get("window_after", 7)),
        })
    return parsed


def score_against_ground_truth(
    episodes: list[Episode], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match episodes to ground-truth events.

    A hit = an episode sharing at least one country whose first day
    falls inside the event's detection window. Lead < 0 means the
    detection PRECEDED the event.

    Returns:
        One result dict per ground-truth event.
    """
    results = []
    for ev in events:
        window_start = ev["date"] - timedelta(days=ev["window_before"])
        window_end = ev["date"] + timedelta(days=ev["window_after"])
        relevant = [ep for ep in episodes if ev["countries"] & set(ep.countries)]

        # Fresh detection: an episode STARTING inside the window.
        fresh = [ep for ep in relevant
                 if window_start <= ep.first_day <= window_end]
        # Ongoing coverage: a chronic episode (started before the window)
        # still actively firing through it — counts as detected, but
        # without a meaningful lead.
        ongoing = [ep for ep in relevant
                   if ep.first_day < window_start and ep.last_day >= window_start]

        best = min(fresh, key=lambda e: e.first_day) if fresh else (
            min(ongoing, key=lambda e: e.first_day) if ongoing else None)
        if best is not None:
            best.matched_event = ev["name"]
        results.append({
            "event": ev["name"],
            "date": ev["date"].isoformat(),
            "detected": best is not None,
            "ongoing": bool(best is not None and not fresh),
            "detector": best.detector if best else None,
            "first_detection": best.first_day.isoformat() if best else None,
            "lead_days": (best.first_day - ev["date"]).days
            if best is not None and fresh else None,
            "episode_strength": best.max_strength if best else None,
        })
    return results


# ---------------------------------------------------------------------------
# Detectors (clock-injected replays)
# ---------------------------------------------------------------------------

def detect_escalations(es: Any, day: date) -> list[Detection]:
    """Replay Rule 1's geopolitical layer (baseline-gated escalations)."""
    as_of = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    rule = DiplomaticAPTRule(es=es, octi=None, as_of=as_of)
    detections: list[Detection] = []
    for pair_key in rule._find_escalations():
        a, b = pair_key.split("||")
        baseline = rule._pair_baseline(a, b)
        if baseline is not None and baseline.zscore < ZSCORE_THRESHOLD:
            continue
        detections.append(Detection(
            day=day,
            detector="escalation",
            key=pair_key,
            countries=(a, b),
            strength=round(baseline.zscore, 2) if baseline else 0.0,
        ))
    return detections


def detect_rhetoric(es: Any, day: date) -> list[Detection]:
    """Replay Rule 4 (rhetoric shift) at *day*."""
    as_of = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    rule = RhetoricShiftRule(es=es, as_of=as_of)
    detections: list[Detection] = []
    for corr in rule.run():
        countries = tuple(corr.get("countries_involved", []))
        detections.append(Detection(
            day=day,
            detector="rhetoric",
            key="||".join(countries),
            countries=countries,
            strength=abs(float(
                corr.get("deviation", corr.get("severity_score", 0)) or 0
            )),
        ))
    return detections


DETECTORS = {
    "escalation": detect_escalations,
    "rhetoric": detect_rhetoric,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_backtest(
    es: Any,
    start: date,
    end: date,
    step_days: int = 1,
    detectors: list[str] | None = None,
) -> dict[str, Any]:
    """Replay the detectors over [start, end] and build the report.

    Args:
        es: Elasticsearch client.
        start: First replay day.
        end: Last replay day (inclusive).
        step_days: Replay stride.
        detectors: Detector names (default: all).

    Returns:
        Report dict (episodes, ground-truth scoring, volume stats).
    """
    if step_days < 1:
        raise ValueError("step_days must be >= 1")
    names = detectors or list(DETECTORS)
    detections: list[Detection] = []
    day = start
    steps = 0
    while day <= end:
        for name in names:
            try:
                detections.extend(DETECTORS[name](es, day))
            except Exception:
                logger.exception("Detector %s failed at %s.", name, day)
        steps += 1
        if steps % 10 == 0:
            logger.info(
                "Backtest progress: %s (%d detections so far).",
                day, len(detections),
            )
        day += timedelta(days=step_days)

    # The collapse gap must tolerate the replay stride, or every step of
    # one continuous situation would open a new "episode" and corrupt
    # the volume numbers used for threshold tuning.
    episodes = collapse_episodes(detections, gap_days=max(2, step_days))
    ground_truth = load_ground_truth()
    gt_results = score_against_ground_truth(episodes, ground_truth)

    detected = [r for r in gt_results if r["detected"]]
    with_lead = [r for r in detected if r["lead_days"] is not None]
    report: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "steps": steps,
        "detectors": names,
        "detections_total": len(detections),
        "episodes_total": len(episodes),
        "episodes_per_detector": {
            name: sum(1 for e in episodes if e.detector == name)
            for name in names
        },
        "ground_truth": {
            "events": len(gt_results),
            "detected": len(detected),
            "recall": round(len(detected) / len(gt_results), 2)
            if gt_results else None,
            "mean_lead_days": round(
                sum(r["lead_days"] for r in with_lead) / len(with_lead), 1
            ) if with_lead else None,
            "results": gt_results,
        },
        "episodes": [
            {
                "detector": e.detector,
                "countries": list(e.countries),
                "first_day": e.first_day.isoformat(),
                "last_day": e.last_day.isoformat(),
                "days_active": e.days_active,
                "max_strength": e.max_strength,
                "matched_event": e.matched_event,
            }
            for e in episodes
        ],
    }
    return report


def _print_summary(report: dict[str, Any]) -> None:
    print(f"\n=== Backtest {report['start']} -> {report['end']} "
          f"({report['steps']} steps) ===")
    print(f"Detections: {report['detections_total']}  "
          f"Episodes: {report['episodes_total']}  "
          f"({report['episodes_per_detector']})")
    gt = report["ground_truth"]
    if gt["events"]:
        print(f"Ground truth: {gt['detected']}/{gt['events']} detected "
              f"(recall {gt['recall']}), mean lead {gt['mean_lead_days']} days")
        for r in gt["results"]:
            mark = "HIT " if r["detected"] else "MISS"
            if not r["detected"]:
                lead = ""
            elif r.get("ongoing"):
                lead = f"couvert (episode en cours) via {r['detector']}"
            else:
                lead = f"lead {r['lead_days']:+d}d via {r['detector']}"
            print(f"  [{mark}] {r['event']} ({r['date']}) {lead}")
    else:
        print("Ground truth: no events configured "
              "(backtest/ground_truth.yaml) — volume report only.")
    print("\nTop episodes:")
    top = sorted(report["episodes"], key=lambda e: -e["max_strength"])[:10]
    for e in top:
        print(f"  {e['first_day']} {e['detector']:11} "
              f"{','.join(e['countries']):35} z={e['max_strength']:<6} "
              f"{e['days_active']}d"
              + (f"  => {e['matched_event']}" if e["matched_event"] else ""))


def main() -> None:
    """CLI entry point."""
    setup_logging()
    parser = argparse.ArgumentParser(description="Replay GEON detectors "
                                                 "over historical data.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--step-days", type=int, default=1)
    parser.add_argument("--detectors", nargs="*", choices=list(DETECTORS),
                        default=None)
    parser.add_argument("--output", default=None,
                        help="Write the full JSON report to this path.")
    args = parser.parse_args()

    es = get_es_client()
    report = run_backtest(es, args.start, args.end, args.step_days,
                          args.detectors)
    _print_summary(report)
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\nRapport JSON: {args.output}")


if __name__ == "__main__":
    main()
