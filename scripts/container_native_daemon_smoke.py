#!/usr/bin/env python3
"""Prove that BlackBoxDaemon owns a clean native capture lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any


SCHEMA_VERSION = "blackboxrs.container_native_daemon_smoke.v1"
ZERO_STATUS_COUNTERS = (
    "dropped",
    "dropped_bytes",
    "storage_errors",
    "clock_anomalies",
    "status_publish_failures",
    "graph_wait_faults",
    "graph_coverage_faults",
    "graph_snapshot_failures",
    "node_snapshot_failures",
    "endpoint_query_failures",
    "subscription_failures",
    "runtime_callback_faults",
    "rate_status_failures",
    "trigger_intent_lost",
    "rmw_messages_lost",
    "rmw_event_callbacks_unavailable",
    "incompatible_qos_events",
    "ambiguous_topic_types",
    "incident_manifest_errors",
    "retention_evicted_segments",
    "retention_evicted_events",
    "retention_evicted_bytes",
)
ZERO_QUALITY_COUNTERS = (
    "dropped",
    "bytes_dropped",
    "storage_errors",
    "clock_anomalies",
    "graph_wait_faults",
    "graph_coverage_faults",
    "graph_snapshot_failures",
    "node_snapshot_failures",
    "endpoint_query_failures",
    "subscription_failures",
    "runtime_callback_faults",
    "rmw_messages_lost",
    "rmw_event_callbacks_unavailable",
    "incompatible_qos_events",
    "ambiguous_topic_types",
    "retention_evicted_segments",
    "retention_evicted_events",
    "retention_evicted_bytes",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"expected JSON object: {path}")
    return value


def _nonzero(record: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: record.get(name) for name in names if record.get(name) != 0}


def _wait_for_ready(native_capture: Any, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if "READY session=" in native_capture.output_tail:
            return
        process = native_capture._process
        if process is None or process.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError(
        "daemon-owned native recorder did not publish READY\n" + native_capture.output_tail
    )


def run_smoke(output_root: Path, report_path: Path) -> dict[str, Any]:
    output_root = output_root.resolve()
    report_path = report_path.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    _require(
        report_path != output_root and report_path.is_relative_to(output_root),
        "report must be a file inside the persisted output root",
    )

    # Import after resolving the output root so the smoke can redirect the
    # daemon PID contract into the persisted container volume.
    from blackboxrs.cli.daemon import BlackBoxDaemon
    from blackboxrs.core.config import BlackBoxConfig
    from blackboxrs.recording.native import resolve_current_native_session
    from blackboxrs.recording.native_process import NativeCaptureProcess

    config = BlackBoxConfig.default()
    config.log_dir = str(output_root / "logs")
    config.capture.backend = "cpp"
    config.capture.topics = ["/blackboxrs/container_native_smoke"]
    config.capture.native_output_dir = str(output_root / "native")
    config.capture.native_startup_timeout_sec = 10.0
    config.capture.native_shutdown_timeout_sec = 15.0
    config.ros_monitor.enabled = False
    config.system_monitor.enabled = False
    config.anomaly_engine.enabled = False
    config.rosbag2.enabled = False
    config.prometheus.enabled = False

    BlackBoxDaemon._PID_DIR = output_root
    BlackBoxDaemon._PID_FILE = output_root / "blackboxrs.pid"
    daemon = BlackBoxDaemon(config)
    native_capture: NativeCaptureProcess | None = None
    session_directory: Path | None = None

    try:
        daemon.start()
        native_components = [
            component
            for component in daemon._components
            if isinstance(component, NativeCaptureProcess)
        ]
        _require(len(native_components) == 1, "daemon did not own exactly one native recorder")
        native_capture = native_components[0]
        process = native_capture._process
        watchdog = native_capture._watch_thread
        _require(process is not None and process.poll() is None, "native child is not alive")
        _require(watchdog is not None and watchdog.is_alive(), "native watchdog is not alive")
        _require(native_capture.unexpected_exit_code is None, "native child exited unexpectedly")
        _wait_for_ready(native_capture, 5.0)
        session_directory = resolve_current_native_session(config.capture.native_output_dir)
        _require(session_directory is not None, "daemon did not publish a native session pointer")
        _require(session_directory.is_dir(), "native session directory is missing")
    finally:
        daemon.stop()

    _require(native_capture is not None, "native recorder was never registered")
    _require(session_directory is not None, "native session was not resolved")
    final_status = native_capture.latest_status
    _require(final_status is not None, "native final status was not supervised")
    _require(final_status.get("state") == "STOPPED_CLEAN", "native shutdown was not clean")
    _require(final_status.get("backend") == "cpp", "native final status has wrong backend")
    status_nonzero = _nonzero(final_status, ZERO_STATUS_COUNTERS)
    _require(not status_nonzero, f"native final status has faults: {status_nonzero}")
    _require(final_status.get("drop_breakdown") == [], "native final status has drop details")
    _require(final_status.get("queue_depth") == 0, "native queue did not drain")
    _require(final_status.get("accepting") is False, "native recorder is still accepting")
    _require(final_status.get("writer_alive") is False, "native writer is still alive")
    _require(final_status.get("writer_faulted") is False, "native writer faulted")
    _require(
        final_status.get("received")
        == final_status.get("admitted")
        == final_status.get("committed")
        == final_status.get("durable"),
        "native final status counters do not reconcile",
    )
    _require(native_capture.unexpected_exit_code is None, "watchdog observed an unexpected exit")
    _require(native_capture._process is None, "native process handle survived daemon shutdown")
    _require(native_capture._watch_thread is None, "native watchdog survived daemon shutdown")
    _require(not BlackBoxDaemon._PID_FILE.exists(), "daemon PID file survived clean shutdown")

    quality_path = session_directory / "capture_quality.json"
    quality = _read_json(quality_path)
    _require(quality.get("schema_version") == "blackboxrs.capture_quality.v1", "bad quality")
    _require(quality.get("backend") == "cpp", "capture backend was not native")
    _require(quality.get("clean") is True, "persisted capture quality is not clean")
    nonzero = _nonzero(quality, ZERO_QUALITY_COUNTERS)
    _require(not nonzero, f"persisted capture has loss or fault counters: {nonzero}")
    _require(quality.get("drop_breakdown") == [], "persisted capture has drop details")
    _require(
        quality.get("received")
        == quality.get("admitted")
        == quality.get("committed")
        == quality.get("durable"),
        "persisted capture counters do not reconcile",
    )

    segments = sorted((session_directory / "segments").glob("*.mcap"))
    partials = sorted((session_directory / "segments").glob("*.partial.mcap"))
    _require(segments, "daemon-owned native recorder produced no finalized segment")
    _require(not partials, f"partial native segments remain: {partials}")
    for segment in segments:
        sidecar = _read_json(segment.with_suffix(".json"))
        _require(sidecar.get("clean") is True, f"unclean segment sidecar: {segment}")
        _require(sidecar.get("file_bytes") == segment.stat().st_size, "segment size mismatch")
        digest = hashlib.sha256(segment.read_bytes()).hexdigest()
        _require(sidecar.get("sha256") == digest, f"segment checksum mismatch: {segment}")

    result = {
        "schema_version": SCHEMA_VERSION,
        "valid": True,
        "daemon_session_id": daemon.session.session_id,
        "native_session_id": quality.get("session_id"),
        "native_component_count": 1,
        "watchdog_observed_running_child": True,
        "startup_state": "READY",
        "final_state": final_status.get("state"),
        "persisted_clean": quality.get("clean"),
        "received": quality.get("received"),
        "committed": quality.get("committed"),
        "durable": quality.get("durable"),
        "dropped": quality.get("dropped"),
        "storage_errors": quality.get("storage_errors"),
        "segment_count": len(segments),
        "session_path": str(session_directory.relative_to(output_root)),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_smoke(args.output_root, args.report)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
