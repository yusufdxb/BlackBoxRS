"""Evidence loading: a flight bundle, validated before anything replays it.

Replay Lab reads the same bundle format the flight recorder writes
(``manifest.json`` + ``records.jsonl``; see docs/FLIGHT_RECORDER.md). The
flight loader tolerates damage so an interrupted capture can still be read;
the lab is stricter, because a replay verdict built on silently repaired
evidence would be misleading:

* a missing manifest, a torn record line or an unfinalized ``.partial``
  bundle is refused unless ``allow_partial`` is set, and when it is set the
  result says so;
* every record must carry ``kind``, an integer ``seq`` and integer clocks;
  duplicate ``seq`` values are refused (they would make tie ordering
  ambiguous);
* message records must name a topic, a role and a type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blackboxrs.flight.bundle import load_bundle
from blackboxrs.flight.profile import FlightProfile, ProfileError, profile_from_text
from blackboxrs.lab.values import digest

# Record kinds that are inputs to a replay. Trigger, health and clock_jump
# records are outputs the recorder derived live; the replay derives its own.
INPUT_KINDS = ("msg", "graph", "marker", "sys")

# Roles published by the GO2 main computer (robot clock). Everything else in
# the go2 profiles is published on the payload computer. Used only as the
# default ``host`` of a topic; a case file can override it per topic.
ROBOT_ROLES = frozenset({"odometry", "go2_state", "sport_response", "operator_remote"})


class EvidenceError(ValueError):
    """Evidence is malformed or incomplete for the requested replay."""


@dataclass(frozen=True)
class TopicInfo:
    """What the lab needs to know about a topic beyond the record itself.

    ``liveness`` is ``periodic`` (silence past ``stale_after_s`` is a
    finding) or ``event`` (publishes only when something happens, like a
    joystick; silence is normal). ``host`` names the computer, and so the
    clock and the network path, the publisher runs on.
    """

    name: str
    role: str
    host: str
    liveness: str
    stale_after_s: float | None


@dataclass(frozen=True)
class Evidence:
    source: str
    manifest: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    profile: FlightProfile
    digest: str
    synthetic: bool
    partial: bool
    read_info: dict[str, Any] = field(default_factory=dict)

    @property
    def t0_mono_ns(self) -> int:
        return min(r["t_mono_ns"] for r in self.records if r["kind"] in INPUT_KINDS)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise EvidenceError(msg)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    for i, r in enumerate(records):
        where = f"record #{i + 1}"
        _require(isinstance(r, dict), f"{where}: not a JSON object")
        _require(isinstance(r.get("kind"), str), f"{where}: missing 'kind'")
        _require(_is_int(r.get("seq")), f"{where}: missing integer 'seq'")
        _require(r["seq"] not in seen, f"{where}: duplicate seq {r['seq']}")
        seen.add(r["seq"])
        if r["kind"] not in INPUT_KINDS:
            continue
        _require(_is_int(r.get("t_mono_ns")), f"{where} (seq {r['seq']}): missing t_mono_ns")
        _require(_is_int(r.get("t_wall_ns")), f"{where} (seq {r['seq']}): missing t_wall_ns")
        if r["kind"] == "msg":
            for k in ("topic", "role", "type"):
                _require(isinstance(r.get(k), str) and r[k],
                         f"{where} (seq {r['seq']}): message without '{k}'")
            _require(r.get("data") is None or isinstance(r["data"], dict),
                     f"{where} (seq {r['seq']}): 'data' must be an object or null")
    _require(any(r["kind"] == "msg" for r in records), "evidence has no message records")
    return sorted(records, key=lambda r: r["seq"])


def load_evidence(path: str | Path, *, allow_partial: bool = False,
                  label: str | None = None) -> Evidence:
    """Load and validate a bundle. ``label`` is what results call it (default: the path)."""
    p = Path(path)
    _require(p.is_dir(), f"{path}: not a bundle directory")
    _require((p / "records.jsonl").is_file(), f"{path}: no records.jsonl")
    manifest, records, info = load_bundle(p)
    problems = []
    if info["manifest_missing"]:
        problems.append("manifest.json missing or unreadable")
    if info["torn_lines"]:
        problems.append(f"{info['torn_lines']} torn record line(s)")
    if manifest.get("status") in ("capturing", "interrupted_unfinalized", "unknown"):
        problems.append(f"bundle status is {manifest.get('status')!r}")
    if problems and not allow_partial:
        raise EvidenceError(f"{path}: incomplete evidence ({'; '.join(problems)}); "
                            "pass allow_partial to replay it anyway")
    text = (manifest.get("profile") or {}).get("text")
    _require(bool(text), f"{path}: manifest has no embedded profile text")
    try:
        profile = profile_from_text(text)
    except ProfileError as exc:
        raise EvidenceError(f"{path}: embedded profile is invalid: {exc}") from exc
    recs = validate_records(records)
    return Evidence(
        source=label or str(path),
        manifest=manifest,
        records=tuple(recs),
        profile=profile,
        digest=digest({"records": recs, "profile_sha256": profile.sha256}),
        synthetic=bool((manifest.get("session") or {}).get("synthetic")),
        partial=bool(problems),
        read_info={**info, "problems": problems},
    )


def topic_table(ev: Evidence, overrides: dict[str, dict[str, Any]] | None = None
                ) -> dict[str, TopicInfo]:
    """Per-topic lab metadata: profile defaults, then case overrides.

    Defaults: ``periodic`` when the profile gives the topic a
    ``stale_after_sec`` (the recorder already treats silence there as a
    trigger), otherwise ``event``; ``host`` from the role.
    """
    overrides = overrides or {}
    roles = {t.name: t.role for t in ev.profile.topics}
    for r in ev.records:
        if r["kind"] == "msg":
            roles.setdefault(r["topic"], r["role"])
    unknown = sorted(set(overrides) - set(roles))
    _require(not unknown, f"topic overrides name topics not in the evidence: {unknown}")
    out: dict[str, TopicInfo] = {}
    for name in sorted(roles):
        spec = ev.profile.topic(name)
        stale = spec.stale_after_sec if spec else None
        o = overrides.get(name, {})
        bad = sorted(set(o) - {"host", "liveness", "stale_after_s"})
        _require(not bad, f"topic override for {name}: unknown keys {bad}")
        stale = o.get("stale_after_s", stale)
        liveness = o.get("liveness", "periodic" if stale else "event")
        _require(liveness in ("periodic", "event"),
                 f"topic override for {name}: liveness must be periodic or event")
        _require(liveness == "event" or (isinstance(stale, (int, float)) and stale > 0),
                 f"topic {name}: periodic liveness needs stale_after_s > 0")
        out[name] = TopicInfo(
            name=name, role=roles[name],
            host=o.get("host", "robot" if roles[name] in ROBOT_ROLES else "payload"),
            liveness=liveness,
            stale_after_s=float(stale) if liveness == "periodic" else None)
    return out

