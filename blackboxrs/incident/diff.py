"""Config and version signature diff models + computation.

Compares the current incident's :class:`ConfigSignature` and
:class:`VersionSignature` against a *baseline* (typically the most
recent prior bundle on the same host). Surfaces three buckets:

- **changed**: keys present in both, with different values.
- **only_in_current**: keys present now but not in the baseline.
- **only_in_baseline**: keys present in the baseline but not now.

What the diff does NOT claim:

- That a change *caused* the incident. The renderer presents the diff
  alongside likely-cause hypotheses; it never promotes a config diff
  to a cause without supporting evidence.
- That equality means safety. Two runs can have identical configs and
  still fail differently because of timing, network, or hardware.

The diff is persisted at ``signatures/diff.json`` so the report is
self-contained.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import ConfigSignature, VersionSignature


class FieldChange(BaseModel):
    """A single key-level change with both sides."""

    model_config = ConfigDict(extra="forbid")

    key: str
    before: Any | None = None
    after: Any | None = None


class SignatureDiff(BaseModel):
    """Structured comparison of two payload dicts."""

    model_config = ConfigDict(extra="forbid")

    baseline_hash: str | None = None
    current_hash: str
    identical: bool = False
    changed: list[FieldChange] = Field(default_factory=list)
    only_in_current: list[FieldChange] = Field(default_factory=list)
    only_in_baseline: list[FieldChange] = Field(default_factory=list)


class IncidentDiff(BaseModel):
    """Combined config + version diffs for an incident bundle."""

    model_config = ConfigDict(extra="forbid")

    config: SignatureDiff
    versions: SignatureDiff


def _flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict to dotted keys for symmetric diffing.

    Lists are compared as opaque values; we don't recurse into list
    elements because order changes are not always meaningful.
    """
    flat: dict[str, Any] = {}
    for key, value in payload.items():
        full = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, full))
        else:
            flat[full] = value
    return flat


def _diff_dicts(prev: dict[str, Any], curr: dict[str, Any]) -> SignatureDiff:
    flat_prev = _flatten(prev)
    flat_curr = _flatten(curr)

    changed: list[FieldChange] = []
    only_curr: list[FieldChange] = []
    only_prev: list[FieldChange] = []

    for key in sorted(flat_curr.keys() & flat_prev.keys()):
        if flat_curr[key] != flat_prev[key]:
            changed.append(FieldChange(
                key=key, before=flat_prev[key], after=flat_curr[key]
            ))
    for key in sorted(flat_curr.keys() - flat_prev.keys()):
        only_curr.append(FieldChange(key=key, after=flat_curr[key]))
    for key in sorted(flat_prev.keys() - flat_curr.keys()):
        only_prev.append(FieldChange(key=key, before=flat_prev[key]))

    return SignatureDiff(
        current_hash="",  # filled in by the caller
        identical=not (changed or only_curr or only_prev),
        changed=changed,
        only_in_current=only_curr,
        only_in_baseline=only_prev,
    )


def compute(
    baseline_config: ConfigSignature | None,
    baseline_version: VersionSignature | None,
    current_config: ConfigSignature,
    current_version: VersionSignature,
) -> IncidentDiff:
    """Compute the full incident diff against a baseline.

    Either baseline may be ``None`` (no prior bundle on this host); the
    corresponding diff is then "everything is new" with
    ``baseline_hash=None``.
    """
    # Config side.
    if baseline_config is None:
        cfg_diff = SignatureDiff(
            current_hash=current_config.hash,
            identical=False,
            only_in_current=[
                FieldChange(key=k, after=v)
                for k, v in sorted(_flatten(current_config.payload).items())
            ],
        )
    elif baseline_config.hash == current_config.hash:
        cfg_diff = SignatureDiff(
            baseline_hash=baseline_config.hash,
            current_hash=current_config.hash,
            identical=True,
        )
    else:
        cfg_diff = _diff_dicts(baseline_config.payload, current_config.payload)
        cfg_diff = cfg_diff.model_copy(update={
            "baseline_hash": baseline_config.hash,
            "current_hash": current_config.hash,
        })

    # Version side.
    if baseline_version is None:
        ver_diff = SignatureDiff(
            current_hash=current_version.hash,
            identical=False,
            only_in_current=[
                FieldChange(key=k, after=v)
                for k, v in sorted(_flatten(current_version.payload).items())
            ],
        )
    elif baseline_version.hash == current_version.hash:
        ver_diff = SignatureDiff(
            baseline_hash=baseline_version.hash,
            current_hash=current_version.hash,
            identical=True,
        )
    else:
        ver_diff = _diff_dicts(baseline_version.payload, current_version.payload)
        ver_diff = ver_diff.model_copy(update={
            "baseline_hash": baseline_version.hash,
            "current_hash": current_version.hash,
        })

    return IncidentDiff(config=cfg_diff, versions=ver_diff)
