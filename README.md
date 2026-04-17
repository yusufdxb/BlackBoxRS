# BlackBoxRS

**Flight recorder for ROS 2 robots** — early-stage / single-host

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![ROS 2 Humble | Iron | Jazzy](https://img.shields.io/badge/ROS%202-Humble%20%7C%20Iron%20%7C%20Jazzy-brightgreen)
![License MIT](https://img.shields.io/badge/license-MIT-green)

> Status: **alpha (v0.1.0)**. Single-host, in-process. Useful as a
> development-time observability daemon. Not yet validated on hardware
> beyond a desktop Linux workstation. See _Status & Limitations_ below
> for what is verified, what is inferred, and what is **not** built yet.

---

## Overview

BlackBoxRS is a development-time observability daemon for ROS 2 robots.
It runs as a single Python process alongside your stack and writes a
structured JSONL stream of:

- ROS 2 graph state (topics, nodes, per-publisher QoS, per-subscriber QoS)
- Per-topic message frequencies (sliding-window estimates)
- Host telemetry (CPU, memory, disk usage + I/O rate, thermal zones)
- GPU telemetry on hosts where `nvidia-smi` is available, or on Jetson
  via the `/sys/devices/gpu.0/load` sysfs node and the GPU thermal zone
- Anomaly events fired by four built-in detectors (threshold, frequency
  drop, dead topic, QoS mismatch)

Logs are appended as newline-delimited JSON to size-rotated files in
`~/.blackboxrs/logs/`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     BlackBoxRS Daemon                        │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ ros_monitor   │  │system_monitor│  │  anomaly_engine  │  │
│  │ (rclpy node)  │  │  (psutil +   │  │  threshold       │  │
│  │               │  │   sysfs)     │  │  frequency       │  │
│  │ topology +    │  │ cpu/mem/disk │  │  dead-topic      │  │
│  │ frequency +   │  │ thermal/gpu  │  │  qos-mismatch    │  │
│  │ qos snapshot  │  │              │  │                  │  │
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
└─────────────────────────────────────────────────────────────┘
```

Threading model: each component owns exactly one background thread
(spawned inside its own `start()`); the daemon does not stack additional
worker threads on top.

---

## Implemented features

- **ROS 2 topic introspection** — discovery, dynamic generic
  subscription, sliding-window frequency tracker, per-publisher and
  per-subscriber QoS snapshot. Requires `rclpy`. With `rclpy` absent the
  ROS monitor logs a warning and stays inactive.
- **Host telemetry** — CPU usage and load average, memory + swap, disk
  usage + I/O rate, Linux `thermal_zone*` zones.
- **GPU telemetry** — `nvidia-smi` on desktops, sysfs on Jetson. Skipped
  silently on hosts with neither.
- **Built-in anomaly detectors** — threshold (CPU%, memory%, GPU °C),
  per-topic frequency drop with auto-learned baseline, dead-topic
  silence, pub/sub QoS mismatch.
- **Structured JSONL logging** with size-based rotation and a bounded
  retained-file count.
- **CLI (`robot-blackbox` or `python -m blackboxrs`)** — `start`,
  `stop`, `status`, `dump-log` (with severity / source filters and a
  `--follow` mode), `replay`, `config`, `init`.
- **Pure Python defaults**, optional `rclpy` for ROS 2 integration.

## Not yet implemented

These are listed so docs do not overstate what the code does:

- No SQLite log backend — JSONL only.
- No time-based log retention. Rotation is **size + file-count** based;
  oldest files are pruned when more than `log_max_files` exist.
- No custom-detector plugin system. The four built-in detectors are
  hard-wired in `anomaly_engine.engine.AnomalyEngine._init_detectors`.
- No anomaly-triggered rosbag2 recording.
- No web dashboard, no Prometheus exporter, no fleet aggregation.
- No TF tree monitoring, no service / action introspection.
- `replay` prints the events from a log file in order. It is **not** a
  time-aligned playback or a publisher to a topic.
- ROS 2 latency is **not** measured end-to-end. The ROS monitor reports
  `interval_ms = 1 / frequency_hz` (mean inter-message interval), not
  pub→sub wall-clock latency.

---

## Quick Start

```bash
git clone https://github.com/yusufdxb/BlackBoxRS.git
cd BlackBoxRS
./setup.sh

# Initialise ~/.blackboxrs with a default config and logs/ dir
robot-blackbox init

# Run in the foreground (Ctrl-C to stop)
robot-blackbox start --foreground

# Or detach into the background
robot-blackbox start
robot-blackbox status
robot-blackbox stop

# Inspect recent events
robot-blackbox dump-log --last 100
robot-blackbox dump-log --source anomaly --severity warning
robot-blackbox dump-log --follow             # tail -f the active log
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `robot-blackbox start [--foreground] [-c CFG]` | Start the daemon (background by default). Refuses if the pidfile identity already matches a live daemon. |
| `robot-blackbox stop`     | Send SIGTERM to a *verified* running daemon. Refuses if the pidfile identity cannot be verified (see _PID file safety_ below). |
| `robot-blackbox status`   | Show running state, event counts by source/severity, latest event. |
| `robot-blackbox dump-log` | Print recorded events. Filters: `--source {ros,system,anomaly,all}`, `--severity`, `--last N`, `--json`, `--follow`. |
| `robot-blackbox replay`   | Print a log file in chronological order (no time-aligned playback). |
| `robot-blackbox config`   | Print the effective configuration as YAML. |
| `robot-blackbox init`     | Create `~/.blackboxrs/{config.yaml,logs/}` if missing. |

`python -m blackboxrs <subcommand>` is equivalent in case the entry
point is not on `PATH`.

### PID file safety

The pidfile at `~/.blackboxrs/blackboxrs.pid` is a small JSON document
carrying process identity, not just a bare PID:

```json
{"pid": 12345, "starttime": 9876543,
 "cmdline": "python -m blackboxrs start --foreground"}
```

`starttime` is field 22 of `/proc/<pid>/stat` (boot-relative jiffies,
stable for a PID's lifetime). Every `status` / `stop` call re-reads
`/proc` and compares:

- PID is alive (via `kill(pid, 0)`);
- `starttime` matches the recorded value — a recycled PID belonging to
  an unrelated process will always have a later `starttime`;
- the live `cmdline` either contains `blackboxrs` or exactly matches
  the recorded one.

If any check fails the pidfile is treated as **stale**, removed, and
the operation reports "not running". Legacy plain-integer pidfiles
written by releases before the JSON identity format are also rejected
as stale — BlackBoxRS will not signal a PID it cannot prove it owns.
The writer uses `mkstemp + os.replace` so a concurrent reader never
observes a partially written pidfile. Identity verification is
Linux-specific (uses `/proc`); on non-Linux hosts only the cmdline
round-trip check runs.

---

## Configuration

Config lives at `~/.blackboxrs/config.yaml` (or pass `-c PATH` to
`start`). The schema is the dataclass tree in
`blackboxrs.core.config.BlackBoxConfig`; any unknown key is ignored,
any missing key falls back to the dataclass default.

The exact defaults written by `robot-blackbox init` are:

```yaml
# ~/.blackboxrs/config.yaml

log_dir: "~/.blackboxrs/logs"
log_rotation_mb: 50            # rotate when current file exceeds this size
log_max_files: 20              # keep at most N rotated files (oldest pruned)
event_bus_queue_maxsize: 1024  # bounded per-subscriber queue capacity;
                                # full queues drop events + increment a
                                # per-queue drop counter rather than
                                # back-pressuring producers.

ros_monitor:
  enabled: true
  poll_interval_sec: 1.0       # how often to re-snapshot the ROS graph
  track_latency: true          # add interval_ms to ros.frequency events
  topic_filters: []            # fnmatch patterns; [] means "all topics"

system_monitor:
  enabled: true
  interval_sec: 1.0
  gpu_backend: "auto"          # auto | nvidia-smi | tegrastats | none

anomaly_engine:
  enabled: true
  thresholds:
    cpu_percent: 90.0
    memory_percent: 85.0
    gpu_temp_c: 80.0
  frequency:
    tolerance_percent: 20.0    # alert when measured Hz < (1-tol/100)*baseline
  dead_topic:
    timeout_sec: 5.0
```

There is no `general:` block, no `log_format` field, no
`log_retention_days`, no `detectors:` list, and no per-detector custom
registration — all anomaly settings live under the three nested blocks
(`thresholds`, `frequency`, `dead_topic`) shown above. If you write
something else in your YAML, it will be silently ignored rather than
applied.

---

## Event Schema

Every line in the log is a single JSON object validated by
`blackboxrs.core.schemas.BlackBoxEvent`:

```json
{
  "timestamp": "2026-04-16T14:59:53.691602Z",
  "source":    "system_monitor",
  "event_type": "system.cpu",
  "severity":  "info",
  "data":      { "cpu_percent": 6.2, "cpu_count": 24, "per_cpu_percent": [...] },
  "metadata":  { "session_id": "20c8b5f68030", "hostname": "mewtwo",
                 "start_time": "2026-04-16T14:59:53.585415+00:00" }
}
```

`source` is one of `ros_monitor`, `system_monitor`, `anomaly_engine`.
`severity` is one of `debug | info | warning | error | critical`.

### Emitted event types

| `source` | `event_type` | Notes |
|----------|--------------|-------|
| `system_monitor` | `system.cpu`     | `cpu_percent`, `cpu_count`, `per_cpu_percent`, `load_avg_*` |
| `system_monitor` | `system.memory`  | `memory_percent`, `memory_used_mb`, `memory_total_mb`, swap |
| `system_monitor` | `system.disk`    | `disk_percent`, `disk_used_gb`, `disk_total_gb`, `disk_read_mb_s`, `disk_write_mb_s` |
| `system_monitor` | `system.gpu`     | `gpu_util_percent`, `gpu_temp_c`, mem + power (when backend supports them) |
| `system_monitor` | `system.thermal` | `items: [{zone, type, temp_c}, ...]` |
| `ros_monitor`    | `ros.topology`   | `topic_count`, `node_count`, `topics`, `nodes` |
| `ros_monitor`    | `ros.frequency`  | `topic`, `frequency_hz`, optional `interval_ms` |
| `ros_monitor`    | `ros.qos`        | `publisher_qos_profiles`, `subscriber_qos_profiles`, counts |
| `anomaly_engine` | `anomaly.threshold`     | `detector`, `metric`, `value`, `threshold`, `message` |
| `anomaly_engine` | `anomaly.frequency`     | as above; `metric` is `frequency:<topic>` |
| `anomaly_engine` | `anomaly.dead_topic`    | as above; `metric` is `dead_topic:<topic>` |
| `anomaly_engine` | `anomaly.qos_mismatch`  | as above; `metric` is `qos:<topic>` |

These strings are the actual contract. The detector test suite (`tests/unit/test_detectors.py`) and the integration tests
(`tests/integration/test_daemon_pipeline.py`) both build on these
exact payloads.

---

## Project Structure

```
BlackBoxRS/
├── blackboxrs/
│   ├── __init__.py
│   ├── __main__.py            # `python -m blackboxrs` entry point
│   ├── core/                  # event bus, config, session, clock, schemas
│   ├── ros_monitor/           # rclpy node, introspection, frequency tracker
│   ├── system_monitor/        # psutil/sysfs/nvidia-smi/tegrastats collectors
│   ├── anomaly_engine/        # threshold/frequency/dead-topic/qos detectors
│   ├── logging/               # rotating JSONL writer, log reader
│   └── cli/                   # click app + daemon lifecycle
├── tests/
│   ├── unit/                  # detectors, event bus, config, schemas, tracker, writer
│   ├── synthetic/             # in-process bus → engine → writer scenarios
│   └── integration/           # full BlackBoxDaemon end-to-end tests
├── pyproject.toml
├── setup.sh
└── README.md
```

---

## Status & Limitations

**What is verified locally** (`pytest -q` on this repo)

- **Unit tests** — detector contracts, event bus bounded queue + drop
  accounting, config round-trip, log writer rotation (including the
  sub-second collision regression), log reader, CLI log-follow
  rotation-awareness, PID-file identity verification, schema /
  event-name contracts.
- **Integration tests** booting the full `BlackBoxDaemon` — one thread
  per component (no double-start), threshold detector firing against
  real `system.cpu` events, events reaching the JSONL log on disk, and
  PID-file lifecycle.
- **CLI subprocess tests** — real `python -m blackboxrs` child
  processes exercise both lifecycles: `start --foreground` + SIGTERM,
  and the default `start` (background) / `status` / `stop` operator
  path. Stale and foreign pidfiles are refused and cleaned up, and
  `start` recovers on the next attempt without manual intervention.
- **Live ROS 2 tests** (`tests/integration/test_ros_live.py`) — boot a
  real `rclpy` publisher on an isolated, per-session-random
  `ROS_DOMAIN_ID` and confirm `RosMonitor` discovers the topic,
  subscribes, and emits `ros.topology` + `ros.frequency` events with a
  positive `frequency_hz`; `topic_filters` is verified to actually
  exclude filtered topics. These tests auto-skip when `rclpy` is not
  importable, so they are honest on hosts without ROS 2 rather than
  fabricated.

**Verified in CI** — `ruff check` and `pytest -q` on Python 3.10 /
3.11 / 3.12 via `.github/workflows/ci.yml`. The rclpy-gated live-ROS
tests are skipped on hosted runners (no ROS 2 install); everything
else runs.

**Still inferred**

- Jetson-specific paths (`tegrastats`, sysfs GPU load). The desktop
  `nvidia-smi` branch is exercised locally; the Jetson branch is
  implemented but not run here.
- Multi-host ROS 2 scenarios (`ROS_LOCALHOST_ONLY=0`, DDS on a
  physical network).
- ROS distros other than Humble (live-ROS tests are Humble on this
  host).

**Not yet built** — see _Not yet implemented_ above.

---

## Requirements

- Python 3.10+
- `psutil`, `pydantic>=2`, `click`, `pyyaml`
- `rclpy` (Humble / Iron / Jazzy) — optional; system monitoring runs
  without it.

---

## Roadmap (aspirational, not implemented)

- Anomaly-triggered rosbag2 recording
- Pluggable custom detectors loaded from config
- Web dashboard / Prometheus exporter
- Multi-robot fleet aggregation
- TF tree, service, and action monitoring
- True time-aligned log replay (with optional ROS republish)

---

## License

MIT. See [LICENSE](LICENSE).
