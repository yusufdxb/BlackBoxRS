"""Preflight runner.

Executes the loaded prevention rules and returns a :class:`PreflightReport`
with per-check results. Exit-code semantics:

* 0: all checks pass.
* 1: at least one blocking check failed.
* 2: warnings only (no blockers).

Supported check kinds: topic_present, qos_match, node_running,
env_var, param_value, resource_threshold, custom_python.

An unrecognised kind is treated as ``("error", ...)`` rather than
``("skipped", ...)`` so that typos in rule files do not silently
pass the preflight gate (audit finding CF-1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from .checks import custom_python as _custom_python_check
from .checks import env_var as _env_var_check
from .checks import node_running as _node_running_check
from .checks import param_value as _param_value_check
from .checks import qos_match as _qos_match_check
from .checks import resource_threshold as _resource_threshold_check
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


_CHECK_REGISTRY: dict[str, CheckFn] = {
    "topic_present": _adapt(_topic_present_check.run),
    "qos_match": _adapt(_qos_match_check.run),
    "node_running": _adapt(_node_running_check.run),
    "env_var": _adapt(_env_var_check.run),
    "param_value": _adapt(_param_value_check.run),
    "resource_threshold": _adapt(_resource_threshold_check.run),
    "custom_python": _adapt(_custom_python_check.run),
}


def _unknown_kind(check: PreflightCheck) -> tuple[str, str]:
    """Fail-fast handler for kinds not present in _CHECK_REGISTRY.

    Returns ``("error", ...)`` so that a typo or future-version kind
    in a rule file does NOT silently produce a passing exit code.
    """
    supported = sorted(_CHECK_REGISTRY)
    return (
        "error",
        f"unknown check kind {check.kind!r}; supported: {supported}. "
        f"Fix the rule file or upgrade BlackBoxRS.",
    )


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

            fn = _CHECK_REGISTRY.get(rule.check.kind, _unknown_kind)
            kind_known = rule.check.kind in _CHECK_REGISTRY
            try:
                status, message = fn(rule.check)
            except Exception as exc:
                logger.exception("Check %s raised", rule.rule_id)
                status, message = "error", f"{type(exc).__name__}: {exc}"

            # If the underlying check failed and the rule says "block",
            # promote the status accordingly. Skipped/error stay as-is.
            if status == "fail":
                status = rule.check.severity_on_fail  # "warn" or "block"

            if kind_known:
                result = PreflightCheckResult(
                    rule_id=rule.rule_id,
                    name=rule.check.name,
                    kind=rule.check.kind,
                    status=status,  # type: ignore[arg-type]
                    message=message,
                    severity_on_fail=rule.check.severity_on_fail,
                )
            else:
                # Unknown kind bypassed Pydantic (e.g. via model_copy or direct
                # object mutation in tests / future YAML schema drift). Use
                # model_construct to avoid re-raising a ValidationError and so
                # the error result is still surfaced to the caller.
                result = PreflightCheckResult.model_construct(
                    rule_id=rule.rule_id,
                    name=rule.check.name,
                    kind=rule.check.kind,  # type: ignore[arg-type]
                    status=status,         # type: ignore[arg-type]
                    message=message,
                    severity_on_fail=rule.check.severity_on_fail,
                )
            results.append(result)

        finished = datetime.now(timezone.utc)
        return PreflightReport(
            started_at=started, finished_at=finished, results=results
        )
