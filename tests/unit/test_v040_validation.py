"""Validation tests added during the v0.4.0 end-to-end pass.

These tests cover gaps surfaced when exercising the sample bundle
(`examples/incidents/inc_demo_tf_break`) end-to-end through the CLI:

* `incident build --log-dir` flag (previously absent, forced operators
  to edit `~/.blackboxrs/config.yaml` to point at a different log dir).
* `incident build --config` flag (load a non-default YAML config).
* Bundle build idempotency: building twice from the same logs returns
  the same incident_id and stable file content.
* `incident list --incidents-dir` on empty and on missing directories.
* `incident show` round-trip: rendering the persisted bundle matches
  the report rendered at build time.
* Cause ranker edge cases (precursor at trigger.t boundary, recurrence
  with prior_count >= big-fix-history threshold).
* Caveat policy combinations (very-low confidence + recurrence,
  no-evidence singleton trigger).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from blackboxrs.cli.incident_cmd import incident_group
from blackboxrs.core.config import BlackBoxConfig
from blackboxrs.incident import cause as cause_mod
from blackboxrs.incident.api import build_incident, render_report
from blackboxrs.incident.bundle import BundleReader
from blackboxrs.incident.models import (
    DetectorTrigger,
    RecurrenceContext,
    TimelineEvent,
)


_T0 = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ev(t, source, event_type, data, severity="info", metadata=None):
    return {
        "timestamp": t.isoformat().replace("+00:00", "Z"),
        "source": source,
        "event_type": event_type,
        "severity": severity,
        "data": data,
        "metadata": metadata or {"session_id": "sess_validate"},
    }


def _seed_dead_topic_log(log_path: Path) -> None:
    """Write a small synthetic log: healthy freq samples + dead-topic trigger."""
    events = [
        _ev(_T0, "ros_monitor", "ros.frequency",
            {"topic": "/scan", "frequency_hz": 10.0}),
        _ev(_T0 + timedelta(seconds=1), "system_monitor", "system.cpu",
            {"metric": "cpu_percent", "value": 35.0, "unit": "%"}),
        _ev(_T0 + timedelta(seconds=5), "anomaly_engine",
            "anomaly.dead_topic",
            {
                "detector": "DeadTopicDetector",
                "topic": "/scan",
                "metric": "/scan",
                "value": 0.0,
                "threshold": 5.0,
                "message": "Topic /scan silent for 5s.",
            },
            severity="error",
            metadata={
                "session_id": "sess_validate",
                "detector_class": (
                    "blackboxrs.anomaly_engine.detectors."
                    "dead_topic.DeadTopicDetector"
                ),
                "signature_fields": ["topic"],
                "target_subsystem": "ros",
            }),
    ]
    with open(log_path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev))
            fh.write("\n")


def _trigger(
    cls: str,
    *,
    subject: str = "/scan",
    severity: str = "warning",
    t_offset: float = 10.0,
    subsystem: str = "ros",
) -> DetectorTrigger:
    return DetectorTrigger(
        trigger_id=DetectorTrigger.make_id(
            cls, _T0 + timedelta(seconds=t_offset), subject
        ),
        detector=cls,
        detector_class=f"blackboxrs.anomaly_engine.detectors.x.{cls}",
        t=_T0 + timedelta(seconds=t_offset),
        subsystem=subsystem,
        subject=subject,
        severity=severity,
        message=f"{cls} fired on {subject}",
        data={"topic": subject},
        signature_fields=["topic"],
        source_event_ref="events.jsonl#L99",
    )


def _derived(
    summary: str,
    *,
    t_offset: float,
    subsystem: str = "ros",
    data: dict | None = None,
) -> TimelineEvent:
    return TimelineEvent(
        t=_T0 + timedelta(seconds=t_offset),
        kind="derived",
        subsystem=subsystem,
        summary=summary,
        confidence=0.9,
        evidence_ref=f"events.jsonl#L{int(t_offset) + 1}",
        data=data or {},
    )


# ---------------------------------------------------------------------------
# CLI: `incident build --log-dir`
# ---------------------------------------------------------------------------


class TestIncidentBuildLogDirFlag:
    """``incident build --log-dir`` must redirect the LogReader without
    touching the user's global ~/.blackboxrs/config.yaml."""

    def test_log_dir_flag_routes_builder_to_custom_dir(
        self, tmp_path: Path
    ):
        log_dir = tmp_path / "alt_logs"
        log_dir.mkdir()
        _seed_dead_topic_log(
            log_dir / "blackboxrs_20260507_120000_000000.jsonl"
        )
        incidents_dir = tmp_path / "incidents"

        runner = CliRunner()
        result = runner.invoke(
            incident_group,
            [
                "build",
                "--start", _T0.isoformat().replace("+00:00", "Z"),
                "--end", (_T0 + timedelta(seconds=10))
                          .isoformat().replace("+00:00", "Z"),
                "--log-dir", str(log_dir),
                "--incidents-dir", str(incidents_dir),
                "--tag", "validation",
            ],
        )
        assert result.exit_code == 0, result.output
        # The bundle must contain the trigger we seeded, NOT an empty
        # bundle (which is what happened before --log-dir existed and
        # the CLI defaulted to ~/.blackboxrs/logs).
        bundles = sorted(incidents_dir.iterdir())
        assert len(bundles) == 1
        reader = BundleReader(bundles[0])
        triggers = reader.load_triggers()
        assert len(triggers) == 1
        assert triggers[0].subject == "/scan"

    def test_log_dir_flag_overrides_yaml_config_log_dir(
        self, tmp_path: Path
    ):
        """When --config and --log-dir are both passed, --log-dir wins."""
        wrong_dir = tmp_path / "wrong_logs"
        wrong_dir.mkdir()  # empty, no events here
        right_dir = tmp_path / "right_logs"
        right_dir.mkdir()
        _seed_dead_topic_log(
            right_dir / "blackboxrs_20260507_120000_000000.jsonl"
        )

        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(f"log_dir: \"{wrong_dir}\"\n")

        incidents_dir = tmp_path / "incidents"
        runner = CliRunner()
        result = runner.invoke(
            incident_group,
            [
                "build",
                "--start", _T0.isoformat().replace("+00:00", "Z"),
                "--end", (_T0 + timedelta(seconds=10))
                          .isoformat().replace("+00:00", "Z"),
                "--config", str(cfg_path),
                "--log-dir", str(right_dir),
                "--incidents-dir", str(incidents_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        bundle = next(iter(incidents_dir.iterdir()))
        triggers = BundleReader(bundle).load_triggers()
        # If the override worked we see the seeded /scan trigger; if it
        # didn't, the wrong_dir is empty and triggers would be [].
        assert len(triggers) == 1
        assert triggers[0].subject == "/scan"


# ---------------------------------------------------------------------------
# CLI: `incident list` edge cases
# ---------------------------------------------------------------------------


class TestIncidentListEdges:
    def test_empty_incidents_dir_reports_no_incidents(self, tmp_path: Path):
        empty = tmp_path / "empty_incidents"
        empty.mkdir()
        runner = CliRunner()
        result = runner.invoke(
            incident_group, ["list", "--incidents-dir", str(empty)]
        )
        assert result.exit_code == 0
        assert "No incidents found" in result.output

    def test_nonexistent_incidents_dir_reports_missing(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist"
        runner = CliRunner()
        result = runner.invoke(
            incident_group, ["list", "--incidents-dir", str(missing)]
        )
        assert result.exit_code == 0
        assert "No incidents directory" in result.output


# ---------------------------------------------------------------------------
# Round-trip: built bundle + re-rendered report match
# ---------------------------------------------------------------------------


class TestBundleRoundTrip:
    def test_render_report_matches_persisted_report_md(self, tmp_path: Path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _seed_dead_topic_log(
            log_dir / "blackboxrs_20260507_120000_000000.jsonl"
        )
        cfg = BlackBoxConfig.default()
        cfg.log_dir = str(log_dir)

        bundle = build_incident(
            _T0, _T0 + timedelta(seconds=10),
            config=cfg, incidents_dir=tmp_path / "out",
        )
        on_disk = (bundle / "report.md").read_text(encoding="utf-8")
        re_rendered = render_report(bundle)
        assert on_disk == re_rendered, (
            "report.md persisted at build time must equal the bytes "
            "produced by render_report(reader) on the same bundle"
        )

    def test_build_twice_produces_same_incident_id(self, tmp_path: Path):
        """A rebuild from the same window/host/session must return the
        same incident_id. The committed example relies on this contract
        so the README's bundle path stays stable across regenerations."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _seed_dead_topic_log(
            log_dir / "blackboxrs_20260507_120000_000000.jsonl"
        )
        cfg = BlackBoxConfig.default()
        cfg.log_dir = str(log_dir)

        first = build_incident(
            _T0, _T0 + timedelta(seconds=10),
            config=cfg, incidents_dir=tmp_path / "out",
        )
        second = build_incident(
            _T0, _T0 + timedelta(seconds=10),
            config=cfg, incidents_dir=tmp_path / "out",
        )
        assert first == second
        # And the trigger_id inside is deterministic too.
        ta = BundleReader(first).load_triggers()
        tb = BundleReader(second).load_triggers()
        assert [t.trigger_id for t in ta] == [t.trigger_id for t in tb]


# ---------------------------------------------------------------------------
# Cause ranker: edge cases not previously covered
# ---------------------------------------------------------------------------


class TestCauseRankerEdgeCases:
    def test_event_at_exact_trigger_time_is_not_a_precursor(self):
        """``ev.t >= trigger.t`` filter must exclude same-instant events,
        even if they are derived. A "precursor" that fires simultaneously
        cannot be ordered, and treating it as a precursor would credit
        confidence that isn't warranted."""
        trig = _trigger("DeadTopicDetector", t_offset=10.0)
        same_t = _derived(
            "silence interval on /scan: 4.0s gap",
            t_offset=10.0, data={"topic": "/scan"},
        )
        causes = cause_mod.rank([trig], [same_t])
        assert len(causes) == 1
        assert causes[0].precursor_chain == []

    def test_post_trigger_event_does_not_count(self):
        """A derived event after the trigger is not a precursor."""
        trig = _trigger("DeadTopicDetector", t_offset=10.0)
        after = _derived(
            "silence interval on /scan: 4.0s gap",
            t_offset=11.0, data={"topic": "/scan"},
        )
        causes = cause_mod.rank([trig], [after])
        assert causes[0].precursor_chain == []

    def test_no_evidence_singleton_emits_no_precursor_caveat(self):
        """When a trigger fires with no precursors and no relevant diff,
        the caveat should explicitly say 'the trigger alone is the entire
        signal'. This is the honest-output contract from cause.py."""
        trig = _trigger("DeadTopicDetector", t_offset=10.0)
        h = cause_mod.rank([trig], [])[0]
        # Base + severity_warning (0.03) is well below the 0.7 threshold,
        # so the caveat must include the singleton trigger line.
        assert h.caveat is not None
        assert "no precursor evidence" in h.caveat.lower()

    def test_recurrence_high_count_surfaces_in_caveat(self):
        """Recurrence with prior_count >= 3 must show in the caveat with
        the prior-count number; this is the operator-visible fix-history
        signal."""
        trig = _trigger("DeadTopicDetector", t_offset=10.0)

        def lookup(fp_id: str) -> RecurrenceContext:
            return RecurrenceContext(
                fingerprint_id=fp_id,
                prior_count=4,
                prior_incident_ids=["inc_a", "inc_b", "inc_c", "inc_d"],
                last_seen_at=_T0 - timedelta(days=1),
            )

        h = cause_mod.rank(
            [trig], [],
            recurrence_lookup=lookup,
            fingerprint_id="fpr_" + "1" * 16,
        )[0]
        assert h.recurrence is not None
        assert h.recurrence.prior_count == 4
        assert h.caveat is not None
        assert "4" in h.caveat
        assert "recurrence" in h.caveat.lower()

    def test_multi_trigger_severity_breaks_ties_by_severity(self):
        """When two triggers produce hypotheses with the same confidence,
        the higher-severity trigger must rank first."""
        # Two identical detector classes, different severity. With zero
        # precursor evidence the base score is equal; severity is the
        # tiebreaker.
        warn = _trigger(
            "DeadTopicDetector",
            subject="/topic_a", severity="warning", t_offset=10.0,
        )
        err = _trigger(
            "DeadTopicDetector",
            subject="/topic_b", severity="error", t_offset=10.0,
        )
        causes = cause_mod.rank([warn, err], [])
        assert len(causes) == 2
        # Error trigger has +0.07, warning has +0.03; error must lead.
        assert "/topic_b" in causes[0].cause
        assert "/topic_a" in causes[1].cause

    def test_recurrence_lookup_returning_none_is_silent(self):
        """A recurrence_lookup that returns None (no priors) must not
        crash and must not add recurrence reasoning."""
        trig = _trigger("DeadTopicDetector", t_offset=10.0)

        def lookup(fp_id: str) -> RecurrenceContext | None:
            return None

        h = cause_mod.rank(
            [trig], [],
            recurrence_lookup=lookup,
            fingerprint_id="fpr_" + "2" * 16,
        )[0]
        assert h.recurrence is None
        # No recurrence reasoning bullet should be present.
        assert not any(
            "recurrence" in r.lower() for r in h.reasoning
        )

    def test_recurrence_lookup_raising_is_swallowed(self):
        """A recurrence_lookup that raises must not break ranking; the
        bundle is enrichment, not load-bearing."""
        trig = _trigger("DeadTopicDetector", t_offset=10.0)

        def lookup(fp_id: str) -> RecurrenceContext:
            raise RuntimeError("backend down")

        h = cause_mod.rank(
            [trig], [],
            recurrence_lookup=lookup,
            fingerprint_id="fpr_" + "3" * 16,
        )[0]
        # No recurrence captured, hypothesis still produced.
        assert h.recurrence is None
        assert h.confidence > 0.0


# ---------------------------------------------------------------------------
# Sample bundle on disk: smoke contract for the committed example
# ---------------------------------------------------------------------------


_EXAMPLE_BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "examples" / "incidents" / "inc_demo_tf_break"
)


@pytest.mark.skipif(
    not _EXAMPLE_BUNDLE.is_dir(),
    reason="committed sample bundle not present in this checkout",
)
class TestCommittedSampleBundle:
    """The repo ships a sample bundle for the README. These tests pin
    the contract so a future builder change can't silently break the
    demo without a failing test."""

    def test_sample_bundle_loads_and_has_dead_topic_trigger(self):
        reader = BundleReader(_EXAMPLE_BUNDLE)
        triggers = reader.load_triggers()
        assert len(triggers) == 1
        t = triggers[0]
        assert t.subject == "/tf_static"
        assert t.detector_class.endswith("DeadTopicDetector")
        assert t.severity == "error"

    def test_sample_bundle_top_cause_is_high_confidence(self):
        reader = BundleReader(_EXAMPLE_BUNDLE)
        incident = reader.load_incident()
        assert incident.likely_causes, "sample must produce at least 1 cause"
        top = incident.likely_causes[0]
        assert top.confidence >= 0.7, (
            f"sample top hypothesis confidence dropped to {top.confidence}; "
            "the README banks on it being above the adoption threshold."
        )
        # Precursor chain must include the pub_count drop. We assert on
        # the chain length and on the topic in the chain rather than on
        # raw confidence so calibration drift can't make this test flaky.
        chain_topics = [
            p.summary for p in top.precursor_chain
        ]
        assert any("/tf_static" in s for s in chain_topics), (
            f"expected /tf_static in precursor chain summaries; "
            f"got {chain_topics}"
        )
