"""Incident bundle writer and reader.

Layout of one bundle directory::

    <evidence_dir>/<session_id>/<bundle_id>/
        manifest.json    provenance, profile, topic availability, window, status
        records.jsonl    every record in the window, in recorder ingest order
        report.json      machine-readable report (regenerable from the two above)
        report.md        human-readable summary of report.json

While capturing, the directory is named ``<bundle_id>.partial`` and
``records.jsonl`` is appended and flushed as records arrive (fsync at least
every ``fsync_every_sec``). A clean finish renames it to ``<bundle_id>``. A
recorder killed mid-capture leaves the ``.partial`` directory, which
``robot-blackbox flight replay`` can still read and report on as INCOMPLETE.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import queue
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from blackboxrs.flight.profile import FlightProfile

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = "blackboxrs.flight.manifest.v1"
_NS = 1_000_000_000


def _utc_from_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / _NS, tz=timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _dump(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def disk_free_mb(path: Path) -> float:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / (1024 * 1024)


class BundleWriter:
    """One incident bundle. Implements ``core.IncidentSink``.

    All file I/O, fsync and the final analysis run on a dedicated writer
    thread, so the recorder's executor never waits on the disk: ``open``
    only creates the directory and manifest, and ``append``/``add_trigger``/
    ``close`` enqueue. ``wait()`` blocks until the bundle is finalized.
    Records must not be mutated after they are handed over (the core never
    does).
    """

    def __init__(
        self,
        session_dir: Path,
        profile: FlightProfile,
        session: dict[str, Any],
        topic_status: Callable[[], dict[str, Any]],
        *,
        fsync_every_sec: float = 1.0,
        analyze: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]] | None = None,
        render: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self._dir_root = session_dir
        self._profile = profile
        self._session = session
        self._topic_status = topic_status
        self._fsync_every = fsync_every_sec
        self._analyze = analyze
        self._render = render
        self._fh: Any = None
        self._last_sync = 0.0
        self.bundle_id = ""
        self.path: Path | None = None
        self.triggers: list[dict[str, Any]] = []
        self.pre_window: dict[str, Any] = {}
        self.records_written = 0
        self.write_errors = 0
        self.first_write_error: str | None = None
        self._records: list[dict[str, Any]] = []

    # -- IncidentSink ------------------------------------------------------

    def open(self, trigger: dict[str, Any], pre_records: list[dict[str, Any]],
             pre_window: dict[str, Any]) -> str:
        stamp = _utc_from_ns(trigger.get("t_wall_ns") or time.time_ns())
        self.bundle_id = f"inc_{stamp}_{trigger['type']}"
        self.path = self._dir_root / f"{self.bundle_id}.partial"
        self.path.mkdir(parents=True, exist_ok=False)
        self.triggers = [trigger]
        self.pre_window = pre_window
        self._q: queue.SimpleQueue[tuple[str, Any]] = queue.SimpleQueue()
        self._final_path = str(self._dir_root / self.bundle_id)
        self._thread = threading.Thread(target=self._run, name=f"bbrs-writer-{stamp}",
                                         daemon=True)
        self._q.put(("pre", pre_records))
        self._q.put(("rec", {"kind": "trigger", **trigger}))
        self._q.put(("sync", None))
        self._thread.start()
        return self.bundle_id

    def append(self, record: dict[str, Any]) -> None:
        self._q.put(("rec", record))

    def add_trigger(self, trigger: dict[str, Any]) -> None:
        self.triggers.append(trigger)
        self._q.put(("rec", {"kind": "trigger", **trigger}))

    def close(self, status: str, stats: dict[str, Any]) -> str | None:
        """Enqueue finalization; returns the path the bundle will have."""
        self._q.put(("close", (status, dict(stats))))
        return self._final_path

    def wait(self, timeout: float | None = None) -> None:
        if getattr(self, "_thread", None) is not None:
            self._thread.join(timeout)

    # -- writer thread -----------------------------------------------------

    def _run(self) -> None:
        # The manifest (with fsync) is written here, not in open(): open() runs
        # on the recorder's executor thread at the trigger, the worst moment
        # to wait on the disk.
        self._write_manifest(status="capturing", stats={})
        try:
            self._fh = (self.path / "records.jsonl").open("a", encoding="utf-8")
        except OSError as exc:
            self.write_errors += 1
            self.first_write_error = str(exc)
        while True:
            op, arg = self._q.get()
            if op == "pre":
                for i, rec in enumerate(arg):
                    self._write(rec)
                    if i % 200 == 199:
                        time.sleep(0.0005)  # hand the GIL back to the executor
            elif op == "rec":
                self._write(arg)
            elif op == "sync":
                self._safe_sync(force=True)
            elif op == "close":
                self._finalize(*arg)
                return

    def _write(self, record: dict[str, Any]) -> None:
        self._records.append(record)
        if self._fh is None or self.write_errors:
            if self.write_errors:
                self.write_errors += 1
            return
        try:
            self._fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            self.records_written += 1
            self._sync()
        except OSError as exc:
            self.write_errors += 1
            self.first_write_error = f"{errno.errorcode.get(exc.errno, exc.errno)}: {exc}"
            logger.error("bundle %s: write failed (%s); keeping records in memory",
                         self.bundle_id, self.first_write_error)

    def _safe_sync(self, force: bool = False) -> None:
        try:
            self._sync(force=force)
        except OSError as exc:
            self.write_errors += 1
            self.first_write_error = self.first_write_error or str(exc)

    def _finalize(self, status: str, stats: dict[str, Any]) -> None:
        assert self.path is not None
        try:
            if self._fh is not None:
                self._sync(force=True)
                self._fh.close()
        except OSError as exc:
            self.write_errors += 1
            self.first_write_error = self.first_write_error or str(exc)
        self._fh = None
        if self.write_errors:
            status = "write_failed" if status == "complete" else status
        manifest = self._write_manifest(status=status, stats=stats)
        if self._analyze is not None:
            try:
                report = self._analyze(manifest, self._records)
                _dump(self.path / "report.json", report)
                if self._render is not None:
                    (self.path / "report.md").write_text(self._render(report), encoding="utf-8")
            except OSError as exc:
                logger.error("bundle %s: report write failed: %s", self.bundle_id, exc)
                self.write_errors += 1
            except Exception:  # noqa: BLE001 - a report bug must not lose the records
                logger.exception("bundle %s: analysis failed; records are intact",
                                 self.bundle_id)
        final = self._dir_root / self.bundle_id
        try:
            os.replace(self.path, final)
            self.path = final
        except OSError as exc:
            logger.error("bundle %s: could not finalize (%s); left at %s",
                         self.bundle_id, exc, self.path)
            self._final_path = str(self.path)
        self._records = []

    # -- helpers -----------------------------------------------------------

    def _sync(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_sync >= self._fsync_every:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._last_sync = now

    def _write_manifest(self, *, status: str, stats: dict[str, Any]) -> dict[str, Any]:
        assert self.path is not None
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "bundle_id": self.bundle_id,
            "status": status,
            "session": self._session,
            "profile": {
                "name": self._profile.name,
                "sha256": self._profile.sha256,
                "source": self._profile.source,
                "pre_trigger_sec": self._profile.buffer.pre_trigger_sec,
                "post_trigger_sec": self._profile.buffer.post_trigger_sec,
                "stop_criteria": self._profile.to_dict()["stop"],
                "co_hosted_roles": sorted(self._profile.co_hosted_roles),
                "text": self._profile.text,
                "topics": [
                    {"name": t.name, "type": t.type, "role": t.role,
                     "required": t.required, "expected_hz": t.expected_hz,
                     "stale_after_sec": t.stale_after_sec, "store_max_hz": t.store_max_hz,
                     "fields": list(t.fields)}
                    for t in self._profile.topics
                ],
            },
            "triggers": self.triggers,
            "pre_window": self.pre_window,
            "topic_status": self._topic_status(),
            "recorder_stats": stats,
            "writer": {"records_written": self.records_written,
                       "write_errors": self.write_errors,
                       "first_write_error": self.first_write_error},
            "finalized_wall_ns": time.time_ns() if status != "capturing" else None,
        }
        try:
            _dump(self.path / "manifest.json", manifest)
        except OSError as exc:
            logger.error("bundle %s: manifest write failed: %s", self.bundle_id, exc)
            self.write_errors += 1
        return manifest


def load_bundle(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Read ``manifest.json`` and ``records.jsonl`` from a bundle directory.

    Tolerates a torn final line (recorder killed mid-write). Returns
    ``(manifest, records, read_info)``; ``read_info`` says what was repaired.
    """
    p = Path(path)
    info: dict[str, Any] = {"path": str(p), "partial_dir": p.name.endswith(".partial"),
                            "torn_lines": 0, "manifest_missing": False}
    try:
        manifest = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {"schema": MANIFEST_SCHEMA, "status": "unknown"}
        info["manifest_missing"] = True
    records: list[dict[str, Any]] = []
    rp = p / "records.jsonl"
    if rp.exists():
        with rp.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    info["torn_lines"] += 1
    if info["partial_dir"] and manifest.get("status") in ("capturing", "unknown"):
        manifest = {**manifest, "status": "interrupted_unfinalized"}
    return manifest, records, info
