"""Acceptance test — fraction of harness-touching commits that ship as
pure DATA (Bucket A) vs. CODE (Bucket B).

Measures data-vs-code migration progress on the coding-harness surface.
The higher the Bucket-A fraction, the more of the surface is expressible
as YAML edits an operator can make without an adapter release.

See :mod:`automation.harness_bucket_classifier` for the classifier."""

from __future__ import annotations

import json
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
                "pr": c.pr,
                "date": c.date,
                "subject": c.subject,
                "bucket": cls.bucket.value,
                "reason": cls.reason,
                "adapter_paths": list(cls.adapter_paths),
                "touched_files": list(c.touched),
            }
        )
    entries.sort(key=lambda e: (e["date"], e["pr"] or "", e["subject"]))
    return {
        "bucket_a_count": sum(1 for e in entries if e["bucket"] == "A"),
        "bucket_b_count": sum(1 for e in entries if e["bucket"] == "B"),
        "commits": entries,
    }


def test_replay_matches_baseline(canon_snapshot, pytestconfig) -> None:
    """Live replay of every harness-touching commit reachable from HEAD.

    Identity keys off PR number (stable across squash-merge / rebase),
    not commit SHA and not commit date. A history rewrite that changes
    SHAs but preserves the same PR set is a no-op for this metric;
    a genuine regression is a PR present in the baseline that no
    HEAD-reachable commit still names.

    The full metric is regenerable via ``--update-canon`` for the
    record; only the PR-set membership is asserted, so environmental
    differences on committer date or ordering do not flip the lane
    red on an unchanged set of PRs.
    """
    metric = _build_metric()
    # Captured by pytest and surfaced on the CI log; carries the current
    # metric even on green so passing runs still report the counts.
    print(
        f"\n[harness-registry replay]  Bucket A: {metric['bucket_a_count']}  |  "
        f"Bucket B: {metric['bucket_b_count']}  |  "
        f"total: {len(metric['commits'])}"
    )
    snapshot = canon_snapshot("harness_registry_replay")
    if pytestconfig.getoption("--update-canon"):
        snapshot.assert_match(metric, "metric.json")
        return
    baseline = json.loads((snapshot.snapshot_dir / "metric.json").read_text())
    baseline_prs = {e["pr"] for e in baseline["commits"] if e.get("pr")}
    metric_prs = {e["pr"] for e in metric["commits"] if e.get("pr")}
    missing_prs = baseline_prs - metric_prs
    assert not missing_prs, (
        f"baseline names PR(s) unreachable from HEAD ({sorted(missing_prs)}): "
        "commits genuinely removed from history rather than just rebased."
    )


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
