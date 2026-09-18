"""Capture profiles for the flight recorder.

A profile is a YAML file that declares which topics to observe, how long a
window to keep around a trigger, which triggers are armed and which stop
criteria the report applies. Built-in profiles live next to this module in
``profiles/`` and are selected by name (``--profile go2``); any other value
is treated as a path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

_BUILTIN_DIR = Path(__file__).parent / "profiles"

# Roles the report knows how to interpret. Anything else is captured and
# counted but not used by the chain reconstruction.
KNOWN_ROLES = frozenset({
    "sport_request", "sport_response", "odometry", "go2_state", "operator_remote",
    "cmd_vel_out", "cmd_vel_source", "helix_fault", "recovery_hint", "recovery_action",
    "helix_hold", "arbiter_status", "sink_trace", "helix_health", "helix_diagnosis_text",
    "phoenix_safety", "phoenix_action", "phoenix_observation", "other",
})


class ProfileError(ValueError):
    """Raised for a malformed profile."""


@dataclass(frozen=True)
class TopicSpec:
    name: str
    type: str
    role: str = "other"
    required: bool = False
    expected_hz: float | None = None
    stale_after_sec: float | None = None
    store_max_hz: float | None = None
    fields: tuple[str, ...] = ()
    frame_ids: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BufferSpec:
    pre_trigger_sec: float = 10.0
    post_trigger_sec: float = 15.0
    max_records: int = 400_000
    max_bytes: int = 256 * 1024 * 1024


@dataclass(frozen=True)
class SamplingSpec:
    graph_poll_sec: float = 0.5
    system_sample_hz: float = 2.0
    health_tick_sec: float = 0.25


@dataclass(frozen=True)
class StopCriteria:
    stopped_speed_mps: float = 0.03
    stop_deadline_sec: float = 1.5
    moving_speed_mps: float = 0.05
    pose_speed_baseline_sec: float = 0.05


@dataclass(frozen=True)
class TriggerSpec:
    helix_hold_asserted: bool = True
    recovery_action_stop: bool = True
    recovery_actions: tuple[str, ...] = ("STOP_AND_HOLD",)
    recovery_statuses: tuple[str, ...] = ("ACCEPTED",)
    arbiter_forced_zero: bool = True
    arbiter_reasons: tuple[str, ...] = ("HELIX_HOLD", "HELIX_STATE_STALE", "HELIX_STATE_MISSING")
    node_disappeared: bool = True
    topic_stale: bool = True
    manual_marker: bool = True
    max_incidents_per_run: int = 20


@dataclass(frozen=True)
class PreflightSpec:
    listen_sec: float = 3.0
    expect_rmw: str | None = None
    max_recorder_cpu_percent: float = 50.0
    max_recorder_rss_mb: float = 500.0


@dataclass(frozen=True)
class FlightProfile:
    name: str
    topics: tuple[TopicSpec, ...]
    source: str
    sha256: str
    text: str = ""
    description: str = ""
    evidence_dir: str = "~/blackboxrs_evidence"
    min_free_disk_mb: int = 2048
    buffer: BufferSpec = field(default_factory=BufferSpec)
    sampling: SamplingSpec = field(default_factory=SamplingSpec)
    stop: StopCriteria = field(default_factory=StopCriteria)
    triggers: TriggerSpec = field(default_factory=TriggerSpec)
    co_hosted_roles: frozenset[str] = frozenset()
    expected_nodes: tuple[str, ...] = ()
    preflight: PreflightSpec = field(default_factory=PreflightSpec)

    def topic(self, name: str) -> TopicSpec | None:
        for spec in self.topics:
            if spec.name == name:
                return spec
        return None

    def without_topics(self, names: list[str] | tuple[str, ...]) -> "FlightProfile":
        """Drop topics for one run (lab fallback). The sha256 still names the
        profile file; callers must record ``names`` next to it."""
        unknown = sorted(set(names) - {t.name for t in self.topics})
        if unknown:
            raise ProfileError(f"--exclude-topic not in profile: {unknown}")
        from dataclasses import replace
        return replace(self, topics=tuple(t for t in self.topics if t.name not in set(names)))

    def topics_with_role(self, role: str) -> list[TopicSpec]:
        return [t for t in self.topics if t.role == role]

    @property
    def evidence_path(self) -> Path:
        return Path(self.evidence_dir).expanduser()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("text", None)
        d["co_hosted_roles"] = sorted(self.co_hosted_roles)
        return d


def _flag(raw: Any, default: bool) -> tuple[bool, dict[str, Any]]:
    """Trigger entries are either a bool or a mapping with ``enabled``."""
    if raw is None:
        return default, {}
    if isinstance(raw, bool):
        return raw, {}
    if isinstance(raw, dict):
        return bool(raw.get("enabled", True)), raw
    raise ProfileError(f"trigger value must be bool or mapping, got {raw!r}")


def _positive(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{name} must be a number, got {value!r}") from exc
    if v <= 0:
        raise ProfileError(f"{name} must be > 0, got {v}")
    return v


def resolve_profile_path(name_or_path: str) -> Path:
    builtin = _BUILTIN_DIR / f"{name_or_path}.yaml"
    if builtin.is_file():
        return builtin
    p = Path(name_or_path).expanduser()
    if p.is_file():
        return p
    known = sorted(x.stem for x in _BUILTIN_DIR.glob("*.yaml"))
    raise ProfileError(f"profile {name_or_path!r} not found (built-in: {known})")


def parse_profile(raw: dict[str, Any], *, source: str, text: str) -> FlightProfile:
    if not isinstance(raw, dict):
        raise ProfileError("profile must be a mapping")
    topics: list[TopicSpec] = []
    seen: set[str] = set()
    for i, t in enumerate(raw.get("topics") or []):
        if not isinstance(t, dict) or "name" not in t or "type" not in t:
            raise ProfileError(f"topics[{i}] needs name and type")
        name = str(t["name"])
        if not name.startswith("/"):
            raise ProfileError(f"topic {name!r} must be fully qualified")
        if name in seen:
            raise ProfileError(f"topic {name} listed twice")
        seen.add(name)
        if len(str(t["type"]).split("/")) != 3:
            raise ProfileError(f"topic {name}: type must be pkg/msg/Type")
        role = str(t.get("role", "other"))
        if role not in KNOWN_ROLES:
            raise ProfileError(f"topic {name}: unknown role {role!r}")
        topics.append(TopicSpec(
            name=name,
            type=str(t["type"]),
            role=role,
            required=bool(t.get("required", False)),
            expected_hz=None if t.get("expected_hz") is None
            else _positive(f"{name}.expected_hz", t["expected_hz"]),
            stale_after_sec=None if t.get("stale_after_sec") is None
            else _positive(f"{name}.stale_after_sec", t["stale_after_sec"]),
            store_max_hz=None if t.get("store_max_hz") is None
            else _positive(f"{name}.store_max_hz", t["store_max_hz"]),
            fields=tuple(str(f) for f in (t.get("fields") or ())),
            frame_ids=tuple(sorted((str(k), str(v)) for k, v in
                                   (t.get("frame_ids") or {}).items())),
        ))
    if not topics:
        raise ProfileError("profile declares no topics")

    b = raw.get("buffer") or {}
    buffer = BufferSpec(
        pre_trigger_sec=_positive("buffer.pre_trigger_sec", b.get("pre_trigger_sec", 10.0)),
        post_trigger_sec=_positive("buffer.post_trigger_sec", b.get("post_trigger_sec", 15.0)),
        max_records=int(_positive("buffer.max_records", b.get("max_records", 400_000))),
        max_bytes=int(_positive("buffer.max_bytes", b.get("max_bytes", 256 * 1024 * 1024))),
    )
    s = raw.get("sampling") or {}
    sampling = SamplingSpec(
        graph_poll_sec=_positive("sampling.graph_poll_sec", s.get("graph_poll_sec", 0.5)),
        system_sample_hz=_positive("sampling.system_sample_hz", s.get("system_sample_hz", 2.0)),
        health_tick_sec=_positive("sampling.health_tick_sec", s.get("health_tick_sec", 0.25)),
    )
    sc = raw.get("stop_criteria") or {}
    stop = StopCriteria(
        stopped_speed_mps=_positive("stopped_speed_mps", sc.get("stopped_speed_mps", 0.03)),
        stop_deadline_sec=_positive("stop_deadline_sec", sc.get("stop_deadline_sec", 1.5)),
        moving_speed_mps=_positive("moving_speed_mps", sc.get("moving_speed_mps", 0.05)),
        pose_speed_baseline_sec=_positive(
            "pose_speed_baseline_sec", sc.get("pose_speed_baseline_sec", 0.05)),
    )
    tr = raw.get("triggers") or {}
    hold, _ = _flag(tr.get("helix_hold_asserted"), True)
    ras, ras_cfg = _flag(tr.get("recovery_action_stop"), True)
    afz, afz_cfg = _flag(tr.get("arbiter_forced_zero"), True)
    nd, _ = _flag(tr.get("node_disappeared"), True)
    ts, _ = _flag(tr.get("topic_stale"), True)
    mm, _ = _flag(tr.get("manual_marker"), True)
    defaults = TriggerSpec()
    triggers = TriggerSpec(
        helix_hold_asserted=hold,
        recovery_action_stop=ras,
        recovery_actions=tuple(ras_cfg.get("actions", defaults.recovery_actions)),
        recovery_statuses=tuple(ras_cfg.get("statuses", defaults.recovery_statuses)),
        arbiter_forced_zero=afz,
        arbiter_reasons=tuple(afz_cfg.get("reasons", defaults.arbiter_reasons)),
        node_disappeared=nd,
        topic_stale=ts,
        manual_marker=mm,
        max_incidents_per_run=int(tr.get("max_incidents_per_run", 20)),
    )
    pf = raw.get("preflight") or {}
    preflight = PreflightSpec(
        listen_sec=_positive("preflight.listen_sec", pf.get("listen_sec", 3.0)),
        expect_rmw=pf.get("expect_rmw"),
        max_recorder_cpu_percent=_positive("preflight.max_recorder_cpu_percent",
                                           pf.get("max_recorder_cpu_percent", 50.0)),
        max_recorder_rss_mb=_positive("preflight.max_recorder_rss_mb",
                                      pf.get("max_recorder_rss_mb", 500.0)),
    )
    return FlightProfile(
        preflight=preflight,
        name=str(raw.get("profile", Path(source).stem)),
        description=str(raw.get("description", "")).strip(),
        topics=tuple(topics),
        source=source,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        evidence_dir=str(raw.get("evidence_dir", "~/blackboxrs_evidence")),
        min_free_disk_mb=int(raw.get("min_free_disk_mb", 2048)),
        buffer=buffer,
        sampling=sampling,
        stop=stop,
        triggers=triggers,
        co_hosted_roles=frozenset(str(r) for r in raw.get("co_hosted_roles") or ()),
        expected_nodes=tuple(str(n) for n in raw.get("expected_nodes") or ()),
    )


def _resolve_extends(raw: dict[str, Any], path: Path, depth: int = 0) -> dict[str, Any]:
    """``extends: <profile>`` overlays: top-level keys replace the base's,
    ``exclude_topics`` removes base topics, ``topics`` (if given) appends."""
    base_name = raw.get("extends")
    if not base_name:
        return raw
    if depth > 3:
        raise ProfileError(f"{path}: extends chain too deep")
    base_path = resolve_profile_path(str(base_name))
    try:
        base = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{base_path}: invalid YAML: {exc}") from exc
    base = _resolve_extends(base, base_path, depth + 1)
    merged = {**base, **{k: v for k, v in raw.items()
                         if k not in ("extends", "exclude_topics", "topics")}}
    drop = set(raw.get("exclude_topics") or ())
    names = {t.get("name") for t in base.get("topics") or []}
    missing = sorted(drop - names)
    if missing:
        raise ProfileError(f"{path}: exclude_topics not in {base_name}: {missing}")
    merged["topics"] = [t for t in base.get("topics") or [] if t.get("name") not in drop] + \
        list(raw.get("topics") or [])
    merged["resolved_from"] = {"base": str(base_name), "excluded_topics": sorted(drop)}
    return merged


def load_profile(name_or_path: str, *, evidence_dir: str | None = None) -> FlightProfile:
    """Load a built-in profile by name or a profile YAML by path.

    ``evidence_dir`` overrides the profile's evidence location without
    changing the profile hash (the hash identifies the capture contract, not
    where it was written). A profile that ``extends`` another is resolved
    first; its hash and embedded text are those of the resolved profile.
    """
    path = resolve_profile_path(name_or_path)
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: invalid YAML: {exc}") from exc
    if isinstance(raw, dict) and raw.get("extends"):
        raw = _resolve_extends(raw, path)
        text = yaml.safe_dump(raw, sort_keys=False)
    prof = parse_profile(raw, source=str(path), text=text)
    if evidence_dir is not None:
        prof = FlightProfile(**{**prof.__dict__, "evidence_dir": evidence_dir})
    return prof


def profile_from_dict(raw: dict[str, Any], *, source: str = "<inline>") -> FlightProfile:
    """Build a profile from an in-memory mapping (tests, replays)."""
    text = json.dumps(raw, sort_keys=True)
    return parse_profile(raw, source=source, text=text)


def profile_from_text(text: str, *, source: str = "<bundle>") -> FlightProfile:
    """Rebuild a profile from the YAML text embedded in a bundle manifest."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"{source}: invalid YAML: {exc}") from exc
    return parse_profile(raw, source=source, text=text)
