"""Lightweight recurrence lookup over a local bundle store.

The recurrence module answers one question: *has this fingerprint been
seen before in any bundle on this host?* It is intentionally minimal:

* It does not cluster, classify, or recommend.
* It walks bundles directly. No index, no SQLite. The bundle directory
  layout is the source of truth.
* It only counts bundles whose ``window_end`` is before the current
  bundle's ``window_start``. Concurrent / future bundles are excluded.
* Failure is silent: an unreadable prior bundle is skipped, never
  raised. Recurrence is a "best-effort enrichment", not a
  load-bearing gate.

The store interface is just a directory of bundle directories, the
same shape as ``~/.blackboxrs/incidents``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .bundle import BundleReader
from .models import RecurrenceContext

logger = logging.getLogger(__name__)


# How many prior incident IDs to surface in the recurrence context.
# Older prior matches are counted but not listed.
_MAX_PRIOR_IDS = 5


def _iter_bundle_dirs(root: Path) -> Iterator[Path]:
    """Yield candidate bundle directories under *root* (best-effort)."""
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir():
                yield child
    except (FileNotFoundError, OSError):
        return


class FingerprintIndex:
    """Walks an incidents directory to answer recurrence queries.

    The index reads bundles lazily. The first call to :meth:`lookup`
    walks the directory and caches the result; subsequent calls reuse
    the cache. This keeps incident builds cheap even with hundreds of
    bundles.

    Attributes are intentionally minimal so the structure stays easy
    to swap for a SQLite-backed index when the store grows.
    """

    def __init__(
        self,
        incidents_dir: Path,
        *,
        host: str | None = None,
        before: datetime | None = None,
        exclude_id: str | None = None,
    ) -> None:
        self._dir = Path(incidents_dir)
        self._host = host
        self._before = before
        self._exclude_id = exclude_id
        # _by_fp is built lazily on first lookup.
        self._by_fp: dict[str, list[tuple[datetime, str]]] | None = None

    # -- public ----------------------------------------------------------

    def lookup(self, fingerprint_id: str) -> RecurrenceContext | None:
        """Return prior occurrences of *fingerprint_id* on this host.

        Returns ``None`` when there are zero prior matches; otherwise
        a :class:`RecurrenceContext` with count, the most recent up to
        :data:`_MAX_PRIOR_IDS` ids, and the timestamp of the most
        recent match.
        """
        if not fingerprint_id:
            return None
        index = self._ensure_index()
        matches = index.get(fingerprint_id, [])
        if not matches:
            return None
        # matches list is ordered earliest-first; take the latest tail
        # for ID surfacing.
        prior_ids = [bid for _, bid in matches[-_MAX_PRIOR_IDS:]]
        last_seen_at = matches[-1][0]
        return RecurrenceContext(
            fingerprint_id=fingerprint_id,
            prior_count=len(matches),
            prior_incident_ids=prior_ids,
            last_seen_at=last_seen_at,
        )

    # -- internal --------------------------------------------------------

    def _ensure_index(self) -> dict[str, list[tuple[datetime, str]]]:
        if self._by_fp is not None:
            return self._by_fp
        out: dict[str, list[tuple[datetime, str]]] = {}
        for bundle_dir in _iter_bundle_dirs(self._dir):
            if self._exclude_id and bundle_dir.name == self._exclude_id:
                continue
            entry = self._read_bundle(bundle_dir)
            if entry is None:
                continue
            inc, fp_id = entry
            if self._host and inc.host and inc.host != self._host:
                continue
            if self._before is not None and inc.window_end >= self._before:
                continue
            out.setdefault(fp_id, []).append((inc.window_end, inc.incident_id))
        # Sort each list earliest-first for stable lookup output.
        for fp_id in out:
            out[fp_id].sort(key=lambda r: r[0])
        self._by_fp = out
        return out

    @staticmethod
    def _read_bundle(bundle_dir: Path):
        try:
            reader = BundleReader(bundle_dir, strict=False)
            inc = reader.load_incident()
            fp = reader.load_fingerprint()
        except Exception:
            logger.debug("Skipping unreadable bundle: %s", bundle_dir,
                         exc_info=True)
            return None
        if fp is None:
            return None
        return inc, fp.fingerprint_id


def build_recurrence_lookup(
    incidents_dir: Path,
    *,
    host: str,
    before: datetime,
    exclude_id: str,
):
    """Convenience factory used by the incident builder.

    Returns a callable ``(fingerprint_id) -> RecurrenceContext | None``.
    """
    index = FingerprintIndex(
        incidents_dir, host=host, before=before, exclude_id=exclude_id,
    )
    return index.lookup


__all__ = ["FingerprintIndex", "build_recurrence_lookup"]
