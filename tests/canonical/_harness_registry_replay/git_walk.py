"""Thin subprocess helpers around ``git log`` / ``git diff-tree`` for the
harness-registry replay acceptance test.

The helpers surface every commit reachable from ``HEAD`` that touched
ANY file on the harness surface — code, data, docs, examples, or
snapshots — together with its full touched-file set. The classifier in
:mod:`automation.harness_bucket_classifier` decides whether each is
Bucket A or Bucket B; this module does not.

The path-based filter (over a subject-based one) is deliberate. Unlike
the models auto-integrations (which land under a stable
``^integrate: `` subject prefix), harness commits are heterogeneous:
some carry ``harness`` in the subject (PR #1083), some do not
(PR #1159 — ``fix(tbench): opencode ANTHROPIC_BASE_URL /v1 + kimi-code
multi-turn docs`` is a harness commit by any reasonable read but has
no ``harness`` token in the subject). A path-based filter is the only
one that captures both without an ever-growing subject regex.

Same subprocess primitives + field separator as
:mod:`tests.canonical._models_wheel_replay.git_walk`; the differences
are the path-based filter (paragraph above) and the return shape
(harness commits are heterogeneous, so there is no "one integration =
one model slug" invariant).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PR_RE = re.compile(r"\(#(\d+)\)$")
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_FIELD_SEP}%cs{_FIELD_SEP}%s"

# The path arguments passed to ``git log -- <paths>``. Any commit that
# touched at least one file under one of these paths is enumerated. The
# classifier then decides Bucket A / Bucket B on the FULL touched-file
# set (including any files outside these paths that the same commit
# touched — a mixed-surface commit correctly lands in Bucket B).
#
# Kept deliberately tight so commits that touched other bits of the
# tbench adapter (``adapter.py``, ``task_parser.py``, etc.) do not
# dilute the metric. The surface is exactly the harness-touching files:
# the ``tolokaforge_coding_harnesses`` package and its shipped data, the
# compose-synthesis file that hosts the skill-delivery logic, the adapter
# README that documents the harness surface, the two harness-mode
# canonical snapshot trees, the harness-mode examples, and the four
# harness ADRs.
#
# The two ``tolokaforge_adapter_terminal_bench`` paths are anchors for
# the commits that landed before ADR-0036 moved the surface out of the
# adapter. Dropping them would make the replay skip that history while
# still passing — a green lane that has stopped measuring.
HARNESS_SURFACE_PATHS: tuple[str, ...] = (
    "tolokaforge_coding_harnesses/src/tolokaforge_coding_harnesses/",
    "external_adapters/tolokaforge-adapter-terminal-bench/src/"
    "tolokaforge_adapter_terminal_bench/data/",
    "external_adapters/tolokaforge-adapter-terminal-bench/src/"
    "tolokaforge_adapter_terminal_bench/harness/",
    "external_adapters/tolokaforge-adapter-terminal-bench/src/"
    "tolokaforge_adapter_terminal_bench/compose_synthesis.py",
    "external_adapters/tolokaforge-adapter-terminal-bench/README.md",
    "tests/canonical/snapshots/tbench_echo_hello_harness/",
    "tests/canonical/snapshots/tbench_echo_hello_skills_harness/",
    "examples/terminal_bench/",
    "docs/adr/0033-external-harness-registry.md",
    "docs/adr/0034-external-harness-plugin-discovery.md",
    "docs/adr/0036-tolokaforge-coding-harnesses-split.md",
    "docs/adr/0037-runtime-gateway-as-harness-data.md",
)


@dataclass(frozen=True)
class HarnessCommit:
    """A single harness-surface-touching commit and the files it touched.

    ``date`` is the committer-date (``%cs`` — short ISO ``YYYY-MM-DD``):
    author-date is unstable under rebase / cherry-pick, whereas
    committer-date is when the commit actually landed on the branch,
    which is what the replay measures. ``pr`` is ``None`` iff the
    subject lacks a trailing ``(#N)`` group. ``touched`` is sorted and
    deduped and includes EVERY file the commit changed — not just the
    ones under :data:`HARNESS_SURFACE_PATHS`.
    """

    sha: str
    subject: str
    date: str
    pr: int | None
    touched: tuple[str, ...]


def _run_git(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}) "
            f"in {repo_root}: {result.stderr.strip()}"
        )
    return result.stdout


def enumerate_harness_commits(repo_root: Path) -> list[HarnessCommit]:
    """Return every commit reachable from ``HEAD`` that touched the harness surface.

    Chronological, oldest-first (``git log --reverse`` over committer-date
    order). One ``git log`` call across all matching commits, plus one
    ``git diff-tree --no-commit-id --name-only -r`` per commit for the
    FULL touched-file set — ``diff-tree -r`` lists paths across all
    parents on a merge commit, whereas ``git show --name-only`` reports
    a combined diff that returns the empty set for a conflict-free merge
    and would silently misclassify.

    Raises ``RuntimeError`` if ``git`` is not on ``PATH``, if any git
    subprocess exits non-zero (e.g. ``repo_root`` is not a git working
    tree), or if the touched-file set for a matching commit is empty.
    """
    if shutil.which("git") is None:
        raise RuntimeError("git is not on PATH; enumerate_harness_commits requires git")
    # ``--no-merges`` skips merge commits — those touch no files themselves,
    # and ``git diff-tree -r`` on a conflict-free merge returns an empty set
    # that would misclassify. The changes a merge brings in are already
    # counted as their own individual commits on the branch that merged.
    log_output = _run_git(
        repo_root,
        [
            "log",
            "HEAD",
            "--no-merges",
            f"--format={_LOG_FORMAT}",
            "--reverse",
            "--",
            *HARNESS_SURFACE_PATHS,
        ],
    )
    commits: list[HarnessCommit] = []
    for line in log_output.splitlines():
        if not line:
            continue
        sha, date, subject = line.split(_FIELD_SEP, 2)
        pr_match = _PR_RE.search(subject)
        pr = int(pr_match.group(1)) if pr_match is not None else None
        # ``git show --format= --name-only`` handles both regular commits
        # (which ``diff-tree -r`` also handles) AND root commits with no
        # parent (which ``diff-tree`` returns nothing for). A conflict-free
        # merge is already excluded above via ``--no-merges``.
        touched_output = _run_git(repo_root, ["show", "--format=", "--name-only", sha])
        touched = tuple(sorted({p for p in touched_output.splitlines() if p}))
        if not touched:
            raise RuntimeError(
                f"commit {sha}: 'git show --name-only' returned an empty "
                "touched-file set for a matching commit — implausible and "
                "worth failing loud on"
            )
        commits.append(
            HarnessCommit(
                sha=sha,
                subject=subject,
                date=date,
                pr=pr,
                touched=touched,
            )
        )
    return commits
