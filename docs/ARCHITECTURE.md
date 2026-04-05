# BlackBoxRS — Architecture

## System Overview

BlackBoxRS is a flight-recorder for ROS 2 robots. It passively observes a running
ROS 2 graph, collects system telemetry, detects anomalies, and streams all events
into structured, replayable logs.

```
┌─────────────────────────────────────────────────────────────┐
│                     BlackBoxRS Daemon                        │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ ros_monitor   │  │system_monitor│  │  anomaly_engine  │  │
│  │              │  │              │  │                  │  │
│  │ • topic disc │  │ • CPU/mem    │  │ • threshold      │  │
│  │ • freq track │  │ • GPU/temp   │  │ • rate-of-change │  │
│  │ • QoS snap   │  │ • disk I/O   │  │ • QoS mismatch   │  │
│  │ • latency    │  │ • Jetson     │  │ • dead topics    │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                 │                    │             │
│         ▼                 ▼                    ▼             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                   core.event_bus                     │    │
│  │          (in-process async event queue)              │    │
│  └──────────────────────┬──────────────────────────────┘    │
│                         │                                   │
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                  logging pipeline                    │    │
│  │                                                     │    │
│  │  EventSerializer → RotatingFileWriter → index.jsonl │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                      CLI                             │    │
│  │  start · stop · status · dump-log · replay · config  │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Module Responsibilities

### core/ — Shared Infrastructure
- **event_bus.py** — Async in-process pub/sub for `BlackBoxEvent` objects.
  All monitors publish here; the logging pipeline subscribes.
- **schemas.py** — Pydantic models for the unified event schema.
- **config.py** — YAML-based configuration loader with sane defaults.
- **clock.py** — Monotonic + wall-clock timestamp helper (handles ROS sim time).

### ros_monitor/ — ROS 2 Graph Observer
- Runs as a standard `rclpy` node (`/blackbox/ros_monitor`).
- Uses `rclpy` introspection APIs to discover topics, services, and nodes.
- Subscribes to topics dynamically, measures inter-message intervals.
- Captures QoS profiles per topic via `get_publishers_info_by_topic()`.
- Emits `RosTopicEvent`, `RosQoSEvent`, `RosLatencyEvent` to the event bus.

### system_monitor/ — Host Telemetry
- Pure-Python, no ROS dependency (can run standalone).
- Uses `psutil` for CPU, memory, disk.
- Parses `/sys/devices/virtual/thermal/` for thermals.
- On Jetson: parses `tegrastats` for GPU util, power draw, GPU temp.
- On desktop: optionally reads `nvidia-smi` via subprocess.
- Emits `SystemMetricEvent` at configurable intervals (default 1 Hz).

### anomaly_engine/ — Detection Layer
- Stateless rule engine operating on the event stream.
- Built-in detectors:
  - **ThresholdDetector** — fires when a metric exceeds a configured bound.
  - **FrequencyDetector** — fires when topic rate drops below expected Hz.
  - **QoSMismatchDetector** — fires when pub/sub QoS profiles are incompatible.
  - **DeadTopicDetector** — fires when a topic goes silent for N seconds.
- Each detector consumes events from the bus, emits `AnomalyEvent` back.
- Detectors are pluggable — users register custom detectors via config.

### logging/ — Structured Persistence
- Subscribes to ALL events on the event bus.
- Serializes events to newline-delimited JSON (JSONL).
- Writes to rotating log files under `~/.blackboxrs/logs/`.
- Maintains an index file for fast time-range queries.
- Log replay: reads JSONL back into event objects for post-mortem.

### cli/ — User Interface
- Built with `click`.
- Commands:
  - `robot-blackbox start` — launches daemon (foreground or background).
  - `robot-blackbox stop` — graceful shutdown via PID file.
  - `robot-blackbox status` — shows live metric summary.
  - `robot-blackbox dump-log` — exports logs (filters: time range, source, level).
  - `robot-blackbox replay` — replays a log file to stdout or a topic.
  - `robot-blackbox config` — prints effective config.

## Data Flow

```
[ROS 2 Graph] ──► ros_monitor ──┐
                                 ├──► event_bus ──► logging ──► JSONL files
[Host OS]     ──► system_monitor─┘        │
                                          ▼
                                   anomaly_engine ──► AnomalyEvent ──► event_bus
```

## Unified Event Schema

```json
{
  "timestamp": "2026-04-05T14:23:01.123456Z",
  "source": "ros_monitor",
  "event_type": "topic_frequency",
  "severity": "info",
  "data": {
    "topic": "/camera/image_raw",
    "frequency_hz": 29.4,
    "expected_hz": 30.0
  },
  "metadata": {
    "node": "/blackbox/ros_monitor",
    "hostname": "jetson-orin",
    "session_id": "a1b2c3d4"
  }
}
```

## Configuration

YAML config at `~/.blackboxrs/config.yaml`:

```yaml
blackboxrs:
  log_dir: ~/.blackboxrs/logs
  log_rotation_mb: 50
  log_max_files: 20

  ros_monitor:
    enabled: true
    poll_interval_sec: 1.0
    track_latency: true
    topic_filters: []  # empty = all topics

  system_monitor:
    enabled: true
    interval_sec: 1.0
    gpu_backend: auto  # auto | tegrastats | nvidia-smi | none

  anomaly_engine:
    enabled: true
    detectors:
      threshold:
        cpu_percent: 90.0
        memory_percent: 85.0
        gpu_temp_c: 80.0
      frequency:
        tolerance_percent: 20.0
      dead_topic:
        timeout_sec: 5.0
```

## Technology Choices

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Language | Python 3.10+ | ROS 2 ecosystem, rapid iteration |
| ROS 2 API | rclpy | Standard Python client library |
| System metrics | psutil | Cross-platform, well-maintained |
| Event schema | Pydantic v2 | Validation, serialization, performance |
| CLI | click | Clean, composable, well-documented |
| Config | PyYAML | Standard for ROS ecosystem |
| Logging format | JSONL | Streamable, greppable, replayable |
| Async | asyncio + threading | Event bus async, monitors in threads |

## Threading Model

```
Main Thread (CLI / signal handling)
  │
  ├── Thread: ros_monitor (rclpy spin)
  ├── Thread: system_monitor (polling loop)
  ├── Thread: anomaly_engine (event consumer)
  └── Thread: logging pipeline (event consumer + file I/O)
      │
      └── asyncio event_bus (bridge between threads via queue)
```

The event bus uses `queue.Queue` (thread-safe) internally. Each producer
appends events; each consumer drains from its own subscription queue.
This avoids asyncio complexity while remaining safe and performant.
