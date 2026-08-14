"""Acceptance test — fraction of harness-touching commits that ship as
pure DATA (Bucket A) vs. CODE (Bucket B).

Measures data-vs-code migration progress on the coding-harness surface.
The higher the Bucket-A fraction, the more of the surface is expressible
as YAML edits an operator can make without an adapter release.

See :mod:`automation.harness_bucket_classifier` for the classifier."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from automation.harness_bucket_classifier import classify_harness_paths

from tests.canonical._harness_registry_replay.git_walk import (
    enumerate_harness_commits,
)

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _build_metric() -> dict[str, Any]:
    commits = enumerate_harness_commits(_REPO_ROOT)
    entries = []
    for c in commits:
        cls = classify_harness_paths(c.touched)
        entries.append(
            {
                "sha": c.sha,
                "pr": c.pr,
                "date": c.date,
                "subject": c.subject,
                "bucket": cls.bucket.value,
                "reason": cls.reason,
                "adapter_paths": list(cls.adapter_paths),
                "touched_files": list(c.touched),
            }
        )
    entries.sort(key=lambda e: (e["date"], e["sha"]))
    return {
        "bucket_a_count": sum(1 for e in entries if e["bucket"] == "A"),
        "bucket_b_count": sum(1 for e in entries if e["bucket"] == "B"),
        "commits": entries,
    }


def test_replay_matches_baseline(canon_snapshot) -> None:
    """Live replay of every harness-touching commit reachable from HEAD."""
    metric = _build_metric()
    # Captured by pytest and surfaced on the CI log; carries the current
    # metric even on green so passing runs still report the counts.
    print(
        f"\n[harness-registry replay]  Bucket A: {metric['bucket_a_count']}  |  "
        f"Bucket B: {metric['bucket_b_count']}  |  "
        f"total: {len(metric['commits'])}"
    )
    canon_snapshot("harness_registry_replay").assert_match(metric, "metric.json")


def test_git_walk_returns_expected_shape() -> None:
    """Sanity-check the git subprocess contract independently of the classifier."""
    commits = enumerate_harness_commits(_REPO_ROOT)
    assert commits, (
        "expected at least one harness-touching commit reachable from HEAD "
        "(PR #1083 shipped the initial harness registry)"
    )
    for commit in commits:
        assert commit.sha, "sha must be non-empty"
        assert commit.touched, f"{commit.sha}: touched files must be non-empty"
        date_msg = f"{commit.sha}: date {commit.date!r} does not match YYYY-MM-DD"
        assert _DATE_RE.match(commit.date), date_msg
