"""Zero-motion hardware preflight for the flight recorder.

Preflight only observes: it starts the same subscribe-only recorder node the
real capture uses, with every trigger disabled, listens for a few seconds and
checks the result. It publishes no message on any robot or HELIX topic; the
``no_publish`` check verifies that on the live node, and the verdict is NO-GO
if it ever fails.

Verdict: any FAIL -> NO-GO; else any WARN -> GO WITH WARNINGS; else GO.
Exit codes follow ``robot-blackbox preflight``: 0 GO, 2 GO WITH WARNINGS,
1 NO-GO.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackboxrs.flight.analysis import analyze
from blackboxrs.flight.bundle import BundleWriter, _dump, disk_free_mb, load_bundle
from blackboxrs.flight.profile import FlightProfile, TriggerSpec
from blackboxrs.flight.provenance import build_session, git_state
from blackboxrs.flight.recorder import HARD_DISK_FLOOR_MB, NODE_NAME, NODE_NAMESPACE

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_NS = 1_000_000_000
ALLOWED_OWN_PUBLISHERS = {"/parameter_events"}  # created by rclpy itself


class _Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, cid: str, status: str, summary: str, **detail: Any) -> None:
        self.items.append({"id": cid, "status": status, "summary": summary, **detail})

    def verdict(self) -> str:
        st = {c["status"] for c in self.items}
        if FAIL in st:
            return "NO-GO"
        if WARN in st:
            return "GO WITH WARNINGS"
        return "GO"


def _get(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def _check_env(c: _Checks, profile: FlightProfile) -> bool:
    distro = os.environ.get("ROS_DISTRO")
    try:
        import rclpy  # noqa: F401
        ok = True
    except ImportError as exc:
        ok = False
        c.add("ros_env", FAIL, f"rclpy not importable: {exc}", ros_distro=distro)
    if ok:
        c.add("ros_env", PASS if distro else WARN,
              f"ROS_DISTRO={distro}, rclpy importable" if distro else
              "rclpy importable but ROS_DISTRO is unset (setup.bash not sourced?)",
              ros_distro=distro)
    rmw_env = os.environ.get("RMW_IMPLEMENTATION")
    domain = os.environ.get("ROS_DOMAIN_ID", "0 (default)")
    local = os.environ.get("ROS_LOCALHOST_ONLY")
    ident = None
    if ok:
        from rclpy.utilities import get_rmw_implementation_identifier
        try:
            ident = get_rmw_implementation_identifier()
        except Exception:  # noqa: BLE001
            ident = None
    want = profile.preflight.expect_rmw
    detail = {"rmw_identifier": ident, "RMW_IMPLEMENTATION": rmw_env,
              "ROS_DOMAIN_ID": domain, "ROS_LOCALHOST_ONLY": local}
    if want and ident and ident != want:
        c.add("rmw", FAIL, f"RMW is {ident}, profile expects {want} (DDS vendors do not "
              "interoperate reliably with the payload)", **detail)
    elif want and not rmw_env:
        c.add("rmw", WARN, f"RMW_IMPLEMENTATION unset (using default {ident}); set it to "
              f"{want} explicitly", **detail)
    else:
        c.add("rmw", PASS, f"RMW {ident}, domain {domain}", **detail)
    if local == "1":
        c.add("dds_scope", WARN, "ROS_LOCALHOST_ONLY=1: traffic from the robot will not be seen")
    uri = os.environ.get("CYCLONEDDS_URI")
    if (ident or "").startswith("rmw_cyclonedds"):
        if not uri:
            c.add("cyclonedds_config", WARN, "CYCLONEDDS_URI unset: Cyclone picks an "
                  "interface itself, which on the payload may not be the robot link")
        else:
            path = uri[7:] if uri.startswith("file://") else uri
            ifaces: list[str] = []
            if "<" in uri:
                text = uri
            else:
                try:
                    text = Path(path).read_text()
                except OSError as exc:
                    c.add("cyclonedds_config", FAIL, f"CYCLONEDDS_URI file unreadable: {exc}")
                    return ok
            ifaces = re.findall(r'NetworkInterface[^>]*name="([^"]+)"', text)
            ifaces += re.findall(r"<NetworkInterfaceAddress>([^<]+)<", text)
            states = {}
            for i in ifaces:
                op = Path(f"/sys/class/net/{i}/operstate")
                states[i] = op.read_text().strip() if op.exists() else "missing"
            bad = {i: s for i, s in states.items() if s not in ("up", "unknown")}
            c.add("cyclonedds_config", FAIL if bad else PASS,
                  f"interfaces {states}" if ifaces else "no interface pinned in config",
                  uri=uri, interfaces=states)
    return ok


def _check_evidence(c: _Checks, profile: FlightProfile) -> None:
    d = profile.evidence_path
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".write_probe_{uuid.uuid4().hex}"
        with probe.open("wb") as fh:
            fh.write(b"x" * 4096)
            fh.flush()
            os.fsync(fh.fileno())
        probe.unlink()
        c.add("evidence_writable", PASS, f"{d} writable (4 KiB write+fsync+unlink)")
    except OSError as exc:
        c.add("evidence_writable", FAIL, f"{d} not writable: {exc}")
        return
    free = disk_free_mb(d)
    if free < HARD_DISK_FLOOR_MB:
        c.add("disk_capacity", FAIL, f"{free:.0f} MB free < hard floor {HARD_DISK_FLOOR_MB:.0f} MB",
              free_mb=round(free, 1))
    elif free < profile.min_free_disk_mb:
        c.add("disk_capacity", WARN, f"{free:.0f} MB free < profile minimum "
              f"{profile.min_free_disk_mb} MB", free_mb=round(free, 1))
    else:
        c.add("disk_capacity", PASS, f"{free:.0f} MB free", free_mb=round(free, 1))


def _check_clock(c: _Checks) -> None:
    now = time.time()
    year = datetime.fromtimestamp(now, tz=timezone.utc).year
    head = None
    try:
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct"],
                             capture_output=True, text=True, timeout=5)
        head = int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        head = None
    if year < 2026 or (head is not None and now < head):
        c.add("clock_wall", FAIL, f"wall clock {datetime.fromtimestamp(now, tz=timezone.utc)} is "
              "before 2026 or before this code's HEAD commit (no RTC? set it with date -s)")
    else:
        iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
        c.add("clock_wall", PASS, f"wall clock {iso}")
    sync = None
    if shutil.which("timedatectl"):
        try:
            r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                               capture_output=True, text=True, timeout=5)
            sync = r.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            sync = None
    if sync == "yes":
        c.add("clock_sync", PASS, "NTP synchronized (timedatectl)")
    else:
        c.add("clock_sync", WARN, f"NTP sync not confirmed (timedatectl: {sync}); the lab "
              "network has no internet, so wall stamps across hosts may disagree. Latencies "
              "never mix hosts' clocks, but cross-log alignment will be approximate.")


def _qos_incompatible(pub: Any, sub: Any) -> list[str]:
    out = []
    try:
        if pub.reliability.name == "BEST_EFFORT" and sub.reliability.name == "RELIABLE":
            out.append("reliability: best-effort publisher, reliable subscriber")
        if pub.durability.name == "VOLATILE" and sub.durability.name == "TRANSIENT_LOCAL":
            out.append("durability: volatile publisher, transient-local subscriber")
    except AttributeError:
        pass
    return out


def run_preflight(profile: FlightProfile, *, listen_sec: float | None = None,
                  experiment: str | None = None, experiment_repos: list[str] | None = None,
                  keep_selftest: bool = False,
                  excluded_topics: list[str] | None = None) -> dict[str, Any]:
    c = _Checks()
    started = time.time()
    if not _check_env(c, profile):
        return _finish(c, profile, None, started)
    _check_evidence(c, profile)
    _check_clock(c)
    if any(x["id"] == "evidence_writable" and x["status"] == FAIL for x in c.items):
        return _finish(c, profile, None, started)

    from blackboxrs.flight.recorder import FlightRecorder, resolve_type

    # Type availability on this host.
    missing_types = {}
    for t in profile.topics:
        cls, why = resolve_type(t.type)
        if cls is None:
            missing_types[t.name] = why
    req_missing = [n for n in missing_types if profile.topic(n).required]
    c.add("message_types", FAIL if req_missing else (WARN if missing_types else PASS),
          f"{len(profile.topics) - len(missing_types)}/{len(profile.topics)} profile types "
          "importable" + (f"; not importable: {sorted(missing_types)}" if missing_types else ""),
          not_importable=missing_types)

    quiet = dataclasses.replace(profile, triggers=TriggerSpec(
        helix_hold_asserted=False, recovery_action_stop=False, arbiter_forced_zero=False,
        node_disappeared=False, topic_stale=False, manual_marker=False))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session = build_session(quiet, experiment=experiment, experiment_repos=experiment_repos,
                            session_id=f"preflight/{stamp}_{uuid.uuid4().hex[:4]}")
    session["excluded_topics"] = list(excluded_topics or [])
    if excluded_topics:
        c.add("excluded_topics", WARN, f"topics dropped for this run: {excluded_topics}")
    rec = FlightRecorder(quiet, session)
    listen = listen_sec if listen_sec is not None else profile.preflight.listen_sec
    try:
        import psutil
        proc = psutil.Process()
        proc.cpu_percent(None)
        rss0 = proc.memory_info().rss / 2**20
        m0, w0 = time.monotonic_ns(), time.time_ns()
        rec.spin(listen)
        m1, w1 = time.monotonic_ns(), time.time_ns()
        cpu = proc.cpu_percent(None)
        rss1 = proc.memory_info().rss / 2**20
        step = ((w1 - w0) - (m1 - m0)) / _NS
        if abs(step) > 0.05:
            c.add("clock_stepping", FAIL, f"wall clock moved {step:+.3f} s against monotonic "
                  f"during a {listen:.1f} s listen (clock being stepped?)")
        else:
            c.add("clock_stepping", PASS, f"wall vs monotonic drift {step * 1000:+.1f} ms over "
                  f"{listen:.1f} s")
        own = dict(rec.node.get_publisher_names_and_types_by_node(NODE_NAME, NODE_NAMESPACE))
        extra = sorted(set(own) - ALLOWED_OWN_PUBLISHERS)
        c.add("no_publish", FAIL if extra else PASS,
              f"recorder node publishers: {sorted(own)}" +
              (f"; UNEXPECTED: {extra}" if extra else " (rclpy built-in only)"))
        records = rec.core.window_records()
        _topic_checks(c, profile, rec, records, listen, m1)
        _qos_checks(c, profile, rec)
        _node_checks(c, profile, rec)
        lim = profile.preflight
        load_bad = cpu > lim.max_recorder_cpu_percent or rss1 > lim.max_recorder_rss_mb
        host = rec.sampler.sample()
        c.add("recorder_load", FAIL if load_bad else PASS,
              f"recorder CPU {cpu:.1f}% of one core (limit {lim.max_recorder_cpu_percent}), "
              f"RSS {rss1:.0f} MB (limit {lim.max_recorder_rss_mb}; +{rss1 - rss0:.0f} MB "
              f"while listening); host CPU {host['cpu_percent']}%, RAM {host['mem_percent']}%",
              recorder_cpu_percent=cpu, recorder_rss_mb=round(rss1, 1),
              host_cpu_percent=host["cpu_percent"], host_mem_percent=host["mem_percent"],
              msgs_per_s=round(sum(1 for r in records if r.get("kind") == "msg") / listen, 1))
        c.add("gpu_thermal", INFO if host.get("gpu") or host.get("thermal_c") else WARN,
              f"GPU: {host.get('gpu') or host.get('gpu_unavailable_reason')}; thermal zones: "
              f"{len(host.get('thermal_c') or {})}")
        _selftest_bundle(c, quiet, rec, records, session, keep_selftest)
    finally:
        rec.close("preflight_done")
    return _finish(c, profile, session, started)


def _topic_checks(c: _Checks, profile: FlightProfile, rec: Any, records: list[dict[str, Any]],
                  listen: float, now_mono: int) -> None:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("kind") == "msg":
            by.setdefault(r["topic"], []).append(r)
    present, absent, mism = [], [], []
    for t in profile.topics:
        st = rec.topic_status[t.name]
        if st["status"] == "subscribed" or st.get("publishers"):
            present.append(t.name)
        elif st["status"] == "type_mismatch":
            mism.append(t.name)
        else:
            absent.append(t.name)
    req_absent = [n for n in absent if profile.topic(n).required]
    key_roles = {"odometry": "odometry_stopped", "helix_hold": "helix_hold",
                 "cmd_vel_out": "cmd_vel_zero", "sport_request": "sport_stopmove_request",
                 "sport_response": "sport_response", "arbiter_status": "arbiter_forced_zero",
                 "recovery_action": "recovery_action", "helix_fault": "fault"}
    blind = sorted({stage for role, stage in key_roles.items()
                    if not any(t.name in present for t in profile.topics_with_role(role))})
    uncapturable = sorted(n for n, st in rec.topic_status.items()
                          if st["status"] == "type_unavailable" and st.get("publishers"))
    status = FAIL if req_absent else (WARN if blind or uncapturable else PASS)
    summary = f"{len(present)}/{len(profile.topics)} profile topics have publishers"
    if blind:
        summary += f"; HELIX chain stages that cannot be observed now: {blind}"
    if uncapturable:
        summary += f"; PUBLISHED BUT NOT CAPTURABLE (type not importable): {uncapturable}"
    c.add("topics_present", status, summary, present=present, absent=absent,
          unobservable_stages=blind, uncapturable=uncapturable,
          note="absent optional topics are recorded as unavailable, not as faults")
    if mism:
        c.add("topic_types", FAIL, f"type mismatch on {mism}",
              detail={n: rec.topic_status[n]["reason"] for n in mism})
    else:
        c.add("topic_types", PASS, "every published profile topic has the expected type")
    rates, stale, slow, silent = {}, [], [], []
    for t in profile.topics:
        msgs = by.get(t.name, [])
        if t.name not in present:
            continue
        hz = len(msgs) / listen
        age = (now_mono - msgs[-1]["t_mono_ns"]) / _NS if msgs else None
        rates[t.name] = {"hz": round(hz, 2), "last_age_s": None if age is None else round(age, 3),
                         "expected_hz": t.expected_hz}
        if not msgs:
            if t.role != "operator_remote":
                silent.append(t.name)
            continue
        if t.expected_hz and hz < 0.8 * t.expected_hz:
            slow.append(f"{t.name} {hz:.1f} Hz < 80% of {t.expected_hz}")
        if t.stale_after_sec and age is not None and age > t.stale_after_sec:
            stale.append(f"{t.name} last message {age:.2f} s ago > {t.stale_after_sec}")
    req_silent = [n for n in silent if profile.topic(n).required]
    status = FAIL if req_silent else (WARN if slow or stale or silent else PASS)
    c.add("freshness_rates", status,
          "; ".join(slow + stale + ([f"publisher present but silent: {silent}"] if silent else []))
          or "every present topic delivered fresh messages at its expected rate",
          rates=rates)
    frames = []
    bad = []
    for t in profile.topics:
        if not t.frame_ids:
            continue
        stored = [r for r in by.get(t.name, []) if r.get("data")]
        if t.name not in present:
            continue
        if not stored:
            frames.append(f"{t.name}: no message to check")
            continue
        d = stored[-1]["data"]
        for path, want in t.frame_ids:
            got = _get(d, path)
            (frames if got == want else bad).append(f"{t.name} {path}={got!r} (expected {want!r})")
    if bad:
        c.add("frame_ids", FAIL, "; ".join(bad), checked=frames)
    elif frames:
        c.add("frame_ids", PASS if all("no message" not in f for f in frames) else WARN,
              "; ".join(frames))


def _qos_checks(c: _Checks, profile: FlightProfile, rec: Any) -> None:
    node = rec.node
    problems = []
    for t in profile.topics:
        pubs = [i for i in node.get_publishers_info_by_topic(t.name)
                if i.node_namespace.rstrip("/") != NODE_NAMESPACE]
        subs = [i for i in node.get_subscriptions_info_by_topic(t.name)
                if i.node_namespace.rstrip("/") != NODE_NAMESPACE]
        for p in pubs:
            for s in subs:
                for why in _qos_incompatible(p.qos_profile, s.qos_profile):
                    problems.append(f"{t.name}: {p.node_name} -> {s.node_name}: {why}")
    c.add("qos_compat", WARN if problems else PASS,
          ("incompatible endpoint pairs (not the recorder's; HELIX preflight C4 gates motion "
           "edges): " + "; ".join(problems)) if problems else
          "no incompatible publisher/subscriber pair on profile topics; recorder uses "
          "BEST_EFFORT/VOLATILE, which matches any publisher")


HELIX_NODES = ("/helix_arbiter", "/helix_go2_sport_sink")


def _node_checks(c: _Checks, profile: FlightProfile, rec: Any) -> None:
    names = {f"{ns.rstrip('/')}/{n}" for n, ns in rec.node.get_node_names_and_namespaces()}
    helix = [n for n in HELIX_NODES if n in names]
    if helix and profile.topic("/cmd_vel") is not None:
        c.add("helix_compat", FAIL,
              f"HELIX is running ({helix}) and this profile subscribes to /cmd_vel: HELIX "
              "preflight C6 would count the recorder as a second /cmd_vel consumer and refuse "
              "the stage, and ArbiterStatus.sink_subscribers would change. Use "
              "--profile go2_helix (or --exclude-topic /cmd_vel).")
    elif helix:
        c.add("helix_compat", PASS, "recorder does not subscribe to /cmd_vel; HELIX C6 and "
              "ArbiterStatus.sink_subscribers are unaffected")
    missing = [n for n in profile.expected_nodes if n not in names]
    if profile.expected_nodes:
        c.add("expected_nodes", WARN if missing else PASS,
              (f"not running: {missing} (fine if this experiment does not use them)"
               if missing else "all expected nodes present"), present=sorted(names))


def _selftest_bundle(c: _Checks, profile: FlightProfile, rec: Any,
                     records: list[dict[str, Any]], session: dict[str, Any],
                     keep: bool) -> None:
    root = profile.evidence_path / session["session_id"] / "selftest"
    try:
        w = BundleWriter(root, profile, session, lambda: rec.topic_status,
                         analyze=analyze, render=None)
        mono, wall = time.monotonic_ns(), time.time_ns()
        w.open({"type": "preflight_selftest", "t_mono_ns": mono, "t_wall_ns": wall, "seq": 0},
               [dict(r) for r in records], {"requested_s": 0, "available_s": 0})
        path = Path(w.close("complete", {}))
        w.wait(30)
        manifest, recs, info = load_bundle(path)
        stored = json.loads((path / "report.json").read_text())
        again = analyze(manifest, recs)
        same = json.dumps(stored, sort_keys=True, default=str) == json.dumps(
            again, sort_keys=True, default=str)
        ok = manifest.get("status") == "complete" and len(recs) == len(records) + 1 and same \
            and not info["torn_lines"]
        c.add("bundle_roundtrip", PASS if ok else FAIL,
              f"opened, wrote {len(recs)} records, finalized, re-read and re-analyzed "
              f"({'identical report' if same else 'REPORT DIFFERS'})", path=str(path))
        if not keep:
            shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        c.add("bundle_roundtrip", FAIL, f"bundle write/read failed: {exc}")


def _finish(c: _Checks, profile: FlightProfile, session: dict[str, Any] | None,
            started: float) -> dict[str, Any]:
    result = {
        "schema": "blackboxrs.flight.preflight.v1",
        "verdict": c.verdict(),
        "reasons": [f"{x['id']}: {x['summary']}" for x in c.items if x["status"] in (FAIL, WARN)],
        "checks": c.items,
        "profile": {"name": profile.name, "sha256": profile.sha256, "source": profile.source},
        "session": session,
        "blackboxrs": git_state(Path(__file__).resolve().parents[2]),
        "motion_commands_published": 0,
        "duration_s": round(time.time() - started, 2),
    }
    if session is not None:
        out = profile.evidence_path / session["session_id"] / "preflight.json"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            _dump(out, result)
            result["written_to"] = str(out)
        except OSError:
            pass
    return result


def format_result(result: dict[str, Any]) -> str:
    lines = [f"BlackBoxRS preflight (profile {result['profile']['name']}, "
             f"sha256 {result['profile']['sha256'][:12]})", ""]
    for x in result["checks"]:
        lines.append(f"  [{x['status']:<4}] {x['id']:<18} {x['summary']}")
    lines.append("")
    lines.append(f"VERDICT: {result['verdict']}")
    for r in result["reasons"]:
        lines.append(f"  - {r}")
    if result.get("written_to"):
        lines.append(f"(written to {result['written_to']})")
    lines.append("Motion commands published by preflight: 0")
    return "\n".join(lines)
