"""Preflight runner.

Executes the loaded prevention rules and returns a :class:`PreflightReport`
with per-check results. Exit-code semantics:

* 0: all checks pass.
* 1: at least one blocking check failed.
* 2: warnings only (no blockers).

The actual ROS-coupled checks (``topic_present``, ``qos_match``,
``node_running``) ship in M6. The runner already routes to those check
kinds; the unimplemented kinds return a ``skipped`` result with a clear
message so a partial v0.4 ship still produces a useful report.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from .checks import node_running as _node_running_check
from .checks import qos_match as _qos_match_check
from .checks import topic_present as _topic_present_check
from .rules import (
    PreflightCheck,
    PreflightCheckResult,
    PreflightReport,
    PreventionRule,
)

logger = logging.getLogger(__name__)


# A check function takes the check parameters and returns
# (status, message). Status "pass" / "fail" / "skipped" / "error".
# The runner promotes "fail" to the rule's severity_on_fail
# ("warn" or "block") before recording the result.
CheckFn = Callable[[PreflightCheck], tuple[str, str]]


def _adapt(run_fn: Callable[[dict], tuple[str, str]]) -> CheckFn:
    """Adapt a check module's ``run(params) -> (status, message)`` to the
    runner's expected ``CheckFn(PreflightCheck) -> (status, message)``."""
    def _wrapped(check: PreflightCheck) -> tuple[str, str]:
        return run_fn(check.params)
    return _wrapped


def _check_not_implemented(check: PreflightCheck) -> tuple[str, str]:
    return ("skipped",
            f"{check.kind} is not implemented yet; v0.4 ships "
            "topic_present / qos_match / node_running. Future work.")


_CHECK_REGISTRY: dict[str, CheckFn] = {
    "topic_present": _adapt(_topic_present_check.run),
    "qos_match": _adapt(_qos_match_check.run),
    "node_running": _adapt(_node_running_check.run),
    "env_var": _check_not_implemented,
    "param_value": _check_not_implemented,
    "resource_threshold": _check_not_implemented,
    "custom_python": _check_not_implemented,
}


class PreflightRunner:
    """Executes a list of :class:`PreventionRule`s and returns a report."""

    def __init__(self, rules: list[PreventionRule]) -> None:
        self._rules = rules

    def run(self) -> PreflightReport:
        started = datetime.now(timezone.utc)
        results: list[PreflightCheckResult] = []
        for rule in self._rules:
            if rule.disabled:
                results.append(
                    PreflightCheckResult(
                        rule_id=rule.rule_id,
                        name=rule.check.name,
                        kind=rule.check.kind,
                        status="skipped",
                        message="Rule disabled.",
                        severity_on_fail=rule.check.severity_on_fail,
                    )
                )
                continue

            fn = _CHECK_REGISTRY.get(rule.check.kind, _check_not_implemented)
            try:
                status, message = fn(rule.check)
            except Exception as exc:
                logger.exception("Check %s raised", rule.rule_id)
                status, message = "error", f"{type(exc).__name__}: {exc}"

            # If the underlying check failed and the rule says "block",
            # promote the status accordingly. Skipped/error stay as-is.
            if status == "fail":
                status = rule.check.severity_on_fail  # "warn" or "block"
            results.append(
                PreflightCheckResult(
                    rule_id=rule.rule_id,
                    name=rule.check.name,
                    kind=rule.check.kind,
                    status=status,  # type: ignore[arg-type]
                    message=message,
                    severity_on_fail=rule.check.severity_on_fail,
                )
            )

        finished = datetime.now(timezone.utc)
        return PreflightReport(
            started_at=started, finished_at=finished, results=results
        )
