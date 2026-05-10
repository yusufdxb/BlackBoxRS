"""Generate a sample incident bundle for the demo and tests.

Builds a synthetic JSONL fixture seeded with a TF-break scenario (S1 from
DEMO_PLAN.md) and runs the v0.4 incident builder against it. The output
bundle is committed under examples/incidents/ so the README and the
``incident show`` flow have something to point at without depending on a
running daemon.

Usage::

    python scripts/generate_sample_incident.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident.api import build_incident
from blackboxrs.logging.reader import LogReader  # noqa: F401  (sanity import)


REPO = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO / "examples" / "incidents"
EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)


def _ev(t: datetime, source: str, event_type: str, data: dict, severity: str = "info",
        metadata: dict | None = None) -> dict:
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {"session_id": "demo_tf_break"},
    }


def _seed_tf_break_events(start: datetime) -> list[dict]:
    """Build a synthetic event stream that resembles a TF-break incident.

    The shape, in order:
      - 5 healthy frequency samples on /tf_static (1.0 Hz).
      - 5 healthy CPU samples (well under threshold).
      - 1 dead-topic anomaly on /tf_static (the killer event).
      - 2 trailing CPU samples to give the timeline some breath.
    """
    events: list[dict] = []

    # Healthy /tf_static at 1 Hz.
    for i in range(5):
        events.append(_ev(
            start + timedelta(seconds=i),
            "ros_monitor",
            "ros.frequency",
            {"topic": "/tf_static", "frequency_hz": 1.0, "interval_ms": 1000.0},
        ))

    # Healthy CPU samples (well below threshold).
    for i in range(5):
        events.append(_ev(
            start + timedelta(seconds=i, milliseconds=500),
            "system_monitor",
            "system.cpu",
            {"cpu_percent": 32.5, "metric": "cpu_percent", "value": 32.5, "unit": "%"},
        ))

    # Sustained CPU excursion 5..9s above default threshold (90.0%).
    # Demonstrates the resource_excursion deriver and provides a second
    # signal correlated in time with the dead-topic kill at t=8s.
    for i in range(5):
        events.append(_ev(
            start + timedelta(seconds=5 + i, milliseconds=250),
            "system_monitor",
            "system.cpu",
            {"cpu_percent": 95.0, "metric": "cpu_percent", "value": 95.0, "unit": "%"},
        ))

    # Graph state snapshot precursor: /tf_static had 1 publisher, then 0.
    # Two ros.qos events so the snapshotter sees pub_count drop, which the
    # graph_delta deriver picks up and the cause ranker treats as a
    # topic-aligned, same-subsystem precursor to the dead-topic trigger.
    events.append(_ev(
        start + timedelta(seconds=2),
        "ros_monitor",
        "ros.qos",
        {
            "topic": "/tf_static",
            "msg_type": "tf2_msgs/msg/TFMessage",
            "publisher_count": 1,
            "subscriber_count": 1,
            "publisher_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "TRANSIENT_LOCAL"},
            ],
            "subscriber_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "TRANSIENT_LOCAL"},
            ],
        },
    ))
    events.append(_ev(
        start + timedelta(seconds=6),
        "ros_monitor",
        "ros.qos",
        {
            "topic": "/tf_static",
            "msg_type": "tf2_msgs/msg/TFMessage",
            "publisher_count": 0,
            "subscriber_count": 1,
            "publisher_qos_profiles": [],
            "subscriber_qos_profiles": [
                {"reliability": "RELIABLE", "durability": "TRANSIENT_LOCAL"},
            ],
        },
    ))

    # The kill: dead-topic anomaly on /tf_static.
    events.append(_ev(
        start + timedelta(seconds=8),
        "anomaly_engine",
        "anomaly.dead_topic",
        {
            "detector": "DeadTopicDetector",
            "metric": "/tf_static",
            "topic": "/tf_static",
            "value": 0.0,
            "threshold": 5.0,
            "message": "Topic /tf_static silent for 5.0 s (timeout exceeded).",
        },
        severity="error",
        metadata={
            "session_id": "demo_tf_break",
            "detector_class": "blackboxrs.anomaly_engine.detectors.dead_topic.DeadTopicDetector",
            "signature_fields": ["topic"],
            "target_subsystem": "ros",
        },
    ))

    # Continued CPU baseline so the timeline has trailing context.
    for i in range(2):
        events.append(_ev(
            start + timedelta(seconds=11 + i),
            "system_monitor",
            "system.cpu",
            {"cpu_percent": 30.0, "metric": "cpu_percent", "value": 30.0, "unit": "%"},
        ))

    return events


def main() -> Path:
    start = datetime(2026, 5, 7, 14, 22, 0, tzinfo=timezone.utc)
    events = _seed_tf_break_events(start)

    work = Path(tempfile.mkdtemp(prefix="bbrs_sample_"))
    log_dir = work / "logs"
    log_dir.mkdir()
    incidents_dir = work / "incidents"
    incidents_dir.mkdir()

    # Write the synthetic events to a single rotating-style JSONL file
    # so LogReader picks it up.
    log_file = log_dir / "blackboxrs_20260507_142200_000000.jsonl"
    with open(log_file, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev))
            fh.write("\n")

    # Build the bundle.
    cfg = BlackBoxConfig.default()
    cfg.log_dir = str(log_dir)

    bundle = build_incident(
        window_start=start,
        window_end=start + timedelta(seconds=20),
        config=cfg,
        incidents_dir=incidents_dir,
        title="TF break on /tf_static",
        notes="Synthetic demo bundle. See DEMO_PLAN.md scenario S1.",
        tags=["demo", "tf-break"],
    )

    target = EXAMPLES_DIR / "inc_demo_tf_break"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(bundle, target)
    print(f"Sample bundle: {target}")
    return target


if __name__ == "__main__":
    main()
