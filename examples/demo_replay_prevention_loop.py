#!/usr/bin/env python3
"""Two-minute offline replay -> prevention demo.

This script intentionally stays thin: it invokes the public BlackBoxRS CLI
commands, then reads the produced bundle/rule only to print proof-bearing
fields. The ROS graph is stubbed at the preflight check boundary so the demo
is deterministic and hardware-free.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from click.testing import CliRunner

    from blackboxrs.cli.app import cli
    from blackboxrs.incident.bundle import BundleReader
    from blackboxrs.prevention.checks import topic_present
    from blackboxrs.prevention.rules import load_rules
except ImportError as exc:  # pragma: no cover - exercised manually.
    print(f"ERROR: missing demo dependency: {exc}", file=sys.stderr)
    print("Install the project dependencies first, for example: python3 -m pip install -e .")
    sys.exit(2)


DROP_TOPIC = "/die"
KEEP_TOPIC = "/keep"
TIMEOUT_SEC = "2.0"


class DemoError(RuntimeError):
    """Raised when a required proof point is missing."""


class _FakeNode:
    def __init__(self, publishers: dict[str, int]) -> None:
        self._publishers = publishers

    def get_publishers_info_by_topic(self, topic: str):
        return [SimpleNamespace()] * self._publishers.get(topic, 0)


def _write_db3(path: Path, rows: list[tuple[str, int]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT,"
            " serialization_format TEXT, offered_qos_profiles TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER,"
            " timestamp INTEGER, data BLOB);"
        )
        topic_ids: dict[str, int] = {}
        for topic, _ts in rows:
            if topic not in topic_ids:
                topic_id = len(topic_ids) + 1
                topic_ids[topic] = topic_id
                conn.execute(
                    "INSERT INTO topics VALUES (?,?,?,?,?)",
                    (topic_id, topic, "std_msgs/msg/Empty", "cdr", ""),
                )
        for topic, ts in rows:
            conn.execute(
                "INSERT INTO messages (topic_id, timestamp, data) VALUES (?,?,?)",
                (topic_ids[topic], ts, b""),
            )
        conn.commit()
    finally:
        conn.close()


def _write_dropout_bag(work_dir: Path) -> Path:
    """Create the deterministic sqlite rosbag2 fixture used by the integration test."""
    bag_path = work_dir / "deterministic_dropout.db3"
    rows: list[tuple[str, int]] = []
    step_ns = 100_000_000
    for i in range(90):
        rows.append((KEEP_TOPIC, i * step_ns))
        if i < 30 or i == 55:
            rows.append((DROP_TOPIC, i * step_ns))
    _write_db3(bag_path, rows)
    return bag_path


def _invoke(runner: CliRunner, args: list[str], *, expect: int = 0) -> str:
    result = runner.invoke(cli, args)
    if result.exit_code != expect:
        raise DemoError(
            "CLI command failed:\n"
            f"  robot-blackbox {' '.join(args)}\n"
            f"  expected exit {expect}, got {result.exit_code}\n"
            f"{result.output}"
        )
    return result.output


def _invoke_preflight(runner: CliRunner, rules_dir: Path, publishers: dict[str, int]):
    def _stub_with_graph_node(fn, *, settle_sec=0.0, name=""):
        return fn(_FakeNode(publishers))

    with (
        patch.object(topic_present, "with_graph_node", _stub_with_graph_node),
        patch.object(topic_present, "RCLPY_AVAILABLE", True),
    ):
        return runner.invoke(cli, ["preflight", "--rules-dir", str(rules_dir)])


def _require(value, message: str):
    if value is None or value == "" or value == []:
        raise DemoError(message)
    return value


def _select_source_trigger(reader: BundleReader):
    incident = reader.load_incident()
    triggers = reader.load_triggers()
    _require(incident.likely_causes, "incident has no likely-cause hypothesis")
    top = incident.likely_causes[0]
    trigger_by_id = {trigger.trigger_id: trigger for trigger in triggers}
    for ref in top.evidence_refs:
        if ref.startswith("triggers.json#"):
            trigger_id = ref.split("#", 1)[1]
            if trigger_id in trigger_by_id:
                return incident, top, trigger_by_id[trigger_id]
    raise DemoError("top cause did not retain a trigger-level evidence reference")


def _select_silence_precursor(reader: BundleReader) -> str:
    for event in reader.load_timeline():
        if (
            event.kind == "derived"
            and "silence interval" in event.summary
            and event.data.get("topic") == DROP_TOPIC
        ):
            return event.summary
    raise DemoError("incident timeline did not retain the structured silence precursor")


def _print_field(name: str, value) -> None:
    print(f"  {name}: {value}")


def run_demo() -> int:
    runner = CliRunner()
    with tempfile.TemporaryDirectory(prefix="blackboxrs_replay_demo_") as tmp:
        work_dir = Path(tmp)
        bag = _write_dropout_bag(work_dir)
        incidents_dir = work_dir / "incidents"
        rules_dir = work_dir / "rules"

        print("[1/5] Replaying recorded robot failure")
        print(
            "  command: robot-blackbox replay-bag "
            f"{bag.name} --timeout {TIMEOUT_SEC} --out <temp>/incidents"
        )
        _invoke(
            runner,
            [
                "replay-bag",
                str(bag),
                "--out",
                str(incidents_dir),
                "--timeout",
                TIMEOUT_SEC,
                "--title",
                "Two-minute replay-to-prevention demo",
                "--tag",
                "demo",
                "--tag",
                "offline-replay",
            ],
        )
        bundles = sorted(path for path in incidents_dir.iterdir() if path.is_dir())
        if len(bundles) != 1:
            raise DemoError(f"expected one incident bundle, found {len(bundles)}")
        bundle = bundles[0]
        reader = BundleReader(bundle)
        incident, top_cause, source_trigger = _select_source_trigger(reader)
        silence_precursor = _select_silence_precursor(reader)

        _print_field("incident_id", incident.incident_id)
        _print_field("top_cause", top_cause.cause)
        _print_field("source_trigger_id", source_trigger.trigger_id)
        _print_field("detector_class", source_trigger.detector_class)
        _print_field("source_event_ref", source_trigger.source_event_ref)
        _print_field("confidence", f"{top_cause.confidence:.2f}")
        _print_field("topic", source_trigger.subject)
        _print_field("silence_precursor", silence_precursor)

        print("\n[2/5] Adopting and persisting prevention rule")
        print(
            "  command: robot-blackbox prevention adopt "
            "--from-incident <incident> --rules-dir <temp>/rules"
        )
        _invoke(
            runner,
            [
                "prevention",
                "adopt",
                "--from-incident",
                str(bundle),
                "--rules-dir",
                str(rules_dir),
            ],
        )
        rules = load_rules(rules_dir)
        if len(rules) != 1:
            raise DemoError(f"expected one persisted rule, found {len(rules)}")
        rule = rules[0]
        derivation = rule.derivation
        required = {
            "rule_id": rule.rule_id,
            "rule_type": rule.check.kind,
            "source_incident_id": rule.source_incident_id,
            "source_fingerprint": rule.source_fingerprint_id,
            "source_trigger_id": derivation.get("source_trigger_id"),
            "detector_class": derivation.get("source_detector_class"),
            "event_ref": derivation.get("source_event_ref"),
            "matching_params": rule.check.params,
            "confidence": derivation.get("hypothesis_confidence"),
        }
        for field, value in required.items():
            _require(value, f"derived rule missing required provenance field: {field}")
            _print_field(field, value)

        print("\n[3/5] Proving the matching bad launch condition is blocked")
        print(f"  bad_graph: {DROP_TOPIC} publishers=0")
        blocked = _invoke_preflight(runner, rules_dir, {DROP_TOPIC: 0})
        if blocked.exit_code == 0:
            raise DemoError("bad preflight configuration exited zero")
        if blocked.exit_code != 1 or "[  BLOCK]" not in blocked.output:
            raise DemoError(
                "bad preflight configuration did not produce the expected block:\n"
                f"{blocked.output}"
            )
        print(blocked.output.strip())

        print("\n[4/5] Proving a nearby valid launch condition passes")
        print(f"  valid_graph: {DROP_TOPIC} publishers=1")
        healthy = _invoke_preflight(runner, rules_dir, {DROP_TOPIC: 1})
        if healthy.exit_code != 0:
            raise DemoError(
                "nearby valid preflight configuration did not pass:\n"
                f"{healthy.output}"
            )
        if "[   PASS]" not in healthy.output:
            raise DemoError("valid preflight output did not include a PASS result")
        print(healthy.output.strip())

        print("\n[5/5] Evidence summary")
        print("PASS: recorded incident produced structured evidence")
        print("PASS: prevention rule retained source provenance")
        print("PASS: matching launch condition was blocked")
        print("PASS: nearby valid configuration passed")
        print("PASS: workflow executed through public BlackBoxRS CLI commands")
        print(
            "NOTE: unrelated-trigger adoption rejection is covered by "
            "tests/unit/test_prevention_derivation.py"
        )
        print("BOUNDARY: offline deterministic replay; no live robot validation")
    return 0


def main() -> int:
    try:
        return run_demo()
    except DemoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
