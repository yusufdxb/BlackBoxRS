"""Live rclpy flight recorder (observation only).

The node creates subscriptions and graph queries and nothing else: no
publishers, no services, no parameter services, no rosout. rclpy itself
still creates the node's ``/parameter_events`` publisher (Humble has no
switch for it); that topic carries parameter changes, not commands, and
the recorder never sets a parameter. ``tests/unit/flight/test_no_publish.py``
checks both the source and a live node.

Subscription QoS is BEST_EFFORT + VOLATILE + KEEP_LAST(200) on every topic.
That profile is compatible with any publisher, and a best-effort reader
sends no acknowledgements, so it cannot slow a reliable writer down (a
lagging RELIABLE reader can make a Cyclone DDS writer block). The price
is that the recorder can itself miss a sample under load; those losses show
up in the publisher sequence counters (HELIX hold and arbiter status) and
in the middleware message-lost events, which are both reported.
"""

from __future__ import annotations

import importlib
import logging
import os
import queue
import signal
import threading
import time
from pathlib import Path
from typing import Any

from blackboxrs.flight.analysis import analyze
from blackboxrs.flight.bundle import BundleWriter, disk_free_mb
from blackboxrs.flight.core import FlightCore
from blackboxrs.flight.profile import FlightProfile, TopicSpec
from blackboxrs.flight.records import (StoreDecimator, extract_fields, make_event_record,
                                       make_msg_record)
from blackboxrs.flight.render import render_markdown
from blackboxrs.flight.sysinfo import SystemSampler

logger = logging.getLogger(__name__)

NODE_NAME = "blackboxrs_flight_recorder"
NODE_NAMESPACE = "/blackbox"
HARD_DISK_FLOOR_MB = 256.0
_NS = 1_000_000_000


def resolve_type(type_str: str) -> tuple[type | None, str | None]:
    pkg, iface, name = type_str.split("/")
    try:
        mod = importlib.import_module(f"{pkg}.{iface}")
    except ImportError as exc:
        return None, f"message package not importable: {exc}"
    cls = getattr(mod, name, None)
    return (cls, None) if cls is not None else (None, f"{type_str} not found in {pkg}.{iface}")


def control_dir(profile: FlightProfile) -> Path:
    return profile.evidence_path / "control"


def request_marker(profile: FlightProfile, note: str) -> Path:
    """Ask a running recorder for a manual marker (file drop, no DDS)."""
    d = control_dir(profile)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{time.time_ns()}_{os.getpid()}.mark"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(note, encoding="utf-8")
    os.replace(tmp, p)
    return p


class FlightRecorder:
    """Owns the rclpy context, node, executor and the recorder core."""

    def __init__(self, profile: FlightProfile, session: dict[str, Any], *,
                 gpu: str = "auto") -> None:
        import rclpy

        self.profile = profile
        self.session = session
        self.session_dir = profile.evidence_path / session["session_id"]
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._stop = False
        self._marks_pending: list[str] = []
        self.topic_status: dict[str, dict[str, Any]] = {
            t.name: {"status": "absent", "reason": "not seen on the graph yet",
                     "message_lost": 0, "received": 0} for t in profile.topics}
        self._decimator = StoreDecimator({t.name: t.store_max_hz for t in profile.topics
                                          if t.store_max_hz})
        self._rclpy = rclpy
        self._ctx = rclpy.Context()
        rclpy.init(context=self._ctx)
        self.node = rclpy.create_node(NODE_NAME, namespace=NODE_NAMESPACE, context=self._ctx,
                                      enable_rosout=False, start_parameter_services=False)
        self.session["use_sim_time"] = bool(self.node.get_parameter("use_sim_time").value)
        self._executor = _InfoExecutor(context=self._ctx)
        self._executor.add_node(self.node)
        self.core = FlightCore(profile, self._new_writer, can_open=self._can_open)
        self.sampler = SystemSampler(profile.evidence_path, gpu=gpu)
        self.session["gpu_backend"] = self.sampler.gpu_backend
        self._subs: dict[str, Any] = {}
        self._types: dict[str, type] = {}
        for spec in profile.topics:
            self._subscribe(spec)
        s = profile.sampling
        # Sampling runs on its own thread: nvidia-smi alone takes ~25 ms, and
        # on the executor thread it would delay every message callback behind
        # it. Samples are handed over through a queue and ingested by the tick.
        self._sys_queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._sampler_stop = threading.Event()
        self._sampler_thread = threading.Thread(
            target=self._sampler_loop, args=(1.0 / s.system_sample_hz,),
            name="bbrs-sys-sampler", daemon=True)
        self._sampler_thread.start()
        self.node.create_timer(s.graph_poll_sec, self._poll_graph)
        self.node.create_timer(s.health_tick_sec, self._tick)
        self._poll_graph()
        names = {f"{ns.rstrip('/')}/{n}" for n, ns in self.node.get_node_names_and_namespaces()}
        if profile.topic("/cmd_vel") is not None and names & {"/helix_arbiter",
                                                              "/helix_go2_sport_sink"}:
            logger.warning("HELIX is running and this profile subscribes to /cmd_vel: HELIX "
                           "preflight C6 will refuse its stages. Use --profile go2_helix.")
            self.session["helix_cmd_vel_conflict"] = True
        self._write_session_file("recording")

    # -- setup -------------------------------------------------------------

    def _new_writer(self) -> BundleWriter:
        return BundleWriter(self.session_dir, self.profile, self.session,
                            lambda: {k: dict(v) for k, v in self.topic_status.items()},
                            analyze=analyze, render=render_markdown)

    def _can_open(self) -> tuple[bool, str]:
        free = disk_free_mb(self.session_dir)
        if free < HARD_DISK_FLOOR_MB:
            return False, f"disk_pressure: {free:.0f} MB free < floor {HARD_DISK_FLOOR_MB:.0f} MB"
        if free < self.profile.min_free_disk_mb:
            logger.warning("evidence disk low: %.0f MB free (profile minimum %d MB)",
                           free, self.profile.min_free_disk_mb)
        return True, ""

    def _subscribe(self, spec: TopicSpec) -> None:
        from rclpy.qos import (QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile,
                               QoSReliabilityPolicy)
        from rclpy.qos_event import SubscriptionEventCallbacks

        cls, why = resolve_type(spec.type)
        if cls is None:
            self.topic_status[spec.name].update(status="type_unavailable", reason=why)
            return
        self._types[spec.name] = cls
        qos = QoSProfile(reliability=QoSReliabilityPolicy.BEST_EFFORT,
                         durability=QoSDurabilityPolicy.VOLATILE,
                         history=QoSHistoryPolicy.KEEP_LAST, depth=200)

        def lost(info: Any, name: str = spec.name) -> None:
            self.topic_status[name]["message_lost"] = int(info.total_count)

        def cb(msg: Any, info: dict[str, Any] | None, spec: TopicSpec = spec) -> None:
            self._on_msg(spec, msg, info)

        cb._bbrs_info = True  # type: ignore[attr-defined]
        # Topics stored at a reduced rate are taken serialized and only the
        # stored samples are deserialized: at GO2 rates (/lowstate 500 Hz,
        # /sportmodestate 295 Hz) full Python deserialization of every sample
        # was the recorder's largest CPU cost (docs/FLIGHT_RECORDER.md,
        # Performance).
        raw = spec.store_max_hz is not None
        try:
            self._subs[spec.name] = self.node.create_subscription(
                cls, spec.name, cb, qos, raw=raw,
                event_callbacks=SubscriptionEventCallbacks(message_lost=lost))
        except Exception:  # noqa: BLE001 - message_lost unsupported by some RMWs
            self._subs[spec.name] = self.node.create_subscription(cls, spec.name, cb, qos,
                                                                  raw=raw)

    # -- callbacks -----------------------------------------------------------

    def _on_msg(self, spec: TopicSpec, msg: Any, info: dict[str, Any] | None) -> None:
        mono, wall = time.monotonic_ns(), time.time_ns()
        st = self.topic_status[spec.name]
        st["received"] = st.get("received", 0) + 1
        store = self._decimator.keep(spec.name, mono)
        data: dict[str, Any] = {}
        if store:
            if isinstance(msg, (bytes, bytearray)):
                from rclpy.serialization import deserialize_message
                msg = deserialize_message(bytes(msg), self._types[spec.name])
            data, missing = extract_fields(msg, spec.fields)
            if missing and not st.get("missing_fields"):
                st["missing_fields"] = missing
                logger.warning("%s: profile fields not in message: %s", spec.name, missing)
        rec = make_msg_record(
            topic=spec.name, role=spec.role, msg_type=spec.type, data=data,
            t_mono_ns=mono, t_wall_ns=wall, t_ros_ns=self.node.get_clock().now().nanoseconds,
            dds_src_ns=(info or {}).get("source_timestamp"),
            dds_rx_ns=(info or {}).get("received_timestamp"), stored=store)
        self.core.ingest(rec)

    def _poll_graph(self) -> None:
        mono, wall = time.monotonic_ns(), time.time_ns()
        n = self.node
        nodes = sorted(f"{ns.rstrip('/')}/{name}" for name, ns in n.get_node_names_and_namespaces())
        graph_types = dict(n.get_topic_names_and_types())
        publishers: dict[str, list[str]] = {}
        for spec in self.profile.topics:
            st = self.topic_status[spec.name]
            # Our own subscription puts the topic on the graph, so presence
            # and type are judged from publishers only.
            infos = n.get_publishers_info_by_topic(spec.name)
            infos = [i for i in infos if i.node_namespace.rstrip("/") != NODE_NAMESPACE]
            pubs = sorted(f"{i.node_namespace.rstrip('/')}/{i.node_name}" for i in infos)
            ptypes = sorted({i.topic_type for i in infos})
            st["publishers"] = pubs
            st["graph_types"] = ptypes or None
            if st["status"] == "type_unavailable":
                continue
            if not infos:
                if st.get("ever_published"):
                    st["left_graph"] = True
                else:
                    st.update(status="no_publishers" if spec.name in graph_types else "absent",
                              reason="no publisher on the graph")
                continue
            publishers[spec.name] = pubs
            if spec.type not in ptypes:
                st.update(status="type_mismatch",
                          reason=f"publishers use {ptypes}, profile expects {spec.type}")
                continue
            if len(ptypes) > 1:
                st["reason"] = f"mixed publisher types {ptypes}"
            st.update(status="subscribed", ever_published=True, left_graph=False,
                      publisher_qos=sorted({_qos_str(i.qos_profile) for i in infos}))
            if len(ptypes) == 1:
                st["reason"] = None
        topics = {k: v for k, v in graph_types.items()}
        self.core.graph(mono, wall, nodes, topics, publishers)

    def _sampler_loop(self, period: float) -> None:
        while not self._sampler_stop.wait(period):
            mono, wall = time.monotonic_ns(), time.time_ns()
            try:
                payload = self.sampler.sample()
            except Exception as exc:  # noqa: BLE001
                payload = {"error": str(exc)}
            self._sys_queue.put(make_event_record("sys", t_mono_ns=mono, t_wall_ns=wall,
                                                  sample_ms=round((time.monotonic_ns() - mono)
                                                                  / 1e6, 2), **payload))

    def _tick(self) -> None:
        while True:
            try:
                self.core.ingest(self._sys_queue.get_nowait())
            except queue.Empty:
                break
        mono, wall = time.monotonic_ns(), time.time_ns()
        self._drain_markers(mono, wall)
        self.core.tick(mono, wall)

    def _drain_markers(self, mono: int, wall: int) -> None:
        notes = self._marks_pending
        self._marks_pending = []
        d = control_dir(self.profile)
        if d.is_dir():
            for f in sorted(d.glob("*.mark")):
                try:
                    notes.append(f.read_text(encoding="utf-8").strip())
                    f.unlink()
                except OSError:
                    continue
        for note in notes:
            self.core.mark(mono, wall, note=note, source="operator")

    # -- run -----------------------------------------------------------------

    def request_stop(self, *_: Any) -> None:
        self._stop = True

    def request_mark(self, *_: Any) -> None:
        self._marks_pending.append("SIGUSR1 marker")

    def spin(self, duration_s: float | None = None) -> None:
        end = None if duration_s is None else time.monotonic() + duration_s
        while not self._stop and (end is None or time.monotonic() < end):
            self._executor.spin_once(timeout_sec=0.05)

    def close(self, reason: str = "recorder_stopped") -> list[str]:
        self._sampler_stop.set()
        self._sampler_thread.join(timeout=5.0)
        self.core.shutdown(reason)
        self.core.wait_writers()
        self._write_session_file("stopped")
        try:
            self._executor.remove_node(self.node)
            self.node.destroy_node()
        finally:
            self._executor.shutdown()
            if self._ctx.ok():
                self._rclpy.shutdown(context=self._ctx)
        return list(self.core.closed_bundles)

    def _write_session_file(self, state: str) -> None:
        from blackboxrs.flight.bundle import _dump
        _dump(self.session_dir / "session.json", {
            "state": state, "session": self.session, "profile": self.profile.to_dict(),
            "topic_status": self.topic_status, "core": self.core.stats_dict(),
            "bundles": self.core.closed_bundles, "updated_wall_ns": time.time_ns()})


def _qos_str(q: Any) -> str:
    try:
        return f"{q.reliability.name}/{q.durability.name}/{q.history.name}:{q.depth}"
    except AttributeError:
        return str(q)


def _make_info_executor():
    from rclpy.executors import SingleThreadedExecutor

    class InfoExecutor(SingleThreadedExecutor):
        """Hands (message, message_info) to callbacks that ask for it.

        Humble's executor drops the rmw message info (DDS source and
        reception timestamps). The recorder needs both, so for callbacks
        marked ``_bbrs_info`` this executor passes them through.
        """

        def _take_subscription(self, sub):  # noqa: ANN001
            with sub.handle:
                taken = sub.handle.take_message(sub.msg_type, sub.raw)
            if taken is None:
                return None
            if getattr(sub.callback, "_bbrs_info", False):
                return _Taken(taken[0], taken[1] if len(taken) > 1 else None)
            return taken[0]

        async def _execute_subscription(self, sub, msg):  # noqa: ANN001
            if msg is None:
                return
            if isinstance(msg, _Taken):
                sub.callback(msg.msg, msg.info)
            else:
                sub.callback(msg)

    return InfoExecutor


class _Taken:
    __slots__ = ("msg", "info")

    def __init__(self, msg: Any, info: Any) -> None:
        self.msg = msg
        self.info = info if isinstance(info, dict) else None


def _InfoExecutor(**kw: Any):  # noqa: N802
    return _make_info_executor()(**kw)


def run_recorder(profile: FlightProfile, session: dict[str, Any], *,
                 duration_s: float | None = None, gpu: str = "auto") -> list[str]:
    rec = FlightRecorder(profile, session, gpu=gpu)
    old_int = signal.signal(signal.SIGINT, rec.request_stop)
    old_term = signal.signal(signal.SIGTERM, rec.request_stop)
    old_usr1 = signal.signal(signal.SIGUSR1, rec.request_mark)
    try:
        rec.spin(duration_s)
    finally:
        bundles = rec.close("signal" if rec._stop else "duration_elapsed")
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGUSR1, old_usr1)
    return bundles
