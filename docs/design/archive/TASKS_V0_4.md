# BlackBoxRS: v0.4 Execution Tasks

Atomic, implementation-oriented checklist. Every task names its
files, declares its dependency, and states how it is verified.
Priorities: **P0** = MVP-blocking, **P1** = MVP-complete, **P2** =
stretch.

Progress key:
- `[ ]` open
- `[x]` done in this session
- `[~]` partially done in this session

---

## 0. Repo plumbing

- [x] **0.1** Create `blackboxrs/incident/` package skeleton with
  `__init__.py`, `models.py`, `bundle.py`, `builder.py`,
  `report.py`, `timeline.py`, `fingerprint.py`, `cause.py`,
  `diff.py`, `pack.py`, `api.py`. **Priority:** P0. **Deps:**
  none. **Verify:** `python -c "import blackboxrs.incident"`.

- [x] **0.2** Create `blackboxrs/prevention/` package skeleton.
  **Priority:** P1. **Deps:** none. **Verify:**
  `python -c "import blackboxrs.prevention"`.

- [x] **0.3** Add `incidents_dir`, `prevention_dir` to
  `BlackBoxConfig` with sensible defaults. **Priority:** P0.
  **Deps:** none. **Verify:** new keys round-trip through YAML
  load/save in `tests/unit/test_config.py`.

- [ ] **0.4** Move `BlackBoxDaemon` from `cli/daemon.py` to
  `core/daemon.py` with a one-line shim left in `cli/daemon.py`.
  **Priority:** P1. **Deps:** none. **Verify:** existing daemon
  tests still pass; new import path works.

- [ ] **0.5** Split `cli/app.py` into `cli/daemon_cmd.py` (existing
  commands), `cli/incident_cmd.py` (new), `cli/preflight_cmd.py`
  (new). Aggregate in `cli/app.py`. **Priority:** P0. **Deps:**
  0.4. **Verify:** `robot-blackbox --help` shows all groups; old
  invocations unchanged.

- [ ] **0.6** Add `tests/fixtures/sessions/session_minimal.jsonl`,
  `session_tf_break.jsonl`, `session_qos_mismatch.jsonl`.
  **Priority:** P0. **Deps:** none. **Verify:** files load via
  `LogReader.read_log()` without errors.

---

## 1. Incident schema (M1)

- [x] **1.1** Define `Incident` pydantic model in
  `blackboxrs/incident/models.py` per `ARCHITECTURE_PIVOT.md` §1.1.
  **Priority:** P0. **Deps:** 0.1. **Verify:** unit test for
  required/optional fields, schema_version pinning.

- [x] **1.2** Define `EvidenceBundle` model.
  **Priority:** P0. **Deps:** 1.1. **Verify:** model holds the
  bundle path; layout enforcement is in `bundle.py` not the model.

- [x] **1.3** Define `TimelineEvent` model with kind/subsystem
  literals and `evidence_ref` validator. **Priority:** P0. **Deps:**
  0.1. **Verify:** invalid kinds rejected.

- [x] **1.4** Define `DetectorTrigger` model with deterministic
  `trigger_id` factory `from_event(event)`. **Priority:** P0.
  **Deps:** 0.1. **Verify:** same input event → same trigger_id.

- [x] **1.5** Define `SystemSnapshot`, `TopicSnapshot`,
  `ProcessSnapshot`, `GPUSnapshot` models. **Priority:** P0.
  **Deps:** 0.1. **Verify:** round-trip JSON.

- [x] **1.6** Define `LikelyCauseHypothesis` model.
  **Priority:** P0. **Deps:** 0.1. **Verify:** confidence in [0,1].

- [x] **1.7** Define `FailureFingerprint` model with
  `algorithm_version="v1"` constant. **Priority:** P0.
  **Deps:** 0.1. **Verify:** id pattern matches `fpr_[0-9a-f]{16}`.

- [x] **1.8** Define `ConfigSignature` and `VersionSignature`
  models with `hash` SHA-256 hex validator. **Priority:** P0.
  **Deps:** 0.1. **Verify:** invalid hashes rejected.

- [ ] **1.9** Add `tests/unit/test_incident_models.py` covering all
  models. **Priority:** P0. **Deps:** 1.1-1.8. **Verify:** tests
  green; coverage ≥ 90% on `models.py`.

---

## 2. Bundle layout (M1)

- [x] **2.1** Implement `BundleWriter` in
  `blackboxrs/incident/bundle.py` with `init_dir(incident_id) ->
  Path`, `write_incident(incident)`, `write_events_jsonl(events)`,
  `write_triggers(triggers)`, `write_snapshots(snapshots)`,
  `write_signatures(config_sig, version_sig)`,
  `write_timeline(timeline)`, `write_fingerprint(fp)`,
  `write_report(text)`. **Priority:** P0. **Deps:** 1.x. **Verify:**
  every required file from `ARCHITECTURE_PIVOT.md` §1.2 exists
  after a write.

- [x] **2.2** Implement `BundleReader` with `load_incident()`,
  `iter_events()`, `load_triggers()`, `load_signatures()`,
  `load_timeline()`, `load_fingerprint()`. **Priority:** P0.
  **Deps:** 2.1. **Verify:** read-after-write round-trip.

- [ ] **2.3** Add `tests/unit/test_bundle_layout.py`. **Priority:**
  P0. **Deps:** 2.1, 2.2. **Verify:** required files present;
  optional files absent when not provided; no extra files.

---

## 3. Incident builder (M1 + M3)

- [x] **3.1** Implement `IncidentBuilder.build(window_start,
  window_end, *, output_dir=None, session_id=None) -> Path` that
  produces the partial bundle (events, triggers, signatures
  placeholders, incident.json). **Priority:** P0. **Deps:** 2.x.
  **Verify:** end-to-end test from synthetic JSONL fixture.

- [x] **3.2** Implement deterministic `incident_id` from
  `(window_start, session_id, host)` using a sha8 suffix.
  **Priority:** P0. **Deps:** 3.1. **Verify:** same inputs → same id.

- [ ] **3.3** Wire `IncidentBuilder` to call `timeline.reconstruct()`
  (after M3) and `fingerprint.compute()` (after M5). **Priority:**
  P0. **Deps:** 7.x, 9.x. **Verify:** integration test
  `test_incident_builder_basic.py`.

- [ ] **3.4** Idempotency test: re-running on same inputs produces
  the same bundle, except `created_at`. **Priority:** P0. **Deps:**
  3.1. **Verify:** diff fixture-to-fixture.

---

## 4. Report renderer (M2)

- [x] **4.1** Implement `report.render(bundle: BundleReader) -> str`
  with sections per `ARCHITECTURE_PIVOT.md` §2.5. **Priority:** P0.
  **Deps:** 2.x. **Verify:** output contains every required
  section header.

- [x] **4.2** Make every claim resolve to an evidence file:
  `evidence_ref` strings of the form `events.jsonl#L<n>` or
  `triggers.json#<trigger_id>` or `snapshots.json#<index>`.
  **Priority:** P0. **Deps:** 4.1. **Verify:** unit test parses each
  ref and checks the file exists.

- [ ] **4.3** Recommended-prevention-rule rendering: if confidence
  ≥ 0.7, emit a YAML block under "Recommended preflight rule."
  **Priority:** P1. **Deps:** 4.1, 11.x. **Verify:** integration
  test for QoS mismatch fixture renders a `qos_match` rule
  proposal.

- [ ] **4.4** Add `tests/unit/test_report_renderer.py`. **Priority:**
  P0. **Deps:** 4.1, 4.2. **Verify:** required sections present;
  no orphan claims.

---

## 5. Snapshot projection (M3.5)

- [ ] **5.1** Add `core/snapshots.py` with `SystemSnapshotter` that
  consumes a slice of events and projects a typed
  `SystemSnapshot` at fixed cadence (default 5 s). **Priority:**
  P0. **Deps:** 1.5. **Verify:** test using fixture events
  produces N snapshots covering the window.

- [ ] **5.2** Wire snapshotter into `IncidentBuilder` →
  `evidence/snapshots.json`. **Priority:** P0. **Deps:** 5.1, 3.1.
  **Verify:** integration test, snapshot count > 0 for
  multi-second fixture.

- [ ] **5.3** Add unit tests for snapshot derivation
  (`tests/unit/test_system_snapshotter.py`). **Priority:** P0.
  **Deps:** 5.1.

---

## 6. Timeline reconstruction (M3)

- [ ] **6.1** Implement `timeline.reconstruct(bundle) ->
  list[TimelineEvent]` with raw-event ordering and source-priority
  tie-breaking. **Priority:** P0. **Deps:** 2.x. **Verify:** unit
  test on fixture asserts ordering.

- [ ] **6.2** Implement `silence_interval_detector(snapshots,
  freq_events, threshold_sec) -> list[TimelineEvent(kind=derived)]`.
  **Priority:** P0. **Deps:** 5.1. **Verify:**
  `tests/unit/test_timeline_silence.py` on fixture.

- [ ] **6.3** Implement `resource_excursion_detector(events,
  thresholds, sustain_sec=3.0)`. **Priority:** P0. **Deps:** 6.1.
  **Verify:** `tests/unit/test_timeline_resource_excursion.py`.

- [ ] **6.4** Implement `graph_delta_detector(snapshots)`.
  **Priority:** P0. **Deps:** 5.1. **Verify:**
  `tests/unit/test_timeline_graph_delta.py`.

- [ ] **6.5** Causality hint annotator: for each trigger, label
  events within ±30 s as `precursor` / `consequence`.
  **Priority:** P1. **Deps:** 6.1. **Verify:** unit test on
  fixture; ensure independent events stay unlabelled.

---

## 7. Likely-cause ranking (M3 → M5)

- [x] **7.1** Implement stub `cause.rank(triggers, timeline,
  config_diff) -> list[LikelyCauseHypothesis]` returning a single
  hypothesis derived from the highest-severity trigger.
  **Priority:** P0. **Deps:** 6.1. **Verify:** unit test on
  fixture.

- [ ] **7.2** Replace stub with full heuristic from
  `ARCHITECTURE_PIVOT.md` §4.C: detector-class weight + temporal
  proximity + config-diff precursor. **Priority:** P1. **Deps:**
  7.1, 8.x. **Verify:** unit tests for each axis.

- [ ] **7.3** Confidence-clamping rule: confidence ≥ 0.7 means
  hypothesis is promoted to `Incident.summary`; otherwise summary
  is generic. **Priority:** P1. **Deps:** 7.2. **Verify:** unit
  test.

---

## 8. Config + version signatures (M4)

- [ ] **8.1** Implement `core/signatures/config.py` with
  `ConfigSignatureCollector.collect() -> ConfigSignature`. Hashes
  ROS distro, ROS_DOMAIN_ID, RMW impl, env subset, attached
  launch/param/URDF files. **Priority:** P0. **Deps:** 1.8.
  **Verify:** `tests/unit/test_config_signature.py`,
  determinism, missing-file handling.

- [ ] **8.2** Implement `core/signatures/versions.py` with
  `VersionSignatureCollector.collect() -> VersionSignature`.
  Reads `/etc/os-release`, `python -V`, `apt list --installed`
  filtered, `pip freeze`, `nvidia-smi --query-gpu=driver_version`.
  **Priority:** P0. **Deps:** 1.8. **Verify:**
  `tests/unit/test_version_signature.py`: gracefully absent
  binaries do not break the call.

- [ ] **8.3** Add `attach-launch <path>` CLI command that registers
  a launch file in `~/.blackboxrs/state/session_<id>/launch.json`
  for the next signature capture. **Priority:** P1. **Deps:** 8.1.
  **Verify:** integration test: attach → restart daemon → signature
  contains the file.

- [ ] **8.4** Daemon hook: at session start, write
  `~/.blackboxrs/state/session_<id>/signatures/config.json` and
  `versions.json`. **Priority:** P0. **Deps:** 8.1, 8.2. **Verify:**
  `tests/integration/test_signature_session_capture.py`.

- [ ] **8.5** `IncidentBuilder` copies signatures from session
  state into `signatures/` of the bundle. **Priority:** P0.
  **Deps:** 8.4, 3.1. **Verify:** integration test.

- [ ] **8.6** Implement `incident/diff.py` with
  `ConfigDiff.compute(prev: ConfigSignature, curr: ConfigSignature)`
  and `VersionDiff.compute(...)`. **Priority:** P1. **Deps:** 1.8.
  **Verify:** `tests/unit/test_config_diff.py`.

- [ ] **8.7** Renderer integration: `report.md` includes a
  ConfigDiff section when prev signatures exist for the same
  host. **Priority:** P1. **Deps:** 4.1, 8.6. **Verify:** report
  fixture comparison.

---

## 9. Failure fingerprint (M5)

- [ ] **9.1** Add `signature_fields: list[str] = []` class
  attribute to `BaseDetector` and to each built-in detector with
  the values specified in `REPO_RESTRUCTURE_PLAN.md` §1. **Priority:**
  P0. **Deps:** none. **Verify:** existing detector tests still
  pass.

- [ ] **9.2** Implement `fingerprint.compute(triggers, snapshots) ->
  FailureFingerprint`. **Priority:** P0. **Deps:** 9.1, 1.7.
  **Verify:** `tests/unit/test_fingerprint_determinism.py`.

- [ ] **9.3** Implement `normalize(value)` rounding floats to a
  detector-declared precision (default 1 decimal place).
  **Priority:** P0. **Deps:** 9.2. **Verify:** unit test.

- [ ] **9.4** Implement `topology_signature` (sha8 of sorted topic
  set with QoS class). **Priority:** P0. **Deps:** 9.2. **Verify:**
  unit test.

- [ ] **9.5** Wire fingerprint into `IncidentBuilder`. **Priority:**
  P0. **Deps:** 9.2, 3.1. **Verify:** every built bundle has a
  `fingerprint.json`.

- [ ] **9.6** Add `Incident.fingerprint` field population.
  **Priority:** P0. **Deps:** 9.5. **Verify:** integration test.

- [ ] **9.7** Collision test: two seeded fixtures with same
  triggers + topology produce same id; perturb one and id changes.
  **Priority:** P0. **Deps:** 9.2. **Verify:** unit test.

---

## 10. CLI: incident commands (M2)

- [x] **10.1** `robot-blackbox incident build [--since DURATION]
  [--start ISO] [--end ISO] [--note TEXT] [--tag TAG]`,
  produces a bundle. **Priority:** P0. **Deps:** 3.x. **Verify:**
  manual run on fixture session.

- [x] **10.2** `robot-blackbox incident show <bundle>`: renders
  `report.md` to stdout. **Priority:** P0. **Deps:** 4.x.
  **Verify:** manual run on fixture bundle.

- [x] **10.3** `robot-blackbox incident list`: table of bundles
  in `incidents_dir`. **Priority:** P0. **Deps:** 2.x. **Verify:**
  manual.

- [ ] **10.4** `robot-blackbox incident attach <bundle> <path>
  [--copy|--symlink]`: adds an attachment. **Priority:** P1.
  **Deps:** 2.x. **Verify:** integration test, default symlink
  behaviour.

- [ ] **10.5** `robot-blackbox incident pack <bundle>` (M7).
  **Priority:** P2. **Deps:** 2.x. **Verify:** round-trip.

- [ ] **10.6** `robot-blackbox incident unpack <archive>` (M7).
  **Priority:** P2. **Deps:** 10.5. **Verify:** verifier rejects
  tampered manifests.

---

## 11. Prevention rules + preflight (M6)

- [x] **11.1** Define `PreventionRule` and `PreflightCheck` models
  in `blackboxrs/prevention/rules.py`. **Priority:** P0. **Deps:**
  0.2. **Verify:** YAML round-trip.

- [x] **11.2** Implement `load_rules(path) -> list[PreventionRule]`
  and `save_rule(rule, path)`. **Priority:** P0. **Deps:** 11.1.
  **Verify:** `tests/unit/test_prevention_rule_yaml.py`.

- [ ] **11.3** Implement `prevention/checks/topic_present.py`.
  **Priority:** P0. **Deps:** rclpy. **Verify:** unit test with
  rclpy mock; live test under `_ROS_AVAILABLE`.

- [ ] **11.4** Implement `prevention/checks/qos_match.py`.
  **Priority:** P0. **Deps:** rclpy. **Verify:** unit test;
  live test.

- [ ] **11.5** Implement `prevention/checks/node_running.py`.
  **Priority:** P0. **Deps:** rclpy. **Verify:** unit test;
  live test.

- [x] **11.6** Implement `prevention/runner.py` with
  `PreflightRunner.run() -> PreflightReport` and exit-code
  semantics (0 pass, 1 block, 2 warn). **Priority:** P0.
  **Deps:** 11.2. **Verify:** unit test.

- [ ] **11.7** `robot-blackbox preflight [--rules-dir]` CLI.
  **Priority:** P0. **Deps:** 11.6. **Verify:** manual run with a
  failing rule blocks; with no rules, exits 0.

- [ ] **11.8** `robot-blackbox prevention adopt --from-incident
  <id>` writes a YAML rule from the recommended block in the
  bundle's `report.md`. **Priority:** P0. **Deps:** 11.2, 4.3.
  **Verify:** integration test: build → adopt → preflight fires.

- [ ] **11.9** `robot-blackbox prevention list / disable / enable`.
  **Priority:** P1. **Deps:** 11.2. **Verify:** unit tests.

- [ ] **11.10** Stubs raising `NotImplementedError` for `env_var`,
  `param_value`, `resource_threshold`, `custom_python` checks.
  **Priority:** P1. **Deps:** 11.1. **Verify:** unit test asserts
  raise.

---

## 12. Sample bundles + fixtures

- [x] **12.1** Generate `examples/incidents/inc_demo_tf_break/`
  from a synthetic JSONL fixture and commit it. **Priority:** P0.
  **Deps:** 3.x, 4.x. **Verify:** `incident show` re-renders
  identically.

- [ ] **12.2** Generate `examples/incidents/inc_demo_qos_mismatch/`.
  **Priority:** P1. **Deps:** 12.1. **Verify:** same.

- [ ] **12.3** Build script `scripts/regen_example_bundles.sh` for
  reproducibility. **Priority:** P1. **Deps:** 12.x. **Verify:** CI
  job that runs it and diffs against committed bundle.

---

## 13. Documentation

- [x] **13.1** Rewrite `README.md` per `PIVOT_BRIEF.md` §6
  requirements. **Priority:** P0. **Deps:** all of M1-M5.
  **Verify:** README links resolve; sample bundle reference works.

- [ ] **13.2** Add `docs/incident-anatomy.md` walking through
  `inc_demo_tf_break` file by file. **Priority:** P0. **Deps:**
  12.1. **Verify:** every file mentioned exists.

- [ ] **13.3** Add `docs/preflight.md` for writing prevention
  rules. **Priority:** P1. **Deps:** 11.x.

- [ ] **13.4** Replace `docs/ARCHITECTURE.md` with the contents
  of `ARCHITECTURE_PIVOT.md` once v0.4 ships; archive
  `ARCHITECTURE_PIVOT.md` under `docs/history/`. **Priority:** P1.
  **Deps:** ship-date.

---

## 14. Performance + benchmarks

- [ ] **14.1** Add `tests/benchmarks/test_incident_builder_bench.py`
  asserting `IncidentBuilder.build` on a 50 MB JSONL fixture
  finishes in ≤ 5 s on mewtwo. **Priority:** P1. **Deps:** 3.x.
  **Verify:** `pytest -m benchmark`.

- [ ] **14.2** Add benchmark for `report.render` with a large
  trigger list (≥ 200 triggers) staying under 250 ms.
  **Priority:** P2. **Deps:** 4.x.

---

## 15. Backwards-compatibility test

- [ ] **15.1** Add `tests/unit/test_backcompat_imports.py`
  asserting all rows of `REPO_RESTRUCTURE_PLAN.md` §5 still work.
  **Priority:** P0. **Deps:** 0.4, 0.5. **Verify:** test green.

---

## 16. Release

- [ ] **16.1** Bump version to `0.4.0.dev0` after first incident
  vertical slice merges. **Priority:** P1. **Deps:** M1+M2.
  **Verify:** `pyproject.toml`, `__init__.py`, README badge.

- [ ] **16.2** Bump version to `0.4.0` once MVP criteria pass.
  **Priority:** P1. **Deps:** M1-M5 + M8. **Verify:** all MVP
  criteria from `ROADMAP_V0_4.md` §4 met.

---

## Summary at end of this session

Completed in this session: 0.1, 0.2, 0.3, 1.1-1.8, 2.1-2.2,
3.1-3.2, 4.1-4.2, 7.1, 10.1-10.3, 11.1-11.2, 11.6, 12.1, 13.1.

Next 10 highest-leverage tasks (in order):
1. 0.6: fixtures (unblocks builder tests on real-shaped data)
2. 1.9: model unit tests
3. 2.3: bundle layout test
4. 5.1 + 5.2: snapshotter projection
5. 6.1: timeline ordering
6. 6.2-6.4: derived event detectors
7. 8.1 + 8.2: signature collectors
8. 8.4: daemon session-start hook
9. 9.1-9.5: fingerprint
10. 7.2: full likely-cause ranking

These ten tasks land the MVP. Anything beyond is upside.
