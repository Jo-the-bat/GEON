"""Central tunable settings for the GEON analytics layer.

Correlation-rule thresholds, engine behaviour, alerting windows and
risk-score weights live in ``ingestors/config.yaml`` (versioned), so
tuning the detection sensitivity is a config change — not a code edit
followed by a redeploy of hardcoded constants.

Every consumer keeps a sane default, so a missing or partial YAML file
degrades to the historical behaviour instead of crashing.

Usage::

    from common.settings import setting

    GOLDSTEIN_THRESHOLD: float = setting(
        "correlation.diplomatic_apt.goldstein_threshold", -5.0)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    """Load and cache the YAML config; empty dict when absent/invalid."""
    try:
        with CONFIG_PATH.open() as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.warning("config.yaml is not a mapping — ignoring it.")
            return {}
        return data
    except FileNotFoundError:
        logger.warning("config.yaml not found at %s — using defaults.", CONFIG_PATH)
        return {}
    except Exception:
        logger.exception("Failed to parse config.yaml — using defaults.")
        return {}


def setting(path: str, default: Any) -> Any:
    """Return the config value at dotted *path*, or *default*.

    Args:
        path: Dotted key path, e.g. ``"correlation.diplomatic_apt.lookback_days"``.
        default: Value used when the path is absent.

    Returns:
        The configured value (coerced to the default's type for scalar
        int/float mismatches such as YAML ``10`` for a ``10.0`` default).
    """
    node: Any = _load()
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    if node is None:
        return default
    # YAML "10" for a float default (or "10.5"-style floats for int
    # defaults) — coerce so type hints on the constants stay honest.
    if isinstance(default, bool):
        return bool(node)
    if isinstance(default, float) and isinstance(node, int):
        return float(node)
    if isinstance(default, int) and isinstance(node, float) and node.is_integer():
        return int(node)
    return node


def reload() -> None:
    """Clear the cache (used by tests)."""
    _load.cache_clear()
