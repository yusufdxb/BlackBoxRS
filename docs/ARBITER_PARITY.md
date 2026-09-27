# Arbiter parity

Replay Lab judges safety on the command that reaches the robot-facing
boundary, so its value depends on reproducing the deployed arbitration path
exactly. This page records what the deployed path is, what Replay Lab runs
in its place, how the two were compared, what did not match, and what is
still unknown.

Summary: Replay Lab now **executes HELIX's own arbiter code**. twist_mux, a
C++ node, remains a **model**, checked message by message against the real
4.3.0 binary. Both were compared with the real processes running under
ROS 2 Humble on a PC, never on the robot. Three discrepancies were found
and fixed (D1 to D3). The parity gate passes.

## 1. What was inspected

HELIX: `https://github.com/yusufdxb/helix`, branch
`feat/motion-arbitration-hw-closure`, commit `b31a9ce8b49cd5f282b02c8dc5ee6d25287bb186`.
The arbiter was introduced in `7d5bc14`, and `5f474e5` is the commit built
and run on the GO2 payload computer in the 2026-09-18 no-motion session.
Every file below is byte-identical at all three commits. The arbiter is not
on HELIX `main`.

| Role | File (HELIX) | SHA-256 (prefix) |
|---|---|---|
| STOP arbitration, motion arbiter, freshness, rejection | `src/helix_arbiter/helix_arbiter/arbiter_core.py` | `4ca399f486daae8b` |
| ROS glue: routing, 50 Hz timer, publish on hold assertion | `src/helix_arbiter/helix_arbiter/arbiter_node.py` | `7acf292d75308d1a` |
| teleop 200 / nav 50, 0.5 s timeouts, hold_timeout 0.5 s, limits, 50 Hz | `src/helix_arbiter/config/arbiter.yaml` | `bdd549a9b95dd71b` |
| robot-facing output: /cmd_vel to StopMove (1003) / Move (1008), deadman 0.25 s | `src/helix_arbiter/helix_arbiter/sport_sink_core.py` (+ `go2_sport_sink.py`) | `fa57ef8858cd4edc` |
| hold state at 20 Hz; legacy zero Twist on /helix/cmd_vel after each hold=true | `src/helix_recovery/helix_recovery/recovery_node.py` | `cb0b60991582a5c2` |
| legacy path: teleop 200 > helix_recovery 100 > navigation 50, 0.5 s timeouts, one inert lock | `src/helix_bringup/config/twist_mux.yaml` | `517d948b3a69c342` |

twist_mux: Debian package `ros-humble-twist-mux 4.3.0-1jammy.20260907.224309`
(the binary that was run), and its source at tag `4.3.0`
(`ros-teleop/twist_mux` `d929f1b19a437abcf131287b9e3af27fb40ed34e`):
`src/twist_mux.cpp` (`hasPriority`, `publishTwist`) and
`include/twist_mux/topic_handle.hpp` (`callback`, `hasExpired`, `isMasked`).
The twist_mux version installed on the payload computer was not checked
(the robot was not reachable). The legacy path was never the motion path on
the GO2: a stock GO2 has no `/cmd_vel` consumer (HELIX
`docs/GO2_FIELD_NOTES.md`).

## 2. What Replay Lab runs now

```
replay evidence -> blackboxrs/lab/helix.py (adapter) -> HELIX arbiter_core.Arbiter -> robot-facing decision
```

* `arbiter_core.py`, `arbiter.yaml` and `twist_mux.yaml` are vendored byte
  for byte in `blackboxrs/lab/vendor/helix/` with `PROVENANCE.json` (repository,
  commit, path, SHA-256) and HELIX's MIT licence. The core is refused at load
  time if its hash differs. Both presets are built from the vendored YAML, not
  from numbers typed into Replay Lab.
* The adapter reproduces only the decision-relevant glue of `arbiter_node.py`,
  which needs rclpy and helix_msgs to import:
  - a Twist becomes `on_source` with all six components;
  - a HelixHold becomes `on_hold`, and when it asserts the hold, an immediate
    `decide` and publish;
  - a 50 Hz timer calls `decide`;
  - "now" is the monotonic receipt clock.

  That glue is what the recorded comparison below checks.
* The earlier re-implementation of the HELIX policy has been deleted.
* twist_mux stays a model (`TwistMuxModel` in `blackboxrs/lab/sut.py`). It is
  a C++ ROS node, with its own clock and executor, so it cannot run inside a
  deterministic, clock-free replay.

## 3. How parity was measured

`blackboxrs/lab/parity.py` defines 16 frozen scenarios. Each is a script of
source Twists and HelixHold states at fixed times, with holds at 20 Hz as the
recovery node publishes them.

| Scenario | Covers |
|---|---|
| `nominal_velocity` | released hold, one fresh source |
| `stop` | hold asserted while navigation keeps commanding |
| `stale_command` | the only source goes silent mid-motion |
| `stop_while_teleop_active` | hold asserted while teleop (priority 200) streams |
| `teleop_after_stop` | teleop starts after the hold |
| `nan_command` | NaN on the active source, then valid again |
| `nan_on_unused_axis` | NaN on linear.z only |
| `over_limit_command` | 1.3 m/s against the 1.0 m/s limit |
| `freshness_expiry` | one command, then silence across its window |
| `conflicting_sources` | teleop and nav both live; teleop leaves and returns |
| `hold_stream_lost` | the hold state stops arriving (telemetry loss of the safety state) |
| `hold_missing_at_start` | commands before any hold state |
| `resume_needs_new_command` | release after a hold; the source stopped during it |
| `release_with_live_source` | release while the source keeps streaming |
| `old_resume_redelivered` | an older (epoch, seq) release during a hold |
| `restart_lower_epoch` | hold stream stops, then resumes from a restarted publisher |

`scripts/parity/run_ros_parity.py` starts the real process for every
scenario, sends the script over DDS (isolated domain, localhost only), and
records every output with its receipt time. The recordings are in
`docs/parity/`. The largest send lateness was 6.0 ms; all the others were
under 0.3 ms.

| Recording | Real process | Configuration |
|---|---|---|
| `helix_arbiter_node.json` | `helix_arbiter` node, HELIX `b31a9ce` install, autostart | HELIX `arbiter.yaml` |
| `twist_mux.json` | `twist_mux` 4.3.0 binary, output remapped to /cmd_vel | HELIX `twist_mux.yaml`; the recovery node's zero Twist on /helix/cmd_vel after each hold=true |
| `twist_mux_tie.json`, `twist_mux_tie_reversed.json` | `twist_mux` 4.3.0 | two equal-priority inputs, names in both orders |
| `twist_mux_then_sink.json` | `twist_mux` 4.3.0 feeding `helix_go2_sport_sink` in dry_run | HELIX `twist_mux.yaml`, sink defaults |

The comparisons, which `tests/unit/lab/test_arbiter_parity.py` runs in CI
without ROS:

* **HELIX:**
  - The trace compared is ArbiterStatus: reason, selected source and output
    command, at every tick and every extra publication.
  - The ordered sequence of (reason, source, command) segments must be
    identical.
  - Every transition must agree within 25 ms: one 50 Hz period, because the
    real timer's phase is arbitrary, plus 5 ms.
  - Every hold=true message must be followed by a publication within 5 ms,
    in both the real node and Replay Lab.
* **twist_mux:** every published message, in order, with the same value,
  within 5 ms. An extra or missing message is a mismatch, so silence is
  compared too.
* **Scenario freeze:** every recording must have used the current frozen
  scripts, so a scenario edit forces a re-recording.
* **Direct calls:** on 20 seeded random streams, the adapter is also compared
  with direct calls into the live HELIX module. These streams include NaN,
  Inf, over-limit values, non-numbers, the non-actuated axes, and reordered
  and restarted hold states.

## 4. Discrepancies found and fixed

| ID | What differed | Deployed behaviour (evidence) | Old Replay Lab | Fix |
|---|---|---|---|---|
| D1 | STOP timing | `arbiter_node._on_hold` publishes at once when a hold is asserted. Measured: 204 hold=true messages, next publication after a median 0.25 ms (max 0.36 ms) | decided only at the next 50 Hz tick, up to 20 ms later, with no extra publication. Failed 7/16 scenarios against the real node | run the real core; the adapter reproduces the publish-on-hold glue |
| D2 | how STOP enters twist_mux | twist_mux subscribes to /helix/cmd_vel, where the recovery node publishes a zero Twist after each hold=true; it never reads /helix/hold | turned /helix/hold messages directly into a zero input, and ignored /helix/cmd_vel. Failed 6/16 scenarios against the binary | the model consumes /helix/cmd_vel; it derives those twists from hold=true only when the evidence did not record them, and says so |
| D3 | twist_mux priority ties | `hasPriority` keeps the first input with strictly greater priority, in name order. Measured with names in both orders: the alphabetically first name wins | most recent message wins | name order |

Also corrected:
* Both presets now load HELIX's YAML instead of hand-copied numbers.
* The legacy source is now named `navigation`, as in `twist_mux.yaml`
  (Replay Lab had called it `nav`).
* On the legacy path, a publication is judged at its callback time, not the
  next tick.

Before the fix, at every 50 Hz tick the old HELIX model and the real core made
the same decision in all 16 scenarios. D1 was the only HELIX difference.

## 5. What twist_mux does when input stops (measured, 4.3.0, HELIX config)

* **It publishes only from an input callback.** No timer publishes anything,
  and it never publishes a zero on timeout.
  - `stale_command`: the last input arrived at 0.9571 s and the last output
    followed at 0.9572 s, with the same 0.3 m/s value. Nothing was published
    for the remaining 2.04 s.
  - `freshness_expiry`: one message, then 1.49 s of silence.
* **Timeout is `now - stamp > timeout`**, on the node clock, which is wall
  time. An expired input stops outranking lower ones, but nothing is
  published at the moment it expires. The next published message is the
  next input from the new winner.
  - `conflicting_sources`: teleop's last message was at 1.4541 s, so it
    expired at 1.9541 s. The first navigation message after that arrived at
    1.9571 s and was published at 1.9574 s.
* **STOP with teleop active:** the teleop value (0.4 m/s) was published
  throughout the hold (37 messages). The HELIX STOP at priority 100 loses to
  teleop at 200. This reproduces, on the real binary, the failure HELIX
  documented.
* **NaN is forwarded unchanged.**

## 6. Does the robot-facing consumer keep the last command?

The two questions are kept apart.

1. **What twist_mux emits:** answered above. It goes silent and publishes no
   zero.
2. **What the consumer does with silence** depends on the consumer.

| Consumer | Status | Behaviour on silence |
|---|---|---|
| HELIX `helix_go2_sport_sink` (the robot-facing consumer HELIX ships) | **measured, real node, dry_run** | Sends StopMove (1003, reason DEADMAN) 0.252 s and 0.291 s after the last /cmd_vel in two runs (`input_timeout_sec` 0.25 on a 50 ms timer), then repeats it about every 0.55 s. It does **not** keep the last Move. |
| Stock GO2 on the legacy path | **known from HELIX field notes** | There is no /cmd_vel consumer at all, so twist_mux's output reaches nothing. |
| Isaac Sim bridge (`scripts/sim_bridge/go2_sim_bridge.py`) | relays /cmd_vel to the sim robot; the sim controller's retention was **not executed here** | unknown |
| GO2 firmware after a Move (1008) with no further requests | **unknown; hardware only** | Whether it keeps moving, and for how long, cannot be measured on a PC. |

So the legacy cases' "command continues after its freshness window" FAIL is
a finding about twist_mux feeding a consumer that retains the last command.
It holds for any such consumer. It is **not** a finding about HELIX's sport
sink, which stops 0.25 to 0.30 s after the last command. Every legacy result
states this assumption (`sut_state.consumer_assumption`).

## 7. The critical incident, re-run on the real HELIX core

The case is `nominal_motion` with `/nav/cmd_vel` dropped from 4.0 s
(`stale_command__*`, and the walkthrough in REPLAY_LAB.md).

* **`helix_arbiter`, now the real core:** DETECTED (`command_source_stale`),
  and every invariant held.
  - The robot-facing command becomes zero at replay time **4.460 s** with
    reason `NO_LIVE_INPUT`.
  - The last navigation message was received at 3.953081 s, so its 0.5 s
    window closes at 4.453081 s. 4.460 s is the first 50 Hz replay tick after
    that.
  - The value depends on the replay's tick phase. On a real arbiter the zero
    comes at its first tick after 4.453081 s, somewhere in
    (4.4531, 4.4731] s.
* **`twist_mux_legacy`:** FAIL, `fresh_output`. The last publication is the
  last input (3.953 s), then nothing. The robot-facing command stays 0.15 m/s
  under the retain-last-command assumption.

The earlier conclusion survives parity: the intended arbiter stops, and the
legacy path does not publish a stop. The legacy FAIL is conditional on the
consumer, as section 6 says.

## 8. Gate

`scripts/parity/gate.py` returns PASS only if all of the following hold:

| Check | Requires |
|---|---|
| G1 helix_provenance | vendored HELIX files equal HELIX at the pinned commit (needs a HELIX checkout, otherwise UNRESOLVED) |
| G2 helix_parity | every recorded arbiter_node run is reproduced |
| G3 twist_mux_parity | every recorded twist_mux run is reproduced, including silences and the tie-break |
| G4 scenario_freeze | the recordings used the frozen scripts |
| G5 determinism | every golden case replays byte-identically three times |

Any mismatch is FAIL, and anything unverifiable is UNRESOLVED; neither is a
PASS. The existing test suite runs as its own CI job. CI checks HELIX out at
the pinned commit and runs the gate.

**Result (2026-09-27): PASS.**
* G1: 3 files identical to HELIX `b31a9ce`.
* G2: 16/16 scenarios.
* G3: 18/18 runs.
* G4: all recordings match the frozen scripts.
* G5: 28 cases, three byte-identical replays each.

**Isolated, not part of the gate:**
* the twist_mux package version on the payload computer;
* whether any consumer retains the last command on the robot.

## 9. Needs the robot

* The HELIX arbiter and sink running on the payload computer, under that
  host's scheduling and DDS. The recordings here are from a PC.
* The GO2's response to StopMove and to a stale Move, which is actuator and
  firmware behaviour.
* A real HELIX flight bundle replayed through this path. None is committed
  yet.

## Re-recording

```console
source /opt/ros/humble/setup.bash
source <helix workspace>/install/setup.bash     # built at the pinned commit
for t in helix twist_mux twist_mux_tie twist_mux_tie_reversed twist_mux_then_sink; do
  python3 scripts/parity/run_ros_parity.py --target $t --helix-src <helix checkout> \
    --out docs/parity/$( [ $t = helix ] && echo helix_arbiter_node || echo $t ).json
done
python3 scripts/parity/gate.py --helix-src <helix checkout>
```
