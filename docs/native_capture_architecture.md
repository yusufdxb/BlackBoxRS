# Native capture architecture

## Language boundary

BlackBoxRS separates high-rate evidence capture from incident intelligence. The
`blackbox_capture_cpp` process owns serialized ROS 2 ingestion, bounded memory,
monotonic ordering, graph chronology, loss accounting, and durable segment
publication. Python continues to own incident construction, semantic detectors,
causal ranking, fingerprints, reports, replay, and prevention rules.

This is a latency and failure-isolation boundary, not a language rewrite. Opaque
CDR payloads do not cross into Python during capture. Python reads a stable
evidence contract after, or while, segments become durable.

```mermaid
flowchart TD
    DDS[ROS 2 and DDS graph] --> CAP[blackbox_capture_cpp]
    CAP --> RING[Bounded SPSC descriptor ring]
    CAP --> ARENA[Fixed-block payload arena]
    RING --> WRITER[Single segment writer thread]
    ARENA --> WRITER
    WRITER --> MCAP[Versioned MCAP segments and sidecars]
    MCAP --> READER[Python NativeCaptureReader]
    READER --> INTEL[Incidents, timelines, causes, fingerprints, reports, prevention]
    CAP --> STATUS[/blackbox/capture_status]
```

The package and executable contracts are:

- package: `blackbox_capture_cpp`
- executable: `blackbox_capture`
- node: `/blackbox/blackbox_capture`
- experimental component class: `blackbox_capture::RecorderNode`, registered only
  when `BLACKBOX_CAPTURE_ENABLE_EXPERIMENTAL_COMPOSITION=ON`
- status topic: `/blackbox/capture_status`, `std_msgs/msg/String`, JSON schema
  `blackboxrs.capture_status.v1`
- benchmark package and executable: `blackbox_capture_bench publisher`

Standalone execution is the deployment default. The component entry point makes
composition testable, but no performance advantage is claimed without a matched
standalone-versus-container artifact.

## Hot-path concurrency

The first implementation deliberately uses SPSC instead of an unproven lock-free
MPSC structure.

1. Generic subscriptions and capture-control callbacks run in one mutually
   exclusive callback group.
2. The standalone binary uses `SingleThreadedExecutor`.
3. That callback group is the only producer of event descriptors.
4. One writer thread is the only consumer.
5. Graph and clock notifications update bounded state. Reconciliation in the
   capture callback group creates chronology events.

`SpscRingBuffer` uses monotonically increasing head and tail counters with
acquire/release publication. Data admission stops at `capacity - control_reserve`.
Control events may use the reserve. The policy is reject-newest, never overwrite,
because the writer can own the oldest descriptor. Rejected data and rejected
control attempts have separate counters.

The process shutdown order is admission stop, callback stop, bounded writer
drain, segment close and sync, final status publication, and thread join. Signal
handlers request shutdown; they do not perform storage work.

## Bounded memory

`EventHeader` is fixed-width and includes steady time, signed ROS time, global
sequence, topic ID, payload size, flags, and a reserved field. `Event` is asserted
to 56 bytes at compile time. Sequence and steady time define chronology. ROS time
is evidence that may move backward or forward.

Serialized bytes are copied into a startup-sized fixed-block arena. Each block
has bounded link and generation metadata. A payload consumes
`ceil(payload_size / block_size)` blocks and is rejected before copying if it
exceeds `max_payload_bytes` or the arena has insufficient free blocks. Generation
tokens and bounded chain traversal detect stale or corrupt handles.

For these configuration terms:

- `N`: event ring capacity
- `R`: reclaim ring capacity, currently `N + 1`
- `K`: payload block count
- `B`: payload block bytes
- `T`: topic capacity
- `D`: topic string arena bytes
- `Qt`: topic-command capacity, currently `T + 1`
- `Qg`: trigger-command capacity, currently `T + 12`
- `Pmax`: maximum accepted payload and writer scratch allowance
- `C`: MCAP chunk allowance
- `W`: fixed writer buffers and bounded segment/topic indexes

the dominant capture-owned allocation is approximately:

```text
N * 56
+ R * sizeof(PayloadHandle)
+ Qt * sizeof(TopicCommand)
+ Qg * sizeof(TriggerCommand)
+ K * (B + sizeof(BlockMetadata))
+ T * sizeof(TopicEntry)
+ D
+ (T + 1) * sizeof(TopicMetricsAndDropLedgers)
+ Pmax
+ C
+ W
```

The exact startup estimate uses `sizeof` values from the compiled binary. With
the checked-in `native_capture.yaml`, the Humble GCC build reports 85,094,916
capture-owned bytes (about 81.15 MiB) against a 134,217,728-byte ceiling and
refuses startup if the estimate exceeds that ceiling.
ROS middleware history, RMW allocations, generic-subscription serialized-message
objects, shared libraries, executor state, and allocator bookkeeping are outside
this capture-owned budget. RSS therefore exceeds the formula. Stable deployment
requires both an enforced capture budget and a measured long-run RSS artifact.

## ROS subscriptions and graph chronology

Configured topics are discovered at runtime and subscribed through
`rclcpp::create_generic_subscription` with the discovered type. The callback
copies serialized CDR without semantic deserialization. Runtime type support for
the discovered type must be installed.

Configured-topic mode does not enumerate the full DDS topic graph. It queries
only the configured endpoint set and derives the active type from publisher
endpoint information, which keeps the default reconciliation work proportional
to configured scope. Explicit `discover_all` mode still performs graph-wide
enumeration on the serialized callback lane and remains subject to the churn
promotion gate.

Topic identity is `(resolved topic, ROS type, serialization format)`. IDs start at
one and do not recycle during a session. A type change creates a new ID. Topic
entries and string bytes both have fixed capacities.

Graph snapshots normalize observed changes in nodes, topics, endpoint counts,
types, publisher identity sets, and available QoS metadata into the same ordered chronology. DDS graph
notifications can coalesce and discovery is eventually consistent, so these are
observed diffs, not a claim of perfect instantaneous graph history. Unsupported
RMW loss or QoS event callbacks are recorded as a capability limitation.

Topic registration is a two-stage bounded operation. The capture registry owns
the stable ID, then a separate bounded command registers that ID with the writer.
Subscriptions are created only after the writer command enters its queue. A full
command queue is retried on the next graph reconciliation instead of creating a
subscription whose payloads the writer cannot identify.

ROS clock callbacks may also coalesce before the capture timer emits a control
record. The record carries the last observed delta plus `coalesced_count` and
`anomaly_count`. Clock activation and deactivation remain clock events but do not
increment the anomaly counter. Configured forward and backward thresholds avoid
classifying ordinary simulated-clock ticks as jumps.

## Clocks and triggers

`std::chrono::steady_clock` controls ordering, deadlines, rates, and durations.
The node's ROS clock supplies signed ROS nanoseconds when active. A ROS clock jump
therefore cannot reorder the incident stream. Clock activation, rollback, and
forward jumps become bounded control records.

Native trigger emissions are deliberately narrow: configured-topic heartbeat,
bounded rate deviation, and queue high watermark. Payload exhaustion, storage
faults, RMW loss, and clock anomalies are explicit quality or chronology records,
not trigger codes. TF meaning, process diagnosis, likely causes, and other
semantic reasoning remain Python responsibilities.

Pre-trigger history uses continuously rotated MCAP segments rather than an
unbounded RAM window. Closed segments remain in a rolling set bounded by both
`storage.retention_max_bytes` and `storage.retention_max_segments`. A trigger
rotates the writer, extends capture to the steady-clock post-trigger deadline,
and hard-links overlapping finalized segments into a bounded incident directory.
Hard links preserve incident evidence when its rolling path is later evicted
without copying the payload.

Each `blackboxrs.incident_capture.v1` manifest records the requested and actual
steady-time interval, selected segments, capture counters, `history_complete`,
`post_window_elapsed`, and `links_complete`. These fields are the authority for
whether the configured time window was actually retained. Seconds configure the
desired interval, while the segment and byte caps remain the hard retention
limits. `storage.max_incidents` bounds the number of pinned incident directories;
the promotion suite must still prove rotation, overlap selection, hard-link
failure handling, and eviction under pressure before this is a deployment claim.

The output root is also bounded across restarts. At startup,
`storage.total_max_bytes` reserves the configured worst-case rolling, incident,
and active-segment footprint for the new session, while `storage.max_sessions`
and the remaining byte allowance prune oldest `capture_*` directories. The
accounting is conservative because hard links are counted by pathname. Each
incident is independently capped at one `storage.retention_max_bytes` allowance,
including the combined activation and finalization link sets.

`buffer.memory_budget_bytes` is an enforced preflight ceiling for capture-owned
bounded structures and writer scratch space. The reported estimate uses compiled
structure sizes plus configured graph and segment-state allowances. ROS client,
DDS middleware, allocator overhead, and the transient full discovery snapshot in
`discover_all` mode remain outside that ceiling and are not a whole-process RSS
guarantee.

## Onboard, observer, and composition modes

The same binary can subscribe through the local DDS graph from an onboard host or
an observer workstation. It has no Jetson-specific device assumption. In observer
mode, capture timestamps describe arrival at the observer and publisher-to-callback
latency is not meaningful across hosts without a separately verified clock
synchronization contract.

The package can register an `rclcpp_components` entry point for evaluation, but safe
component unload is not yet a supported deployment contract. Executor work can
already hold a callback that captures recorder state when teardown begins, and
the component cannot quiesce an externally owned executor. Standalone execution
is therefore required for deployment. Component registration is off by default
until an adversarial load/unload test proves
the lifetime barrier. No composition performance or durability advantage is
claimed.

When Python launches the standalone backend, it supervises the child after
`READY`, consumes machine-readable `HEALTH_STATUS` and `FINAL_STATUS` lines, and
turns unexpected exit, sticky storage or invariant faults, and incomplete
shutdown into BlackBoxRS events. The ROS status topic remains available for
other operators, but incident integrity does not depend on an optional status
subscriber.

## Why not only rosbag2?

ROS 2 Humble rosbag2 already solves generic topic recording and offers mature
storage plugins. Its MCAP conventions are reused, and it remains the comparison
baseline for full-payload recording.

The native component exists for BlackBoxRS-specific capture contracts that are
not exposed together by rosbag2 snapshot recording:

- monotonic and ROS timestamps in one incident chronology;
- structured graph, clock, trigger, drop, and storage-state events;
- per-topic, per-reason count and byte accounting after callback receipt;
- deterministic pre-trigger and post-trigger incident state;
- a reserved control path under data pressure;
- explicit committed and durable watermarks;
- capture-quality metadata that constrains Python incident confidence.

This is not a claim that rosbag2 is slower. Where rosbag2 already supplies a
requirement, BlackBoxRS uses its conventions or compares against it. If future
rosbag2 APIs expose the remaining integrity and trigger contracts, the native
component should delegate more work rather than retain duplicate machinery.

## Proof boundary

The recorder can account for every generic-subscription callback that begins and
every later admission, rejection, queue, payload, writer, shutdown, and recovery
outcome. It cannot prove publisher intent, messages DDS never delivered,
semantic validity of opaque CDR, perfectly instantaneous graph state, or
power-loss durability beyond the last successful storage barrier. Recovery can
prove bytes and records present in a valid input prefix. It cannot reconstruct a
message that never reached the partial file, so that tail is reported as unknown.
