# BlackBoxRS — Architecture

## System Overview

BlackBoxRS is a single-host observability daemon for ROS 2 robots. It
passively observes a running ROS 2 graph, collects system telemetry,
runs a small set of built-in anomaly detectors, and streams every event
into a structured JSONL log on disk. There is no out-of-process queue,
no message broker, no remote storage — everything runs in one Python
process and writes to local files.

```
┌─────────────────────────────────────────────────────────────┐
│                     BlackBoxRS Daemon                        │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ ros_monitor  │  │system_monitor│  │  anomaly_engine  │  │
│  │ (rclpy node) │  │ (psutil +    │  │   threshold      │  │
│  │              │  │  sysfs)      │  │   frequency      │  │
│  │ topology +   │  │ cpu/mem/disk │  │   dead-topic     │  │
│  │ frequency +  │  │ thermal/gpu  │  │   qos-mismatch   │  │
│  │ qos snapshot │  │              │  │                  │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                 │                    │             │
│         ▼                 ▼                    ▼             │
│        ┌────────────────── core.event_bus ──────────────┐   │
│        │  thread-safe in-process pub/sub (queue.Queue)  │   │
│        └─────────────────────┬──────────────────────────┘   │
│                              ▼                              │
│                ┌────── logging pipeline ───────┐            │
│                │  RotatingJsonlWriter -> *.jsonl│           │
│                └───────────────────────────────┘            │
│                                                             │
│  CLI (click): start · stop · status · dump-log · replay     │
│              · config · init                                 │
└─────────────────────────────────────────────────────────────┘
```

## Module Responsibilities

### `core/` — Shared infrastructure
- **event_bus.py** — Thread-safe in-process pub/sub for `BlackBoxEvent`.
  Backed by `queue.Queue`. Subscribers may listen on a specific channel
  (matched against `event.source`) or globally with `channel=None`.
  Every subscriber queue is **bounded** (default capacity
  `event_bus_queue_maxsize = 1024`, overridable per-subscription).
  `publish()` is strictly non-blocking: when a subscriber's queue is
  full the event is dropped for that subscriber, a per-queue drop
  counter is incremented, and a rate-limited warning is logged.
  Slow consumers cannot back-pressure or memory-bomb the producers.
  Critical internal consumers (logger, anomaly engine, recorder) ask
  for larger protected queue capacities than the bus default.
- **schemas.py** — Pydantic v2 models for the event envelope and a few
  typed payloads. The envelope enforces a fixed `source` Literal
  (`ros_monitor | system_monitor | anomaly_engine | rosbag_recorder`)
  and `severity` Literal (`debug | info | warning | error | critical`).
- **config.py** — Dataclass-based YAML config. Unknown keys are
  warned on by default and raised in strict mode; missing keys fall
  back to dataclass defaults.
- **clock.py** — Centralised UTC-now and ISO formatting. There is no
  ROS sim-time integration today.
- **session.py** — Per-run identifier (UUID prefix), hostname, and
  start time. Attached as metadata to every emitted event.

### `ros_monitor/` — ROS 2 graph observer
- Stands up as an rclpy node `/blackbox/blackbox_ros_monitor`.
- Polls the graph on a timer, dynamically subscribes to discovered
  topics with a permissive best-effort QoS profile, records each
  inter-arrival time in `FrequencyTracker`, and emits per-topic
  `ros.frequency` events on a separate timer.
- Captures **publisher and subscriber QoS profiles separately** via
  `RosIntrospector` and emits one `ros.qos` event per topic (carrying
  `publisher_qos_profiles` and `subscriber_qos_profiles` lists). The
  QoS-mismatch detector pairs each pub × sub combination.
- If `rclpy` is not importable, the monitor logs a warning at
  `start()` and stays inactive — the rest of the daemon runs unaffected.

### `system_monitor/` — Host telemetry
- Pure-Python, no ROS dependency.
- Per-collector classes: `CpuCollector`, `MemoryCollector`,
  `DiskCollector`, `ThermalCollector`, `GpuCollector`. Each returns
  either a dict, a list, or `None`. Lists are wrapped under
  `{"items": [...]}` before publishing so the event payload always
  satisfies the `dict[str, Any]` schema contract.
- `GpuCollector` auto-selects between `nvidia-smi` (desktop), Jetson
  sysfs (`/sys/devices/gpu.0/load` + GPU thermal zone), or skips when
  no backend is present.
- One thread per `SystemMonitor`. Polls every `interval_sec`, publishes
  events to the bus, sleeps the remainder of the interval.

### `anomaly_engine/` — Detection layer
- Subscribes globally to the bus (`channel=None`) and runs every event
  through every registered detector. Anomaly events are republished to
  the bus; the engine skips events whose `source == "anomaly_engine"`
  to avoid feedback loops.
- Four built-in detectors are always loaded. Additional user-supplied
  detectors can be registered via `anomaly_engine.custom_detectors` in
  `config.yaml` — each entry specifies a dotted import path to a
  `BaseDetector` subclass plus optional `params` kwargs. The loader
  (`detectors/loader.py`) imports, validates, and instantiates them at
  startup; errors are logged and skipped.

| Detector | Consumes | Emits |
|---|---|---|
| `ThresholdDetector` | `system.cpu` (`cpu_percent`), `system.memory` (`memory_percent`), `system.gpu` (`gpu_temp_c`) | `anomaly.threshold` |
| `FrequencyDetector` | `ros.frequency` | `anomaly.frequency` (auto-learned baseline + tolerance floor) |
| `DeadTopicDetector` | `ros.frequency` | `anomaly.dead_topic` (only fires when *another* event arrives — driven by the bus, not by an internal heartbeat) |
| `QoSMismatchDetector` | `ros.qos` | `anomaly.qos_mismatch` (one event per topic with at least one incompatible pub × sub pair) |
| Custom detectors | User-defined | User-defined (should use `anomaly.` prefix by convention) |

### `recording/` — Anomaly-triggered rosbag2 capture
- `Rosbag2Recorder` subscribes to `channel="anomaly_engine"` and
  reacts only to configured anomaly types (default: all four built-in
  anomaly event types, overrideable in config).
- Starts `ros2 bag record` as a supervised subprocess in its own
  process group, stops it after `record_duration_sec`, and enforces a
  cooldown between captures.
- Emits structured lifecycle events
  (`rosbag.recorder_ready|recorder_unavailable|recording_started|
  stopped|failed|skipped`) under the
  `rosbag_recorder` source so detached/background mode remains
  debuggable through the normal JSONL log.

### `logging/` — Structured persistence
- `LoggingPipeline` subscribes globally and drains events to a
  `RotatingJsonlWriter`.
- The writer rotates by **size** (`log_rotation_mb`) and prunes by
  **file count** (`log_max_files`). There is **no time-based retention**.
- Filenames embed a UTC timestamp with microsecond resolution
  (`blackboxrs_YYYYMMDD_HHMMSS_uuuuuu.jsonl`); lexicographic order
  matches chronological order. Microseconds + exclusive-create on open
  guarantee that multiple rotations within the same wall-clock second
  always land in distinct files — a previous second-resolution naming
  scheme silently collided under high rotation rates.
- `LogReader` provides streaming time-range queries, tail, and source/
  severity filters. There is no separate index file — readers scan in
  order.

### `cli/` — User interface
- Built with `click`. The `BlackBoxDaemon` orchestrates every component
  and writes a PID file at `~/.blackboxrs/blackboxrs.pid`.
- `start` runs in the background by default, spawning
  `python -m blackboxrs start --foreground` as a detached subprocess.
  `--foreground` runs in the current process and installs SIGINT/SIGTERM
  handlers.
- The PID file at `~/.blackboxrs/blackboxrs.pid` is a JSON payload
  carrying `{pid, starttime, cmdline}`. `starttime` is field 22 of
  `/proc/<pid>/stat` (boot-relative jiffies); it is re-checked on every
  `is_running()` call so a recycled PID from an unrelated process is
  rejected. `stop_running()` relies on this verification — it never
  signals an unverified PID. The pidfile is written atomically via
  `mkstemp + os.replace` so concurrent readers never observe a partial
  write. Legacy plain-integer pidfiles (from releases before v0.2) are
  treated as stale and cleaned up since identity cannot be verified.
  Behaviour is Linux-specific because `/proc` provides the identity
  tokens; on non-Linux hosts the `starttime` check degrades to a
  cmdline-only round-trip comparison.

## Data Flow

```
[ROS 2 Graph] ──► ros_monitor ──┐
                                 ├──► event_bus ──┬─► anomaly_engine ──► event_bus (re-publish)
                                 │                ├─► rosbag2 recorder ─► ros2 bag record
[Host OS]    ──► system_monitor─┘                 │
                                                  └─► logging pipeline ──► JSONL file
```

The anomaly engine starts before the producers do, so it sees every
event from `t=0`. The recorder starts immediately after the anomaly
engine so it can catch the first anomaly of the session. The engine and
logging pipeline subscribe **globally** to the bus; the recorder uses an
`anomaly_engine`-scoped subscription.

## Threading Model

Each component owns exactly one background thread, started inside its
own `start()`:

| Component | Thread name | What it does |
|---|---|---|
| `LoggingPipeline` | `logging-pipeline` | Drains its bus subscription, writes JSONL |
| `AnomalyEngine`   | `anomaly-engine`   | Drains its bus subscription, runs detectors |
| `Rosbag2Recorder` | `rosbag2-recorder` | Supervises anomaly-triggered rosbag2 captures |
| `SystemMonitor`   | `blackbox-system-monitor` | Polls collectors at `interval_sec` |
| `RosMonitor`      | `blackbox-ros-monitor` | Spins the rclpy executor |

The daemon does not stack additional worker threads on top of these.
An earlier version of `BlackBoxDaemon._register` did so, which caused
duplicate event delivery (for thread-driven components) and queue-
splitting (for queue-driven ones). The current contract is documented
on `_Component` in `cli/daemon.py`.

The bus uses bounded `queue.Queue` instances (thread-safe). Each
subscriber gets its own queue; producers append with `put_nowait` and
drop-on-full, so a slow consumer cannot block producers or grow memory
unboundedly. Per-queue drop counters are exposed via
`EventBus.dropped_count(queue)`.

## Unified Event Schema

```json
{
  "timestamp": "2026-04-16T14:59:53.691602Z",
  "source": "system_monitor",
  "event_type": "system.cpu",
  "severity": "info",
  "data": { "cpu_percent": 6.2, "cpu_count": 24 },
  "metadata": {
    "session_id": "20c8b5f68030",
    "hostname": "mewtwo",
    "start_time": "2026-04-16T14:59:53.585415+00:00"
  }
}
```

`event_type` is the contract. The full inventory of emitted types is
in the README.

## Configuration

YAML config at `~/.blackboxrs/config.yaml`. The schema is the
`BlackBoxConfig` dataclass tree:

```yaml
log_dir: ~/.blackboxrs/logs
log_rotation_mb: 50
log_max_files: 20
event_bus_queue_maxsize: 1024

ros_monitor:
  enabled: true
  poll_interval_sec: 1.0
  track_latency: true
  topic_filters: []

system_monitor:
  enabled: true
  interval_sec: 1.0
  gpu_backend: auto       # auto | nvidia-smi | tegrastats | none

anomaly_engine:
  enabled: true
  thresholds:
    cpu_percent: 90.0
    memory_percent: 85.0
    gpu_temp_c: 80.0
  frequency:
    tolerance_percent: 20.0
  dead_topic:
    timeout_sec: 5.0

rosbag2:
  enabled: false
  executable: ros2
  output_dir: ~/.blackboxrs/bags
  record_duration_sec: 30.0
  cooldown_sec: 60.0
  storage_id: sqlite3
  max_recordings_per_run: 10
  trigger_event_types:
    - anomaly.threshold
    - anomaly.frequency
    - anomaly.dead_topic
    - anomaly.qos_mismatch
  topics: []
```

There is no top-level `general:` block, no `log_format` field, no
`log_retention_days`, no `detectors:` list, and no per-detector custom
registration.

## Technology Choices

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Python 3.10+ | Matches ROS 2 Python ecosystem |
| ROS 2 API | rclpy | Standard Python client library |
| System metrics | psutil | Cross-platform, well-maintained |
| Event schema | Pydantic v2 | Validation + serialisation |
| CLI | click | Composable, well-documented |
| Config | PyYAML | Standard for the ROS ecosystem |
| Log format | JSONL | Streamable, greppable, easy to read back |
| Concurrency | `threading` + `queue.Queue` | Plain stdlib; the bus is sync |

There is no `asyncio` in the current implementation, despite earlier
diagrams suggesting so.

## What is verified vs. inferred

- **Verified locally** (`tests/integration/`):
  - one thread per component, threshold detector against real
    `system.cpu` events, events reaching the JSONL log on disk, PID-
    file lifecycle, anomaly engine subscribe-before-publish ordering,
    and anomaly-triggered recorder lifecycle events flushed to disk via
    the full daemon path;
  - CLI subprocess smoke test (`test_cli_subprocess.py`): `python -m
    blackboxrs start --foreground` writes an identity pidfile, flushes
    real events to disk, and exits cleanly on SIGTERM;
  - Live ROS 2 integration (`test_ros_live.py`): boots a real `rclpy`
    publisher on an isolated `ROS_DOMAIN_ID`, confirms `RosMonitor`
    discovers the topic, auto-subscribes, and emits `ros.topology` +
    `ros.frequency` events with a positive `frequency_hz`, and that
    `topic_filters` actually excludes filtered topics.
- **Verified in CI**: `ruff check` and `pytest -q` on Python 3.10 / 3.11
  / 3.12, a benchmark regression gate on Ubuntu 22.04 / Python 3.10,
  and a Docker-built ROS 2 Humble job that runs the live `rclpy`
  integration test inside `docker/Dockerfile.humble`.
- **Still inferred**: live Jetson sysfs / tegrastats values,
  multi-host ROS 2 scenarios, behaviour under ROS distros other than
  Humble.
