# Replay Lab

Replay Lab is a deterministic reliability-testing layer for reproducing robot
software incidents. It takes recorded or synthetic flight-recorder evidence,
replays it on a virtual clock, injects declared faults, runs the command path
and the BlackBoxRS detectors over it, and reports a causal timeline and a
verdict. The same input always gives the same bytes out, so an incident that
was reproduced once becomes a permanent regression test.

```
record (flight recorder) -> incident bundle -> replay -> inject fault
    -> inspect the causal chain -> golden case -> regression test in CI
```

## What it is not

* **Not a physics simulator.** Recorded odometry and other physical responses
  are replayed as recorded. An injected fault never changes them, and the
  flight report's "robot stopped within 1.5 s" verdicts are deliberately not
  used in a replay.
* **Not a GO2 digital twin.** The only modelled component is the motion
  arbitration path (a policy model, see below). Everything downstream of the
  robot-facing command is out of scope.
* **Not proof of hardware safety.** A PASS says the software invariants held
  on this evidence and this model. It says nothing about actuators, firmware,
  networks or timing on the robot.
* **Not a replacement for live validation.** Every result on synthetic
  evidence is labelled SYNTHETIC. The golden evidence shipped here is
  synthetic.

## How it works

A replay runs in separate layers (`blackboxrs/lab/`):

| Layer | Module | Job |
|---|---|---|
| evidence | `evidence.py` | load a flight bundle; refuse a torn or unfinalized bundle, duplicate `seq`, or records without clocks (unless `--allow-partial`, which caps the verdict at INCOMPLETE) |
| events | `events.py` | records to replay events with a total order |
| clock | `clock.py` | virtual monotonic replay clock; wall-clock pacing is separate and cannot change a result |
| faults | `faults.py` | deterministic injectors over the event stream |
| system under test | `sut.py` | the robot-facing command: recorded, or from a reference arbitration model |
| detectors | `monitors.py`, `liveness.py`, `transport.py` | safety invariants, command-path detectors, and the reused recorder and analyzer detectors |
| dispatch and result | `engine.py`, `timeline.py`, `verdict.py` | run everything in a fixed order, build the timeline, decide the verdict |

### Ordering and time

Replay time is the recorder's monotonic receipt clock, in integer
nanoseconds from the start of the evidence. Events are ordered by
`(time, order)`: at an equal time, recorded events keep the recorder's ingest
sequence (`seq`), then copies a fault made of them, then events a fault
created from nothing, in fault order. Two events with the same key are
refused. Ordering never depends on dict or set iteration.

The arbitration model ticks every 20 ms (50 Hz, like the HELIX arbiter's
timer). At one instant, every event is delivered before the tick. Nothing in
`blackboxrs/lab/` reads the wall clock, randomness or ids;
`tests/unit/lab/test_no_wall_clock.py` enforces it, including for the flight
modules the replay drives.

### System under test

`--sut observed` judges the arbitration output recorded in the evidence
(`/cmd_vel`, or `ArbiterStatus.out_*` when the `go2_helix` profile left
`/cmd_vel` alone). Input faults cannot change a recorded output, so this mode
is for checking recorded incidents and for faults injected into the output
itself.

`--sut helix_arbiter` and `--sut twist_mux_legacy` run a reference model on
the replayed inputs so a fault can change the outcome. Recorded outputs are
then suppressed and counted.

* `helix_arbiter` models the policies of HELIX `helix_arbiter/arbiter_core.py`
  (P1 to P10 in HELIX `docs/MOTION_ARBITRATION.md`): the hold state dominates
  every source; a missing or stale hold state forces zero; NaN, Inf and
  over-limit input is rejected and discards that source's older command; a
  hold transition discards every stored command; freshness uses receipt time.
  `tests/unit/lab/test_helix_parity.py` checks it decision by decision against
  the HELIX module on 20 seeded random streams when `HELIX_SRC` points at a
  HELIX checkout (it is skipped otherwise, including in CI).
* `twist_mux_legacy` models the behaviour measured on the real twist_mux 4.3.0
  binary and recorded in the same HELIX document: STOP is a zero-twist input at
  priority 100 below teleop at 200, NaN is forwarded, and nothing is published
  when every input is stale. The consumer keeps its last command (same
  document), so the model's robot-facing sink holds the last published
  command. ASSUMPTION: that the robot-facing consumer behaves this way on a
  given robot is a property of that consumer, not of this model.
* `--set freshness_clock=source_timestamp` is a counterfactual that judges
  source freshness by DDS source timestamps. HELIX P10 forbids exactly this;
  the setting exists to show why.

### Invariants and detectors

Safety invariants (a violation makes the verdict FAIL). The monitors are an
independent oracle: they never call the model and always use receipt time.

| Invariant | Statement |
|---|---|
| `stop_dominance` | while a HELIX hold is asserted (ordered by `(epoch, seq)`, so a stale RESUME cannot end it), every robot-facing command from 0.05 s after the hold until its release is zero |
| `finite_output` | every robot-facing command component is a finite number |
| `fresh_output` | a nonzero robot-facing command equals the latest valid message of a command source received within its freshness window (+0.05 s); zero is always allowed |
| `consistent_state` | a recorded ArbiterStatus never reports the hold active with a nonzero output |

Detectors:

| Detection | Source | Fires on |
|---|---|---|
| `nonfinite_input`, `malformed_input` | new, `monitors.py` | NaN/Inf or non-numeric twist component on a command source |
| `command_source_stale` | new | a source whose last command was motion is silent past its window (a source that stopped on zero is inactivity) |
| `stamp_ahead`, `stamp_behind` | new | a topic's receipt minus DDS source time moves more than 0.2 s from its own baseline (clock step, late or repeated delivery) |
| `clock_offset` (info) | new | a host's publisher clock differs from the receiver's by more than 0.5 s from the start |
| `odometry_frozen`, `odometry_jump`, `nonfinite_telemetry` | new | odometry position that does not move while its twist says it moves, steps larger than its twist allows, or non-finite values |
| `stale_telemetry`, `transport_loss`, `publisher_lost`, `node_disappeared` | flight recorder `FlightCore` triggers (`topic_stale`, `node_disappeared`), classified in `liveness.py` | a periodic topic goes silent alone, a whole host goes silent together, or its publisher leaves the graph |
| `duplicate_messages`, `out_of_order_stamps`, `sequence_reordered`, `sequence_loss` | flight analyzer `analyze()` data quality, `transport.py` | on the stream as delivered, whole-stream pass |
| `recorder_trigger:*`, `motion_request_while_held` (info) | recorder triggers; new | what the flight recorder would have fired on; a source asking for motion during a hold |

Liveness classes: an `event` topic (teleop, joystick, fault reports) is never
judged stale. A `periodic` topic (one with `stale_after_sec` in the profile,
or set in the case) that goes silent while its host's other periodic topics
keep arriving is `stale_telemetry`. When every periodic topic of one host
goes silent together, that is one `transport_loss`, and if the graph still
advertises the publishers the finding says it matches the CycloneDDS
wrong-interface signature (HELIX `docs/GO2_FIELD_NOTES.md` section 1:
topics advertised, no data). That is a match, not a diagnosis.

### Verdict

| Verdict | Meaning | `lab replay` exit |
|---|---|---|
| PASS | every invariant held, nothing above info reported | 0 |
| FAIL | a safety invariant was violated | 1 |
| INCOMPLETE | an invariant could not be evaluated (for example no arbitration output in the evidence) or the evidence is partial | 3 |
| DETECTED | invariants held and a detector reported a warning or critical finding | 4 |

Malformed evidence, case or fault definitions exit 5 (click's own usage
errors exit 2). `NOT_EXERCISED` invariants (for example `stop_dominance`
when no hold was asserted) do not block PASS and are listed, so a reader can
see what a replay did not test.

## Fault injectors

`robot-blackbox lab faults` lists them with every parameter. Times are
seconds from the start of the evidence on the receipt clock; windows are
`[from_s, to_s)`. No injector is random, so none takes a seed. A fault that
matches no event is an error. Faults apply in the order given.

| Category | Kinds |
|---|---|
| timing / transport | `drop`, `gap`, `delay`, `duplicate`, `reorder`, `stale_redelivery`, `clock_skew`, `timestamp_jump` |
| data | `nan`, `inf`, `malformed`, `set_value`, `freeze`, `step` |
| control | `inject_stream` (a stream not in the evidence, for example a teleop joystick) |

Select topics with `topic`, `topics` or `host` (`payload` or `robot`). In the
result, every fault lists how many events it touched and which; every touched
event carries the fault id; the timeline starts each fault with a `FAULT`
entry. A finding also lists `related_faults`: faults that touched a topic the
finding names, at or before it. That is an association by topic and time,
used because a dropped message leaves no event to link.

On the command line a fault is `kind:key=value,...` (values read as JSON when
they parse, lists separated by `|`) or a JSON object; a case file lists them
under `faults`.

## Commands

Every line below is run by `tests/unit/lab/test_cli.py` from the repository
root, and must exit with the code shown.

```console
$ robot-blackbox lab faults  # exit 0
$ robot-blackbox lab verify examples/replay_lab/cases --repeat 3  # exit 0
$ robot-blackbox lab replay examples/replay_lab/cases/nominal_motion.json --no-timeline  # exit 0
$ robot-blackbox lab replay examples/replay_lab/evidence/nominal_motion --sut twist_mux_legacy --no-timeline  # exit 0
$ robot-blackbox lab replay examples/replay_lab/evidence/nominal_motion --sut twist_mux_legacy --inject drop:topic=/nav/cmd_vel,from_s=4.0  # exit 1
$ robot-blackbox lab replay examples/replay_lab/evidence/nominal_motion --sut helix_arbiter --inject drop:topic=/nav/cmd_vel,from_s=4.0  # exit 4
$ robot-blackbox lab replay examples/replay_lab/evidence/nominal_motion --set freshness_clock=source_timestamp --inject clock_skew:host=payload,offset_s=1.5 --inject drop:topic=/nav/cmd_vel,from_s=4.0 --no-timeline  # exit 1
$ robot-blackbox lab replay examples/replay_lab/cases/teleop_vs_stop__twist_mux_legacy.json --json /dev/null  # exit 1
```

Other options of `lab replay`: `--faults FILE` (a JSON list of faults),
`--from S` and `--to S` (replay a window; state before `--from` is not
replayed and the result says so), `--speed X` (pace on the wall clock, 1.0 is
real time; the result is identical to an unpaced run), `--step` (pause at
every timeline entry), `--json PATH` (`-` for stdout), `--allow-partial`.

`lab verify` runs every case (default `examples/replay_lab/cases`), checks the
expected verdict, invariant statuses and the exact set of warning/critical
detections (so a false positive fails as well as a miss), replays each case
`--repeat` times and requires identical bytes. It exits 0 only when all of
that holds.

## End-to-end example: one fault, a different outcome

The evidence is the synthetic nominal run: `/nav/cmd_vel` commands 0.15 m/s
from t = 2 s at 20 Hz, HELIX never asserts a hold. Replayed through the legacy
twist_mux path without a fault:

```console
$ robot-blackbox lab replay examples/replay_lab/evidence/nominal_motion --sut twist_mux_legacy --no-timeline
...
invariants
  NOT_EXERCISED consistent_state
  PASS          finite_output
  PASS          fresh_output
  NOT_EXERCISED stop_dominance

findings
  none above info
  (+2 info finding(s); see --json)

VERDICT   PASS: invariants held; no warning or critical finding
          not exercised: consistent_state, stop_dominance
```

The same evidence with the navigation source going silent at t = 4.0 s while
it is commanding motion (output abridged to the causal timeline; the first
lines before t = 2 s are start-up):

```console
$ robot-blackbox lab replay examples/replay_lab/evidence/nominal_motion --sut twist_mux_legacy --inject drop:topic=/nav/cmd_vel,from_s=4.0
...
fault     F1 drop(every_n=1, from_s=4.0, topic=/nav/cmd_vel) -> 40 event(s)

invariants
  NOT_EXERCISED consistent_state
  PASS          finite_output
  FAIL          fresh_output  first at +4.520s, 75 violating tick(s)
  NOT_EXERCISED stop_dominance
...
causal timeline
  ...
  T0008   +2.003s  INPUT     /nav/cmd_vel command (0.150, 0.000, 0.000)
  T0009   +2.020s  OUTPUT    robot-facing command (0.150, 0.000, 0.000)  <- T0008
  T0010   +3.000s  DETECT    [info] recorder_trigger:manual_marker operator marker (synthetic): flight recorder trigger manual_marker fired
  T0011   +3.953s  INPUT     /nav/cmd_vel command (0.150, 0.000, 0.000)
  T0012   +4.003s  FAULT     fault F1 drop (every_n=1, from_s=4.0, topic=/nav/cmd_vel): 40 event(s) affected
  T0013   +4.460s  DECIDE    arbitration SILENT (time-driven, no new input): no live input, nothing is published, the robot-facing sink keeps (0.150, 0.000, 0.000)
  T0014   +4.520s  DETECT    [warning] command_source_stale /nav/cmd_vel: /nav/cmd_vel last commanded [0.15, 0.0, 0.0] and has been silent 0.567 s (window 0.5 s)  <- T0011  [related fault: F1]
  T0015   +4.520s  INVARIANT [critical] stale_command_forwarded robot_output: robot-facing command [0.15, 0.0, 0.0] is not backed by a fresh valid source message (matches /nav/cmd_vel aged 0.567 s)  <- T0009, T0011  [related fault: F1]

VERDICT   FAIL: invariant(s) violated: fresh_output
          not exercised: consistent_state, stop_dominance
```

Read it bottom up: the robot-facing command is still 0.15 m/s (T0015) because
the arbiter published nothing once its only input went stale (T0013) and the
sink kept the last command, which came from the last navigation message
received before the silence (T0011). The same fault through the intended
arbiter (`--sut helix_arbiter`) ends in DETECTED: `command_source_stale`
fires, and the robot-facing command is zero from t = 4.46 s.

## Golden cases

`examples/replay_lab/cases/` holds one case per failure class, over two
synthetic evidence bundles in `examples/replay_lab/evidence/`.
`examples/replay_lab/README.md` lists every case with its initial conditions,
injected fault and expected detector, safety and verdict result.

Regenerate the evidence with `python scripts/generate_replay_lab_evidence.py`;
`tests/unit/lab/test_fixtures.py` fails if the committed bundles drift from
the generator.

## Writing a case

```json
{
  "schema": "blackboxrs.lab.case.v1",
  "name": "stale_command__twist_mux_legacy",
  "regression": "which failure this pins down, and the source that documents it",
  "initial_conditions": "what the evidence shows before any fault",
  "injected_fault": "the fault in words",
  "evidence": "../evidence/nominal_motion",
  "sut": {"mode": "reference", "preset": "twist_mux_legacy", "overrides": {}},
  "faults": [{"kind": "drop", "topic": "/nav/cmd_vel", "from_s": 4.0}],
  "topics": {"/lowstate": {"liveness": "periodic", "stale_after_s": 0.5, "host": "robot"}},
  "expect": {"verdict": "FAIL", "invariants": {"fresh_output": "FAIL"},
             "detections": ["command_source_stale"], "info": []}
}
```

Optional keys: `window` (`from_s`, `to_s`), `monitors` (`stop_grace_s`,
`fresh_grace_s`, `clock_step_threshold_s`, `clock_offset_info_s`,
`observed_period_s`), `command_sources` (`{topic: window_s}`, observed mode).
Write the expectation from the failure you are pinning down before you run
the case; a case whose expectation was copied from its own output tests
nothing.

## Limitations

* The golden evidence is synthetic (the flight recorder's own generator,
  labelled in every result). Replay of a real captured flight bundle works the
  same way, but no real HELIX-run flight bundle is committed yet.
* The reference models are models of documented policy. The HELIX parity test
  runs only where a HELIX checkout exists; the twist_mux model has no parity
  test against the binary here (its behaviour is taken from the measurements
  in HELIX `docs/MOTION_ARBITRATION.md`).
* Reordering is only visible on topics that carry a publisher sequence number
  or an embedded stamp; a reordered `geometry_msgs/Twist` stream has neither.
* Transport findings from the flight analyzer are whole-stream counts, placed
  at the end of the replay without a first-offending event.
* A decision's cause is the input that determines it (winner, hold, rejected
  message) or the clock. Nothing more subtle is inferred.
* Replaying from `--from` starts without the state before it; the verdict
  says so.
* Sensitivity is bounded by fixed thresholds: a receipt-vs-source step under
  0.2 s, a periodic topic silent for less than its `stale_after_s`, and
  odometry frozen for fewer than 5 samples are not reported
  (`test_detection_thresholds_are_not_hair_triggers`).
* A topic is judged for staleness only if it is declared periodic (a
  `stale_after_sec` in the profile or `stale_after_s` in the case). An
  undeclared periodic topic that stops is not flagged: a missed detection,
  never a false one.
* `stop_dominance` overlaps the flight report's `nonzero_outputs_while_held`;
  a test checks the two agree on the same stream. The replay keeps its own
  because it also judges the model's output and names the offending event.
* The `clock_offset` finding overlaps the flight report's robot-clock warning
  (odometry stamps vs receipt); the replay's version works per host from DDS
  source timestamps.
