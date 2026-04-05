# BlackBoxRS

**Flight recorder for ROS 2 robots**

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![ROS 2 Humble | Iron | Jazzy](https://img.shields.io/badge/ROS%202-Humble%20%7C%20Iron%20%7C%20Jazzy-brightgreen)
![License MIT](https://img.shields.io/badge/license-MIT-green)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

---

## Overview

BlackBoxRS is a robotics observability and failure-forensics platform. It passively monitors ROS 2 systems, capturing topic graph topology, QoS profiles, message frequencies, and system telemetry (CPU, GPU, memory, thermals). When anomalies occur — latency spikes, dropped messages, thermal throttling — BlackBoxRS detects and logs them into structured, replayable event streams.

Think of it as a flight data recorder for your robot. Every metric, every anomaly, every state change gets written to a structured JSONL log that you can replay, filter, and analyze after the fact. No more guessing what went wrong during that 3 AM autonomous test run.

BlackBoxRS is designed to be lightweight and non-intrusive. It runs as a background daemon alongside your ROS 2 stack, requires zero changes to your existing nodes, and works equally well on a beefy workstation or a Jetson Orin embedded board. Use it in development to catch subtle issues early, or deploy it in production as your robot's permanent black box.

---

## Architecture

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
│  ┌─────────────────────────────────────────────────────────┐│
│  │                   core.event_bus                        ││
│  └──────────────────────┬──────────────────────────────────┘│
│                         ▼                                   │
│  ┌─────────────────────────────────────────────────────────┐│
│  │               logging pipeline → JSONL                  ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

---

## Features

- **Real-time ROS 2 topic introspection** — discovery, frequency tracking, QoS snapshot
- **System telemetry** — CPU, memory, GPU, thermals, disk I/O
- **Jetson-native support** — tegrastats parsing, thermal zone monitoring
- **Pluggable anomaly detection** — threshold, frequency drop, QoS mismatch, dead topic detection
- **Structured JSONL event logging** with automatic rotation
- **CLI interface** for operations and debugging
- **Zero-config defaults** with full YAML customization
- **Modular** — use any subset of components independently

---

## Quick Start

```bash
# Install
git clone https://github.com/yusufdxb/BlackBoxRS.git
cd BlackBoxRS
./setup.sh

# Initialize config
robot-blackbox init

# Start recording
robot-blackbox start --foreground

# View status
robot-blackbox status

# Dump recent events
robot-blackbox dump-log --last 100

# Filter by source
robot-blackbox dump-log --source system --severity warning
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `robot-blackbox start` | Start the BlackBoxRS daemon |
| `robot-blackbox stop` | Stop the running daemon |
| `robot-blackbox status` | Show system status and recent metrics |
| `robot-blackbox dump-log` | Display recorded events with filters |
| `robot-blackbox replay` | Replay a log file for debugging |
| `robot-blackbox config` | Show effective configuration |
| `robot-blackbox init` | Initialize config directory (`~/.blackboxrs/`) |

---

## Configuration

BlackBoxRS uses YAML configuration stored at `~/.blackboxrs/config.yaml`. Run `robot-blackbox init` to generate the default config.

```yaml
# ~/.blackboxrs/config.yaml

general:
  log_dir: ~/.blackboxrs/logs/
  log_format: jsonl            # jsonl | sqlite (future)
  log_rotation_mb: 50          # rotate log files at this size
  log_retention_days: 30       # auto-delete logs older than this
  poll_interval_sec: 1.0       # global polling interval

ros_monitor:
  enabled: true
  topic_discovery_interval: 5.0   # seconds between full topic scans
  frequency_window_sec: 10.0      # sliding window for Hz calculation
  track_qos: true                 # snapshot QoS profiles per topic
  latency_estimation: true        # estimate pub-to-sub latency

system_monitor:
  enabled: true
  cpu: true
  memory: true
  gpu: true                       # NVIDIA via nvidia-smi or Jetson tegrastats
  thermals: true
  disk_io: true
  poll_interval_sec: 2.0

anomaly_engine:
  enabled: true
  detectors:
    - type: threshold
      metric: cpu_percent
      max: 95.0
      severity: warning

    - type: threshold
      metric: gpu_temp_c
      max: 85.0
      severity: critical

    - type: frequency_drop
      topic: "*"                  # wildcard — monitor all topics
      drop_percent: 50.0
      window_sec: 10.0
      severity: warning

    - type: dead_topic
      timeout_sec: 30.0
      severity: error

    - type: qos_mismatch
      severity: warning
```

---

## Event Schema

Every event is written as a single JSON line in the log file:

```json
{
  "timestamp": "2026-04-05T14:32:01.482Z",
  "source": "anomaly_engine",
  "event_type": "anomaly.threshold",
  "severity": "warning",
  "data": {
    "metric": "cpu_percent",
    "value": 96.3,
    "threshold_max": 95.0,
    "message": "CPU usage exceeded threshold: 96.3% > 95.0%"
  },
  "context": {
    "hostname": "jetson-orin",
    "session_id": "a1b2c3d4"
  }
}
```

| Field | Description |
|-------|-------------|
| `timestamp` | ISO 8601 UTC timestamp |
| `source` | Module that generated the event (`ros_monitor`, `system_monitor`, `anomaly_engine`) |
| `event_type` | Dot-separated event type for filtering |
| `severity` | `debug`, `info`, `warning`, `error`, `critical` |
| `data` | Event-specific payload |
| `context` | Session metadata (hostname, session ID, etc.) |

---

## Project Structure

```
BlackBoxRS/
├── blackboxrs/
│   ├── __init__.py
│   ├── core/               # Event bus, config, session management
│   ├── ros_monitor/        # ROS 2 topic discovery, frequency, QoS
│   ├── system_monitor/     # CPU, GPU, memory, thermals, disk I/O
│   ├── anomaly_engine/     # Pluggable anomaly detectors
│   ├── logging/            # JSONL writer, log rotation
│   └── cli/                # Click-based CLI (robot-blackbox)
├── tests/
├── scripts/
├── docs/
├── pyproject.toml
├── setup.sh
├── LICENSE
└── README.md
```

---

## Requirements

- **Python 3.10+**
- **ROS 2** (Humble, Iron, or Jazzy) — optional; system monitoring works without it
- **psutil** — system telemetry
- **pydantic** — data validation and event schema
- **click** — CLI framework
- **PyYAML** — configuration parsing

---

## Roadmap

- [ ] Rosbag2 triggered recording on anomaly
- [ ] Web dashboard (Grafana integration)
- [ ] Prometheus metrics exporter
- [ ] Multi-robot fleet support
- [ ] TF tree monitoring
- [ ] Custom detector plugin system

---

## Contributing

Contributions are welcome. To get started:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes and add tests
4. Run the test suite (`pytest`)
5. Commit with a clear message
6. Open a pull request against `main`

Please open an issue first for large changes so we can discuss the approach.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
