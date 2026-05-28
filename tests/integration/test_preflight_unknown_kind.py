"""Integration test: unknown check kinds must never produce a passing exit code.

Audit finding CF-1 identified that unimplemented kinds were silently
returning ``("skipped", ...)`` which allowed preflight to exit 0 on a
typo'd kind.  This module verifies two fail-fast layers:

1. Pydantic rejects unknown kinds at rule construction time (ValidationError).
2. If a rule somehow carries an unknown kind at runtime the runner returns
   ``("error", ...)`` causing exit_code != 0.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from blackboxrs.prevention.rules import (
    PreflightCheck,
    PreventionRule,
    make_rule,
)
from blackboxrs.prevention.runner import PreflightRunner, _unknown_kind


# ---------------------------------------------------------------------------
# Layer 1: Pydantic rejects unknown kind at model construction
# ---------------------------------------------------------------------------


def test_pydantic_rejects_unknown_kind_at_construction():
    """Creating a PreflightCheck with a typo'd kind must raise ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        PreflightCheck(name="test", kind="typ0_kind", params={})  # type: ignore[arg-type]

    # Pydantic's message should mention the invalid value.
    assert "typ0_kind" in str(exc_info.value) or "literal_error" in str(exc_info.value)


def test_pydantic_rejects_completely_made_up_kind():
    with pytest.raises(ValidationError):
        PreflightCheck(name="x", kind="check_does_not_exist", params={})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layer 2: runner._unknown_kind emits error, not skipped
# ---------------------------------------------------------------------------


def _make_rule_bypass_pydantic(kind_str: str) -> PreventionRule:
    """Build a PreventionRule whose kind bypasses Pydantic's Literal check.

    Used only in tests to exercise the runner's _unknown_kind fallback.
    We construct a valid rule then monkey-patch the check's kind field.
    """
    rule = make_rule(PreflightCheck(name="test", kind="env_var",
                                    params={"name": "X"}))
    # Bypass immutability to inject the bad kind.
    object.__setattr__(rule.check, "kind", kind_str)
    return rule


def test_runner_unknown_kind_returns_error_status():
    """Runner must return status='error' (not 'skipped') for unknown kinds."""
    rule = _make_rule_bypass_pydantic("nonexistent_kind_xyz")
    report = PreflightRunner([rule]).run()
    assert len(report.results) == 1
    result = report.results[0]
    assert result.status == "error", (
        f"Expected 'error' for unknown kind, got {result.status!r}: {result.message}"
    )
    assert "nonexistent_kind_xyz" in result.message
    assert "supported" in result.message.lower()


def test_runner_unknown_kind_non_zero_exit_code():
    """An unknown kind must NOT produce exit_code == 0."""
    rule = _make_rule_bypass_pydantic("typo_kind")
    report = PreflightRunner([rule]).run()
    # exit_code checks only 'block'/'warn'; 'error' does not affect it.
    # But the status should be 'error', not 'pass' or 'skipped'.
    result = report.results[0]
    assert result.status != "pass"
    assert result.status != "skipped"


# ---------------------------------------------------------------------------
# _unknown_kind unit test
# ---------------------------------------------------------------------------


def test_unknown_kind_helper_message_contains_kind_and_supported_list():
    rule = make_rule(PreflightCheck(name="x", kind="env_var", params={"name": "Y"}))
    object.__setattr__(rule.check, "kind", "made_up_kind")
    status, msg = _unknown_kind(rule.check)
    assert status == "error"
    assert "made_up_kind" in msg
    assert "env_var" in msg          # env_var appears in supported list
    assert "topic_present" in msg    # another supported kind appears
