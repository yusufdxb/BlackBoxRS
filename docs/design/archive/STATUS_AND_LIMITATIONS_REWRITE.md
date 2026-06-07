# BlackBoxRS: Status & Limitations (honest)

This file replaces vague status statements with a brutally honest
account of what is verified, what is inferred, what is unverified,
and what does not exist yet. As of 2026-05-07, against the v0.3.0
release plus this pivot's *initial* vertical slice (incident schema,
bundle pipeline, report generator, signatures stub).

Anything not in this document, treat as "not built."

---

## 1. Legend

- **Verified**: Yusuf executed the path, observed the output,
  has tests covering it, and the test suite runs locally.
- **Inferred**: The code path is implemented and reviewed but
  has not been exercised in this session. Likely works; counts
  as 80% confidence.
- **Unverified**: Not exercised, no current test coverage of
  the specific path, or hardware-dependent and not yet tried on
  hardware.
- **Not built**: Code does not exist.

---

## 2. v0.3.0 baseline (pre-pivot)

These statements describe the v0.3.0 release as it stood before
the v0.4 pivot work began.

### Verified

- `BlackBoxEvent` envelope round-trips JSONL deterministically.
- `EventBus` enforces bounded queues, drops on full, increments
  per-queue drop counters, and publishes to multiple subscribers
  without blocking.
- `RotatingJsonlWriter` rotates by file size, prunes oldest by
  count, and (when `max_age_hours > 0`) prunes by mtime.
- `LogReader` streams events, supports time filtering, and
  `tail()` returns the last N events.
- `BlackBoxDaemon` writes a JSON pidfile with `pid`, `starttime`
  (jiffies), and `cmdline`; `is_running()` rejects recycled PIDs
  and foreign processes.
- The CLI commands `start / stop / status / dump-log / replay /
  config / init` work in the documented form.
- `ThresholdDetector`, `FrequencyDetector`, `DeadTopicDetector`,
  `QoSMismatchDetector` each have unit tests and synthetic
  scenario tests.
- The custom-detector loader instantiates detectors declared in
  YAML and integrates them into `AnomalyEngine`.
- Test suite: 161 tests passing (per memory entry; 2026-04-23).
  Will be re-run in CI before any v0.4 ship.
- Optional `prometheus_client` exporter is feature-gated and
  unit-tested.

### Inferred

- Behaviour on Jetson Orin NX 16 GB. The `/sys/devices/gpu.0/load`
  + thermal-zone path is implemented and reviewed. Last hardware
  exercise on GO2's Jetson was during HELIX validation, *not*
  specifically of BlackBoxRS. We trust the code; we do not have a
  recorded run.
- `Rosbag2Recorder` integration on a real ROS 2 stack with `ros2
  bag record` running concurrently. Unit-tested; live integration
  was sanity-checked at v0.3.0 but no canonical fixture exists.
- Multi-distro support beyond Humble. Code paths are not
  distro-specific, but only Humble is in CI.

### Unverified

- Long-running stability (≥ 24 h continuous capture). Anecdotal
  multi-hour runs exist; no stress test in CI.
- Behaviour on Foxy or Iron. We claim ROS 2 generality; we have
  only verified Humble.
- Behaviour under heavy ROS DDS traffic (>1000 messages/sec
  aggregate) on resource-constrained Jetson. Performance envelope
  was characterised on mewtwo (RTX 5070), not Jetson.

### Not built

- Time-aligned ROS replay. The `replay` CLI command prints
  events; it does not replay messages on topics.
- Multi-host capture. Daemon is single-process, single-host.
- TF tree topology analysis. We can detect `/tf` and `/tf_static`
  silence but do not parse the tree shape.
- Service / action introspection. Topics only.

---

## 3. Pivot work (initial vertical slice committed in this session)

### Verified

- `Incident`, `EvidenceBundle`, `TimelineEvent`, `DetectorTrigger`,
  `SystemSnapshot`, `ConfigSignature`, `VersionSignature`,
  `FailureFingerprint`, `LikelyCauseHypothesis`, `PreventionRule`,
  `PreflightCheck` pydantic models exist in `blackboxrs/incident/models.py`
  and `blackboxrs/prevention/rules.py` and round-trip through JSON.
  *(Run `pytest tests/unit/test_incident_models.py` to confirm.)*
- `IncidentBuilder` (basic) produces a bundle directory with
  required files when given a fixture JSONL and a window.
- `BundleWriter` / `BundleReader` enforce the documented layout.
- `ConfigSignature` and `VersionSignature` collectors hash
  deterministically given the same inputs.
- `report.render()` produces a markdown file containing the
  required section headers; every claim's `evidence_ref` resolves
  to an existing file inside the bundle.
- `compute()` for `FailureFingerprint` is deterministic given the
  same triggers + topology.
- Sample bundle in `examples/incidents/inc_demo_tf_break/` is
  built from a fixture and committed.
- New CLI subcommands `incident build`, `incident show`,
  `incident list` are wired into `cli/incident_cmd.py` and
  surfaced via `robot-blackbox incident --help`.

### Inferred

- `IncidentBuilder` end-to-end on a real captured session
  (i.e., not from a fixture). Implemented but not yet exercised
  on a fresh `daemon → fail → build` flow during this session.
- `PreflightRunner` / `topic_present` / `qos_match` /
  `node_running` checks. Models exist; the runner stub returns
  pass for a no-rule library. Real ROS-coupled execution lives
  behind `_ROS_AVAILABLE` and has not been demoed in this
  session.
- `prevention adopt --from-incident` writing a YAML file. Path
  is implemented but the round-trip "rule survives a daemon
  restart and fires preflight" has not been demoed.

### Unverified

- Performance of `IncidentBuilder` on a 50 MB JSONL set. Target
  is ≤ 5 s on mewtwo. No benchmark yet.
- Determinism of bundle output across machines (same fixture →
  identical files modulo timestamps and host metadata). Plausible
  by construction; no diff test yet.
- `incident pack` / `unpack`. Not built; reserved for M7.
- Cluster IDs / cross-incident clustering. Not built; reserved
  for v0.5.

### Not built (intentionally, in this slice)

- Likely-cause ranking is implemented as a stub returning a
  single hypothesis derived from the highest-severity trigger.
  The full ranking heuristics from `ARCHITECTURE_PIVOT.md` §4.C
  land in M3.
- Timeline derived events (silence interval, resource excursion,
  graph delta). The current timeline is `kind="raw"` events
  ordered by timestamp. Derived events land in M3.
- Config diff against the previous session. The signatures are
  captured; the diff renderer lands in M4.
- Preflight check kinds beyond the three named in M6:
  `env_var`, `param_value`, `resource_threshold`,
  `custom_python` are stubs that raise `NotImplementedError`.
- LLM-assisted narrative generation. Not present, not planned
  for v0.4.

---

## 4. Hardware limitations

- **Verified hardware**: x86_64 desktop Linux (mewtwo, Ubuntu
  22.04, RTX 5070).
- **Inferred hardware**: Jetson Orin NX 16 GB on GO2. Code paths
  for tegrastats / sysfs GPU thermal / Linux thermal zones exist
  and have been exercised in earlier projects (HELIX), but the
  v0.4 incident-build flow has not been run on Jetson in this
  session. Plan: validate during the M5 timebox.
- **Unverified hardware**: AMD GPUs (no rocm-smi support yet),
  Apple Silicon (we are Linux-only by default).
- **Not supported**: Windows, embedded RTOS, microcontroller
  targets.

---

## 5. ROS distro limitations

- **Verified**: ROS 2 Humble.
- **Inferred**: ROS 2 Iron and Jazzy. APIs we use (`rclpy.node`,
  `get_topic_names_and_types`, `get_publishers_info_by_topic`)
  are stable across these distros.
- **Not supported**: ROS 1. Will not be supported. The pivot
  doubles down on ROS 2.
- **Not yet supported**: Distros with breaking changes to QoS
  enums or graph introspection. We will adapt when they land,
  not before.

---

## 6. Scaling limitations

- **Single host.** The capture daemon writes events for one
  host. A multi-host robot needs a forwarder; not in v0.4.
- **JSONL backpressure.** Bus queues are bounded; events are
  dropped on full. We log per-queue drops. We do not buffer to
  disk on the producer side.
- **Bundle size.** Bundle directories are intentionally small
  (KBs to a few MB without attachments). With a rosbag2
  attachment, sizes scale with the bag (often hundreds of MB).
  We document this; we do not auto-truncate.
- **Incident library.** We expect 10-1000 bundles per team. At
  10K+ bundles the current "directory of directories" layout
  becomes inefficient; a SQLite index is reserved for v0.5.
- **Prevention library.** O(N) preflight checks at launch. With
  >100 rules, preflight time grows linearly. Reasonable for v0.4
  scale; in v0.5 we add parallel check execution.
- **Daemon hot path.** No change in this pivot; daemon stays
  single-process and the new incident logic runs *out of band*
  via the CLI. Daemon performance envelope is unchanged.

---

## 7. Things we explicitly do *not* claim

- We do not claim to identify the root cause of any incident.
  We rank likely causes with confidence and surface the
  evidence. The user owns the conclusion.
- We do not claim the prevention library prevents all
  recurrences. A rule can be too narrow (passes despite the
  failure) or too broad (blocks unrelated launches). The user
  reviews each rule.
- We do not claim to be production-ready for safety-critical
  systems. We are a development-time and field-debug tool.
- We do not claim multi-tenant or multi-user safety. The
  bundle directory is owned by the user running the CLI.

---

## 8. What this status doc is *for*

This is the document you read before:

- Writing a marketing claim. (If it is not in §3 "Verified," it
  cannot be in copy.)
- Filing a bug report. (Tells you what is in scope.)
- Demoing in front of a senior engineer. (Tells you what NOT to
  promise during Q&A.)
- Onboarding a contributor. (Tells them what is real vs. planned.)

If you find this doc out of sync with reality, fix it before you
fix anything else.
