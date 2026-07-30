#!/usr/bin/env python3
"""Validate the scrubbed telemetry summary and its source-code binding."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blackboxrs.prevention.bag_manifest import (  # noqa: E402
    BAG_MANIFEST_SCHEMA,
    BagManifest,
    compute_manifest_sha256,
)


DEFAULT_SUMMARY = (
    REPO_ROOT / "examples/telemetry_health/genuine_go2_evidence_summary.json"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GIT_OBJECT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PROTECTED_PATHS = (
    "blackboxrs",
    "scripts/characterize_go2_pose_telemetry.py",
    "scripts/run_telemetry_health_experiment.py",
    "scripts/telemetry_health_publisher.py",
    "scripts/validate_telemetry_thresholds.py",
    "examples/demo_runtime_telemetry_health.py",
    "tests",
)


def validate(summary_path: Path) -> dict:
    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    bag = summary["bag"]
    provenance = summary["provenance"]
    if bag["digest_schema"] != BAG_MANIFEST_SCHEMA:
        raise ValueError("public summary does not use bag-manifest-v2")
    manifest = BagManifest.model_validate(bag["manifest"])
    manifest_hash = compute_manifest_sha256(manifest)
    if bag["canonical_manifest_sha256"] != manifest_hash:
        raise ValueError("public summary canonical manifest hash is inconsistent")
    if bag["metadata_sha256"] != manifest.metadata.sha256:
        raise ValueError("public summary metadata hash is inconsistent")
    if bag["size_bytes"] != manifest.total_size:
        raise ValueError("public summary bag size is inconsistent")
    if not manifest.payloads:
        raise ValueError("public summary omits payload file hashes")
    if not all(_SHA256.fullmatch(record.sha256) for record in manifest.payloads):
        raise ValueError("public summary contains an invalid payload hash")

    validated_commit = provenance["validated_source_commit"]
    validated_tree = provenance["validated_source_tree"]
    if not _COMMIT.fullmatch(validated_commit):
        raise ValueError("public summary source commit is invalid")
    if not _GIT_OBJECT.fullmatch(validated_tree):
        raise ValueError("public summary source tree is invalid")
    actual_tree = _git("rev-parse", f"{validated_commit}^{{tree}}")
    if actual_tree != validated_tree:
        raise ValueError("public summary source tree does not match its commit")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", validated_commit, "HEAD"],
        cwd=REPO_ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("public summary source commit is not in HEAD history")
    source_diff = subprocess.run(
        ["git", "diff", "--quiet", validated_commit, "HEAD", "--", *_PROTECTED_PATHS],
        cwd=REPO_ROOT,
        check=False,
    )
    if source_diff.returncode != 0:
        raise ValueError(
            "protected source or tests changed after genuine-data validation"
        )
    if provenance.get("validated_worktree_clean") is not True:
        raise ValueError("public summary does not record a clean validation tree")

    serialized = json.dumps(summary, sort_keys=True)
    if "/home/" in serialized or "extended_5min/" in serialized:
        raise ValueError("public summary leaks a private absolute path")
    return {
        "validated_source_commit": validated_commit,
        "validated_source_tree": validated_tree,
        "canonical_manifest_sha256": manifest_hash,
        "payload_count": len(manifest.payloads),
    }


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path, nargs="?", default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    print(json.dumps(validate(args.summary), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
