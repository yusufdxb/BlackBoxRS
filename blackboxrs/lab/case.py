"""Replay cases: evidence + faults + system under test + expected outcome.

A case file (``blackboxrs.lab.case.v1``, JSON) names a flight bundle, the
faults to inject, the arbitration path to run, and what the replay must
conclude. The golden regression set in ``examples/replay_lab/cases/`` is a
directory of these files.

::

    {
      "schema": "blackboxrs.lab.case.v1",
      "name": "stale_command__twist_mux_legacy",
      "regression": "which failure class this pins down, and its source",
      "initial_conditions": "...",
      "evidence": "../evidence/nominal_motion",       # relative to this file
      "sut": {"mode": "reference", "preset": "twist_mux_legacy", "overrides": {}},
      "faults": [{"kind": "drop", "topic": "/nav/cmd_vel", "from_s": 4.0}],
      "topics": {"/lowstate": {"liveness": "periodic", "stale_after_s": 0.5}},
      "window": {"from_s": 0.0, "to_s": null},
      "monitors": {"stop_grace_s": 0.05},
      "expect": {"verdict": "FAIL", "invariants": {"fresh_output": "FAIL"},
                 "detections": ["command_source_stale"]}
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackboxrs.lab.engine import ReplayConfig
from blackboxrs.lab.faults import Fault, parse_fault

CASE_SCHEMA = "blackboxrs.lab.case.v1"
_KEYS = {"schema", "name", "description", "regression", "initial_conditions", "injected_fault",
         "evidence", "sut",
         "faults", "topics", "window", "monitors", "expect", "command_sources"}
_MONITOR_KEYS = {"stop_grace_s", "fresh_grace_s", "clock_step_threshold_s",
                 "clock_offset_info_s", "observed_period_s"}


class CaseError(ValueError):
    pass


@dataclass(frozen=True)
class Case:
    path: Path
    name: str
    evidence: Path
    config: ReplayConfig
    expect: dict[str, Any]
    raw: dict[str, Any]


def is_case_file(path: str | Path) -> bool:
    p = Path(path)
    return p.is_file() and p.suffix == ".json"


def load_case(path: str | Path) -> Case:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseError(f"{path}: cannot read case: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != CASE_SCHEMA:
        raise CaseError(f"{path}: not a {CASE_SCHEMA} file")
    extra = sorted(set(raw) - _KEYS)
    if extra:
        raise CaseError(f"{path}: unknown keys {extra}")
    for k in ("name", "evidence", "sut"):
        if k not in raw:
            raise CaseError(f"{path}: missing {k!r}")
    sut = raw["sut"]
    if not isinstance(sut, dict) or sut.get("mode") not in ("reference", "observed"):
        raise CaseError(f"{path}: sut.mode must be reference or observed")
    faults: list[Fault] = [parse_fault(f, i) for i, f in enumerate(raw.get("faults") or [])]
    mon = raw.get("monitors") or {}
    bad = sorted(set(mon) - _MONITOR_KEYS)
    if bad:
        raise CaseError(f"{path}: unknown monitor settings {bad}")
    win = raw.get("window") or {}
    cfg = ReplayConfig(
        sut_mode=sut["mode"], preset=sut.get("preset", "helix_arbiter"),
        overrides=dict(sut.get("overrides") or {}), faults=tuple(faults),
        topics=dict(raw.get("topics") or {}), from_s=win.get("from_s"), to_s=win.get("to_s"),
        command_sources={k: float(v) for k, v in (raw.get("command_sources") or {}).items()},
        **{k: float(v) for k, v in mon.items()})
    return Case(path=p, name=raw["name"],
                evidence=Path(os.path.normpath(p.parent / raw["evidence"])),
                config=cfg, expect=dict(raw.get("expect") or {}), raw=raw)
