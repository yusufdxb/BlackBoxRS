# Native capture format

## Format choice

Native capture uses MCAP with profile `ros2`. It does not introduce a proprietary
payload container. Original topic names, discovered ROS types, serialization
format `cdr`, and serialized bytes remain interoperable with MCAP tooling.

BlackBoxRS-specific chronology and quality records use a reserved MCAP channel,
`/blackboxrs/events`, with versioned compact JSON written by the storage thread.
The hot path stores fixed enums and numeric structures, not free-form JSON.

## Session layout

```text
capture_<session_id>/
  session.json
  capture_quality.json
  segments/
    0000000000000000.partial.mcap
    0000000000000000.mcap
    0000000000000000.json
  incidents/
    incident_<trigger_sequence>/
      capture.json
      0000000000000000.mcap
      0000000000000000.json
recovered.mcap
recovered.mcap.recovery.json
```

The `.partial.mcap` suffix means the segment has not completed the clean close
contract. Consumers do not treat it as complete. A clean rotation closes MCAP,
performs the configured sync, renames to `.mcap`, syncs the parent directory, and
publishes the sidecar atomically.

Native segments use checksummed MCAP chunks but disable the online message index,
chunk index, and optional summary. Those structures grow with the message or
chunk population and would otherwise sit outside the startup memory estimate.
Segments are intentionally short and Python scans them sequentially.

Incident segment and sidecar entries are hard links to finalized files from the
rolling segment set. `storage.max_incidents` bounds retained incident
directories. Rolling paths are independently bounded by
`storage.retention_max_bytes` and `storage.retention_max_segments`; a hard-linked
inode remains allocated until its last incident or rolling link is removed.

## Record contracts

Ordinary message channels use:

| MCAP field | Value |
|---|---|
| profile | `ros2` |
| topic | Original resolved ROS topic |
| schema name | Discovered ROS interface type |
| schema encoding | `ros2msg` |
| channel message encoding | `cdr` |
| log time | Recorder steady-clock nanoseconds, which preserve capture ordering |
| publish time | Recorder ROS time when valid, otherwise zero |
| sequence | Low 32 bits of the BlackBoxRS global sequence, with the full range in metadata |

The initial writer identifies the installed ROS type but leaves MCAP schema data
empty. The serialized CDR remains intact, but a standalone decoder that requires
the full message definition must resolve it from an installed ROS interface. An
embedded definition is an interoperability improvement, not a current guarantee.

The C++ generic callback does not promise DDS source timestamps or publisher
sequence numbers. `EventHeader.sequence` is BlackBoxRS callback-attempt order.
Dropped attempts consume a sequence number, so persisted gaps can be reconciled
against drop ledgers.

Control records use schema version `blackboxrs.capture_event.v1` and include these
common fields:

```json
{
  "schema_version": "blackboxrs.capture_event.v1",
  "monotonic_ns": 0,
  "ros_time_ns": 0,
  "ros_time_valid": true,
  "sequence": 0,
  "topic_id": 0,
  "flags": 0,
  "kind": "drop"
}
```

Kind-specific fields are versioned. Unknown fields are ignored. Unknown major
schema versions are rejected by the Python reader. Topic ID zero means recorder,
global, or unknown. A type change receives a new topic ID.

## Capture status

`/blackbox/capture_status` carries a `std_msgs/msg/String` JSON document with
schema `blackboxrs.capture_status.v1`. The stable fields are:

- recorder state and session ID;
- received, admitted, committed, durable, and dropped event counts;
- dropped bytes;
- current, capacity, and peak queue depth;
- storage error count and last accepted global sequence;
- admission state, writer liveness, sticky writer-fault state, and lost trigger
  intent count;
- clean, incomplete, or recovery state where applicable.

Status is operational telemetry. The final on-disk session and sidecars are the
offline source of truth. If final status or final capture quality is missing
because the process crashed, the reader marks the session incomplete and reports
any partial segment.

Per-topic and per-reason loss details live in bounded `DROP_EVENT` chronology;
the current status JSON exposes aggregate drop count and bytes only.

## Session and segment metadata

The initial `session.json` identifies schema version, session ID, capture backend,
the relative segment directory, and a system-wall to steady-clock anchor. Python
uses that anchor for incident-window datetimes while preserving ROS time as
independent clock evidence. It does not yet persist requested topics or a complete
segment manifest. Readers must not infer them from absence.

After a successful final segment close, drain writes `capture_quality.json`
atomically with schema
`blackboxrs.capture_quality.v1`. It records the backend and runtime role, clean
state, received/admitted/committed/durable/drop counters, captured and dropped
bytes, storage and clock errors, peak queue depth and capacity, capture memory
budget, retained interval, rolling retention caps, and intentionally evicted
segment/event/byte totals. This final record lets Python distinguish deliberate
rolling-history eviction from unexplained event loss.

For a session directory, this final record is the authority for clean shutdown.
Per-segment sidecars prove their own finalized files, not the cleanliness of the
whole process lifetime. A partial segment forces `clean: false`; an absent final
quality record leaves cleanliness unknown. An incident-window directory likewise
cannot inherit session cleanliness unless its own manifest supplies the required
quality contract.

Each `<index>.json` sidecar records:

- schema and producer version;
- file name, byte length, and SHA-256;
- first and last global sequence and steady timestamp;
- cumulative `received`, `admitted`, `committed`, and `dropped` counts;
- cumulative `bytes_captured` and `bytes_dropped`;
- integer `peak_queue_utilization` percent, storage errors, and clock anomalies;
- clean and recovered flags.

The segment sidecar does not carry a durable count separate from committed.
Readers use the final capture-quality durable watermark and must not infer one
when that final record is absent.

Each incident `capture.json` uses schema
`blackboxrs.incident_capture.v1`. It records the trigger sequence and steady
timestamp, requested and actual steady-time bounds, selected segment identity,
sequence range, size and SHA-256, cumulative received/committed/dropped counts,
and three explicit completeness booleans: `history_complete`,
`post_window_elapsed`, and `links_complete`. A configured history duration does
not imply completeness when any of these fields says otherwise.

Readers validate schema versions, sidecar size and hash, MCAP framing and CRCs,
sequence order, and reconciliation. A sidecar cannot turn a partial or invalid
MCAP into a clean segment.

The recorder publishes `current_session.json` in the configured output root only
after startup reaches READY. The Python daemon removes stale pointers before
launch and waits for a new pointer before declaring the native component started.
Incident bundles copy selected MCAP segments and metadata under
`attachments/native_capture`, so evidence references remain portable after root
retention removes the source session.

## Checksums and recovery

MCAP chunk and data-section CRCs are enabled where supported. The optional summary
is disabled, so there is no summary CRC. SHA-256 in the sidecar covers the
finalized segment. These checks detect accidental damage; they are not signatures
and do not protect against a malicious writer that can replace both data and
metadata.

The required recovery contract is to accept only a complete valid prefix, stop at
the first corrupt or truncated record, and emit a new recovered MCAP. Recovery
metadata needs:

- whether the input had a clean footer;
- recovered record count;
- discarded tail bytes;
- corruption or truncation reason;
- unknown tail loss when exact loss cannot be reconstructed.

The recovery helper preflights CRCs for every complete, uncompressed chunk,
rejects invalid or oversized chunk sizing, and requires exact footer-to-trailing
magic adjacency before calling an input clean. It rewrites messages from the
readable portion, publishes through a `.partial` file plus rename and
parent-directory sync, and atomically writes a
`blackboxrs.capture_recovery.v1` sidecar. The sidecar records recovered message
count, discarded tail bytes, reason, output size, SHA-256, the low 32 bits of the
last recovered sequence when available, and whether unwritten tail loss remains
unknown. At the first uncompressed chunk CRC mismatch, recovery bounds the input
view at that chunk's record offset. Complete earlier chunks are republished;
the corrupt chunk and all later bytes are discarded and never searched for a
new synchronization point. A mismatch in the first chunk therefore yields a
valid empty recovery artifact rather than reissuing corrupt payload.

Recovery serializes writers for the same output with an advisory lock, removes
stale owned `.partial` and sidecar paths for that exact output, and publishes the
recovery sidecar before the final MCAP rename. Metadata writes use unique
same-directory temporary files followed by file sync, rename, and directory sync,
so an old fixed `.tmp` name cannot brick future finalization.

Recovery rejects inputs that resolve to an owned output, partial, or sidecar
path, including an inode alias. Schema and channel tables are capped at 4096
entries, their aggregate metadata at 16 MiB, and individual chunks and messages
at 64 MiB. Python caps each native JSON metadata document at 4 MiB.

The installed recovery interface is:

```bash
ros2 run blackbox_capture_cpp blackbox_capture_recover \
  damaged.partial.mcap recovered.mcap
```

Python recognizes the resulting `.recovery.json`, exposes discarded-tail and
corruption metadata, and keeps recovered evidence incomplete.

## Data-only rosbag2 export

The installed exporter converts finalized native evidence into one standard,
indexed rosbag2 MCAP file:

```bash
ros2 run blackbox_capture_cpp blackbox_capture_export_rosbag2 \
  capture_<session_id> exported.mcap

# A single finalized segment is also accepted.
ros2 run blackbox_capture_cpp blackbox_capture_export_rosbag2 \
  capture_<session_id>/segments/0000000000000000.mcap exported.mcap
```

A session export requires versioned `session.json`, a clean final
`capture_quality.json`, no partial segment, and at least one finalized segment.
The exporter scans segments in filename order, copies only `cdr` ROS channels,
and omits every `/blackboxrs/events` control record. Topic names, ROS schema type
names, schema definitions when present, message sequence low bits, and serialized
payload bytes are retained without deserialization. The output channel includes
the standard `offered_qos_profiles` metadata key. It is empty when the native
capture did not retain the original rosbag2 YAML QoS representation, so rosbag2
uses its default playback QoS unless the caller supplies an override.

For normal rosbag2 time semantics, a nonzero native ROS publish timestamp becomes
both output log and publish time. Records without a valid ROS time retain the
native log timestamp. This exported file is an interoperability artifact, not a
replacement for the native evidence chronology: the source MCAP remains the
authority for steady-clock ordering across ROS-clock jumps.

The exporter rejects non-ROS profiles, non-CDR data channels, missing or
unsupported ROS schemas, type churn, conflicting schema or standard QoS
definitions for one topic, malformed input, and configured capacity overruns.
Default limits are 4096 segments, 4096 topics, 16 MiB of retained schema metadata,
64 MiB per message, and 4 MiB per session metadata document. Output uses a
same-directory temporary, checked writes and file sync, then an atomic
no-overwrite publication. An existing destination is never replaced.
After the no-replace rename succeeds, the result is observably published. If the
following directory sync fails, the command returns an explicit
"published, durability unconfirmed" error and leaves that output in place for
inspection. A retry never overwrites it.

The resulting file can be consumed directly when its message packages are
installed:

```bash
ros2 bag info exported.mcap --storage mcap
ros2 bag play exported.mcap --storage mcap
```

The strict CRC-prefix guarantee currently applies to the uncompressed chunks
written by the native recorder. Compressed chunks are size-gated, but do not yet
have the same precisely located CRC cutoff. The helper also does not prove a
strict scanner for every possible record corruption. The recovered MCAP sequence
can bound what was present in the readable prefix, but it is not the last known
durable sequence from the crashed process. Those remain open promotion gates.
No incident report should claim stronger crash recovery before a broader
corruption corpus demonstrates it.

## Python reader contract

Python consumers use the native reader abstraction rather than MCAP implementation
details:

```python
for event in NativeCaptureReader(session_path):
    incident_pipeline.consume(event)
```

The adapter yields canonical low-rate `BlackBoxEvent` chronology and stable MCAP
evidence references. Large serialized payloads remain in MCAP. Capture-quality
summary data travels alongside events so incident construction can expose loss,
recovery, and clock anomalies.

For Python timelines, `session.json` anchors steady-clock nanoseconds to system
wall time captured at recorder startup. Ordering and window filtering use that
mapping, while each event retains its raw ROS timestamp in
metadata. A ROS clock rollback therefore remains visible without reordering or
filtering out later capture evidence.

The compatibility backend remains `capture.backend: python`. Native capture is
opt-in until the promotion gates pass.

## Versioning

Schema identifiers use a named contract plus major version, for example
`blackboxrs.capture_event.v1`. Additive optional fields are allowed within a major
version. Removing a field, changing its meaning or unit, changing ordering rules,
or changing checksum coverage requires a new major version. Readers fail closed
on an unsupported major version and preserve the path and reason in diagnostic
output.
