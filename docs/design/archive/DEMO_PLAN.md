# BlackBoxRS: Demo Plan

The product wins or loses on first demo. The demo must show
**intentional failure → reproducible incident bundle → prevention
rule that catches it next time.** Anything else is decoration.

This document defines the scenario library, the live demo arc, and
the prerecorded assets the repo and landing page should ship with.

Target audience for the live demo: a senior robotics engineer or a
robotics platform lead. They will tolerate technical depth; they will
not tolerate marketing slides.

---

## 1. Demo arc (live, 5 minutes)

Show, don't tell. Sequence:

1. **00:00**: Robot is running. Daemon is running.
   Open a terminal: `robot-blackbox status`. Show 0 anomalies.
2. **00:30**: Inject failure (TF break, scenario S1). Robot stops
   moving. No errors on stdout. Classic "where do I even start."
3. **00:45**: `robot-blackbox incident build --since 2m`. Output
   path is printed.
4. **01:00**: `cat report.md` (or open in `glow` if available).
   Read aloud the timeline + likely cause.
5. **01:30**: `cat fingerprint.json`. Note the deterministic id.
6. **01:45**: `robot-blackbox prevention adopt --from-incident <id>`.
   Show the new YAML.
7. **02:00**: `robot-blackbox preflight`. Repeat the same failure
   condition. Preflight refuses to launch.
8. **02:30**: Q&A. Resist the urge to keep clicking.

Everything outside this sequence is fluff in a first demo.

---

## 2. Failure scenario library

Five core scenarios. The demo arc above uses S1 (TF break) by
default; S3 (QoS mismatch) is a strong alternative when the audience
is doing perception. S5 (thermal) is the showstopper when on Jetson
hardware.

### S1: TF tree break mid-mission

**Setup.**
- A small ROS 2 stack: `static_transform_publisher` (or
  `tf2_ros static_transform_publisher`) emitting `map → odom`,
  another emitting `odom → base_link`.
- A consumer node that prints the resolved transform.

**Failure injection.**
- `pkill -f "static_transform_publisher .*odom .*base_link"` after
  20 s of healthy operation.

**Expected detection.**
- `DeadTopicDetector` fires on `/tf_static` (silent for >
  `dead_topic.timeout_sec`).
- `DeadTopicDetector` may also fire on a downstream topic if the
  consumer stops publishing.

**Expected evidence bundle.**
- `events.jsonl` includes the last `/tf_static` frequency events
  before silence and the dead-topic anomaly.
- `triggers.json` has a `DeadTopicDetector` trigger with
  `subject="/tf_static"`.
- `snapshots.json` shows publisher_count for `/tf_static` going
  from 1 → 0.
- `signatures/config.json` includes the launch file (when attached)
  and ROS env.

**Expected timeline reconstruction.**
- Raw frequency events on `/tf_static` (1.0 Hz, 1.0 Hz, …).
- Derived `silence_interval` event at the moment of kill.
- `graph_delta` event noting publisher_count drop.
- Trigger event ordered after the silence_interval (because the
  detector waits `timeout_sec`).

**Expected likely-cause narrative.**
- Top hypothesis: "TF publisher disappeared from graph at
  `t=00:20.x`; downstream silence on `/tf_static` followed."
  Confidence ≥ 0.8.

**Expected prevention.**
- `PreventionRule` of kind `topic_present` for `/tf_static` with
  expected publisher count ≥ 1, and (stretch) a `node_running`
  check for the publisher node name.

---

### S2: Topic drop on a critical sensor

**Setup.**
- A simulated sensor publisher emitting `/scan` at 10 Hz.
- A consumer (e.g. nav2 placeholder).

**Failure injection.**
- After 30 s, `kill -STOP` (not -KILL) the publisher PID. The node
  remains in the graph, no new messages flow.

**Expected detection.**
- `FrequencyDetector` fires (frequency drops below tolerance).
- `DeadTopicDetector` fires after `timeout_sec` of silence.

**Expected evidence bundle.**
- Frequency-on-`/scan` curve in `events.jsonl`.
- Two distinct `DetectorTrigger`s on the same subject.
- `snapshots.json` shows publisher_count steady at 1 (the node
  did not unregister).

**Expected timeline reconstruction.**
- Frequency excursion event before the dead-topic event.
- Trigger ordering: frequency drop first, dead-topic second.

**Expected likely-cause.**
- Top hypothesis: "Publisher process stalled (still in graph,
  frequency collapsed)." Confidence ~ 0.7.
- Honest caveat: "Cannot distinguish stalled-publisher from
  network/middleware loss without process telemetry."

**Expected prevention.**
- A `topic_present` rule with a *frequency* lower bound for `/scan`.
  (M6 ships only `topic_present` without freq; the freq variant is
  v0.4.1.)

---

### S3: QoS mismatch silently drops messages

**Setup.**
- Publisher on `/perception/objects` with
  reliability=BEST_EFFORT, durability=VOLATILE.
- Subscriber expecting reliability=RELIABLE.

**Failure injection.**
- Just launch them. The subscriber gets no data despite the topic
  being "alive."

**Expected detection.**
- `QoSMismatchDetector` fires immediately.
- `FrequencyDetector` may *not* fire (we measure publisher-side
  frequency, which is fine).

**Expected evidence bundle.**
- Pub QoS profile + sub QoS profile both captured in
  `events.jsonl`.
- `triggers.json` carries the QoS mismatch trigger with
  `signature_fields=["reliability"]` for fingerprint stability.

**Expected timeline.**
- Single trigger event near `window_start`.
- Snapshots show one publisher, one subscriber, but the consumer's
  internal counters (if available) flat at zero. (Beyond v0.4.)

**Expected likely-cause.**
- Top hypothesis: "QoS reliability mismatch on
  `/perception/objects` (pub=BEST_EFFORT, sub=RELIABLE)."
  Confidence ≥ 0.9.

**Expected prevention.**
- `qos_match` rule asserting both sides on `/perception/objects`
  agree on `reliability`.

---

### S4: Node crash with restart

**Setup.**
- A perception node configured to run.
- A respawn manager (or just shell loop) that restarts it on crash.

**Failure injection.**
- Inside the node, raise after 10 s. Respawn brings it back after
  ~3 s.

**Expected detection.**
- `DeadTopicDetector` fires on the topics the node publishes.
- After respawn, frequency events resume.

**Expected evidence bundle.**
- `snapshots.json` shows publisher disappearing then reappearing.
- A *recurrence* hypothesis: if this is the second incident with
  the same fingerprint within the session, the report flags it.

**Expected timeline.**
- silence_interval event of length ~3 s.
- graph_delta events: publisher_count 1 → 0 → 1.

**Expected likely-cause.**
- Top hypothesis: "Publisher node restarted at `t=00:10.x`
  (re-appeared on graph after 3.1 s of silence)."

**Expected prevention.**
- A `node_running` rule on the node name; combined with a
  warn-only `topic_present` (since transient drops may be
  acceptable).

---

### S5: Thermal-induced node dropout (Jetson)

**Setup.**
- Jetson Orin NX or similar. Run a synthetic CUDA workload to
  drive GPU temp > 80 °C.
- A perception node with non-trivial memory footprint.

**Failure injection.**
- Sustain the CUDA load until thermal throttling. Eventually a
  perception node OOM-kills (you can also inject a memory-eater
  process to accelerate the OOM).

**Expected detection.**
- `ThresholdDetector` fires on `gpu_temp_c`.
- `ThresholdDetector` fires on `mem_percent`.
- `DeadTopicDetector` fires on the perception output topic when
  the node dies.

**Expected evidence bundle.**
- Thermal curve and memory curve visible in `events.jsonl` and
  rendered in the report's "Triggers" section.
- `signatures/versions.json` captures the kernel/driver state.

**Expected timeline.**
- Resource excursion (gpu_temp_c) → resource excursion
  (mem_percent) → dead_topic. Causality hint: gpu_temp_c
  excursion is `precursor` to dead_topic, mem_percent excursion
  is `precursor` to dead_topic.

**Expected likely-cause.**
- Top hypothesis: "Thermal pressure → memory pressure → perception
  node dropped from graph." Confidence 0.6 (multiple precursors,
  ranking is heuristic).
- Honest caveat: "Process-level telemetry needed to confirm OOM
  vs. driver fault."

**Expected prevention.**
- `resource_threshold` rule (warn-level) on `gpu_temp_c < 75 °C`
  pre-launch. Documents that the rule is informational only.

---

### S6: Config drift (stretch scenario)

**Setup.**
- Two runs of the same launch with a parameter file changed
  (`max_velocity: 0.5` → `5.0`).
- Robot misbehaves on second run.

**Failure injection.**
- Change the parameter, relaunch.

**Expected detection.**
- No anomaly necessarily fires; the failure is *behavioural*.
- The user runs `incident build` manually with `--since 5m`
  flagged `--note "behavioral regression"`.

**Expected evidence bundle.**
- `signatures/config.json` differs from the previous session.
- `ConfigDiff` block in the report shows the parameter change.

**Expected likely-cause.**
- Top hypothesis: "Config diff against last session changed
  `max_velocity` from 0.5 to 5.0; behavioural regression coincides
  with this change."
- Confidence is intentionally moderate (no detector fired).

**Expected prevention.**
- The user can pin the parameter file's sha256 in a `param_value`
  rule (M6/M7).

---

### S7: Launch-time missing dependency (stretch scenario)

**Setup.**
- Launch file expects `nav2_lifecycle_manager` to come up. Package
  is uninstalled or wrong distro.

**Failure injection.**
- Forget to `apt install` the package; relaunch.

**Expected detection.**
- Pre-failure: `signatures/versions.json` lacks the package.
- Post-failure: `node_running` check would have caught it.

**Expected evidence bundle.**
- Launch never reaches "all nodes up" state; daemon picks up only
  partial graph.
- Report flags missing nodes against attached launch description
  (when present).

**Expected prevention.**
- A `topic_present` rule (or `node_running`) for the missing
  component, derived from the launch description.

---

## 3. What is live vs prerecorded

**Live (must work in front of the audience):**
- S1 (TF break) end-to-end. Single terminal, no slide deck.
- The `prevention adopt` → `preflight` block in S1 or S3.

**Prerecorded (.cast or short .mp4):**
- S2 (frequency drop): 30 s clip; pause at the report; click into
  the timeline to highlight the silence interval.
- S3 (QoS mismatch): 30 s clip; emphasise the QoS pub vs sub diff
  in the report.
- S5 (thermal): 60 s clip on Jetson; the only scenario that needs
  hardware. Strong differentiator vs. cloud-SaaS observability.

**Static images for the README + landing page:**
- `docs/screenshots/report-summary.png`: top of `report.md`
  rendered.
- `docs/screenshots/timeline-table.png`: the timeline table
  showing trigger + derived events.
- `docs/screenshots/preflight-block.png`: `preflight` exit code
  1 with a friendly message.
- `docs/screenshots/bundle-tree.png`: `tree -L 3` of an incident
  directory.

---

## 4. What must be visible on screen during a live demo

- A single, readable terminal at 14pt minimum. No tmux split panes
  during the demo arc.
- A second tab with the bundle directory open (cd, `ls -la`).
- Optionally, the `report.md` open in a markdown previewer
  (`glow`).

What must *not* be on screen:
- Editor windows, IDE chrome, or unrelated terminals.
- VSCode notifications, Slack, etc.
- The daemon's stdout/stderr (`-f` mode is too noisy for demo).

---

## 5. Demo prerequisites checklist

- [ ] BlackBoxRS v0.4 installed in `~/.venv-blackboxrs` and
      `robot-blackbox` on `$PATH`.
- [ ] ROS 2 Humble sourced.
- [ ] A throwaway workspace `~/blackboxrs_demo/` with the launch
      files for each scenario in `launch/`.
- [ ] `tmux session "demo"` with three windows: `daemon`, `robot`,
      `bbrs`.
- [ ] `~/.blackboxrs/config.yaml` with anomaly thresholds tuned for
      the demo (lower `dead_topic.timeout_sec` to 2.0 for
      tighter timing).
- [ ] Pre-existing prevention library cleared
      (`rm ~/.blackboxrs/prevention/rules/*.yaml`).
- [ ] Pre-existing incidents cleared
      (`rm -rf ~/.blackboxrs/incidents/*`).
- [ ] Screen-recording tool ready in case the live works and we
      want to keep it.
- [ ] Backup prerecorded screencast queued in case live fails.

---

## 6. Repo and landing page assets

In `examples/incidents/`:
- `inc_demo_tf_break/`: fully-built S1 bundle, committed.
- `inc_demo_qos_mismatch/`: fully-built S3 bundle, committed.

In `docs/`:
- `incident-anatomy.md`: annotated walk-through of
  `inc_demo_tf_break`.
- `screenshots/*.png`: listed above.

For the README:
- A single 50-line "Sample incident" section that includes the
  `report.md` of `inc_demo_tf_break` inline.

For the landing page (later):
- Hero: short loop showing the failure → bundle → prevention rule
  flow.
- Three-panel screencast: capture, explain, prevent.
- A "Why this is not just observability" section quoting from
  `POSITIONING.md`.

---

## 7. Demo failure modes (and how to recover)

- **Daemon not running.** `robot-blackbox status` first; if needed
  `start --foreground` in another tmux window.
- **No anomalies fired.** Adjust thresholds in `config.yaml` before
  the demo, not during. Worst case, fall back to S6 (config drift),
  which does not require a detector to fire.
- **`incident build` finds 0 events.** Almost always a window
  problem; widen with `--since 5m`.
- **`preflight` blocks for the wrong reason.** Show the prevention
  YAML, comment the offending rule, rerun. Make it look like a
  feature.
