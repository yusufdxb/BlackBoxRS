# Native capture promotion gate

The Python capture backend remains the default. The existence of a C++ package,
a successful build, or a single throughput result is not enough to promote the
native backend.

## Required evidence

All items require a retained command or machine-readable artifact. A checkbox is
not proof by itself.

### Functional and interface parity

- [ ] Arbitrary configured topics capture through generic serialized subscriptions
      on ROS 2 Humble with type support installed.
- [ ] Topic appearance, disappearance, type churn, endpoint-count changes, and
      observable QoS changes enter the ordered chronology.
- [ ] Steady order survives backward and forward ROS clock jumps.
- [ ] Native output reaches the existing incident pipeline through
      `NativeCaptureReader` without a parallel incident implementation.
- [ ] Dead-topic, timeline, fingerprint, evidence reference, and report workflows
      pass against the committed deterministic bag fixture.
- [ ] The available real GO2-derived replay workflow remains successful. This is
      offline bag evidence, not a live onboard hardware claim.
- [ ] Onboard and observer configurations use the same binary without private
      hardware assumptions.

### Boundedness and loss integrity

- [ ] Startup reports a capture-owned memory estimate from compiled sizes,
      rejects overflow, and rejects estimates above `buffer.memory_budget_bytes`.
- [ ] Event ring, payload arena, topic registry, string arena, segment table, and
      history retention each enforce a finite capacity.
- [ ] Full queues reject or shed according to policy without blocking callbacks.
- [ ] Every recorder-side loss path increments count and bytes with reason, topic,
      time range, and sequence range where available.
- [ ] Control-reserve exhaustion remains visible in sideband metadata.
- [ ] Clean-run counter reconciliation passes and unexplained sequence gaps make
      completeness unknown.

### Storage and lifecycle

- [ ] Normal close, rotation, checksum, reopen, and sidecar validation pass.
- [ ] Scripted short writes, `ENOSPC`, `EIO`, sync failure, and rename failure do
      not produce a clean segment.
- [ ] Truncated and process-crashed `.partial.mcap` files recover only a valid
      prefix and expose discarded or unknown tail loss.
- [ ] SIGINT and SIGTERM-like shutdown drain to the configured deadline and label
      deadline expiry incomplete.
- [ ] Disk slowdown raises queue pressure while the ROS callback remains
      responsive.
- [ ] Triggered pre-history and post-history manifests report the actual retained
      interval under the byte cap.
- [ ] Repeated restarts stay within `storage.total_max_bytes` and
      `storage.max_sessions`, including incident hard-link accounting.

### Testing and toolchain

- [ ] Existing Python tests and lint pass unchanged.
- [ ] GCC and Clang build the native packages.
- [ ] GoogleTest covers ring wrap and overflow, arena exhaustion and stale
      handles, registry type changes, triggers, writer failures, clocks, and
      shutdown.
- [ ] ASan and UBSan runs are clean. Any supported TSan job is identified as a
      separate result, not implied by the other sanitizers.
- [ ] Fuzz targets cover segment recovery and metadata/control decoding, with a
      retained corpus and no unresolved crash.
- [ ] Python malformed-file tests reject or recover inputs deterministically.
- [ ] ROS integration and benchmark smoke tests avoid narrow timing thresholds on
      shared runners.

### Performance and endurance

- [ ] `blackboxrs.capture_benchmark.v1` artifacts exist for the declared workload
      sweep and state validity without missing reconciliation counters.
- [ ] Native versus rosbag2 full-payload comparisons use matched topics, QoS,
      compression, chunking, durability, warmup, and run order.
- [ ] Any Python comparison retains equivalent content and semantics.
- [ ] Five or more fresh controlled launches support each published percentile.
- [ ] A two-to-eight-hour artifact covers RSS samples, queue depth, allocator
      trend, rotation, counters, loss, and shutdown.
- [ ] A matched standalone versus component result exists before making a
      composition performance claim.
- [ ] README numbers link to artifacts and contain no extrapolated or fabricated
      result.

### Incident trust

- [ ] Incident JSON and report expose backend, received/captured/committed/durable
      counts, drops and bytes, peak utilization, storage errors, capture window,
      recovery, and clock anomalies.
- [ ] Any drop, storage fault, unclean segment, missing status, recovery, or
      unresolved sequence gap visibly lowers evidence completeness.
- [ ] Root-cause text never presents incomplete evidence as complete.
- [ ] The Python backend and current rosbag2 compatibility path remain available.

## Promotion decision

Promotion is a deliberate configuration and release decision after every gate is
supported by current artifacts. Until then:

```yaml
capture:
  backend: python
```

Operators may opt into `cpp` for evaluation, but deployment notes must identify
which gates remain open. If rosbag2 gains the missing BlackBoxRS integrity and
trigger contracts, reevaluate whether native code can shrink rather than treating
its continued existence as a goal.
