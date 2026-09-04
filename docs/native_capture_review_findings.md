# Native capture known issues

Findings from an adversarial review pass over the native capture plane, covering
concurrency, storage durability, ROS 2 correctness, and evidence integrity.
The original pass was recorded against the pre-native baseline `55e50f4` and was
re-audited during native hardening. Line references from the original findings
are evidence pointers, not stable API references.

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
| B3 | A chunk CRC mismatch raises `mcap.stream_reader.CRCValidationError`, which inherits `ValueError` and is not in the reader's except clause. One flipped bit makes the incident bundle unbuildable, which is the one artifact the system exists to produce. | `blackboxrs/recording/native.py` | **Fixed.** CRC failures become malformed evidence, preserve diagnostics, and do not prevent incident construction. |
| B4 | A session from a SIGKILLed process reports `quality.clean is True`. A crashed segment has no sidecar by design, so it contributes no value to `all(self._clean_values)` and cannot pull the conjunction false. Also `finish_segment(clean)` is called with a literal `true` at all three call sites, so the sidecar `clean` field is a constant. | `blackboxrs/recording/native.py` | **Fixed.** Final session quality is authoritative; a partial forces false and a missing final record leaves clean unknown. |
| B5 | Recovery reports `discarded_tail_bytes: 0` for a crash that lost a message. The count measures bytes discarded from the input file; a crash ending on a record boundary loses an unflushed chunk that is never counted. Confirmed: partial had sequences 1..199, clean control run had 1..200. | `segment_writer.cpp`, `native.py` | **Fixed for integrity, not reconstruction.** Recovery declares `unwritten_tail_loss_unknown` and exposes the last recovered low-32 sequence. It does not invent an exact lost count. |
| B6 | Use-after-free of `Impl` on shutdown under a multi-threaded component container. Subscription and timer lambdas capture raw `this`; the shutdown barrier only excludes callbacks already holding the producer token, not one dispatched but not yet entered. Untested by construction: the concurrent-stop test runs with no executor attached. | `recorder.cpp` | **Open and contained.** Component registration is disabled by default. Standalone is the only supported deployment path until executor-level quiescence is proven. |
| B7 | Recovery trusted a forged uncompressed chunk size while computing CRC. A 1 GiB value in a 1.8 KiB input caused a segmentation fault. | `segment_writer.cpp` | **Fixed.** Chunk sizes are bounded, uncompressed size must equal compressed size for uncompressed chunks, and a regression test rejects the forged input. |
| B8 | Recovery could unlink its own input when it aliased the output partial or recovery sidecar path. | `segment_writer.cpp` | **Fixed.** Canonical path and inode-equivalence checks run before cleanup; path-alias regression tests preserve the input. |

## Major

| # | Finding | Location | Status |
|---|---|---|---|
| M1 | `state_` is last-writer-wins, so `kStorageFault` and `kInvariantFault` are overwritten by `kShedding`/`kNormal` microseconds later. For a forensic recorder the state field an operator reads can lie. Needs a monotone escalation lattice for sticky states. | `recorder.cpp` | **Fixed.** Fault transitions are sticky and emit one machine-readable health record. |
| M2 | Writer-thread death turns the recorder into a silent no-op: the thread exits, `accepting_` stays true, the ring fills, and every message drops as `kRingFull` while the state reads SHEDDING forever. No watchdog, no escalation to stop. | `recorder.cpp` | **Fixed.** Writer failure closes admission, exposes writer liveness/fault state, and attributes remaining backlog to storage failure. |
| M3 | Storage fault is terminal and unrecoverable. `writer_faulted_` is latched and never cleared; the `RECOVERING` state named in the plan does not exist in the code. Retention only runs on segment close, so a faulted recorder can no longer free its own disk. A transient disk-full costs the rest of the shift. | `recorder.cpp` | **Open by fail-stop policy.** Runtime recovery is not claimed; supervision must restart into a new session after external disk remediation. |
| M4 | Nothing subscribes to `/blackbox/capture_status` and nothing supervises the child process. The recorder publishes STORAGE_FAULT at 1 Hz to a topic with no subscriber; an operator learns of it whenever someone next builds a bundle. | `blackboxrs/recording/native_process.py` | **Fixed.** Post-READY child monitoring parses machine-readable health/final lines and emits BlackBoxRS health events. |
| M5 | Shedding policy inverts incident value. At the watermark every topic not in `high_priority_topics` is dropped, and the shipped default lists only `/tf_static`. Under the exact backpressure that constitutes an incident, the recorder discards `/imu/data`, `/joint_states` and `/cmd_vel` and preserves a latched static transform. Docs describe this as graduated; the code is a binary cliff. | `recorder.cpp`, `config/native_capture.yaml` | **Fixed.** Four explicit priority tiers shed at graduated watermarks; control and robot-state topics occupy the protected tiers. |
| M6 | Sequence-gap laundering. Drop records are cumulative per (topic, reason), so one record's first/last sequence can span the session and `_gap_accounted` retires an arbitrarily large hole. Confirmed: 2 accounted drops laundered 798 unexplained missing sequences. Completeness survives, the detail does not. | `native.py` | **Fixed fail-closed.** Only exact contiguous ranges retire a gap; sparse cumulative spans are flagged unverifiable and make evidence incomplete. |
| M7 | Unaccounted drop in the shutdown race. The first `accepting_` check returns without recording either received or dropped, so messages delivered between `request_stop()` and subscription teardown vanish with zero counter movement while `clean` still computes true. These are the last messages before shutdown, which is exactly the evidence wanted when shutdown was caused by the failure under investigation. | `recorder.cpp` | **Fixed.** Callback entry always consumes a global sequence and records received plus shutdown or storage drop before returning. |
| M8 | Discovery, graph introspection and subscription creation run on the same single-threaded executor and callback group as message ingestion. With `discover_all` and `max_topics: 1024` a single graph change forces a full scan inline with ingest. Scenario C's 17x p50-to-p99 ratio is consistent with this but does not confirm it. | `recorder.cpp` | **Partially fixed.** Configured-topic mode no longer takes a graph-wide topic snapshot and derives types from bounded endpoint queries. Explicit `discover_all` still scans and reconciles inline; a bounded worker/intent path and churn measurements remain open. |
| M9 | False sharing in `CaptureMetrics`: `TopicCounters` is eight adjacent atomics on one cache line, written by the producer and the writer thread once per message in both directions. This undoes the padding discipline the ring buffer was careful about. | `metrics.hpp` | **Fixed structurally.** Producer, writer, and shared drop totals occupy separate aligned cache-line lanes, and the padded footprint remains part of the enforced capture-memory estimate. A repeated performance comparison is still required before claiming a measured speedup. |
| M10 | A stale `.tmp` sidecar permanently bricks sidecar writes, because `write_atomic_text` opens with `O_CREAT \| O_EXCL` and never cleans up. The segment is already renamed to its clean name first, producing exactly the artifact B4 misreads. The recorder's own copy of this helper uses `O_TRUNC`; the two disagree. | `segment_writer.cpp` | **Fixed.** Unique same-directory temporary files replace the fixed name. |
| M11 | A failed recovery leaves a lossy unlabelled MCAP at the final name and is not retryable without hand-deleting files during an incident. | `segment_writer.cpp` | **Fixed for the owned output path.** Recovery locks the target, replaces stale owned partial/sidecar state, and publishes metadata before final rename. |
| M12 | MCAP writer index memory escapes the enforced budget. `chunkIndex_` and `currentMessageIndex_` grow for the life of a segment and are omitted from the budget computation, despite the plan's own formula naming an index allowance. | `segment_writer.cpp` | **Fixed.** Online message/chunk indexes and the optional summary are disabled; chunk CRCs remain enabled. |
| M13 | An exit code of zero with no authoritative `FINAL_STATUS` could be accepted during a supervisor stop race. | `blackboxrs/recording/native_process.py` | **Fixed.** The supervisor tracks the final record explicitly and requires exactly `STOPPED_CLEAN`; missing, malformed, or incomplete final status emits an incomplete-capture event. |
| M14 | Producer and writer threads could expose an incoherent drop-ledger snapshot, allowing a new count to be observed before its bytes and sequence bounds. | `metrics.hpp` | **Fixed.** Bytes and monotonic min/max bounds are published before the release-ordered count, with an adversarial concurrent final-snapshot test. Periodic snapshots may be conservative, while the stopped final snapshot is coherent. |
| M15 | A writer exception after dequeue could omit the current event from the loss ledger and skip payload reclamation. | `recorder.cpp` | **Fixed.** The in-flight event is tracked, attributed to storage failure on exception, and reclaimed; reclaim failures latch an invariant fault. |
| M16 | Python could call a session clean when `durable < committed`, or when an unclean segment contradicted clean final metadata. | `blackboxrs/recording/native.py` | **Fixed fail-closed.** Counter order, clean durability equality, and cross-artifact clean-state agreement are validated. |
| M17 | One incident could hard-link two disjoint rolling windows and exceed the single-window byte allowance used by the root quota reserve. | `recorder.cpp` | **Fixed.** Each incident's linked segment set now has an explicit `retention_max_bytes` cap. |
| M18 | A CRC mismatch in any complete chunk rejects the whole recovery rather than publishing all earlier valid chunks. | `segment_writer.cpp` | **Fixed for the native uncompressed format.** Recovery stops at the first CRC-invalid chunk, republishes only complete earlier chunks, discards the corrupt chunk and everything after it, and records the discarded suffix and CRC reason. A three-chunk test proves later valid-looking data is not reissued. Compressed-input CRC prefix location remains an open recovery limitation. |
| M19 | A valid footer followed by junk and then trailing magic was labeled clean. | `segment_writer.cpp` | **Fixed.** Clean recovery now requires exact footer-to-magic adjacency, a structurally valid tail, and no parser problem. |
| M20 | Native and Python recovery metadata parsing accepted resource-unbounded inputs. | `segment_writer.cpp`, `blackboxrs/recording/native.py` | **Fixed for declared inputs.** Python JSON files are capped, and native recovery caps schema/channel counts, aggregate metadata, chunks, and message payloads. The recovery-specific rewrite path still lacks direct libFuzzer coverage. |
| M21 | An unterminated child log line could grow the Python supervisor's pending buffer without limit. | `blackboxrs/recording/native_process.py` | **Fixed.** Pending status-line assembly is capped and oversized lines are discarded with an explicit diagnostic. |

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

- The harness can launch native and matched rosbag2 full-payload workloads, but
  no retained comparison matrix with repeated launches exists. Python remains
  excluded because its retained content is not equivalent. No relative speed
  claim is supported yet.
- Recorder and shedding coverage increased, including live DDS shutdown,
  storage fault, oversized-arrival heartbeat, and graduated-tier tests. Graph
  reconciliation and trigger-command saturation still need deterministic seams.
- The benchmarked configuration is not the shipped configuration. The
  supervisor generates a 4096-entry ring and arena; the shipped default is
  16384. The default deployment config has never been benchmarked.
- No ThreadSanitizer result exists. ASan and UBSan do not prove absence of data
  races, and component lifetime remains intentionally outside the supported
  deployment contract.
- Recovery parser, event decoder, and topic metadata libFuzzer targets have a
  CI smoke job. The storage review found that the segment parser target does not
  directly exercise the recovery rewrite path, so recovery fuzz coverage is
  still a promotion gap.
- Continuous writing to maintain rolling history costs roughly 100 GB/day at
  the Scenario B rate. Flash endurance is not discussed anywhere.
