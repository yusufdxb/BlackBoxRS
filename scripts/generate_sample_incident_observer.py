"""Generate the observer-mode sample bundle.

Same shape as ``generate_sample_incident.py`` but runs the builder
with ``runtime.role: observer`` so the resulting bundle exercises the
``observer_host`` / ``observed_host`` plumbing and the report
renderer's two-line ``Observer`` / ``Observed`` header.

The hostname recorded as ``observer_host`` is the machine that runs
this script (the laptop, in real usage). The ``observed_host`` is a
synthetic robot label ``go2-edu-01`` chosen to match the README's
running example.

Usage::

    python scripts/generate_sample_incident_observer.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.core.config import BlackBoxConfig, RuntimeConfig
from blackboxrs.incident.api import build_incident


REPO = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO / "examples" / "incidents"
EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

_OBSERVED_HOST = "go2-edu-01"


def _ev(t: datetime, source: str, event_type: str, data: dict,
        severity: str = "info", metadata: dict | None = None) -> dict:
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {
            "session_id": "demo_observer_dead_topic",
            "role": "observer",
            "observed_host": _OBSERVED_HOST,
        },
    }


def _seed_observer_dead_topic_events(start: datetime) -> list[dict]:
    """Synthetic event stream for a remote /scan dead-topic incident.

    Mirrors what an observer laptop would actually capture from a robot
    over DDS: only DDS-bound events (ros.frequency, ros.qos, the
    eventual anomaly.dead_topic). No system_monitor events, because
    observer mode disables that subsystem.
    """
    events: list[dict] = []

    for i in range(6):
        events.append(_ev(
            start + timedelta(seconds=i),
            "ros_monitor",
            "ros.frequency",
            {"topic": "/scan", "frequency_hz": 10.0, "interval_ms": 100.0},
        ))

    events.append(_ev(
        start + timedelta(seconds=1),
        "ros_monitor",
        "ros.qos",
        {
            "topic": "/scan",
            "msg_type": "sensor_msgs/msg/LaserScan",
            "publisher_count": 1,
            "subscriber_count": 1,
            "publisher_qos_profiles": [
                {"reliability": "BEST_EFFORT", "durability": "VOLATILE"},
            ],
            "subscriber_qos_profiles": [
                {"reliability": "BEST_EFFORT", "durability": "VOLATILE"},
            ],
        },
    ))
    events.append(_ev(
        start + timedelta(seconds=6),
        "ros_monitor",
        "ros.qos",
        {
            "topic": "/scan",
            "msg_type": "sensor_msgs/msg/LaserScan",
            "publisher_count": 0,
            "subscriber_count": 1,
            "publisher_qos_profiles": [],
            "subscriber_qos_profiles": [
                {"reliability": "BEST_EFFORT", "durability": "VOLATILE"},
            ],
        },
    ))

    events.append(_ev(
        start + timedelta(seconds=11),
        "anomaly_engine",
        "anomaly.dead_topic",
        {
            "detector": "DeadTopicDetector",
            "metric": "/scan",
            "topic": "/scan",
            "value": 0.0,
            "threshold": 5.0,
            "message": "Topic /scan silent for 5.0 s (timeout exceeded).",
        },
        severity="error",
        metadata={
            "session_id": "demo_observer_dead_topic",
            "role": "observer",
            "observed_host": _OBSERVED_HOST,
            "detector_class": (
                "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector"
            ),
            "signature_fields": ["topic"],
            "target_subsystem": "ros",
        },
    ))

    return events


def main() -> Path:
    start = datetime(2026, 5, 17, 14, 22, 0, tzinfo=timezone.utc)
    events = _seed_observer_dead_topic_events(start)

    work = Path(tempfile.mkdtemp(prefix="bbrs_observer_sample_"))
    log_dir = work / "logs"
    log_dir.mkdir()
    incidents_dir = work / "incidents"
    incidents_dir.mkdir()

    log_file = log_dir / "blackboxrs_20260517_142200_000000.jsonl"
    with open(log_file, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev))
            fh.write("\n")

    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)
    cfg.runtime = RuntimeConfig(role="observer", observed_host=_OBSERVED_HOST)
    cfg.apply_runtime_role()

    bundle = build_incident(
        window_start=start,
        window_end=start + timedelta(seconds=15),
        config=cfg,
        incidents_dir=incidents_dir,
        title="/scan dead from observer laptop",
        notes=(
            "Synthetic demo bundle captured in observer mode. The "
            "observer is the machine that ran scripts/"
            "generate_sample_incident_observer.py; the observed host "
            "is the synthetic robot label 'go2-edu-01'."
        ),
        tags=["demo", "observer", "dead-topic"],
    )

    target = EXAMPLES_DIR / "inc_demo_observer_dead_topic"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundle, target)
    print(f"Observer sample bundle: {target}")
    return target


if __name__ == "__main__":
    main()
