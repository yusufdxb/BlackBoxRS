"""Source-inventory logic of scripts/native_capture_bag_gate.py (no ROS needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

import pytest

yaml = pytest.importorskip("yaml")
pytest.importorskip("mcap")

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "native_capture_bag_gate.py"
_spec = importlib.util.spec_from_file_location("native_capture_bag_gate", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _write_split(path: Path, rows: list[tuple[str, str, int, bytes]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, type TEXT, "
        "serialization_format TEXT, offered_qos_profiles TEXT)"
    )
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, topic_id INTEGER, "
        "timestamp INTEGER, data BLOB)"
    )
    ids: dict[str, int] = {}
    for name, msg_type, _stamp, _data in rows:
        if name not in ids:
            ids[name] = len(ids) + 1
            connection.execute(
                "INSERT INTO topics VALUES (?, ?, ?, 'cdr', '')", (ids[name], name, msg_type)
            )
    for name, _type, stamp, data in rows:
        connection.execute(
            "INSERT INTO messages (topic_id, timestamp, data) VALUES (?, ?, ?)",
            (ids[name], stamp, data),
        )
    connection.commit()
    connection.close()


def _make_bag(tmp_path: Path, declared_extra: int = 0) -> Path:
    bag = tmp_path / "bag"
    bag.mkdir()
    _write_split(
        bag / "bag_0.db3",
        [
            ("/imu", "sensor_msgs/msg/Imu", 10, b"a"),
            ("/imu", "sensor_msgs/msg/Imu", 20, b"b"),
            ("/rosout", "rcl_interfaces/msg/Log", 15, b"log"),
        ],
    )
    _write_split(bag / "bag_1.db3", [("/imu", "sensor_msgs/msg/Imu", 30, b"c")])
    metadata = {
        "rosbag2_bagfile_information": {
            "relative_file_paths": ["bag_0.db3", "bag_1.db3"],
            "topics_with_message_count": [
                {"topic_metadata": {"name": "/imu"}, "message_count": 3 + declared_extra},
                {"topic_metadata": {"name": "/rosout"}, "message_count": 1},
            ],
        }
    }
    (bag / "metadata.yaml").write_text(yaml.safe_dump(metadata), encoding="utf-8")
    return bag


def test_all_sqlite_splits_are_read_and_bookkeeping_is_separated(tmp_path: Path) -> None:
    bag = _make_bag(tmp_path)
    timestamps: list[int] = []
    totals: dict[str, int] = {}
    schemas, messages = gate._source_inventory(bag, "sqlite3", timestamps, totals)
    assert totals == {"/imu": 3, "/rosout": 1}
    assert schemas["/imu"] == "sensor_msgs/msg/Imu"
    assert messages["/imu"] == [b"a", b"b", b"c"]  # split 1 included, timestamp order
    assert "/rosout" not in messages
    assert max(timestamps) - min(timestamps) == 20


def test_metadata_count_mismatch_fails_loudly(tmp_path: Path) -> None:
    bag = _make_bag(tmp_path, declared_extra=5)
    with pytest.raises(RuntimeError, match="disagree"):
        gate._source_inventory(bag, "sqlite3", [])


def test_unlisted_split_fails_loudly(tmp_path: Path) -> None:
    bag = _make_bag(tmp_path)
    _write_split(bag / "bag_2.db3", [("/imu", "sensor_msgs/msg/Imu", 40, b"d")])
    with pytest.raises(RuntimeError, match="disagree"):
        gate._source_inventory(bag, "sqlite3", [])


def test_unimportable_message_type_names_the_topic() -> None:
    pytest.importorskip("rosidl_runtime_py")
    with pytest.raises(RuntimeError, match="/utlidar/odd.*not importable"):
        gate._require_importable_types({"/utlidar/odd": "nonexistent_pkg/msg/Nope"})
