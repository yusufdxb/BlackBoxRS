"""Precursor-aware likely-cause ranker tests.

These tests are deliberately low-level: they construct typed
:class:`DetectorTrigger`, :class:`TimelineEvent`, and (where needed)
:class:`IncidentDiff` instances and call :func:`cause.rank` directly.
We are testing the scoring model, not the builder pipeline.

Conventions used in this file:

* ``_T0`` is a fixed UTC instant; offsets are seconds from there.
* Triggers are constructed via :func:`_trigger`; precursor derived
  events via :func:`_derived`.
* When a test cares about ordering or relative confidence, it asserts
  on the *delta*, not on absolute numbers; this keeps the tests robust
  to small calibration changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blackboxrs.incident import cause as cause_mod
from blackboxrs.incident.diff import (
    FieldChange,
    IncidentDiff,
    SignatureDiff,
)
from blackboxrs.incident.models import (
    DetectorTrigger,
    LikelyCauseHypothesis,
    RecurrenceContext,
    TimelineEvent,
)


_T0 = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _trigger(
    cls: str,
    *,
    subject: str = "/scan",
    severity: str = "warning",
    t_offset: float = 10.0,
    subsystem: str = "ros",
) -> DetectorTrigger:
    detector_name = cls
    detector_class = f"blackboxrs.anomaly_engine.detectors.x.{cls}"
    return DetectorTrigger(
        trigger_id=DetectorTrigger.make_id(
            detector_name, _T0 + timedelta(seconds=t_offset), subject
        ),
        detector=detector_name,
        detector_class=detector_class,
        t=_T0 + timedelta(seconds=t_offset),
        subsystem=subsystem,  # type: ignore[arg-type]
        subject=subject,
        severity=severity,  # type: ignore[arg-type]
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
        subsystem=subsystem,  # type: ignore[arg-type]
        summary=summary,
        confidence=0.9,
        evidence_ref=f"events.jsonl#L{int(t_offset) + 1}",
        data=data or {},
    )


def _diff(
    *,
    relevant_keys: list[str] = (),
    irrelevant_keys: list[str] = (),
    identical: bool = False,
    no_baseline: bool = False,
) -> IncidentDiff:
    """Build an IncidentDiff with the requested key shapes."""
    if no_baseline:
        return IncidentDiff(
            config=SignatureDiff(current_hash="c" * 64),
            versions=SignatureDiff(current_hash="d" * 64),
        )
    if identical:
        return IncidentDiff(
            config=SignatureDiff(
                baseline_hash="a" * 64, current_hash="a" * 64, identical=True,
            ),
            versions=SignatureDiff(
                baseline_hash="b" * 64, current_hash="b" * 64, identical=True,
            ),
        )
    cfg_changes = [FieldChange(key=k, before="x", after="y") for k in relevant_keys]
    cfg_changes += [FieldChange(key=k, before="x", after="y") for k in irrelevant_keys]
    return IncidentDiff(
        config=SignatureDiff(
            baseline_hash="a" * 64, current_hash="c" * 64,
            identical=False, changed=cfg_changes,
        ),
        versions=SignatureDiff(
            baseline_hash="b" * 64, current_hash="d" * 64, identical=True,
        ),
    )


def _only(causes: list[LikelyCauseHypothesis]) -> LikelyCauseHypothesis:
    assert len(causes) == 1
    return causes[0]


# ---------------------------------------------------------------------------
# Empty / smoke
# ---------------------------------------------------------------------------


def test_rank_empty_triggers_returns_empty_list():
    assert cause_mod.rank([], []) == []


def test_rank_single_trigger_no_evidence_yields_low_confidence():
    """No precursors, no diff, no recurrence: confidence stays modest."""
    h = _only(cause_mod.rank([_trigger("DeadTopicDetector")], []))
    assert h.confidence < 0.75
    assert h.precursor_chain == []
    assert h.score is not None
    # Reasoning must mention absence of evidence honestly.
    assert any("no precursor evidence" in r.lower() for r in h.reasoning)


# ---------------------------------------------------------------------------
# Temporal proximity
# ---------------------------------------------------------------------------


def test_close_precursor_outranks_distant_one():
    """A precursor 2s before the trigger should score above one 25s before.

    Both precursors are inside the 30s window; the near one's higher
    proximity factor must produce strictly higher confidence."""
    near = _derived(
        "silence interval on /scan: 4.0s gap",
        t_offset=8.0, data={"topic": "/scan"},
    )
    far = _derived(
        "silence interval on /scan: 4.0s gap",
        t_offset=-15.0, data={"topic": "/scan"},  # 25s before trigger@10
    )
    near_h = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [near]))
    far_h = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [far]))
    assert near_h.confidence > far_h.confidence
    # Both are within the 30s window so both register; the near one's
    # relevance must be strictly higher.
    assert near_h.precursor_chain[0].relevance > far_h.precursor_chain[0].relevance


def test_precursor_outside_window_is_ignored():
    far = _derived(
        "resource excursion: cpu",
        t_offset=-25.0, subsystem="system",  # 35s before trigger@10
    )
    h = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [far]))
    assert h.precursor_chain == []


def test_post_trigger_event_does_not_count_as_precursor():
    """Events after the trigger must not be treated as precursors."""
    after = _derived("resource excursion: cpu",
                     t_offset=12.0, subsystem="system")  # 2s AFTER trigger@10
    h = _only(cause_mod.rank([_trigger("DeadTopicDetector", t_offset=10.0)], [after]))
    assert h.precursor_chain == []


# ---------------------------------------------------------------------------
# Subsystem alignment
# ---------------------------------------------------------------------------


def test_same_subsystem_precursor_outranks_cross_subsystem():
    """Same-subsystem precursors get the full alignment factor; others get
    less."""
    same = _derived(
        "silence interval on /scan: 4.0s gap",
        t_offset=8.0, subsystem="ros", data={"topic": "/scan"},
    )
    cross = _derived(
        "topic /scan disappeared from the graph",
        t_offset=8.0, subsystem="system", data={"topic": "/scan"},
    )
    h_same = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [same]))
    h_cross = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [cross]))
    # Both are within the window and topic-aligned. The same-subsystem
    # one should rank strictly higher.
    assert h_same.confidence > h_cross.confidence


# ---------------------------------------------------------------------------
# Event-type relevance
# ---------------------------------------------------------------------------


def test_resource_excursion_is_relevant_to_dead_topic():
    excursion = _derived(
        "cpu_percent excursion: peak 95.0%, duration 4.0s above 90.0%",
        t_offset=5.0, subsystem="system",
    )
    h = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [excursion]))
    assert h.precursor_chain
    assert h.confidence > 0.65
    assert any("resource excursion" not in r and "excursion" in r.lower()
               for r in h.reasoning)


def test_resource_excursion_is_not_relevant_to_qos_mismatch():
    """QoS mismatch is a configuration-level failure; resource pressure
    does not explain it."""
    excursion = _derived(
        "cpu_percent excursion: peak 95.0%",
        t_offset=5.0, subsystem="system",
    )
    h = _only(cause_mod.rank([_trigger("QoSMismatchDetector")], [excursion]))
    assert h.precursor_chain == []  # no relevance defined for this pair


def test_graph_delta_is_relevant_to_dead_topic_with_topic_match():
    delta = _derived(
        "topic /scan disappeared from the graph",
        t_offset=8.0, subsystem="ros", data={"topic": "/scan"},
    )
    h_match = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [delta]))

    delta_other = _derived(
        "topic /odom disappeared from the graph",
        t_offset=8.0, subsystem="ros", data={"topic": "/odom"},
    )
    h_other = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [delta_other]))

    # Topic-matching graph_delta should rank above a graph_delta on a
    # different topic.
    assert h_match.confidence > h_other.confidence


def test_silence_interval_is_relevant_to_frequency_drop():
    silence = _derived(
        "silence interval on /scan: 4.0s gap",
        t_offset=5.0, subsystem="ros", data={"topic": "/scan"},
    )
    h = _only(cause_mod.rank([_trigger("FrequencyDetector")], [silence]))
    assert h.precursor_chain
    assert h.confidence > 0.55  # base + precursor bonus


# ---------------------------------------------------------------------------
# Diff relevance
# ---------------------------------------------------------------------------


def test_relevant_config_diff_boosts_dead_topic_confidence():
    h_no = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
    ))
    h_diff = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        incident_diff=_diff(relevant_keys=["ros_distro"]),
    ))
    assert h_diff.confidence > h_no.confidence
    assert any("config diff" in r.lower() for r in h_diff.reasoning)


def test_irrelevant_config_diff_yields_only_coincidence_bonus():
    h_no = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
    ))
    h_diff = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        incident_diff=_diff(irrelevant_keys=["unrelated_field"]),
    ))
    # A bonus is awarded but it must be smaller than the relevant-key one.
    h_relevant = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        incident_diff=_diff(relevant_keys=["ros_distro"]),
    ))
    assert h_diff.confidence > h_no.confidence
    assert h_relevant.confidence > h_diff.confidence


def test_identical_signatures_explicitly_noted():
    h = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        incident_diff=_diff(identical=True),
    ))
    # No diff bonus, but reasoning must explain that.
    assert any("identical" in r.lower() for r in h.reasoning)


def test_no_baseline_does_not_award_bonus():
    h = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        incident_diff=_diff(no_baseline=True),
    ))
    assert any("no prior bundle" in r.lower() for r in h.reasoning)


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


def test_recurrence_lookup_when_no_priors_does_not_boost():
    h = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        recurrence_lookup=lambda fp: None,
        fingerprint_id="fpr_" + "0" * 16,
    ))
    assert h.recurrence is None


def test_recurrence_lookup_with_priors_boosts_and_attaches_caveat():
    ctx = RecurrenceContext(
        fingerprint_id="fpr_" + "1" * 16,
        prior_count=2,
        prior_incident_ids=["inc_a", "inc_b"],
        last_seen_at=_T0,
    )
    h = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        recurrence_lookup=lambda fp: ctx,
        fingerprint_id="fpr_" + "1" * 16,
    ))
    assert h.recurrence is not None
    assert h.recurrence.prior_count == 2
    assert h.caveat is not None
    assert "recurrence" in h.caveat.lower()


def test_recurrence_disabled_when_no_lookup():
    h = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector")], [],
        recurrence_lookup=None,
        fingerprint_id="fpr_" + "1" * 16,
    ))
    assert h.recurrence is None


# ---------------------------------------------------------------------------
# Stability under noise
# ---------------------------------------------------------------------------


def test_irrelevant_noise_does_not_perturb_ranking():
    """A pile of unrelated derived events between two real ones should
    not change the relative ordering of hypotheses."""
    near = _derived(
        "silence interval on /scan: 4.0s gap",
        t_offset=8.0, data={"topic": "/scan"},
    )
    noise = [
        _derived(f"node /helper_{i} appeared on the graph",
                 t_offset=-50.0 - i, data={"node": f"/helper_{i}"})
        for i in range(20)
    ]
    a = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [near]))
    b = _only(cause_mod.rank([_trigger("DeadTopicDetector")], [near] + noise))
    assert abs(a.confidence - b.confidence) < 1e-9


def test_capped_precursor_bonus():
    """Even with five strong same-topic, in-window precursors the
    contribution must not exceed the configured cap."""
    chain = [
        _derived(f"silence interval on /scan: {n}.0s gap",
                 t_offset=5.0 + n * 0.1, data={"topic": "/scan"})
        for n in range(5)
    ]
    h = _only(cause_mod.rank([_trigger("DeadTopicDetector")], chain))
    base = cause_mod._BASE_SCORE["DeadTopicDetector"]
    # confidence cannot exceed base + severity + precursor cap + diff +
    # recurrence; with no diff/recurrence here the ceiling is tight.
    severity = cause_mod._SEVERITY_BONUS["warning"]
    ceiling = base + severity + cause_mod._PRECURSOR_BONUS_CAP + 0.001
    assert h.confidence <= ceiling


# ---------------------------------------------------------------------------
# Confidence policy / caveats
# ---------------------------------------------------------------------------


def test_low_confidence_triggers_evidence_caveat():
    """A generic detector with no precursors must surface an evidence-quality
    caveat."""
    h = _only(cause_mod.rank([_trigger("ThresholdDetector")], []))
    # ThresholdDetector base is 0.40; with no other signals confidence
    # is well below 0.50 and the caveat must explain that.
    assert h.confidence < 0.55
    assert h.caveat is not None
    assert "evidence" in h.caveat.lower() or "manual review" in h.caveat.lower()


def test_high_confidence_no_caveat_when_evidence_supports_it():
    near = _derived(
        "topic /scan disappeared from the graph",
        t_offset=8.0, subsystem="ros", data={"topic": "/scan"},
    )
    excursion = _derived(
        "cpu_percent excursion: peak 95.0%",
        t_offset=6.0, subsystem="system",
    )
    h = _only(cause_mod.rank(
        [_trigger("DeadTopicDetector", severity="error")],
        [near, excursion],
    ))
    assert h.confidence >= 0.85
    # With strong evidence and high confidence the caveat should be None,
    # OR a no-evidence caveat must NOT appear.
    if h.caveat is not None:
        assert "no precursor evidence" not in h.caveat.lower()


# ---------------------------------------------------------------------------
# Multi-trigger ordering
# ---------------------------------------------------------------------------


def test_rank_orders_by_descending_confidence():
    triggers = [
        _trigger("ThresholdDetector", t_offset=10.0),
        _trigger("DeadTopicDetector", t_offset=10.0),
    ]
    causes = cause_mod.rank(triggers, [])
    assert len(causes) == 2
    assert causes[0].confidence >= causes[1].confidence
    # And the load-bearing detector wins the top slot.
    assert "stopped emitting" in causes[0].cause


def test_severity_breaks_confidence_ties():
    """When two triggers of the same class produce identical scores,
    higher-severity goes first."""
    a = _trigger("DeadTopicDetector", subject="/a",
                 severity="warning", t_offset=10.0)
    b = _trigger("DeadTopicDetector", subject="/b",
                 severity="critical", t_offset=10.0)
    causes = cause_mod.rank([a, b], [])
    # b carries the critical-severity bonus so it must come first.
    assert "/b" in causes[0].cause


def test_unrelated_graph_churn_cannot_saturate_precursor_bonus():
    """Coincidental churn on other topics must not be load-bearing evidence.

    Regression for a finding from real quadruped hardware:
    a killed node's dead_topic ranked #567 of 567, behind frequency dips
    on healthy topics that had each absorbed the full precursor cap from
    unrelated graph deltas. Many unrelated precursors must stay worth
    less than a single matching one.
    """
    churn = [
        _derived(
            f"publisher count changed on /unrelated_{i}: 1 -> 2",
            t_offset=8.0 + i * 0.1,
            subsystem="ros",
            data={"topic": f"/unrelated_{i}"},
        )
        for i in range(30)
    ]
    h_churn = _only(cause_mod.rank([_trigger("FrequencyDetector")], churn))

    matching = [
        _derived(
            "silence interval on /scan: 4.0s gap",
            t_offset=8.0, subsystem="ros", data={"topic": "/scan"},
        )
    ]
    h_match = _only(cause_mod.rank([_trigger("FrequencyDetector")], matching))

    # 30 unrelated precursors must not out-evidence one real one.
    assert h_churn.confidence < h_match.confidence

    # And they must not reach the global precursor cap on their own.
    baseline = _only(cause_mod.rank([_trigger("FrequencyDetector")], []))
    assert h_churn.confidence - baseline.confidence <= (
        cause_mod._UNRELATED_PRECURSOR_CAP + 1e-9
    )


def test_dead_topic_outranks_frequency_noise_on_healthy_topics():
    """The topic that actually died must outrank dips on other topics.

    This is the shape of a real robot capture: one node is killed while
    unrelated telemetry keeps publishing at a wobbling rate.
    """
    churn = [
        _derived(
            f"publisher count changed on /telemetry_{i}: 2 -> 3",
            t_offset=7.0 + i * 0.1,
            subsystem="ros",
            data={"topic": f"/telemetry_{i}"},
        )
        for i in range(20)
    ]
    dead = _trigger("DeadTopicDetector", subject="/scan")
    # Subject deliberately absent from the churn list: both triggers
    # ride ONLY unrelated churn, so the comparison is fair.
    noise = _trigger("FrequencyDetector", subject="/other_healthy")

    ranked = cause_mod.rank([dead, noise], churn)
    assert "/scan" in ranked[0].cause, (
        "dead topic on /scan must outrank frequency noise riding on "
        f"unrelated churn; got {ranked[0].cause!r}"
    )
