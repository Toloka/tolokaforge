"""Acceptance test — fraction of auto-integrations that would land Bucket A
under the ADR-0030 widened design.

See ADR-0030 § "What success looks like" for the target this test
reports against, and ``tools/automation/src/automation/bucket_classifier.py``
for the Bucket A / Bucket B classifier.

The snapshot check is frozen at ``_FROZEN_UNTIL`` — a fixed historical
cutoff — so the metric measures **classifier stability** rather than
history size. A new integration commit does not touch the snapshot;
only a classifier-logic change (a new bucket rule, a moved allow-list
path) does. See issue #1531. The live report (the ``print`` below)
still walks to ``HEAD`` so every CI run surfaces the current acceptance-
target fraction on the log.

Bump ``_FROZEN_UNTIL`` when the classifier logic changes and the
historical postures should reflect that; regenerate the snapshot in
the same commit."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from automation.bucket_classifier import classify_paths

from tests.canonical._models_wheel_replay.git_walk import (
    enumerate_integration_commits,
)

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Frozen replay cutoff — last integration commit at the time the freeze
#: landed. New integrations reachable from ``HEAD`` above this SHA are
#: reported live (see the ``print`` in :func:`test_replay_matches_baseline`)
#: but do not enter the pinned snapshot. Bump only for a classifier-logic
#: change (see the module docstring).
_FROZEN_UNTIL = "e8f837d760111f70afeb41a5dbe6302cdc3c7f99"


def _build_metric(until: str | None = None) -> dict[str, Any]:
    commits = enumerate_integration_commits(_REPO_ROOT, until=until)
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
    """Snapshot check is frozen at ``_FROZEN_UNTIL``; live report walks to ``HEAD``."""
    frozen = _build_metric(until=_FROZEN_UNTIL)
    live = _build_metric()
    # Captured by pytest and surfaced on the CI log; carries the LIVE
    # (HEAD) counts so passing runs still report the current fraction.
    print(
        f"\n[models-wheel replay]  live@HEAD  Bucket A: {live['bucket_a_count']}  |  "
        f"Bucket B: {live['bucket_b_count']}  |  "
        f"total: {len(live['integrations'])}    "
        f"(snapshot frozen at {_FROZEN_UNTIL[:12]}: "
        f"A={frozen['bucket_a_count']} B={frozen['bucket_b_count']})"
    )
    canon_snapshot("models_wheel_replay").assert_match(frozen, "metric.json")


def test_git_walk_returns_expected_shape() -> None:
    """Sanity-check the git subprocess contract independently of the classifier."""
    commits = enumerate_integration_commits(_REPO_ROOT)
    assert commits, "expected at least one `^integrate: ` commit reachable from HEAD"
    for commit in commits:
        assert commit.sha, "sha must be non-empty"
        assert commit.touched, f"{commit.sha}: touched files must be non-empty"
        date_msg = f"{commit.sha}: date {commit.date!r} does not match YYYY-MM-DD"
        assert _DATE_RE.match(commit.date), date_msg
