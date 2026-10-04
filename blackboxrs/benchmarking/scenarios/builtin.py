"""Built-in local reliability benchmark scenarios."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from blackboxrs.benchmarking.schema import (
    ExpectedTrigger,
    PreventionExpectation,
    ReplayExpectation,
    ScenarioInput,
    ScenarioSpec,
)
from blackboxrs.core.clock import Clock
from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.core.schemas import BlackBoxEvent


_BASE = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> datetime:
    return _BASE + timedelta(seconds=seconds)


def _event_at(t: datetime, factory: Callable[[], BlackBoxEvent]) -> BlackBoxEvent:
    Clock.set_virtual_time(t)
    try:
        return factory()
    finally:
        Clock.use_wall_clock()


def _ros_frequency(t: datetime, topic: str, hz: float = 10.0) -> BlackBoxEvent:
    return _event_at(
        t,
        lambda: BlackBoxEvent.ros_event(
            "ros.frequency",
            {"topic": topic, "frequency_hz": hz, "interval_ms": 1000.0 / hz},
        ),
    )


def _qos_event(
    t: datetime,
    *,
    topic: str,
    pub_reliability: str,
    sub_reliability: str,
    pub_durability: str = "VOLATILE",
    sub_durability: str = "VOLATILE",
) -> BlackBoxEvent:
    return _event_at(
        t,
        lambda: BlackBoxEvent.ros_event(
            "ros.qos",
            {
                "topic": topic,
                "msg_type": "std_msgs/msg/String",
                "publisher_count": 1,
                "subscriber_count": 1,
                "publisher_qos_profiles": [
                    {
                        "reliability": f"ReliabilityPolicy.{pub_reliability}",
                        "durability": f"DurabilityPolicy.{pub_durability}",
                    }
                ],
                "subscriber_qos_profiles": [
                    {
                        "reliability": f"ReliabilityPolicy.{sub_reliability}",
                        "durability": f"DurabilityPolicy.{sub_durability}",
                    }
                ],
            },
        ),
    )


def _tf_event(
    t: datetime,
    *,
    last_update_sec_ago: float,
    expected_frames: list[str] | None = None,
) -> BlackBoxEvent:
    return _event_at(
        t,
        lambda: BlackBoxEvent.ros_event(
            "ros.tf",
            {
                "expected_frames": expected_frames or ["odom", "base_link"],
                "edges": [
                    {
                        "parent": "odom",
                        "child": "base_link",
                        "last_update_sec_ago": last_update_sec_ago,
                        "is_static": False,
                    }
                ],
            },
        ),
    )


@dataclass(frozen=True)
class EventScenario:
    """Simple event-stream scenario."""

    spec: ScenarioSpec
    events_factory: Callable[[int, int], list[BlackBoxEvent]]
    fault_time_factory: Callable[[int, int], datetime | None]

    def configure(self, config: BlackBoxConfig) -> BlackBoxConfig:
        config.anomaly_engine.dead_topic.timeout_sec = 1.0
        config.anomaly_engine.tf_topology.stale_timeout_sec = 2.0
        return config

    def materialize(
        self,
        work_dir: Path,
        *,
        repetition: int,
        seed: int,
    ) -> ScenarioInput:
        events = self.events_factory(repetition, seed)
        return ScenarioInput(
            session_id=f"bench_{self.spec.scenario_id}_{repetition}",
            events=events,
            window_start=min(event.timestamp for event in events) - timedelta(seconds=0.1),
            window_end=max(event.timestamp for event in events) + timedelta(seconds=0.5),
            fault_activation_time=self.fault_time_factory(repetition, seed),
            clock_mode=self.spec.clock_mode,
        )

@dataclass(frozen=True)
class ArtifactScenario:
    """Scenario that exercises bundle validation or unsupported preflight states."""

    spec: ScenarioSpec

    def configure(self, config: BlackBoxConfig) -> BlackBoxConfig:
        return config

    def materialize(
        self,
        work_dir: Path,
        *,
        repetition: int,
        seed: int,
    ) -> ScenarioInput:
        healthy_event = _ros_frequency(_at(0), f"/artifact_health_{repetition}", 10.0)
        return ScenarioInput(
            session_id=f"bench_{self.spec.scenario_id}_{repetition}",
            events=[healthy_event],
            window_start=healthy_event.timestamp - timedelta(seconds=0.1),
            window_end=healthy_event.timestamp + timedelta(seconds=0.5),
            fault_activation_time=healthy_event.timestamp,
            clock_mode=self.spec.clock_mode,
        )

@dataclass(frozen=True)
class UnsupportedScenario:
    """Documented scenario that current BlackBoxRS cannot support."""

    spec: ScenarioSpec

    def configure(self, config: BlackBoxConfig) -> BlackBoxConfig:
        return config

    def materialize(
        self,
        work_dir: Path,
        *,
        repetition: int,
        seed: int,
    ) -> ScenarioInput:
        raise RuntimeError(self.spec.unsupported_reason or "unsupported")

def _healthy_topic_events(repetition: int, seed: int) -> list[BlackBoxEvent]:
    topic = f"/bench_alive_{seed}_{repetition}"
    return [_ros_frequency(_at(i * 0.2), topic, 10.0) for i in range(8)]


def _dead_topic_events(repetition: int, seed: int) -> list[BlackBoxEvent]:
    dead = f"/bench_dead_{seed}_{repetition}"
    keep = f"/bench_keepalive_{seed}_{repetition}"
    return [
        _ros_frequency(_at(0.0), dead, 10.0),
        _ros_frequency(_at(0.1), keep, 10.0),
        _ros_frequency(_at(0.4), keep, 10.0),
        _ros_frequency(_at(1.3), keep, 10.0),
        _ros_frequency(_at(1.5), dead, 10.0),
    ]


def _healthy_qos_events(repetition: int, seed: int) -> list[BlackBoxEvent]:
    return [
        _qos_event(
            _at(0.0),
            topic=f"/bench_qos_ok_{seed}_{repetition}",
            pub_reliability="RELIABLE",
            sub_reliability="BEST_EFFORT",
        )
    ]


def _qos_mismatch_events(repetition: int, seed: int) -> list[BlackBoxEvent]:
    return [
        _qos_event(
            _at(0.0),
            topic=f"/bench_qos_bad_{seed}_{repetition}",
            pub_reliability="BEST_EFFORT",
            sub_reliability="RELIABLE",
        )
    ]


def _healthy_tf_events(repetition: int, seed: int) -> list[BlackBoxEvent]:
    return [_tf_event(_at(0.0), last_update_sec_ago=0.05)]


def _stale_tf_events(repetition: int, seed: int) -> list[BlackBoxEvent]:
    return [_tf_event(_at(0.0), last_update_sec_ago=3.5)]


def make_corrupted_copy(source: Path, target: Path) -> Path:
    """Copy a finalized bundle and corrupt a manifest-tracked file."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    timeline = target / "timeline.json"
    timeline.write_text("[]\n", encoding="utf-8")
    return target


SCENARIOS = [
    EventScenario(
        spec=ScenarioSpec(
            scenario_id="healthy_topic_publisher",
            description="Healthy ros.frequency stream for a topic publisher.",
            fault_class="healthy_control",
            setup="Emit production-shaped ros.frequency events for one topic.",
            fault_injection="none",
            replay_expectation=ReplayExpectation(supported=True),
            prevention_expectation=PreventionExpectation(healthy_should_pass=True),
            healthy_control=True,
        ),
        events_factory=_healthy_topic_events,
        fault_time_factory=lambda repetition, seed: None,
    ),
    EventScenario(
        spec=ScenarioSpec(
            scenario_id="dead_topic_dropout",
            description="Previously active topic stops publishing while other events advance detector time.",
            fault_class="dead_topic",
            detector_expected="dead_topic",
            setup="Emit one liveness event for the target topic and later keepalive events on another topic.",
            fault_injection="Stop target topic after its first liveness event.",
            expected_anomaly_kind="anomaly.dead_topic",
            expected_trigger_fields={"topic_prefix": "/bench_dead_"},
            replay_expectation=ReplayExpectation(
                supported=True,
                expected_detector="dead_topic",
            ),
            prevention_expectation=PreventionExpectation(
                derivable=True,
                expected_check_kind="topic_present",
                recurrence_should_block=True,
                healthy_should_pass=True,
            ),
            expected_triggers=[
                ExpectedTrigger(detector="dead_topic", event_type="anomaly.dead_topic")
            ],
        ),
        events_factory=_dead_topic_events,
        fault_time_factory=lambda repetition, seed: _at(0.0),
    ),
    EventScenario(
        spec=ScenarioSpec(
            scenario_id="healthy_qos_compatible_graph",
            description="QoS-compatible publisher/subscriber graph.",
            fault_class="healthy_control",
            setup="Emit production-shaped ros.qos with compatible reliability and durability.",
            fault_injection="none",
            healthy_control=True,
        ),
        events_factory=_healthy_qos_events,
        fault_time_factory=lambda repetition, seed: None,
    ),
    EventScenario(
        spec=ScenarioSpec(
            scenario_id="qos_mismatch_reliability",
            description="Publisher offers BEST_EFFORT while subscriber requires RELIABLE.",
            fault_class="qos_mismatch",
            detector_expected="qos_mismatch",
            setup="Emit production-shaped ros.qos for one incompatible pub/sub pair.",
            fault_injection="Set publisher reliability below subscriber reliability.",
            expected_anomaly_kind="anomaly.qos_mismatch",
            expected_trigger_fields={"reliability_mismatch": True},
            prevention_expectation=PreventionExpectation(
                derivable=True,
                expected_check_kind="qos_match",
                recurrence_should_block=True,
                healthy_should_pass=True,
            ),
            expected_triggers=[
                ExpectedTrigger(
                    detector="qos_mismatch",
                    event_type="anomaly.qos_mismatch",
                    fields={"reliability_mismatch": True},
                )
            ],
        ),
        events_factory=_qos_mismatch_events,
        fault_time_factory=lambda repetition, seed: _at(0.0),
    ),
    EventScenario(
        spec=ScenarioSpec(
            scenario_id="healthy_tf_stream",
            description="Healthy TF snapshot with a fresh odom to base_link edge.",
            fault_class="healthy_control",
            setup="Emit production-shaped ros.tf snapshot below stale timeout.",
            fault_injection="none",
            healthy_control=True,
        ),
        events_factory=_healthy_tf_events,
        fault_time_factory=lambda repetition, seed: None,
    ),
    EventScenario(
        spec=ScenarioSpec(
            scenario_id="tf_stale_transform",
            description="TF dynamic edge has not updated within the stale timeout.",
            fault_class="tf_stale_edge",
            detector_expected="tf_topology",
            setup="Emit production-shaped ros.tf snapshot with one dynamic edge.",
            fault_injection="Set last_update_sec_ago above stale_timeout_sec.",
            expected_anomaly_kind="anomaly.tf_topology",
            expected_trigger_fields={"failure_kind": "stale_edge", "frame": "base_link"},
            prevention_expectation=PreventionExpectation(
                derivable=False,
                healthy_should_pass=True,
            ),
            expected_triggers=[
                ExpectedTrigger(
                    detector="tf_topology",
                    event_type="anomaly.tf_topology",
                    subject="tf:stale_edge:base_link",
                    fields={"failure_kind": "stale_edge"},
                )
            ],
        ),
        events_factory=_stale_tf_events,
        fault_time_factory=lambda repetition, seed: _at(0.0),
    ),
    ArtifactScenario(
        spec=ScenarioSpec(
            scenario_id="corrupted_bundle_rejection",
            description="Finalized incident copy is corrupted and must be rejected by integrity validation.",
            fault_class="artifact_integrity",
            setup="Build a real finalized bundle from healthy evidence.",
            fault_injection="Modify a manifest-tracked file after finalization.",
            replay_expectation=ReplayExpectation(supported=False),
            prevention_expectation=PreventionExpectation(derivable=False),
        )
    ),
    ArtifactScenario(
        spec=ScenarioSpec(
            scenario_id="unsupported_prevention_condition",
            description="Unknown preflight check kind fails closed instead of passing silently.",
            fault_class="preflight_unsupported",
            setup="Construct a prevention rule with an unsupported runtime check kind.",
            fault_injection="Bypass model validation to simulate future or malformed rule drift.",
            replay_expectation=ReplayExpectation(supported=False),
            prevention_expectation=PreventionExpectation(derivable=False),
        )
    ),
    UnsupportedScenario(
        spec=ScenarioSpec(
            scenario_id="duplicate_or_forbidden_publisher",
            description="Duplicate or forbidden publisher on a topic.",
            fault_class="graph_policy",
            setup="Would require publisher identity policy or baseline.",
            fault_injection="not implemented",
            status="unsupported",
            unsupported_reason=(
                "No current detector checks duplicate publishers, unexpected "
                "publishers, or publisher allowlists."
            ),
        )
    ),
]
