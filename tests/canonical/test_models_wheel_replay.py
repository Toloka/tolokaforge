"""Acceptance test — fraction of auto-integrations that would land Bucket A
under the ADR-0030 widened design.

See ADR-0030 § "What success looks like" for the target this test
reports against, and ``tests/canonical/_models_wheel_replay/classifier.py``
for the Bucket A / Bucket B classifier."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.canonical._models_wheel_replay.classifier import classify_paths
from tests.canonical._models_wheel_replay.git_walk import (
    enumerate_integration_commits,
)

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _build_metric() -> dict:
    commits = enumerate_integration_commits(_REPO_ROOT)
    integrations = []
    for c in commits:
        cls = classify_paths(c.touched)
        integrations.append(
            {
                "sha": c.sha,
                "pr": c.pr,
                "date": c.date,
                "model": c.model,
                "bucket": cls.bucket.value,
                "reason": cls.reason,
                "engine_paths": list(cls.engine_paths),
                "touched_files": list(c.touched),
            }
        )
    integrations.sort(key=lambda e: (e["date"], e["sha"]))
    return {
        "bucket_a_count": sum(1 for e in integrations if e["bucket"] == "A"),
        "bucket_b_count": sum(1 for e in integrations if e["bucket"] == "B"),
        "integrations": integrations,
    }


def test_replay_matches_baseline(canon_snapshot) -> None:
    """Live replay of every ``^integrate: `` commit reachable from HEAD."""
    metric = _build_metric()
    # Captured by pytest and surfaced on the CI log; carries the current
    # metric even on green so passing runs still report the counts.
    print(
        f"\n[models-wheel replay]  Bucket A: {metric['bucket_a_count']}  |  "
        f"Bucket B: {metric['bucket_b_count']}  |  "
        f"total: {len(metric['integrations'])}"
    )
    canon_snapshot("models_wheel_replay").assert_match(metric, "metric.json")


def test_git_walk_returns_expected_shape() -> None:
    """Sanity-check the git subprocess contract independently of the classifier."""
    commits = enumerate_integration_commits(_REPO_ROOT)
    assert commits, "expected at least one `^integrate: ` commit reachable from HEAD"
    for commit in commits:
        assert commit.sha, "sha must be non-empty"
        assert commit.touched, f"{commit.sha}: touched files must be non-empty"
        date_msg = f"{commit.sha}: date {commit.date!r} does not match YYYY-MM-DD"
        assert _DATE_RE.match(commit.date), date_msg
