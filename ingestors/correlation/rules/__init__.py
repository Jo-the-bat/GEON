"""HEGO correlation rules.

Each rule class implements a ``run()`` method that queries Elasticsearch and/or
OpenCTI for matching patterns and returns a list of correlation dicts.
"""

from correlation.rules.conflict_cyber import ConflictCyberRule
from correlation.rules.diplomatic_apt import DiplomaticAPTRule
from correlation.rules.rhetoric_shift import RhetoricShiftRule
from correlation.rules.sanction_cyber import SanctionCyberRule

__all__ = [
    "ConflictCyberRule",
    "DiplomaticAPTRule",
    "RhetoricShiftRule",
    "SanctionCyberRule",
]
