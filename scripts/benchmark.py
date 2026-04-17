#!/usr/bin/env python3
"""Performance envelope benchmark for BlackBoxRS.

BlackBoxRS markets itself as a low-overhead observability daemon.
Before this script existed there were no numbers backing that claim.

This benchmark measures three things, in isolation, on the current
host:

1. ``EventBus`` publish throughput and drop behaviour under a
   producer that runs faster than a single consumer.
2. ``RotatingJsonlWriter`` sustained write throughput for a realistic
   event payload (a ``system.cpu`` event with 24 per-CPU values).
3. End-to-end producer → EventBus → consumer → writer pipeline
   throughput and drop rate, as a proxy for what the daemon
   experiences when a monitor fires as fast as the host allows.

The benchmark is **deliberately single-host, single-process**, and
uses ``time.perf_counter`` wall clock — not a statistical CPU / memory
profile.  It answers "how many events per second can this code push
through on a developer workstation?" and not much more.  It is not a
substitute for real robot-fleet measurements.

USAGE
-----
::

    # Run all three benchmarks with default counts
    python scripts/benchmark.py

    # Tune individual sizes
    python scripts/benchmark.py --bus-events 500_000 --writer-events 200_000

    # JSON output (one object per bench), useful in CI
    python scripts/benchmark.py --json

OUTPUT
------
The benchmark emits a plain-text report to stdout.  Sample output
from the author's workstation (mewtwo, AMD Ryzen 9 7900X3D, Python
3.10.12, Ubuntu 22.04) is reproduced in ``docs/BENCHMARKS.md``.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty
from typing import Any

# The EventBus emits a WARNING per queue-full drop (rate-limited, but
# still noisy in a bench that deliberately overruns the queue).  Mute
# it so the report is readable.  The drop *counts* below are still
# accurate; we only suppress the log-level chatter.
logging.getLogger("blackboxrs.core.event_bus").setLevel(logging.ERROR)

# Make this runnable as a script without `pip install -e .` first.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackboxrs.core.clock import Clock  # noqa: E402
from blackboxrs.core.event_bus import EventBus  # noqa: E402
from blackboxrs.core.schemas import BlackBoxEvent  # noqa: E402
from blackboxrs.logging.writer import RotatingJsonlWriter  # noqa: E402


def _sample_event() -> BlackBoxEvent:
    """A realistic-size event — mirrors what SystemMonitor emits."""
    return BlackBoxEvent(
        timestamp=Clock.now(),
        source="system_monitor",
        event_type="system.cpu",
        severity="info",
        data={
            "cpu_percent": 24.5,
            "cpu_count": 24,
            "per_cpu_percent": [i * 0.1 for i in range(24)],
            "load_avg_1m": 0.5,
            "load_avg_5m": 0.6,
            "load_avg_15m": 0.7,
        },
        metadata={
            "session_id": "benchmark0000",
            "hostname": "benchmark-host",
            "start_time": "2026-04-16T12:00:00+00:00",
        },
    )


# ---------------------------------------------------------------------------
# Benchmark 1: EventBus publish throughput
# ---------------------------------------------------------------------------


def bench_event_bus(
    n_events: int, queue_depth: int, consumer_delay_us: float
) -> dict[str, Any]:
    """Measure EventBus publish throughput and drop rate.

    A single producer publishes ``n_events`` copies of ``_sample_event``
    through the bus to a single subscriber with ``queue_depth`` slots.
    The consumer sleeps ``consumer_delay_us`` microseconds per event,
    which controls how quickly the queue fills and whether drops occur.

    Returns a dict keyed by: events, queue_depth, publish_elapsed_sec,
    publish_rate_eps, delivered, dropped, drop_rate_pct, consumer_delay_us.
    """
    bus = EventBus(default_queue_maxsize=queue_depth)
    q = bus.subscribe(channel="system_monitor")
    event = _sample_event()

    delivered = 0
    done_event = threading.Event()

    def consumer() -> None:
        nonlocal delivered
        # Drain until producer declares itself done AND the queue is empty.
        while not (done_event.is_set() and q.empty()):
            try:
                q.get(timeout=0.05)
                delivered += 1
                if consumer_delay_us > 0:
                    time.sleep(consumer_delay_us / 1_000_000.0)
            except Empty:
                continue

    t = threading.Thread(target=consumer, name="bench-bus-consumer")
    t.start()

    start = time.perf_counter()
    for _ in range(n_events):
        bus.publish(event)
    publish_elapsed = time.perf_counter() - start

    done_event.set()
    t.join(timeout=10.0)

    dropped = bus.dropped_count(q)
    return {
        "bench": "event_bus",
        "events": n_events,
        "queue_depth": queue_depth,
        "consumer_delay_us": consumer_delay_us,
        "publish_elapsed_sec": round(publish_elapsed, 4),
        "publish_rate_eps": round(n_events / publish_elapsed, 0),
        "delivered": delivered,
        "dropped": dropped,
        "drop_rate_pct": round(100.0 * dropped / n_events, 2) if n_events else 0.0,
    }


# ---------------------------------------------------------------------------
# Benchmark 2: RotatingJsonlWriter throughput
# ---------------------------------------------------------------------------


def bench_writer(n_events: int) -> dict[str, Any]:
    """Measure RotatingJsonlWriter sustained throughput.

    Writes ``n_events`` copies of a realistic event to a temp log
    directory.  Every write call is timed individually so we can
    report both mean and p99 latency, which is the number that
    matters for an observability daemon sharing a thread with a
    real-time control stack.

    Returns a dict with: events, total_elapsed_sec, write_rate_eps,
    bytes_written, bytes_per_sec, per_write_us_{mean,p50,p99,max}.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="blackboxrs-bench-"))
    try:
        writer = RotatingJsonlWriter(
            log_dir=tmp_dir,
            max_file_mb=50,
            max_files=20,
        )
        event = _sample_event()
        # Warm up the file handle so the first-write cost is not
        # included in the per-write latency distribution.
        writer.write(event)

        per_write_ns: list[int] = []
        start = time.perf_counter()
        for _ in range(n_events):
            t0 = time.perf_counter_ns()
            writer.write(event)
            per_write_ns.append(time.perf_counter_ns() - t0)
        total_elapsed = time.perf_counter() - start
        writer.close()

        # Tally bytes written to know bytes/sec.
        bytes_written = sum(
            f.stat().st_size for f in tmp_dir.glob("blackboxrs_*.jsonl")
        )
        per_write_us = [ns / 1_000.0 for ns in per_write_ns]
        per_write_us.sort()
        p50 = per_write_us[len(per_write_us) // 2]
        p99 = per_write_us[min(len(per_write_us) - 1, int(len(per_write_us) * 0.99))]

        return {
            "bench": "writer",
            "events": n_events,
            "total_elapsed_sec": round(total_elapsed, 4),
            "write_rate_eps": round(n_events / total_elapsed, 0),
            "bytes_written": bytes_written,
            "bytes_per_sec": round(bytes_written / total_elapsed, 0),
            "per_write_us_mean": round(statistics.fmean(per_write_us), 2),
            "per_write_us_p50": round(p50, 2),
            "per_write_us_p99": round(p99, 2),
            "per_write_us_max": round(per_write_us[-1], 2),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Benchmark 3: End-to-end pipeline
# ---------------------------------------------------------------------------


def bench_pipeline(n_events: int, queue_depth: int) -> dict[str, Any]:
    """Producer → EventBus → consumer thread → RotatingJsonlWriter.

    Models the real daemon's event path.  Useful for spotting
    regressions where the writer's fsync cadence or GIL contention
    shows up only once the full pipeline is wired together.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="blackboxrs-bench-pipeline-"))
    try:
        bus = EventBus(default_queue_maxsize=queue_depth)
        q = bus.subscribe(channel="system_monitor")
        writer = RotatingJsonlWriter(
            log_dir=tmp_dir,
            max_file_mb=50,
            max_files=20,
        )

        delivered = 0
        done_event = threading.Event()

        def consumer() -> None:
            nonlocal delivered
            while not (done_event.is_set() and q.empty()):
                try:
                    event = q.get(timeout=0.05)
                    writer.write(event)
                    delivered += 1
                except Empty:
                    continue

        t = threading.Thread(target=consumer, name="bench-pipeline-consumer")
        t.start()

        event = _sample_event()
        start = time.perf_counter()
        for _ in range(n_events):
            bus.publish(event)
        publish_elapsed = time.perf_counter() - start

        done_event.set()
        t.join(timeout=30.0)
        total_elapsed = time.perf_counter() - start
        writer.close()

        dropped = bus.dropped_count(q)
        bytes_written = sum(
            f.stat().st_size for f in tmp_dir.glob("blackboxrs_*.jsonl")
        )
        return {
            "bench": "pipeline",
            "events": n_events,
            "queue_depth": queue_depth,
            "publish_elapsed_sec": round(publish_elapsed, 4),
            "total_elapsed_sec": round(total_elapsed, 4),
            "publish_rate_eps": round(n_events / publish_elapsed, 0),
            "delivered": delivered,
            "dropped": dropped,
            "drop_rate_pct": round(100.0 * dropped / n_events, 2) if n_events else 0.0,
            "bytes_written": bytes_written,
            "bytes_per_sec": round(bytes_written / total_elapsed, 0),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _fmt_result(r: dict[str, Any]) -> str:
    lines = [f"\n=== {r['bench']} ==="]
    for k, v in r.items():
        if k == "bench":
            continue
        lines.append(f"  {k:<22} {v}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bus-events", type=int, default=200_000)
    parser.add_argument("--bus-queue-depth", type=int, default=1024)
    parser.add_argument(
        "--bus-consumer-delay-us",
        type=float,
        default=0.0,
        help="Per-event sleep in microseconds for the consumer thread "
        "in the event-bus benchmark. Non-zero values induce queue fill.",
    )
    parser.add_argument("--writer-events", type=int, default=50_000)
    parser.add_argument("--pipeline-events", type=int, default=100_000)
    parser.add_argument("--pipeline-queue-depth", type=int, default=1024)
    parser.add_argument(
        "--skip",
        action="append",
        choices=["event_bus", "writer", "pipeline"],
        default=[],
        help="Skip a specific benchmark (may be repeated).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per benchmark to stdout (no prose).",
    )
    args = parser.parse_args()

    results: list[dict[str, Any]] = []

    if "event_bus" not in args.skip:
        results.append(
            bench_event_bus(
                n_events=args.bus_events,
                queue_depth=args.bus_queue_depth,
                consumer_delay_us=args.bus_consumer_delay_us,
            )
        )
    if "writer" not in args.skip:
        results.append(bench_writer(n_events=args.writer_events))
    if "pipeline" not in args.skip:
        results.append(
            bench_pipeline(
                n_events=args.pipeline_events,
                queue_depth=args.pipeline_queue_depth,
            )
        )

    if args.json:
        for r in results:
            sys.stdout.write(json.dumps(r) + "\n")
    else:
        sys.stdout.write("BlackBoxRS performance envelope\n")
        sys.stdout.write(
            f"Python {sys.version.split()[0]} | event payload: {len(_sample_event().to_jsonl())} bytes\n"
        )
        for r in results:
            sys.stdout.write(_fmt_result(r) + "\n")
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
