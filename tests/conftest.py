"""Pytest configuration for GEON tests.

Adds ``ingestors/`` to ``sys.path`` so test modules can import internal
packages (``gdelt``, ``common``, ``correlation``, ...) with the same module
layout the runtime containers use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_INGESTORS_DIR = Path(__file__).resolve().parent.parent / "ingestors"
if str(_INGESTORS_DIR) not in sys.path:
    sys.path.insert(0, str(_INGESTORS_DIR))

# Ensure modules importing common.config don't crash because of a missing .env.
os.environ.setdefault("ELASTIC_PASSWORD", "test-password")
os.environ.setdefault("GEON_INGESTOR_PASSWORD", "test-password")
os.environ.setdefault("ES_USER", "geon_ingestor")
os.environ.setdefault("ACLED_API_KEY", "test-key")
os.environ.setdefault("ACLED_EMAIL", "test@example.com")
