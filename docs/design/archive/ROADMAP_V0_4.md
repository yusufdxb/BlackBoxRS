# BlackBoxRS: v0.4 Roadmap

Realistic plan for one strong founder + Claude Code, August 2026 ship
target. Anchors on `PIVOT_BRIEF.md` and `ARCHITECTURE_PIVOT.md`.

---

## 0. Constraints used to plan

- One developer with Claude Code support. ~10 focused hours/week.
- v0.3.0 is shipped; CI is green; 161 tests pass. Do not break that.
- Must remain installable on the existing Ubuntu 22.04 + ROS 2 Humble
  + Jetson Orin path.
- Demo-able vertical slice required at each milestone.
- v0.4 is a *bundled* release. Public ship at the end of M5; M6/M7
  are stretch.

---

## 1. Milestone overview

```
M1  Incident schema + bundle pipeline     (MVP foundation)         ★
M2  Human-readable incident report        (MVP, demo unlock)       ★
M3  Timeline reconstruction               (MVP, demo unlock)       ★
M4  Config + version signatures + diff    (MVP, reproducibility)   ★
M5  Failure fingerprint v1                (MVP, dedup + recurrence)★
M6  Preflight prevention engine (3 checks)(stretch / public)
M7  Replay packaging                      (stretch / public)
M8  Documentation + landing demo          (always)
```

★ = required for v0.4 MVP. M6/M7 land in v0.4 if time permits;
otherwise they ship as v0.4.1.

---

## 2. Dependency graph

```
M1 ──► M2
   ╲
    ╲► M3 ──► M5
       │       │
       └───────┴──► M6
                    │
                    └──► M7 (independent of M6 in theory; gated by M5)
M4 ──► (feeds M2 report content)
M4 ──► (feeds M6 preflight comparisons)
```

What can be parallel: M4 (signatures) is independent of M2/M3 and can
land any time after M1. Everything else is serial.

---

## 3. Cuts (explicit)

These are *not* in v0.4:

- Cross-incident clustering. (`cluster_id` reserved on fingerprint.)
- LLM-based narrative generation. The report is template-rendered.
- Web dashboard / hosted UI.
- Multi-host capture.
- TF tree topology analysis. (Topic-level `/tf` only.)
- Time-aligned ROS playback in `incident replay`.
- Auto-attaching journalctl / dmesg.
- Auto-trigger ("build incident automatically when severity ≥ X").
  Manual `incident build` only in v0.4.

---

## 4. MVP criteria

v0.4 MVP ships when *all* of the following are true:

1. `robot-blackbox incident build --since 10m` produces a complete
   bundle (every required file is present, schema-valid).
2. `incident.json` round-trips through pydantic without loss.
3. `report.md` is hand-readable and every claim resolves to a file
   in the bundle.
4. The same bundle, fed to `robot-blackbox incident show <path>`,
   re-renders the same report.
5. A bundle includes a deterministic `FailureFingerprint`.
6. `ConfigSignature` and `VersionSignature` are present.
7. ≥ 90 unit + integration tests covering the new modules; full suite
   stays green.
8. The synthetic demo flow in `DEMO_PLAN.md` scenarios 1 and 2
   completes end-to-end on mewtwo without manual editing of the
   bundle.

---

## 5. Stretch goals

- M6 (preflight) lands; `prevention adopt --from-incident` works for
  `topic_present` and `qos_match`.
- M7 (`incident pack` / `unpack`) ships with deterministic tarballs.
- A 2-minute screencast that runs the demo plan top to bottom.

---

## 6. Failure modes and risks

- **Schema thrash.** If we change the `Incident` schema after M2, every
  test fixture breaks. Mitigation: lock schema in M1, write a
  `schema_version` migration test before shipping.
- **Performance regression.** Building an incident from a 50 MB JSONL
  set must stay under 5 s on mewtwo. Mitigation: explicit benchmark
  in M3 verification.
- **`rclpy` flake on preflight tests.** ROS-coupled tests are flaky on
  CI. Mitigation: keep the rclpy-using preflight tests in
  `tests/integration/test_ros_live.py` and gate behind the existing
  `_ROS_AVAILABLE` switch.
- **Bundle pollution from huge attachments.** A user attaches a 4 GB
  rosbag. Mitigation: `incident attach` enforces a configurable size
  cap and stores symlinks by default with `--copy` for explicit
  copying.
- **Fingerprint over-clustering.** We will see false collisions early.
  Mitigation: the report makes the fingerprint payload visible so a
  user can reject the match before the rule is adopted.

---

## 7. Verification plan

Per milestone:

| Milestone | Unit tests | Integration tests | Manual demo |
|---|---|---|---|
| M1 | model round-trip, bundle path layout, idempotency | build from synthetic JSONL fixture | `incident build --since 10m` on a recorded session |
| M2 | report contains all required sections, no orphan claims | render → re-load consistency | open `report.md` in a fresh editor |
| M3 | derived event correctness (silence interval, resource excursion) | end-to-end timeline from fixture | confirm timeline shows the seeded failure |
| M4 | signature determinism (same input → same hash) | session start writes signatures | tamper a file → hash changes |
| M5 | fingerprint determinism, normalization sanity | two seeded incidents with same kind collide | manual review of fingerprint payload |
| M6 | each check kind unit-tested | preflight against live ROS graph | block-on-fail, warn-on-fail behaviour |
| M7 | pack / unpack round-trip | tar manifest verifier | unzip on a clean machine, run `incident show` |

CI gate: every milestone must pass `pytest`, `ruff`, and the existing
benchmark regression check before merge to `main`.

---

## 8. Milestones in detail

### M1: Incident schema + bundle pipeline (★ MVP)

**Objective.** Create the on-disk bundle layout and the pydantic
models from `ARCHITECTURE_PIVOT.md` §1. Build a basic
`IncidentBuilder` that, given a `LogReader` and a time window,
produces a *partial* bundle (no timeline yet, no fingerprint, no
report).

**Inputs.**
- `~/.blackboxrs/logs/blackboxrs_*.jsonl`
- `(window_start, window_end)`
- `output_dir` (defaults to `~/.blackboxrs/incidents/`)

**Outputs.**
- A directory `inc_<id>/` with:
  - `incident.json` (minimal: id, window, session, severity).
  - `evidence/events.jsonl` (sliced).
  - `evidence/triggers.json` (`DetectorTrigger`s promoted from
    `anomaly_engine` events).
  - `signatures/` placeholder files (filled by M4).
  - Empty `attachments/`.

**Implementation.**
- New package `blackboxrs/incident/`:
  - `models.py`: `Incident`, `EvidenceBundle`, `TimelineEvent`,
    `DetectorTrigger`, `SystemSnapshot`, `LikelyCauseHypothesis`,
    `FailureFingerprint`.
  - `builder.py`: `IncidentBuilder` (orchestrator).
  - `bundle.py`: `BundleWriter`, `BundleReader` (path/IO details).
  - `__init__.py`: public API: `build_incident`, `load_bundle`.

**Tests.**
- `tests/unit/test_incident_models.py`: model round-trip, validation.
- `tests/unit/test_bundle_layout.py`: exact file layout.
- `tests/integration/test_incident_builder_basic.py`: synthetic
  events fixture in, partial bundle out.

**Demo impact.** None standalone; unblocks M2.

---

### M2: Human-readable incident report (★ MVP)

**Objective.** Render `report.md` from a built bundle. This is the
demo unlock.

**Inputs.** A bundle directory from M1.

**Outputs.** `report.md` inside the bundle.

**Implementation.**
- `blackboxrs/incident/report.py`: pure-Python markdown renderer;
  no jinja dependency. Renderer is deterministic.
- Sections per `ARCHITECTURE_PIVOT.md` §2.5.
- New CLI commands:
  - `robot-blackbox incident build`
  - `robot-blackbox incident show <bundle>`
  - `robot-blackbox incident list`

**Tests.**
- `tests/unit/test_report_renderer.py`: required sections, claim
  references resolve.
- `tests/integration/test_incident_show.py`: `incident show` of a
  fixture renders a stable markdown.

**Demo impact.** First end-to-end demo: build → open the report.

---

### M3: Timeline reconstruction (★ MVP)

**Objective.** Implement `TimelineEvent` derivation, ordering, and
causality annotation. Replace the stub timeline from M2 with a real
one.

**Inputs.** `events.jsonl` + `triggers.json`.

**Outputs.** `timeline.json`; the report's "Timeline" section becomes
real.

**Implementation.**
- `blackboxrs/incident/timeline.py`: `reconstruct(bundle) -> list[TimelineEvent]`.
- Derived event detectors:
  - `silence_interval_detector`
  - `resource_excursion_detector`
  - `graph_delta_detector` (requires snapshots from M3.5).
- M3.5 (sub-milestone): `SystemSnapshotter` projection from existing
  `system_monitor` output → `evidence/snapshots.json`. Required so
  `graph_delta_detector` has snapshot deltas to diff.

**Tests.**
- `tests/unit/test_timeline_silence.py`
- `tests/unit/test_timeline_resource_excursion.py`
- `tests/unit/test_timeline_graph_delta.py`
- `tests/integration/test_timeline_end_to_end.py`

**Demo impact.** The "before/after the failure" narrative becomes
mechanical, not handcrafted.

---

### M4: Config + version signatures + diff (★ MVP)

**Objective.** Capture `ConfigSignature` and `VersionSignature` at
session start. Add a `ConfigDiff` block to the report when relevant.

**Inputs.** Filesystem state at session start; user-attached launch /
URDF / parameter files via `robot-blackbox attach-launch <path>`.

**Outputs.** `signatures/config.json`, `signatures/versions.json`.

**Implementation.**
- `blackboxrs/core/signatures/config.py`: `ConfigSignatureCollector`.
- `blackboxrs/core/signatures/versions.py` ,
  `VersionSignatureCollector` (apt + pip + os-release + nvidia-smi).
- Daemon hook: write signatures once at session start to a small
  cache (`~/.blackboxrs/state/session_<id>/signatures/`). Builder
  copies them into the bundle.
- `blackboxrs/incident/diff.py`: `ConfigDiff`, `VersionDiff`.

**Tests.**
- `tests/unit/test_config_signature.py`: determinism.
- `tests/unit/test_version_signature.py`: graceful absence of
  `nvidia-smi` / `apt`.
- `tests/integration/test_signature_session_capture.py`: daemon
  start writes both files.
- `tests/unit/test_config_diff.py`.

**Demo impact.** "Did anything change between yesterday's good run
and today's bad run?" answered with a diff.

---

### M5: Failure fingerprint v1 (★ MVP)

**Objective.** Compute a deterministic `FailureFingerprint` from a
bundle. Add it to `incident.json` and surface it in the report.

**Inputs.** `triggers.json`, `timeline.json`.

**Outputs.** `fingerprint.json` in every bundle.

**Implementation.**
- `blackboxrs/incident/fingerprint.py`: `compute(bundle) -> FailureFingerprint`.
- Modify the four built-in detectors to declare `signature_fields`
  (additive; default empty).
- The `topology_signature` is computed from the QoS-class set on
  topics involved in any trigger.

**Tests.**
- `tests/unit/test_fingerprint_determinism.py`: same bundle →
  same id.
- `tests/unit/test_fingerprint_collision.py`: two bundles seeded
  identically collide; perturbed do not.
- `tests/unit/test_fingerprint_normalization.py`: float rounding,
  set ordering.

**Demo impact.** Recurrence detection. Two consecutive demo runs of
the same scenario produce the same fingerprint id.

---

### M6: Preflight prevention engine, 3 checks (stretch, public)

**Objective.** Ship the prevention loop end to end for the three
most useful checks.

**Inputs.** `~/.blackboxrs/prevention/rules/*.yaml`; the live ROS
graph (when applicable).

**Outputs.** `robot-blackbox preflight` exit code + report; new YAML
files written by `prevention adopt`.

**Implementation.**
- `blackboxrs/prevention/rules.py`: model + YAML loader/saver.
- `blackboxrs/prevention/checks/topic_present.py`: uses rclpy node
  scan with timeout.
- `blackboxrs/prevention/checks/qos_match.py`: compares
  publisher/subscriber QoS profiles.
- `blackboxrs/prevention/checks/node_running.py`: checks for node
  presence on the graph.
- `blackboxrs/prevention/runner.py`.
- CLI: `preflight`, `prevention adopt`, `prevention list`,
  `prevention disable`.

**Tests.**
- Per-check unit tests.
- `tests/integration/test_ros_live.py` extension for live preflight
  on a seeded ROS graph.
- Adopt-from-incident round-trip test.

**Demo impact.** Prevention loop closes. The pivot is real.

---

### M7: Replay packaging (stretch, public)

**Objective.** `incident pack` / `unpack` for sending bundles
between machines.

**Inputs.** A bundle directory.

**Outputs.** `bundle.tar.gz` with deterministic ordering and an
embedded manifest. `unpack` verifies and refuses to overwrite.

**Implementation.**
- `blackboxrs/incident/pack.py`: uses `tarfile` with a sorted
  walk; manifest is `MANIFEST.json` listing every file with its
  sha256.

**Tests.**
- Round-trip pack/unpack on fixtures.
- Bit-for-bit determinism on identical input.
- Tamper detection: modifying a file inside the tar fails verify.

**Demo impact.** Engineer can send a bundle to a colleague who
re-runs `incident show` and sees the same report.

---

### M8: Documentation + landing demo (always)

**Objective.** Make the v0.4 release land *as the new product*, not
as "v0.3 with extra features." Update README, ARCHITECTURE.md,
add `docs/incident-anatomy.md`, record the demo screencast.

**Inputs.** Everything above.

**Outputs.**
- New README (per `PIVOT_BRIEF.md` §6 / repo `README.md` rewrite).
- `docs/incident-anatomy.md`: annotated walk-through of one
  bundle.
- `docs/preflight.md`: guide to writing prevention rules.
- `examples/`: sample bundles in repo (small, real).
- Screencast (linked from README): under 3 minutes.

**Tests.** README links validated by `docs/check_links.sh`.

**Demo impact.** First impression of the project on GitHub becomes
"oh, this is incident intelligence" not "another logger."

---

## 9. Calendar (target dates, treat as goals not commitments)

| Week (from 2026-05-07) | Milestone |
|---|---|
| 1–2  | M1 |
| 3    | M2 |
| 4–5  | M3 (incl. M3.5 snapshotter) |
| 5    | M4 (parallel with late M3) |
| 6    | M5 |
| 7    | M8 (docs + screencast for MVP cut) |
| 8    | v0.4-rc1 (M1–M5 + M8) |
| 9    | M6 if time |
| 10   | M7 if time |
| 11   | v0.4.0 ship |

If M6/M7 slip, ship v0.4.0 without them and follow up with v0.4.1
two weeks later.

---

## 10. Out-of-band tasks

- **Telemetry on telemetry.** Add an internal `incident_built` event
  to the daemon's own JSONL when an incident is built nearby in
  time. Useful for self-debugging.
- **Memory hooks.** Update `MEMORY.md` entry for BlackBoxRS to point
  at this pivot doc and the v0.4 milestones.
- **License + acknowledgements.** No change; MIT.
