"""Unit tests for the resource_threshold preflight check."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


from blackboxrs.prevention.checks.resource_threshold import run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_psutil(cpu=50.0, mem_percent=60.0, disk_percent=70.0, disk_free=20.0):
    """Build a minimal psutil-lookalike namespace."""
    disk_obj = SimpleNamespace(
        percent=disk_percent,
        free=int(disk_free * 1024 ** 3),
    )
    mem_obj = SimpleNamespace(percent=mem_percent)

    ns = SimpleNamespace()
    ns.cpu_percent = lambda interval=1.0: cpu
    ns.virtual_memory = lambda: mem_obj
    ns.disk_usage = lambda path="/": disk_obj
    return ns


def _patch_psutil(fake):
    return patch("blackboxrs.prevention.checks.resource_threshold.psutil", fake,
                 create=True)


# run() imports psutil lazily; patch sys.modules so it resolves to the fake.
def _run_with_fake(params, fake):
    import sys
    orig = sys.modules.get("psutil")
    sys.modules["psutil"] = fake
    try:
        return run(params)
    finally:
        if orig is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = orig


# ---------------------------------------------------------------------------
# error: invalid metric
# ---------------------------------------------------------------------------


def test_error_on_invalid_metric():
    status, msg = _run_with_fake({"metric": "bad_metric"}, _fake_psutil())
    assert status == "error"
    assert "bad_metric" in msg


def test_error_on_missing_metric():
    status, msg = _run_with_fake({}, _fake_psutil())
    assert status == "error"
    assert "metric" in msg.lower()


# ---------------------------------------------------------------------------
# cpu_percent
# ---------------------------------------------------------------------------


def test_cpu_pass_below_max():
    status, msg = _run_with_fake(
        {"metric": "cpu_percent", "max": 90}, _fake_psutil(cpu=50.0)
    )
    assert status == "pass"
    assert "cpu_percent" in msg


def test_cpu_fail_above_max():
    status, msg = _run_with_fake(
        {"metric": "cpu_percent", "max": 80}, _fake_psutil(cpu=95.0)
    )
    assert status == "fail"
    assert "95.00" in msg or "exceeds" in msg


# ---------------------------------------------------------------------------
# memory_percent
# ---------------------------------------------------------------------------


def test_memory_pass():
    status, _ = _run_with_fake(
        {"metric": "memory_percent", "max": 90}, _fake_psutil(mem_percent=60.0)
    )
    assert status == "pass"


def test_memory_fail():
    status, msg = _run_with_fake(
        {"metric": "memory_percent", "max": 50}, _fake_psutil(mem_percent=80.0)
    )
    assert status == "fail"


# ---------------------------------------------------------------------------
# disk_percent
# ---------------------------------------------------------------------------


def test_disk_percent_pass():
    status, _ = _run_with_fake(
        {"metric": "disk_percent", "max": 90}, _fake_psutil(disk_percent=60.0)
    )
    assert status == "pass"


def test_disk_percent_fail():
    status, msg = _run_with_fake(
        {"metric": "disk_percent", "max": 50}, _fake_psutil(disk_percent=80.0)
    )
    assert status == "fail"
    assert "exceeds" in msg


# ---------------------------------------------------------------------------
# disk_free_gb
# ---------------------------------------------------------------------------


def test_disk_free_gb_pass():
    # min threshold: at least 5 GiB free, we have 20 GiB
    status, _ = _run_with_fake(
        {"metric": "disk_free_gb", "min": 5}, _fake_psutil(disk_free=20.0)
    )
    assert status == "pass"


def test_disk_free_gb_fail():
    # min threshold: at least 50 GiB free, we only have 10 GiB
    status, msg = _run_with_fake(
        {"metric": "disk_free_gb", "min": 50}, _fake_psutil(disk_free=10.0)
    )
    assert status == "fail"
    assert "below" in msg
