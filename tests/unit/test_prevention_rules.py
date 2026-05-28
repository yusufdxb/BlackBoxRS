"""Prevention rule YAML I/O + runner exit-code semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from blackboxrs.prevention.rules import (
    PreflightCheck,
    PreflightCheckResult,
    PreflightReport,
    load_rules,
    make_rule,
    save_rule,
)
from blackboxrs.prevention.runner import PreflightRunner


_NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_make_rule_deterministic_id():
    chk = PreflightCheck(name="x", kind="topic_present", params={"topic": "/scan"})
    a = make_rule(chk, rationale="r")
    b = make_rule(chk, rationale="r")
    # rule_ids will tie because the payload is identical
    assert a.rule_id == b.rule_id


def test_save_load_roundtrip(tmp_path: Path):
    chk = PreflightCheck(name="x", kind="topic_present", params={"topic": "/scan"})
    rule = make_rule(chk, rationale="r")
    target = save_rule(rule, tmp_path)
    assert target.exists()
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0].rule_id == rule.rule_id


def test_runner_no_rules_passes():
    runner = PreflightRunner([])
    report = runner.run()
    assert report.exit_code == 0


def test_runner_all_four_checks_are_implemented():
    """env_var / param_value / resource_threshold / custom_python are now
    real implementations (CF-1 fix).  None should return 'skipped' with the
    old stub message; they should all return a real status (pass/fail/error/skipped
    for legitimate reasons like rclpy absent, psutil absent, etc.) -- never the
    old 'not implemented yet' text."""
    rules = [
        make_rule(PreflightCheck(name="env",
                                 kind="env_var", params={"name": "FOO"})),
        make_rule(PreflightCheck(name="param",
                                 kind="param_value", params={"node_name": "n",
                                                             "parameter": "p"})),
        make_rule(PreflightCheck(name="res",
                                 kind="resource_threshold",
                                 params={"metric": "cpu_percent", "max": 99})),
        make_rule(PreflightCheck(name="py",
                                 kind="custom_python",
                                 params={"callable": "no_such_module:fn"})),
    ]
    runner = PreflightRunner(rules)
    report = runner.run()
    for r in report.results:
        assert "not implemented yet" not in r.message


def test_report_exit_code_with_warn_and_block():
    report = PreflightReport(
        started_at=_NOW, finished_at=_NOW,
        results=[
            PreflightCheckResult(
                rule_id="r1", name="a", kind="topic_present",
                status="warn", severity_on_fail="warn",
            ),
            PreflightCheckResult(
                rule_id="r2", name="b", kind="qos_match",
                status="block", severity_on_fail="block",
            ),
        ],
    )
    assert report.exit_code == 1  # any block wins

    warn_only = PreflightReport(
        started_at=_NOW, finished_at=_NOW,
        results=[
            PreflightCheckResult(
                rule_id="r1", name="a", kind="topic_present",
                status="warn", severity_on_fail="warn",
            ),
        ],
    )
    assert warn_only.exit_code == 2

    all_pass = PreflightReport(
        started_at=_NOW, finished_at=_NOW,
        results=[
            PreflightCheckResult(
                rule_id="r1", name="a", kind="topic_present",
                status="pass", severity_on_fail="warn",
            ),
        ],
    )
    assert all_pass.exit_code == 0


def test_disabled_rules_are_skipped():
    chk = PreflightCheck(name="x", kind="topic_present", params={})
    rule = make_rule(chk).model_copy(update={"disabled": True})
    runner = PreflightRunner([rule])
    report = runner.run()
    assert report.results[0].status == "skipped"
    assert "disabled" in report.results[0].message.lower()
