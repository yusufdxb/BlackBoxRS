"""Prevention rule + preflight check models, with YAML I/O.

A :class:`PreventionRule` carries the *reason* a check exists (which
incident produced it, which fingerprint it matches, what the rationale
is). The actual check semantics are in :class:`PreflightCheck`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


CheckKind = Literal[
    "topic_present",
    "qos_match",
    "node_running",
    "env_var",
    "param_value",
    "resource_threshold",
    "custom_python",
    "telemetry_health",
]

SeverityOnFail = Literal["warn", "block"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PreflightCheck(BaseModel):
    """A single preflight check with parameters and severity policy."""

    model_config = ConfigDict(extra="forbid")

    check_id: str | None = None
    name: str
    kind: CheckKind
    params: dict[str, Any] = Field(default_factory=dict)
    severity_on_fail: SeverityOnFail = "block"
    message_template: str = "Preflight check {name} failed."
    applies_to: list[str] = Field(default_factory=list)
    produced_by: str | None = None


class PreventionRule(BaseModel):
    """A prevention rule, normally derived from an incident.

    Stored on disk as a YAML file under
    ``~/.blackboxrs/prevention/rules/<rule_id>.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    created_at: datetime
    source_incident_id: str | None = None
    source_fingerprint_id: str | None = None
    source_trigger_ids: list[str] = Field(default_factory=list)
    rule_fingerprint: str | None = None
    check: PreflightCheck
    rationale: str = ""
    derivation: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False
    last_fired: datetime | None = None
    fire_count: int = 0


class PreflightCheckResult(BaseModel):
    """Single-check execution outcome."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    name: str
    kind: CheckKind
    status: Literal["pass", "warn", "block", "skipped", "error"]
    message: str = ""
    severity_on_fail: SeverityOnFail = "block"


class PreflightReport(BaseModel):
    """Aggregated result of a preflight run."""

    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    finished_at: datetime
    results: list[PreflightCheckResult] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 if all pass, 1 if any blocked/error, 2 if only warnings."""
        any_block = any(r.status == "block" for r in self.results)
        any_error = any(r.status == "error" for r in self.results)
        any_warn = any(r.status == "warn" for r in self.results)
        if any_block or any_error:
            return 1
        if any_warn:
            return 2
        return 0


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _make_rule_id(check: PreflightCheck, rationale: str) -> str:
    """Stable id from check kind + params + rationale."""
    payload = (
        f"{check.kind}|{sorted(check.params.items())!r}|{rationale}".encode("utf-8")
    )
    return "rule_" + hashlib.sha256(payload).hexdigest()[:8]


def _fingerprint_payload(
    *,
    check: PreflightCheck,
    rationale: str,
    source_incident_id: str | None,
    source_fingerprint_id: str | None,
    source_trigger_ids: list[str],
    derivation: dict[str, Any],
) -> dict[str, Any]:
    """Return the immutable rule fields covered by the full fingerprint."""
    return {
        "check": check.model_dump(mode="json"),
        "derivation": derivation,
        "rationale": rationale,
        "source_fingerprint_id": source_fingerprint_id,
        "source_incident_id": source_incident_id,
        "source_trigger_ids": source_trigger_ids,
    }


def compute_rule_fingerprint(rule: PreventionRule) -> str:
    """Compute a full SHA-256 over immutable rule semantics and provenance."""
    payload = _fingerprint_payload(
        check=rule.check,
        rationale=rule.rationale,
        source_incident_id=rule.source_incident_id,
        source_fingerprint_id=rule.source_fingerprint_id,
        source_trigger_ids=rule.source_trigger_ids,
        derivation=rule.derivation,
    )
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verify_rule_fingerprint(rule: PreventionRule) -> bool:
    """Return whether a stored full fingerprint matches the rule contents."""
    return (
        rule.rule_fingerprint is not None
        and rule.rule_fingerprint == compute_rule_fingerprint(rule)
    )


def make_rule(
    check: PreflightCheck,
    *,
    rationale: str = "",
    source_incident_id: str | None = None,
    source_fingerprint_id: str | None = None,
    source_trigger_ids: list[str] | None = None,
    derivation: dict[str, Any] | None = None,
) -> PreventionRule:
    """Build a :class:`PreventionRule` with a deterministic id."""
    rule_id = _make_rule_id(check, rationale)
    if check.check_id is None:
        check = check.model_copy(update={"check_id": rule_id})
    if check.produced_by is None and source_incident_id:
        check = check.model_copy(update={"produced_by": rule_id})
    rule = PreventionRule(
        rule_id=rule_id,
        created_at=datetime.now(timezone.utc),
        source_incident_id=source_incident_id,
        source_fingerprint_id=source_fingerprint_id,
        source_trigger_ids=list(source_trigger_ids or []),
        check=check,
        rationale=rationale,
        derivation=dict(derivation or {}),
    )
    return rule.model_copy(update={"rule_fingerprint": compute_rule_fingerprint(rule)})


def save_rule(rule: PreventionRule, rules_dir: Path) -> Path:
    """Persist *rule* to ``<rules_dir>/<rule_id>.yaml``."""
    rules_dir = Path(rules_dir)
    rules_dir.mkdir(parents=True, exist_ok=True)
    target = rules_dir / f"{rule.rule_id}.yaml"
    with open(target, "w", encoding="utf-8") as fh:
        yaml.safe_dump(
            rule.model_dump(mode="json"),
            fh,
            default_flow_style=False,
            sort_keys=True,
        )
    return target


def load_rule(path: Path) -> PreventionRule:
    """Load a single :class:`PreventionRule` from a YAML file."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    rule = PreventionRule.model_validate(data)
    if rule.rule_fingerprint is not None and not verify_rule_fingerprint(rule):
        raise ValueError(f"Rule fingerprint mismatch: {path}")
    return rule


def load_rules(rules_dir: Path) -> list[PreventionRule]:
    """Load every ``*.yaml`` rule from a directory.

    Missing or empty directory yields an empty list.
    """
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return []
    return [load_rule(p) for p in sorted(rules_dir.glob("*.yaml"))]
