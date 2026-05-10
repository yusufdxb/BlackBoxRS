"""Prevention rule + preflight check models, with YAML I/O.

A :class:`PreventionRule` carries the *reason* a check exists (which
incident produced it, which fingerprint it matches, what the rationale
is). The actual check semantics are in :class:`PreflightCheck`.
"""

from __future__ import annotations

import hashlib
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
    check: PreflightCheck
    rationale: str = ""
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
        """0 if all pass, 1 if any blocked, 2 if only warnings."""
        any_block = any(r.status == "block" for r in self.results)
        any_warn = any(r.status == "warn" for r in self.results)
        if any_block:
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


def make_rule(
    check: PreflightCheck,
    *,
    rationale: str = "",
    source_incident_id: str | None = None,
    source_fingerprint_id: str | None = None,
) -> PreventionRule:
    """Build a :class:`PreventionRule` with a deterministic id."""
    rule_id = _make_rule_id(check, rationale)
    if check.check_id is None:
        check = check.model_copy(update={"check_id": rule_id})
    if check.produced_by is None and source_incident_id:
        check = check.model_copy(update={"produced_by": rule_id})
    return PreventionRule(
        rule_id=rule_id,
        created_at=datetime.now(timezone.utc),
        source_incident_id=source_incident_id,
        source_fingerprint_id=source_fingerprint_id,
        check=check,
        rationale=rationale,
    )


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
    return PreventionRule.model_validate(data)


def load_rules(rules_dir: Path) -> list[PreventionRule]:
    """Load every ``*.yaml`` rule from a directory.

    Missing or empty directory yields an empty list.
    """
    rules_dir = Path(rules_dir)
    if not rules_dir.is_dir():
        return []
    return [load_rule(p) for p in sorted(rules_dir.glob("*.yaml"))]
