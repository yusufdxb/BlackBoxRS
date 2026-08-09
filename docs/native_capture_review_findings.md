# Native capture known issues

Findings from an adversarial review pass over the native capture plane, covering
concurrency, storage durability, ROS 2 correctness, and evidence integrity.
Recorded against commit `55e50f4`.

Findings are listed whether or not they have been fixed; the status column is
the authority on what has actually changed. Anything marked open is a known
limitation of the current implementation and is part of why the native backend
is not the default. See `native_capture_promotion_gate.md`.

## Verified as sound

These were attacked deliberately and held, and are recorded so later work does
not undo them by accident.

- The SPSC ring buffer is correct. Acquire/release pairing is right in both
  directions (`ring_buffer.hpp:56-92`), `size()` reads `tail_` before `head_`
  so an observer can over-report but never underflow, and `head_`/`tail_` are
  on separate cache lines. The single-producer claim is upheld: every
  subscription and timer is bound to one mutually-exclusive callback group.
- The payload arena is not racy despite being non-atomic: producer and writer
  only ever touch disjoint blocks, handed off through two release/acquire
  rings.
- Write ordering in `finish_segment` is correct: writer close, sink sync, fd
  close, rename, directory sync, checksum, then sidecar. A valid sidecar
  pointing at an invalid segment is not reachable. Short writes and injected
  I/O failures were exercised and behaved.
- Mid-chunk truncation recovers a valid prefix and reports discarded tail bytes
  with a specific corruption reason.
- The memory budget is real and enforced at startup from compiled `sizeof`
  values, with checked arithmetic. Topic registry, string arena, trigger state,
  and graph maps are each fixed-capacity with explicit exhaustion accounting.
- The ingest callback does no I/O, no locking, and no heap allocation, and
  drops with accounting rather than blocking.
- The completeness contract mechanically caps root-cause confidence at 0.69
  when native evidence is degraded, and refuses to claim completeness on
  best-effort QoS alone.

## Blockers

| # | Finding | Location | Status |
|---|---|---|---|
| B1 | Incident-window reads zero out every delivery-loss counter and report `complete`. `capture_quality.json` lives at the session root; an incident directory has no equivalent, so `rmw_messages_lost`, `best_effort_topics`, coverage-truncation and fault counters all default to 0/False. Reproduced: same session, same loss, session read says `incomplete` with 5000 lost samples, incident-window read says `complete` with 0. | `blackboxrs/recording/native.py:626-636`, `899-938` | **Fixed.** Completeness now requires `delivery_scope == "callback_received"`; absence adds `delivery_scope_unverified` and forces incomplete. |
| B2 | The default Python backend produced a 0.92-confidence root cause with the capture-quality section omitted entirely from the report and the confidence cap bypassed, because `capture_quality` stayed `None`. Absence read as "no problems". | `blackboxrs/incident/builder.py:213`, `report.py:70-72` | **Fixed.** Builder always constructs a quality object; the report never omits the section. See "Design decision" below. |
| B3 | A chunk CRC mismatch raises `mcap.stream_reader.CRCValidationError`, which inherits `ValueError` and is not in the reader's except clause. One flipped bit makes the incident bundle unbuildable, which is the one artifact the system exists to produce. | `blackboxrs/recording/native.py:1292` | **Open.** |
| B4 | A session from a SIGKILLed process reports `quality.clean is True`. A crashed segment has no sidecar by design, so it contributes no value to `all(self._clean_values)` and cannot pull the conjunction false. Also `finish_segment(clean)` is called with a literal `true` at all three call sites, so the sidecar `clean` field is a constant. | `native.py:374-381`, `segment_writer.cpp:935,1159,1189` | **Open.** |
| B5 | Recovery reports `discarded_tail_bytes: 0` for a crash that lost a message. The count measures bytes discarded from the input file; a crash ending on a record boundary loses an unflushed chunk that is never counted. Confirmed: partial had sequences 1..199, clean control run had 1..200. | `segment_writer.cpp:502-551` | **Open.** |
| B6 | Use-after-free of `Impl` on shutdown under a multi-threaded component container. Subscription and timer lambdas capture raw `this`; the shutdown barrier only excludes callbacks already holding the producer token, not one dispatched but not yet entered. Untested by construction: the concurrent-stop test runs with no executor attached. | `recorder.cpp:1354-1367`, `325-338`, `290` | **Open.** |

## Major

| # | Finding | Location | Status |
|---|---|---|---|
| M1 | `state_` is last-writer-wins, so `kStorageFault` and `kInvariantFault` are overwritten by `kShedding`/`kNormal` microseconds later. For a forensic recorder the state field an operator reads can lie. Needs a monotone escalation lattice for sticky states. | `recorder.cpp:1454-1512` vs `1767-2389` | Open |
| M2 | Writer-thread death turns the recorder into a silent no-op: the thread exits, `accepting_` stays true, the ring fills, and every message drops as `kRingFull` while the state reads SHEDDING forever. No watchdog, no escalation to stop. | `recorder.cpp:1761-1774` | Open |
| M3 | Storage fault is terminal and unrecoverable. `writer_faulted_` is latched and never cleared; the `RECOVERING` state named in the plan does not exist in the code. Retention only runs on segment close, so a faulted recorder can no longer free its own disk. A transient disk-full costs the rest of the shift. | `recorder.cpp:1823-1824` | Open |
| M4 | Nothing subscribes to `/blackbox/capture_status` and nothing supervises the child process. The recorder publishes STORAGE_FAULT at 1 Hz to a topic with no subscriber; an operator learns of it whenever someone next builds a bundle. | `blackboxrs/recording/native_process.py` | Open |
| M5 | Shedding policy inverts incident value. At the watermark every topic not in `high_priority_topics` is dropped, and the shipped default lists only `/tf_static`. Under the exact backpressure that constitutes an incident, the recorder discards `/imu/data`, `/joint_states` and `/cmd_vel` and preserves a latched static transform. Docs describe this as graduated; the code is a binary cliff. | `recorder.cpp:1466-1470`, `config/native_capture.yaml:17-18` | Open |
| M6 | Sequence-gap laundering. Drop records are cumulative per (topic, reason), so one record's first/last sequence can span the session and `_gap_accounted` retires an arbitrarily large hole. Confirmed: 2 accounted drops laundered 798 unexplained missing sequences. Completeness survives, the detail does not. | `native.py:1516-1517` | Open |
| M7 | Unaccounted drop in the shutdown race. The first `accepting_` check returns without recording either received or dropped, so messages delivered between `request_stop()` and subscription teardown vanish with zero counter movement while `clean` still computes true. These are the last messages before shutdown, which is exactly the evidence wanted when shutdown was caused by the failure under investigation. | `recorder.cpp:1443-1445` | Open |
| M8 | Discovery, graph introspection and subscription creation run on the same single-threaded executor and callback group as message ingestion. With `discover_all` and `max_topics: 1024` a single graph change forces a full scan inline with ingest. Scenario C's 17x p50-to-p99 ratio is consistent with this but does not confirm it. | `recorder.cpp:797-800`, `995-1160` | Open |
| M9 | False sharing in `CaptureMetrics`: `TopicCounters` is eight adjacent atomics on one cache line, written by the producer and the writer thread once per message in both directions. This undoes the padding discipline the ring buffer was careful about. | `metrics.hpp:179-189`, `277-282` | Open |
| M10 | A stale `.tmp` sidecar permanently bricks sidecar writes, because `write_atomic_text` opens with `O_CREAT \| O_EXCL` and never cleans up. The segment is already renamed to its clean name first, producing exactly the artifact B4 misreads. The recorder's own copy of this helper uses `O_TRUNC`; the two disagree. | `segment_writer.cpp:455-493` | Open |
| M11 | A failed recovery leaves a lossy unlabelled MCAP at the final name and is not retryable without hand-deleting files during an incident. | `segment_writer.cpp:1490-1531` | Open |
| M12 | MCAP writer index memory escapes the enforced budget. `chunkIndex_` and `currentMessageIndex_` grow for the life of a segment and are omitted from the budget computation, despite the plan's own formula naming an index allowance. | `recorder.cpp:764-767`, `writer.hpp:454,457` | Open |

## Design decision recorded

The review recommended that a `None` capture-quality object be replaced by an
explicit `unknown` and that the existing 0.69 confidence cap then apply. The
first half is implemented. The second is deliberately not: capping every
Python-backend incident would suppress detector-grounded confidence across the
entire shipped product, which is a product decision rather than an evidence
one. The distinction drawn instead is between *measured* degradation, which
caps confidence, and a *standing property* of a backend that never measured
delivery, which is disclosed in the report but does not cap. The silent part of
the failure is closed either way, because the report can no longer omit the
section. If the promotion gate later requires the stricter reading, the change
is one condition in `_apply_capture_quality_limit`.

## Cross-cutting gaps

- No baseline of any kind. There is no rosbag2 comparison and no Python
  comparison, and the supervisor cannot launch either. Every performance
  argument for the C++ boundary is therefore currently undefended by
  measurement. The honest justification is GIL-free determinism and failure
  isolation, which is structural rather than a speed claim.
- Coverage is thin exactly where risk is highest: 6 unit tests cover the
  2,604-line `recorder.cpp`, and none cover the shedding policy, graph
  reconciliation, or the trigger-to-manifest incident flow that is the
  strongest differentiator against rosbag2.
- The benchmarked configuration is not the shipped configuration. The
  supervisor generates a 4096-entry ring and arena; the shipped default is
  16384. The default deployment config has never been benchmarked.
- No ThreadSanitizer target exists for a component whose correctness argument
  is hand-proved memory ordering across three threads.
- Continuous writing to maintain rolling history costs roughly 100 GB/day at
  the Scenario B rate. Flash endurance is not discussed anywhere.
