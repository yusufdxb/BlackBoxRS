"""Replay Lab never reads the wall clock, randomness or ids for a result."""

from __future__ import annotations

import re
from pathlib import Path

import blackboxrs.lab as lab

FORBIDDEN = re.compile(r"\b(time\.time|time\.monotonic|time\.perf_counter|datetime\.now|"
                       r"datetime\.utcnow|random\.|uuid|os\.urandom|Clock\.now)\b")


def test_no_clock_or_randomness_in_the_lab_package():
    pkg = Path(lab.__file__).parent
    flight = pkg.parent / "flight"
    # the flight modules the replay drives: recorder core, analyzer, records
    reused = [flight / n for n in ("core.py", "analysis.py", "records.py")]
    hits = []
    for f in sorted(pkg.glob("*.py")) + reused:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if FORBIDDEN.search(code):
                hits.append(f"{f.name}:{n}: {line.strip()}")
    assert hits == []


def test_only_the_pacer_sleeps():
    pkg = Path(lab.__file__).parent
    users = sorted(f.name for f in pkg.glob("*.py") if "time.sleep" in f.read_text())
    assert users == ["clock.py"]
