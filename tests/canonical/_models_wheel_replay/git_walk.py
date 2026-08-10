"""Thin subprocess helpers around ``git log`` / ``git show`` for the
models-wheel replay acceptance test.

The helpers surface every ``integrate: <slug> (#N)`` commit reachable
from ``HEAD`` together with its touched-file set. The classifier in
``classifier.py`` consumes the touched-file set; this module has no
notion of Bucket A / Bucket B.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SUBJECT_PREFIX = "^integrate: "
_MODEL_RE = re.compile(r"^integrate: (\S+)")
_PR_RE = re.compile(r"\(#(\d+)\)$")
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"%H{_FIELD_SEP}%cs{_FIELD_SEP}%s"


@dataclass(frozen=True)
class IntegrationCommit:
    """A single ``integrate: <slug> (#N)`` commit and the files it touched.

    ``date`` is the committer-date (``%cs`` — short ISO ``YYYY-MM-DD``):
    author-date is unstable under rebase / cherry-pick, whereas
    committer-date is when the commit actually landed on the branch,
    which is what the replay measures. ``pr`` is ``None`` iff the
    subject lacks a trailing ``(#N)`` group. ``touched`` is sorted and
    deduped.
    """

    sha: str
    subject: str
    date: str
    pr: int | None
    model: str
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


def enumerate_integration_commits(repo_root: Path) -> list[IntegrationCommit]:
    """Return every ``^integrate: `` commit reachable from ``HEAD``.

    Chronological, oldest-first (``git log --reverse`` over committer-
    date order). One ``git log`` call across all matching commits, plus
    one ``git show --name-only`` per commit for the touched-file set.
    Raises ``RuntimeError`` if ``git`` is not on ``PATH`` or if any git
    subprocess exits non-zero (e.g. ``repo_root`` is not a git working
    tree).
    """
    if shutil.which("git") is None:
        raise RuntimeError("git is not on PATH; enumerate_integration_commits requires git")
    log_output = _run_git(
        repo_root,
        [
            "log",
            "HEAD",
            f"--grep={_SUBJECT_PREFIX}",
            f"--format={_LOG_FORMAT}",
            "--reverse",
        ],
    )
    commits: list[IntegrationCommit] = []
    for line in log_output.splitlines():
        if not line:
            continue
        sha, date, subject = line.split(_FIELD_SEP, 2)
        model_match = _MODEL_RE.match(subject)
        if model_match is None:
            raise RuntimeError(
                f"commit {sha}: subject '{subject}' matched the log filter "
                "but not the model-slug regex — the '^integrate: ' convention "
                "may have changed"
            )
        pr_match = _PR_RE.search(subject)
        pr = int(pr_match.group(1)) if pr_match is not None else None
        touched_output = _run_git(repo_root, ["show", "--name-only", "--format=", sha])
        touched = tuple(sorted({p for p in touched_output.splitlines() if p}))
        commits.append(
            IntegrationCommit(
                sha=sha,
                subject=subject,
                date=date,
                pr=pr,
                model=model_match.group(1),
                touched=touched,
            )
        )
    return commits
