# BlackBoxRS: Repo Restructure Plan

How the existing tree changes to support the new incident-intelligence
direction without breaking the v0.3 capture path or the existing test
suite.

Conventions:
- "Keep" = unchanged, still imported under the same path.
- "Rename" = imported under a new path; an alias may be left for one
  release.
- "Split" = single module becomes two or more.
- "New" = did not exist before.

---

## 1. Module-by-module disposition

### `blackboxrs/core/`: keep, extend

| Item | Action | Notes |
|---|---|---|
| `core/clock.py` | keep | unchanged |
| `core/event_bus.py` | keep | unchanged; still the daemon's pub/sub |
| `core/schemas.py` | keep | `BlackBoxEvent` is still the canonical event |
| `core/session.py` | keep + extend | new `session_dir()` helper for per-session state cache |
| `core/config.py` | keep + extend | add `IncidentConfig`, `PreventionConfig` sub-dataclasses |
| `core/signatures/` | new | `config.py`, `versions.py` collectors |
| `core/snapshots.py` | new | `SystemSnapshotter` projects monitor outputs into typed snapshots |

### `blackboxrs/ros_monitor/`: keep, additive only

No behavioural change. We can later add `tf_static` / `/tf` topic
inspection but not in v0.4.

### `blackboxrs/system_monitor/`: keep, additive only

We expose a small adapter so `core/snapshots.py` can read the latest
metric values without re-implementing collectors.

### `blackboxrs/anomaly_engine/`: keep, additive only

| Item | Action | Notes |
|---|---|---|
| `engine.py` | keep | unchanged |
| `detectors/base.py` | extend | add optional `signature_fields: list[str] = []` class attribute |
| `detectors/threshold.py` | extend | declare `signature_fields = ["metric"]` |
| `detectors/frequency.py` | extend | declare `signature_fields = ["topic"]` |
| `detectors/dead_topic.py` | extend | declare `signature_fields = ["topic"]` |
| `detectors/qos_mismatch.py` | extend | declare `signature_fields = ["topic","reliability","durability"]` |
| `detectors/loader.py` | keep | unchanged |

These additions are backward compatible: existing custom detectors
continue to work; `signature_fields` defaults to `[]`.

### `blackboxrs/recording/`: keep, light rename consideration

| Item | Action | Notes |
|---|---|---|
| `recording/rosbag2.py` | keep | still triggered by anomaly events; bag dirs become incident attachments via `incident attach --bag` |

Stretch: rename the package to `blackboxrs/recording/rosbag/` if more
recorder backends arrive. Out of scope for v0.4.

### `blackboxrs/logging/`: keep, additive

| Item | Action | Notes |
|---|---|---|
| `logging/writer.py` | keep | rotating writer continues to be the substrate |
| `logging/reader.py` | keep | the incident builder consumes it |
| `logging/pipeline.py` | keep | unchanged |

### `blackboxrs/metrics/`: keep, deemphasize

| Item | Action | Notes |
|---|---|---|
| `metrics/prometheus_exporter.py` | keep | still optional via `[prometheus]` extra; not part of the new wedge |

### `blackboxrs/cli/`: keep + split + extend

| Item | Action | Notes |
|---|---|---|
| `cli/app.py` | split | becomes a thin `cli/__init__.py` that imports subcommand groups |
| `cli/daemon_cmd.py` | new | extracts the existing `start/stop/status/dump-log/replay/config/init` commands |
| `cli/incident_cmd.py` | new | `incident build/show/list/attach/pack/unpack` |
| `cli/preflight_cmd.py` | new | `preflight`, `prevention adopt/list/disable` |
| `cli/formatters.py` | keep | reused by both old and new commands |
| `cli/daemon.py` | keep | this is `BlackBoxDaemon`; the *class*, not the CLI command. Rename note below. |

**Naming cleanup.** `cli/daemon.py` (the class) and `cli/daemon_cmd.py`
(the new CLI subcommand group) coexist. Acceptable but ambiguous.
Optional rename: `cli/daemon.py` → `core/daemon.py`. Out of scope for
v0.4 unless we can do it without breaking `from blackboxrs.cli.daemon
import BlackBoxDaemon` callers (we control all of them; safe). Decision:
**rename in v0.4** with a one-line shim left in `cli/daemon.py` for
external consumers. (See deprecation table below.)

### `blackboxrs/incident/`: new

The pivot's home.

```
blackboxrs/incident/
  __init__.py        # public API: build_incident, load_bundle, render_report
  models.py          # pydantic models from ARCHITECTURE_PIVOT §1
  builder.py         # IncidentBuilder
  bundle.py          # BundleWriter, BundleReader, paths
  timeline.py        # reconstruct() + derived event detectors
  diff.py            # ConfigDiff, VersionDiff
  fingerprint.py     # compute() for FailureFingerprint
  report.py          # markdown renderer
  pack.py            # pack/unpack (M7)
  cause.py           # likely-cause ranking heuristics
  api.py             # high-level functions for non-CLI consumers
```

### `blackboxrs/prevention/`: new

```
blackboxrs/prevention/
  __init__.py
  rules.py           # PreventionRule + PreflightCheck models, YAML I/O
  runner.py          # PreflightRunner, exit codes
  checks/
    __init__.py
    topic_present.py
    qos_match.py
    node_running.py
    env_var.py        (stub for v0.4)
    param_value.py    (stub for v0.4)
    resource_threshold.py (stub for v0.4)
    custom_python.py  (stub; mirrors the existing detector loader pattern)
```

### `tests/`: extend

```
tests/
  unit/
    test_incident_models.py
    test_bundle_layout.py
    test_report_renderer.py
    test_timeline_silence.py
    test_timeline_resource_excursion.py
    test_timeline_graph_delta.py
    test_config_signature.py
    test_version_signature.py
    test_config_diff.py
    test_fingerprint_determinism.py
    test_fingerprint_collision.py
    test_fingerprint_normalization.py
    test_prevention_rule_yaml.py
    test_preflight_runner.py
    test_topic_present_check.py
    test_qos_match_check.py
    test_node_running_check.py
  integration/
    test_incident_builder_basic.py
    test_incident_show.py
    test_timeline_end_to_end.py
    test_signature_session_capture.py
    test_preflight_live.py        # gated on _ROS_AVAILABLE
  fixtures/
    sessions/
      session_minimal.jsonl       # synthetic, ~50 events
      session_tf_break.jsonl      # synthetic, S1 scenario seed
      session_qos_mismatch.jsonl  # synthetic, S3 scenario seed
    bundles/
      inc_demo_tf_break/          # canonical built bundle (small)
      inc_demo_qos_mismatch/
```

### `examples/`: new (top-level)

```
examples/
  incidents/                    # human-readable showcase bundles
    inc_demo_tf_break/
      report.md                 # checked in for landing page reuse
      ...
    inc_demo_qos_mismatch/
  prevention/
    sample_rules.yaml           # human-edited sample rule library
```

`examples/incidents/` is *committed* and used by tests; the demo
fixtures live under `tests/fixtures/`.

### `docs/`: extend

| Item | Action | Notes |
|---|---|---|
| `docs/ARCHITECTURE.md` | rewrite | superseded by `ARCHITECTURE_PIVOT.md` becoming the new ARCHITECTURE.md when v0.4 ships |
| `docs/BENCHMARKS.md` | keep | unchanged; performance-regression gate continues |
| `docs/incident-anatomy.md` | new | annotated walk-through of `inc_demo_tf_break` |
| `docs/preflight.md` | new | guide to writing prevention rules |
| `docs/superpowers/...` | keep | existing custom-detector design docs stay |

When v0.4 ships:
- `PIVOT_BRIEF.md` → archived under `docs/history/PIVOT_BRIEF.md`.
- `ARCHITECTURE_PIVOT.md` → becomes `docs/ARCHITECTURE.md`.
- `ROADMAP_V0_4.md`, `DEMO_PLAN.md`, `REPO_RESTRUCTURE_PLAN.md`,
  `POSITIONING.md`, `STATUS_AND_LIMITATIONS_REWRITE.md`,
  `TASKS_V0_4.md` → archived under `docs/history/`.

Until v0.4 ships they live at the repo root, where they steer
day-to-day execution.

---

## 2. Final package layout (target)

```
blackboxrs/
  __init__.py
  __main__.py
  core/
    clock.py
    config.py
    event_bus.py
    schemas.py
    session.py
    snapshots.py            (new)
    daemon.py               (renamed from cli/daemon.py)
    signatures/             (new)
      __init__.py
      config.py
      versions.py
  ros_monitor/
  system_monitor/
  anomaly_engine/
  recording/
  logging/
  metrics/
  incident/                 (new)
    __init__.py
    api.py
    bundle.py
    builder.py
    cause.py
    diff.py
    fingerprint.py
    models.py
    pack.py
    report.py
    timeline.py
  prevention/               (new)
    __init__.py
    runner.py
    rules.py
    checks/
      __init__.py
      topic_present.py
      qos_match.py
      node_running.py
      env_var.py
      param_value.py
      resource_threshold.py
      custom_python.py
  cli/
    __init__.py             (now exports `cli` group; aggregates subcommands)
    app.py                  (kept for backward compat; thin re-export)
    daemon.py               (one-line shim: `from blackboxrs.core.daemon import *`)
    daemon_cmd.py           (subcommands moved out of app.py)
    incident_cmd.py         (new)
    preflight_cmd.py        (new)
    formatters.py
```

## 3. Boundaries

We separate **library** from **CLI**.

- **Library (importable).** Everything in `blackboxrs/{core, incident,
  prevention, ros_monitor, system_monitor, anomaly_engine, logging,
  recording}` exposes Python APIs. No `click`, no `print`. Returns
  values; raises typed exceptions; logs through `logging`.
- **CLI.** Everything in `blackboxrs/cli/` is allowed to use `click`,
  print, and call `sys.exit`. CLI imports library, never the reverse.
- **Data formats.** `incident/` ships JSON Schema and pydantic models
  for `Incident`, `EvidenceBundle`, `FailureFingerprint`,
  `PreventionRule`, etc. External tools should be able to read a
  bundle without depending on this package.

This boundary is checked by a small `tests/unit/test_module_boundaries.py`
that imports each library module in a subprocess and asserts
`click` is not pulled in.

---

## 4. Naming cleanup

- `BlackBoxDaemon` (class) stays. Renaming it from `cli/daemon.py` to
  `core/daemon.py` reflects the truth: the daemon orchestrator is core
  infrastructure, not a CLI concern. The CLI subcommand that *starts*
  the daemon lives in `cli/daemon_cmd.py`.
- `Rosbag2Recorder` keeps its name. The pyclass remains the recorder.
- New names are deliberately boring: `IncidentBuilder`, `BundleWriter`,
  `BundleReader`, `PreflightRunner`, `PreventionRule`. Avoid clever
  metaphors (no "blackbox flight", no "voyager", etc.). The product
  is *named* BlackBoxRS; the modules should be plain.
- The repo currently spells the package `blackboxrs` (no
  capitalisation). Keep it. Do not introduce CamelCase to module names.

---

## 5. Backward compatibility table

| Old import path | New import path | Notes |
|---|---|---|
| `from blackboxrs.cli.daemon import BlackBoxDaemon` | `from blackboxrs.core.daemon import BlackBoxDaemon` | shim left in place for one release |
| `from blackboxrs.cli.app import cli` | unchanged | `cli/app.py` becomes a thin re-export |
| `from blackboxrs.core.schemas import BlackBoxEvent` | unchanged | |
| `from blackboxrs.core.config import BlackBoxConfig` | unchanged | new subsections added; old keys still parse |
| `from blackboxrs.anomaly_engine.detectors import BaseDetector` | unchanged | new optional `signature_fields` attribute |

A small `tests/unit/test_backcompat_imports.py` asserts each row of
this table works.

---

## 6. What to remove or de-emphasize

Nothing is removed. Specifically:

- The `Prometheus exporter` stays. It is not the wedge; it is a
  reasonable adjacent feature. We will simply not lead with it.
- The `replay` command stays exactly as it is (event re-print). We
  also add `incident replay <bundle>` which renders the report; the
  two are documented as different operations.
- The existing four detectors stay. We extend them; we do not
  rewrite them.

---

## 7. Migration sequence (concrete order)

1. **M1 prep**: create `blackboxrs/incident/` with `models.py`,
   `bundle.py`, and stubs for the rest. Wire through to the CLI as
   `incident build` and have it return a partial bundle. CI green.
2. **CLI split**: move existing commands from `cli/app.py` into
   `cli/daemon_cmd.py`. Leave `cli/app.py` re-exporting `cli`.
   Run all integration tests; green.
3. **Daemon move**: `cli/daemon.py` → `core/daemon.py` with shim.
   Tests green.
4. **M2 onward**: implement each milestone behind the `incident_cmd`
   surface.
5. **Prevention package**: only land after M5; do not introduce
   half-finished prevention plumbing earlier.

This sequence keeps every commit shippable.
