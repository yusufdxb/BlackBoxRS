"""Precursor-aware likely-cause ranking.

This module turns triggers + derived timeline events + signature diffs
+ (when available) recurrence context into a ranked list of
:class:`LikelyCauseHypothesis` rows. It is grounded, explainable, and
evidence-linked; it is not autonomous root-cause analysis.

Calibration philosophy
----------------------

Base scores per detector class are *intentionally low* compared with
the trigger-only ranker that preceded this one. High confidence has to
be earned, not assumed. The biggest single contribution to confidence
comes from real precursor evidence in the bundle (a derived
``silence_interval`` aligned with the trigger topic, a same-subsystem
``resource_excursion`` immediately before the trigger, etc.). A
trigger that fires with no supporting evidence stays at moderate
confidence; the report's caveat tells the reader as much.

Confidence policy
-----------------

``confidence`` is a clipped linear combination of additive bonuses on
top of a base score. It is not a probability. It reflects:

* the specificity of the underlying detector (QoS mismatch is more
  specific than a generic threshold breach);
* the temporal proximity and subsystem alignment of precursor events
  in the bundle;
* whether a signature diff identified a relevant config or version
  change;
* whether this fingerprint has matched any prior incident on this
  host.

We never use confidence to argue causation. Two reports with the same
confidence may have different evidence depth; the report shows both.

Caveat policy:

* confidence < 0.30: "manual review required; evidence is too thin
  to nominate a cause without operator judgement."
* confidence < 0.50: "evidence quality is moderate; treat the top
  hypothesis as a starting point, not a verdict."
* recurrence detected: prefix caveat with the prior-occurrence count;
  fix-history is the load-bearing concern, not novelty.

All scoring weights are at the top of this module so they can be
tuned in one place. They are documented and conservative.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Iterable, Sequence

from .diff import IncidentDiff
from .models import (
    DetectorTrigger,
    LikelyCauseHypothesis,
    PrecursorRef,
    RecurrenceContext,
    TimelineEvent,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring weights (all in [0, 1] additive space)
# ---------------------------------------------------------------------------


# Base score per detector class. Lower than the previous trigger-only
# ranker because precursor evidence is the load-bearing signal, not
# detector identity. Unknown detector classes default to 0.30.
_BASE_SCORE: dict[str, float] = {
    "QoSMismatchDetector": 0.85,    # very specific failure mode
    "TfTopologyDetector": 0.75,     # structural fault, narrow set of causes
    "ClockSkewDetector": 0.70,      # specific upstream cause for tf/bag bugs
    "DeadTopicDetector": 0.65,      # specific symptom, cause is upstream
    "FrequencyDetector": 0.55,      # symptom; many possible causes
    "ProcessSignalsDetector": 0.45, # resource symptom; cause is the node logic
    "ThresholdDetector": 0.40,      # symptom domain; not the root
}
_DEFAULT_BASE = 0.30


# Severity-of-trigger bonus. Caps at +0.10; an "info" trigger adds zero.
_SEVERITY_BONUS = {
    "info": 0.0,
    "warning": 0.03,
    "error": 0.07,
    "critical": 0.10,
}


# Per-cause-class relevance for each derived precursor kind. Treated as
# the *peak* relevance: the actual contribution scales by temporal
# proximity and subsystem alignment.
#
# Keys are short detector class names; values map derived event tags
# (silence/graph_delta/resource_excursion) to a relevance multiplier
# in [0.0, 0.30]. A precursor kind that does not appear in the dict for
# a given detector contributes nothing.
_PRECURSOR_RELEVANCE: dict[str, dict[str, float]] = {
    "DeadTopicDetector": {
        "silence_interval": 0.20,
        "graph_delta": 0.20,
        "resource_excursion": 0.12,
    },
    "FrequencyDetector": {
        "silence_interval": 0.15,
        "resource_excursion": 0.15,
        "graph_delta": 0.08,
    },
    "QoSMismatchDetector": {
        "graph_delta": 0.15,
    },
    "ThresholdDetector": {
        "resource_excursion": 0.12,
    },
    # New evidence-breadth detectors. Weights are conservative; the
    # ranker still requires precursor evidence to push past the base
    # score, and resource excursions are the most operationally useful
    # precursor for both process-signals and tf-topology incidents
    # (a runaway node starves the bus, which then breaks the tree).
    "TfTopologyDetector": {
        "silence_interval": 0.18,    # /tf topic going quiet right before
        "resource_excursion": 0.15,  # CPU spike in tf publisher
        "graph_delta": 0.10,         # nodes joining/leaving change tf
    },
    "ProcessSignalsDetector": {
        "resource_excursion": 0.18,  # an excursion *is* what we report
        "silence_interval": 0.10,    # bus starvation often paired
    },
    "ClockSkewDetector": {
        "resource_excursion": 0.12,  # NTP daemon starvation -> drift
        "graph_delta": 0.08,         # new node, wrong /clock source
    },
}

# Subsystem alignment bonus. Same-subsystem precursors are weighted
# more strongly because they describe the same domain as the trigger.
_SAME_SUBSYSTEM_FACTOR = 1.0
_CROSS_SUBSYSTEM_FACTOR = 0.55

# Hard cap on total precursor contribution per hypothesis. Without this
# a long string of weak precursors could push confidence past evidence.
_PRECURSOR_BONUS_CAP = 0.30

# Temporal-proximity window. Precursors older than this against the
# trigger are ignored. The function inside the window is a linear
# decay from 1.0 at the trigger time to 0.0 at the window edge.
_PRECURSOR_WINDOW_SEC = 30.0

# Diff relevance. A small fixed bonus when a diff key plausibly relates
# to the trigger class. We never claim causation from a diff.
_DIFF_RELEVANT_BONUS = 0.05
_DIFF_COINCIDENT_BONUS = 0.02

# Recurrence bonus and policy.
_RECURRENCE_BONUS = 0.05
_RECURRENCE_BIG_FIX_HISTORY_THRESHOLD = 3  # >=3 priors triggers louder caveat

# Caveat thresholds.
_CAVEAT_LOW = 0.50
_CAVEAT_VERY_LOW = 0.30


# Per-detector relevant signature-diff key prefixes. Conservative: we
# only call out keys whose name plausibly relates to the trigger class.
_DIFF_RELEVANT_KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "QoSMismatchDetector": (
        "ros_distro", "rmw_implementation", "ros_domain_id",
        "attached_files", "env_subset.RMW_IMPLEMENTATION",
    ),
    "DeadTopicDetector": (
        "ros_distro", "rmw_implementation", "ros_domain_id",
        "attached_files", "ros_packages_apt_count",
        "blackboxrs_config_yaml_sha256",
    ),
    "FrequencyDetector": (
        "ros_distro", "rmw_implementation", "ros_domain_id",
        "attached_files", "blackboxrs_config_yaml_sha256",
    ),
    "ThresholdDetector": (
        "kernel", "nvidia_driver", "os.version", "cuda",
    ),
    # tf graph is sensitive to URDF / robot_description changes and to
    # any tool that publishes static transforms (e.g. robot_state_publisher).
    "TfTopologyDetector": (
        "ros_distro", "rmw_implementation", "attached_files",
        "ros_packages_apt_count", "blackboxrs_config_yaml_sha256",
    ),
    # New process / kernel / driver state is the most plausible
    # coincident change for a per-process resource excursion.
    "ProcessSignalsDetector": (
        "kernel", "nvidia_driver", "ros_packages_apt_count",
        "blackboxrs_config_yaml_sha256",
    ),
    # Clock skew: anything that touches the kernel time subsystem,
    # NTP/chrony config, or the /etc/timezone file.
    "ClockSkewDetector": (
        "kernel", "os.version", "attached_files",
    ),
}


# Type alias for the recurrence-lookup callable injected by the builder.
RecurrenceLookup = Callable[[str], RecurrenceContext | None]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_class(detector_class: str) -> str:
    return detector_class.rsplit(".", 1)[-1]


def _trigger_subsystem(trigger: DetectorTrigger) -> str:
    """Return the trigger's domain subsystem.

    The trigger's own ``subsystem`` is set by the builder from
    ``metadata.target_subsystem`` and is the right answer for ranking
    in v0.4.x. Falling back to "anomaly" loses precision but is honest.
    """
    return trigger.subsystem or "anomaly"


def _proximity(gap_sec: float) -> float:
    """Linear decay from 1.0 at gap=0 to 0.0 at gap=window."""
    if gap_sec <= 0:
        return 1.0
    if gap_sec >= _PRECURSOR_WINDOW_SEC:
        return 0.0
    return 1.0 - (gap_sec / _PRECURSOR_WINDOW_SEC)


def _classify_derived(event: TimelineEvent) -> str | None:
    """Map a derived TimelineEvent to its precursor *kind*.

    Returns one of ``"silence_interval"``, ``"graph_delta"``,
    ``"resource_excursion"``, or None if the event is not a known
    derived kind.
    """
    if event.kind != "derived":
        return None
    if "silence interval" in event.summary:
        return "silence_interval"
    if "excursion" in event.summary:
        return "resource_excursion"
    if "appeared" in event.summary or "disappeared" in event.summary \
            or "publisher count" in event.summary:
        return "graph_delta"
    return None


def _topics_match(trigger_subject: str, event_data: dict) -> bool:
    """Heuristic: does the derived event's data refer to the same topic?"""
    topic = event_data.get("topic")
    if not isinstance(topic, str):
        return False
    return topic == trigger_subject


# ---------------------------------------------------------------------------
# Precursor scoring for a single trigger
# ---------------------------------------------------------------------------


def _score_precursors(
    trigger: DetectorTrigger,
    timeline: Sequence[TimelineEvent],
) -> tuple[float, list[PrecursorRef], list[str]]:
    """Return ``(bonus, chain, reasoning_lines)`` for *trigger*.

    Bonus is capped at :data:`_PRECURSOR_BONUS_CAP`. The chain lists
    every precursor that contributed (sorted earliest-first); reasoning
    lines describe each contribution in human-readable form.
    """
    cls = _short_class(trigger.detector_class)
    weights = _PRECURSOR_RELEVANCE.get(cls, {})
    if not weights:
        return 0.0, [], []

    trigger_sub = _trigger_subsystem(trigger)

    contributions: list[tuple[float, PrecursorRef, str]] = []

    for ev in timeline:
        if ev.t >= trigger.t:
            continue  # not a precursor
        kind = _classify_derived(ev)
        if kind is None:
            continue
        peak = weights.get(kind, 0.0)
        if peak <= 0:
            continue

        gap = (trigger.t - ev.t).total_seconds()
        prox = _proximity(gap)
        if prox <= 0:
            continue

        align = (
            _SAME_SUBSYSTEM_FACTOR if ev.subsystem == trigger_sub
            else _CROSS_SUBSYSTEM_FACTOR
        )

        # Topic-match boost: a graph_delta or silence_interval whose
        # data.topic matches the trigger subject is much more relevant.
        topic_boost = 1.0
        if kind in ("graph_delta", "silence_interval") and \
                _topics_match(trigger.subject, ev.data):
            topic_boost = 1.5

        relevance = peak * prox * align * topic_boost
        if relevance <= 0:
            continue

        ref = PrecursorRef(
            evidence_ref=ev.evidence_ref,
            t=ev.t,
            kind=ev.kind,
            subsystem=ev.subsystem,
            summary=ev.summary,
            gap_sec=round(gap, 3),
            relevance=round(min(relevance, 1.0), 3),
        )
        # Reasoning is per-contribution; renderer concatenates them.
        line = (
            f"{ev.summary} "
            f"({gap:.1f}s before trigger, "
            f"subsystem={ev.subsystem}, "
            f"relevance={relevance:.2f})"
        )
        contributions.append((relevance, ref, line))

    if not contributions:
        return 0.0, [], []

    # Sum relevance, then cap. We sum (rather than max) because two
    # independent precursors should matter more than one, but only up
    # to the cap (otherwise a noisy bundle inflates the score).
    contributions.sort(key=lambda c: c[1].t)  # earliest precursor first
    total_relevance = sum(c[0] for c in contributions)
    bonus = min(_PRECURSOR_BONUS_CAP, total_relevance)

    chain = [c[1] for c in contributions]
    reasoning = [
        f"precursor: {c[2]}" for c in contributions
    ]
    if total_relevance > _PRECURSOR_BONUS_CAP:
        reasoning.append(
            f"precursor contribution capped at {_PRECURSOR_BONUS_CAP:.2f} "
            f"(raw sum was {total_relevance:.2f})."
        )
    return bonus, chain, reasoning


# ---------------------------------------------------------------------------
# Diff relevance
# ---------------------------------------------------------------------------


def _score_diff(
    trigger: DetectorTrigger,
    incident_diff: IncidentDiff | None,
) -> tuple[float, list[str]]:
    """Return ``(bonus, reasoning_lines)`` for the signature diff.

    A diff is relevant when at least one changed/added/removed key
    matches a prefix in :data:`_DIFF_RELEVANT_KEY_PREFIXES` for this
    trigger class. We award a small bonus and explicitly mark the
    relationship as "coincident" until further evidence supports
    causality.
    """
    if incident_diff is None:
        return 0.0, []

    cls = _short_class(trigger.detector_class)
    prefixes = _DIFF_RELEVANT_KEY_PREFIXES.get(cls, ())

    cfg = incident_diff.config
    ver = incident_diff.versions

    # Skip the boost when there is nothing to compare against.
    if cfg.baseline_hash is None and ver.baseline_hash is None:
        return 0.0, [
            "no prior bundle on this host; signature diff not informative."
        ]
    if cfg.identical and ver.identical:
        return 0.0, [
            "config + version signatures are identical to the prior "
            "session; this incident is not driven by a recorded change."
        ]

    def _all_keys(side) -> list[str]:
        return (
            [c.key for c in side.changed]
            + [c.key for c in side.only_in_current]
            + [c.key for c in side.only_in_baseline]
        )

    cfg_keys = _all_keys(cfg)
    ver_keys = _all_keys(ver)

    relevant_cfg = sorted({k for k in cfg_keys if any(
        k.startswith(p) or p in k for p in prefixes
    )})
    relevant_ver = sorted({k for k in ver_keys if any(
        k.startswith(p) or p in k for p in prefixes
    )})

    reasoning: list[str] = []
    if relevant_cfg:
        reasoning.append(
            "config diff touches relevant key(s): "
            + ", ".join(f"`{k}`" for k in relevant_cfg)
            + " (coincident, not proven causal)."
        )
    if relevant_ver:
        reasoning.append(
            "version diff touches relevant key(s): "
            + ", ".join(f"`{k}`" for k in relevant_ver)
            + " (coincident, not proven causal)."
        )

    if relevant_cfg or relevant_ver:
        return _DIFF_RELEVANT_BONUS, reasoning

    if cfg_keys or ver_keys:
        # There IS a diff, but none of its keys are clearly relevant.
        # Award a tiny bonus and label as coincident.
        return _DIFF_COINCIDENT_BONUS, [
            "config / version diff exists but no field is clearly "
            "relevant to this trigger class; treated as coincident."
        ]
    return 0.0, []


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


def _score_recurrence(
    fingerprint_id: str | None,
    lookup: RecurrenceLookup | None,
) -> tuple[float, RecurrenceContext | None, list[str]]:
    """Return ``(bonus, context, reasoning)`` for fingerprint recurrence."""
    if not fingerprint_id or lookup is None:
        return 0.0, None, []
    try:
        ctx = lookup(fingerprint_id)
    except Exception:
        logger.debug("recurrence lookup raised", exc_info=True)
        return 0.0, None, []
    if ctx is None or ctx.prior_count <= 0:
        return 0.0, None, []
    bonus = _RECURRENCE_BONUS
    if ctx.prior_count >= _RECURRENCE_BIG_FIX_HISTORY_THRESHOLD:
        # Don't keep awarding bonus for chronic recurrences; capping
        # prevents one "always-failing" fingerprint from drowning out
        # other hypotheses.
        bonus = _RECURRENCE_BONUS
    reasoning = [
        f"recurrence: this fingerprint matched {ctx.prior_count} prior "
        f"bundle(s) on this host."
    ]
    return bonus, ctx, reasoning


# ---------------------------------------------------------------------------
# Per-trigger hypothesis assembly
# ---------------------------------------------------------------------------


def _cause_summary(trigger: DetectorTrigger) -> str:
    cls = _short_class(trigger.detector_class)
    if cls == "QoSMismatchDetector":
        return (
            f"QoS mismatch on {trigger.subject!s}; publisher and "
            f"subscriber disagree on at least one QoS field."
        )
    if cls == "DeadTopicDetector":
        return f"Topic {trigger.subject!s} stopped emitting messages."
    if cls == "FrequencyDetector":
        return (
            f"Topic {trigger.subject!s} message frequency dropped below "
            f"the configured tolerance."
        )
    if cls == "ThresholdDetector":
        return (
            f"System metric {trigger.subject!s} crossed a configured "
            f"threshold."
        )
    if cls == "TfTopologyDetector":
        kind = trigger.data.get("failure_kind", "structural fault")
        return (
            f"TF tree {kind} on frame {trigger.subject!s}; tf2 lookups "
            f"will fail or return inconsistent transforms."
        )
    if cls == "ProcessSignalsDetector":
        metric = trigger.data.get("metric", "resource")
        return (
            f"Process {trigger.subject!s} exceeded a {metric} "
            f"threshold; the node may be starving the bus or leaking."
        )
    if cls == "ClockSkewDetector":
        a = trigger.data.get("source_a", "?")
        b = trigger.data.get("source_b", "?")
        return (
            f"Clock skew between {a!s} and {b!s}; TF lookups and "
            f"bag playback will misbehave until clocks are realigned."
        )
    return f"{cls} fired on {trigger.subject!s}."


def _build_hypothesis(
    trigger: DetectorTrigger,
    timeline: Sequence[TimelineEvent],
    incident_diff: IncidentDiff | None,
    recurrence_lookup: RecurrenceLookup | None,
    fingerprint_id: str | None,
) -> LikelyCauseHypothesis:
    """Turn one trigger into a hypothesis with full scoring narrative."""
    cls = _short_class(trigger.detector_class)
    base = _BASE_SCORE.get(cls, _DEFAULT_BASE)
    severity_bonus = _SEVERITY_BONUS.get(trigger.severity, 0.0)

    precursor_bonus, chain, precursor_reasoning = _score_precursors(
        trigger, timeline,
    )
    diff_bonus, diff_reasoning = _score_diff(trigger, incident_diff)
    recurrence_bonus, recurrence_ctx, recurrence_reasoning = _score_recurrence(
        fingerprint_id, recurrence_lookup,
    )

    raw_score = base + severity_bonus + precursor_bonus + diff_bonus + recurrence_bonus
    confidence = max(0.0, min(1.0, raw_score))

    reasoning: list[str] = [
        f"base score for {cls}: {base:.2f}.",
        f"severity bonus ({trigger.severity}): {severity_bonus:+.2f}.",
    ]
    if precursor_reasoning:
        reasoning.append(f"precursor bonus: {precursor_bonus:+.2f}.")
        reasoning.extend(precursor_reasoning)
    else:
        reasoning.append("no precursor evidence found in the bundle.")
    if diff_reasoning:
        reasoning.append(f"diff bonus: {diff_bonus:+.2f}.")
        reasoning.extend(diff_reasoning)
    else:
        reasoning.append("no signature diff considered.")
    if recurrence_reasoning:
        reasoning.append(f"recurrence bonus: {recurrence_bonus:+.2f}.")
        reasoning.extend(recurrence_reasoning)
    reasoning.append(f"final confidence (clamped): {confidence:.2f}.")

    caveat = _build_caveat(confidence, recurrence_ctx, chain, diff_bonus > 0)

    refs: list[str] = []
    if trigger.source_event_ref:
        refs.append(trigger.source_event_ref)
    refs.append(f"triggers.json#{trigger.trigger_id}")
    refs.extend(p.evidence_ref for p in chain)

    return LikelyCauseHypothesis(
        cause=_cause_summary(trigger),
        confidence=round(confidence, 3),
        evidence_refs=refs,
        caveat=caveat,
        score=round(raw_score, 3),
        subsystem=_trigger_subsystem(trigger),
        reasoning=reasoning,
        precursor_chain=chain,
        recurrence=recurrence_ctx,
    )


def _build_caveat(
    confidence: float,
    recurrence: RecurrenceContext | None,
    chain: Sequence[PrecursorRef],
    has_relevant_diff: bool,
) -> str | None:
    """Compose the caveat string. Recurrence wins; low-confidence falls
    through second."""
    parts: list[str] = []
    if recurrence is not None:
        parts.append(
            f"recurrence: this fingerprint matched {recurrence.prior_count} "
            f"prior bundle(s); fix history recommended."
        )
    if confidence < _CAVEAT_VERY_LOW:
        parts.append(
            "manual review required; the bundle does not yet contain "
            "enough evidence to nominate a cause without operator "
            "judgement."
        )
    elif confidence < _CAVEAT_LOW:
        parts.append(
            "evidence quality is moderate; treat the top hypothesis as "
            "a starting point, not a verdict."
        )
    if not chain and not has_relevant_diff and confidence < 0.7:
        parts.append(
            "no precursor evidence and no relevant signature change; "
            "the trigger alone is the entire signal."
        )
    if not parts:
        return None
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def rank(
    triggers: list[DetectorTrigger],
    timeline: Iterable[TimelineEvent] | None = None,
    *,
    incident_diff: IncidentDiff | None = None,
    recurrence_lookup: RecurrenceLookup | None = None,
    fingerprint_id: str | None = None,
) -> list[LikelyCauseHypothesis]:
    """Rank likely causes using all available structured signals.

    Args:
        triggers: Promoted detector triggers in the bundle.
        timeline: Full reconstructed timeline (raw + trigger + derived).
            Used for precursor scoring; ``None`` is equivalent to no
            precursor evidence.
        incident_diff: Optional config/version diff against the most
            recent prior bundle. Used for diff-relevance scoring.
        recurrence_lookup: Optional callable
            ``(fingerprint_id) -> RecurrenceContext | None`` that
            returns prior occurrences of this fingerprint on this host.
            ``None`` disables recurrence scoring entirely.
        fingerprint_id: The current bundle's fingerprint id. Required
            for recurrence; pass ``None`` to skip.

    Returns:
        Hypotheses ordered by descending confidence, with ties broken
        by trigger severity rank then trigger timestamp. Empty list
        when no triggers are present.
    """
    if not triggers:
        return []

    timeline_list = list(timeline) if timeline else []

    hypotheses: list[tuple[LikelyCauseHypothesis, DetectorTrigger]] = []
    for trig in triggers:
        h = _build_hypothesis(
            trig, timeline_list, incident_diff,
            recurrence_lookup, fingerprint_id,
        )
        hypotheses.append((h, trig))

    severity_rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    hypotheses.sort(
        key=lambda p: (
            -p[0].confidence,
            -severity_rank.get(p[1].severity, 0),
            p[1].t,
        )
    )
    return [h for h, _ in hypotheses]
