"""Executable replay -> incident -> prevention -> preflight loop."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from blackboxrs.cli.app import cli
from blackboxrs.incident.bundle import BundleReader
from blackboxrs.prevention.checks import topic_present
from blackboxrs.prevention.rules import load_rules


DROP_TOPIC = "/die"


class _FakeNode:
    def __init__(self, publishers: dict[str, int]) -> None:
        self._publishers = publishers

    def get_publishers_info_by_topic(self, topic: str):
        return [SimpleNamespace()] * self._publishers.get(topic, 0)


def _patch_topic_graph(fake_node: _FakeNode):
    def _stub_with_graph_node(fn, *, settle_sec=0.0, name=""):
        return fn(fake_node)

    return (
        patch.object(topic_present, "with_graph_node", _stub_with_graph_node),
        patch.object(topic_present, "RCLPY_AVAILABLE", True),
    )


def _write_db3(path: Path, rows: list[tuple[str, int]]) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT,"
        " serialization_format TEXT, offered_qos_profiles TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER,"
        " timestamp INTEGER, data BLOB);"
    )
    topic_ids: dict[str, int] = {}
    for topic, _ts in rows:
        if topic not in topic_ids:
            tid = len(topic_ids) + 1
            topic_ids[topic] = tid
            conn.execute(
                "INSERT INTO topics VALUES (?,?,?,?,?)",
                (tid, topic, "std_msgs/msg/Empty", "cdr", ""),
            )
    for topic, ts in rows:
        conn.execute(
            "INSERT INTO messages (topic_id, timestamp, data) VALUES (?,?,?)",
            (topic_ids[topic], ts, b""),
        )
    conn.commit()
    conn.close()


def _write_dropout_bag(tmp_path: Path) -> Path:
    db3 = tmp_path / "dropout.db3"
    rows: list[tuple[str, int]] = []
    step = 100_000_000
    for i in range(90):
        rows.append(("/keep", i * step))
        if i < 30 or i == 55:
            rows.append((DROP_TOPIC, i * step))
    _write_db3(db3, rows)
    return db3


def test_replay_incident_derives_rule_and_blocks_recurrence_via_cli(tmp_path: Path):
    bag = _write_dropout_bag(tmp_path)
    incidents_dir = tmp_path / "incidents"
    rules_dir = tmp_path / "rules"
    runner = CliRunner()

    replay = runner.invoke(
        cli,
        [
            "replay-bag",
            str(bag),
            "--out",
            str(incidents_dir),
            "--timeout",
            "2.0",
            "--title",
            "Closed loop sqlite replay dropout",
            "--tag",
            "closed-loop",
            "--tag",
            "sqlite-replay",
        ],
    )
    assert replay.exit_code == 0, replay.output
    bundles = sorted(path for path in incidents_dir.iterdir() if path.is_dir())
    assert len(bundles) == 1
    bundle = bundles[0]

    reader = BundleReader(bundle)
    triggers = reader.load_triggers()
    assert triggers
    trigger_ids = {trigger.trigger_id for trigger in triggers}
    assert {trigger.subject for trigger in triggers} == {DROP_TOPIC}
    assert all(trigger.source_event_ref for trigger in triggers)
    assert (bundle / "incident.json").exists()
    assert (bundle / "report.md").exists()
    assert (bundle / "manifest.json").exists()

    adopted = runner.invoke(
        cli,
        [
            "prevention",
            "adopt",
            "--from-incident",
            str(bundle),
            "--rules-dir",
            str(rules_dir),
        ],
    )
    assert adopted.exit_code == 0, adopted.output
    rules = load_rules(rules_dir)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.check.kind == "topic_present"
    assert rule.check.params["topic"] == DROP_TOPIC
    assert set(rule.source_trigger_ids).issubset(trigger_ids)
    assert rule.source_trigger_ids
    assert rule.source_incident_id == reader.load_incident().incident_id
    assert rule.source_fingerprint_id == reader.load_incident().fingerprint.fingerprint_id
    assert rule.derivation["source_trigger_id"] in trigger_ids
    source_trigger = next(
        trigger for trigger in triggers if trigger.trigger_id == rule.derivation["source_trigger_id"]
    )
    assert rule.derivation["source_event_ref"] == source_trigger.source_event_ref
    assert rule.derivation["hypothesis_confidence"] >= 0.7

    missing_graph, missing_rclpy = _patch_topic_graph(_FakeNode({DROP_TOPIC: 0}))
    with missing_graph, missing_rclpy:
        blocked = runner.invoke(cli, ["preflight", "--rules-dir", str(rules_dir)])
    assert blocked.exit_code == 1, blocked.output
    assert "[  BLOCK]" in blocked.output

    healthy_graph, healthy_rclpy = _patch_topic_graph(_FakeNode({DROP_TOPIC: 1}))
    with healthy_graph, healthy_rclpy:
        healthy = runner.invoke(cli, ["preflight", "--rules-dir", str(rules_dir)])
    assert healthy.exit_code == 0, healthy.output
    assert "[   PASS]" in healthy.output
