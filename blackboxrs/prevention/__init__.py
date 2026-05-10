"""Preflight prevention engine for BlackBoxRS.

The prevention loop closes ``observe -> explain -> replay -> prevent``:
every closed incident can produce a :class:`PreventionRule` whose
:class:`PreflightCheck` runs before the next launch and refuses (or
warns) when the same precursor reappears.

Only the scaffold is delivered in this v0.4 vertical slice. The full
check library (rclpy-coupled topic_present, qos_match, node_running)
lands in M6 per ``ROADMAP_V0_4.md``.
"""

from __future__ import annotations

from .rules import (
    PreflightCheck,
    PreflightCheckResult,
    PreflightReport,
    PreventionRule,
    load_rule,
    load_rules,
    save_rule,
)
from .runner import PreflightRunner

__all__ = [
    "PreflightCheck",
    "PreflightCheckResult",
    "PreflightReport",
    "PreflightRunner",
    "PreventionRule",
    "load_rule",
    "load_rules",
    "save_rule",
]
