"""Shared helpers for Replay Lab tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from blackboxrs.lab.events import ReplayEvent
from blackboxrs.lab.evidence import TopicInfo, load_evidence

ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "examples" / "replay_lab"
CASES = sorted((LAB / "cases").glob("*.json"))
NS = 1_000_000_000


def twist(vx: Any = 0.0, vy: Any = 0.0, wz: Any = 0.0) -> dict[str, Any]:
    return {"linear": {"x": vx, "y": vy, "z": 0.0}, "angular": {"x": 0.0, "y": 0.0, "z": wz}}


def msg(t_s: float, topic: str, data: dict[str, Any] | None, *, seq: int, role: str = "",
        src_s: float | None = None, rx_s: float | None = None, **kw: Any) -> ReplayEvent:
    """A message event at replay time t_s; wall clocks default to t_s (in ns)."""
    t = int(round(t_s * NS))
    rx = int(round((rx_s if rx_s is not None else t_s) * NS))
    src = None if src_s is None else int(round(src_s * NS))
    return ReplayEvent(t_ns=t, order=(0, seq, 0), eid=f"r{seq}", kind="msg", topic=topic,
                       role=role, type="x/msg/Y", data=data, src_ns=src, rx_wall_ns=rx,
                       record={"kind": "msg", "seq": seq, "t_mono_ns": t, "t_wall_ns": rx,
                               "topic": topic, "role": role, "type": "x/msg/Y", "data": data,
                               "dds_src_ns": src, "dds_rx_ns": rx}, **kw)


def hold(t_s: float, held: bool, seq: int, *, eseq: int, epoch: int = 1) -> ReplayEvent:
    return msg(t_s, "/helix/hold", {"hold": held, "fault_id": "f" if held else "", "epoch": epoch,
                                    "seq": eseq}, seq=seq, role="helix_hold")


def topics(**hosts: str) -> dict[str, TopicInfo]:
    return {t: TopicInfo(t, "other", h, "event", None) for t, h in hosts.items()}


@pytest.fixture(scope="session")
def nominal():
    return load_evidence(LAB / "evidence" / "nominal_motion")


@pytest.fixture(scope="session")
def clean_stop():
    return load_evidence(LAB / "evidence" / "clean_stop")
