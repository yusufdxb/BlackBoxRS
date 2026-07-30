"""Pin the bounded public semantics of the telemetry-health guard."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
REPORT = REPO_ROOT / "docs/EXPERIENCE_DERIVED_RUNTIME_CONTRACTS.md"
BOUNDARY = REPO_ROOT / "docs/TELEMETRY_RUNTIME_GUARD.md"

MAXIMUM_CLAIM = (
    "In a bounded local ROS 2 evaluation, BlackBoxRS derived a "
    "telemetry-health contract from genuine GO2 bag evidence and prevented "
    "selected semantic arrival-liveness failures while admitting selected "
    "nearby healthy conditions. The hardened guard rejected topic remapping, "
    "mismatched declared context labels, trusted-evidence tampering, and "
    "unsupported dependent-process escape within its documented Linux process "
    "model. It enforces exact topic type and compatible QoS. Thresholds remain "
    "session-derived and require multi-session and live-robot validation."
)


def test_readme_and_boundary_use_the_bounded_maximum_claim():
    assert MAXIMUM_CLAIM in README.read_text(encoding="utf-8")
    assert MAXIMUM_CLAIM in BOUNDARY.read_text(encoding="utf-8")


def test_public_docs_do_not_claim_actual_context_or_exact_qos_attestation():
    public_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (README, REPORT, BOUNDARY)
    ).lower()
    for forbidden in (
        "runtime-context mismatch",
        "exact runtime context",
        "actual runtime-context attestation",
        "exact type and qos",
        "exact qos identity",
    ):
        assert forbidden not in public_text


def test_different_actual_environment_with_same_label_is_outside_guarantee():
    boundary = " ".join(
        BOUNDARY.read_text(encoding="utf-8").split()
    )
    assert (
        "Compatible traffic in another ROS domain can qualify when the "
        "caller supplies the same label"
    ) in boundary
    assert "outside this guard's guarantee" in boundary
