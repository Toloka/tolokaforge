"""The agent-visible workspace-walk contract, one place.

Owns the recipe every grading path takes for reading back the agent's own
filesystem: walk a bound root, skip symlinks and non-files, UTF-8-decode
each file (bytes-in-text-out — binary contents drop out), and label each
kept entry by its logical ``/env/fs/agent-visible/<rel>`` path.

Also owns the *exclusion policy*: five directory basenames whose presence
anywhere along a file's parent path drops the file from the walk. The list
is deliberately narrow — SCM metadata (``.git``), tool caches
(``node_modules``, ``.venv``, ``.next``), and JS-ecosystem build outputs
(``dist``) — because a coding-harness trial that leaves these unfiltered
carries hundreds of megabytes of tool-owned bytes on the grading wire, none
of which any jsonpath assertion has ever addressed.

Purity: stdlib + logging only. No reach into ``tolokaforge.runner``,
``tolokaforge.grader``, or ``tolokaforge.core.grading.substrate_live`` —
locked by the ``filesystem-view-purity`` contract in ``.importlinter``.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = [
    "AGENT_VISIBLE_EXCLUDES",
    "is_excluded_rel_path",
    "read_agent_visible_filesystem",
]

logger = logging.getLogger(__name__)


AGENT_VISIBLE_EXCLUDES: frozenset[str] = frozenset(
    {".git", ".venv", "node_modules", "dist", ".next"}
)
"""Directory basenames excluded from the agent-visible walk at any depth.

Basename-based, not path-prefix-based: a nested ``packages/foo/node_modules/``
is excluded just like a root ``node_modules/``. A file (not directory) whose
leaf equals an excluded name is NOT excluded — the check runs on parent
components only, so :func:`read_agent_visible_filesystem` and
:func:`is_excluded_rel_path` agree at every emitted path.
"""


def is_excluded_rel_path(rel: str) -> bool:
    """True when any PARENT component of ``rel`` (POSIX-style) is in
    :data:`AGENT_VISIBLE_EXCLUDES`.

    Parents only, not leaf — exclusion prunes SUBTREES rooted at excluded
    directory names. A standalone file whose leaf equals an excluded name
    (``"dist"`` at root, ``"packages/foo/.git"`` as a file) is NOT refused:
    the walker also emits it. A file UNDER an excluded dir
    (``".git/HEAD"``, ``"packages/foo/node_modules/bar.js"``) IS refused.

    ``rel`` is workspace-relative POSIX; a leading slash is tolerated. The
    check is component-wise — ``"my_dist_folder"`` shares no component with
    ``AGENT_VISIBLE_EXCLUDES`` and is kept.
    """
    parts = rel.strip("/").split("/")
    return any(part in AGENT_VISIBLE_EXCLUDES for part in parts[:-1])


def read_agent_visible_filesystem(root: Path) -> dict[str, str]:
    """Snapshot the agent-visible tree under ``root`` as a jsonpath-ready dict.

    Returns ``{'/env/fs/agent-visible/<rel>': content}`` — the shape the
    composite grading substrate consumes via
    :meth:`~tolokaforge.core.grading.substrate.GradingSubstrate.filesystem_state`.
    Every non-symlink, UTF-8-decodable file below ``root`` whose parent path
    does not pass through an :data:`AGENT_VISIBLE_EXCLUDES` component is
    included; symlinks, binary files, and files under excluded subtrees are
    skipped.

    An absent or non-directory ``root`` returns ``{}`` (not an error) — a
    trial whose adapter emitted no workspace surface has nothing to read
    back, and a filesystem-only trial that runs without a workspace grades
    on an empty view. ``OSError`` on a per-file read is logged and the file
    is skipped; the walk does not fail loud on a read that races with a
    container write.
    """
    fs: dict[str, str] = {}
    if not root.is_dir():
        return fs
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in AGENT_VISIBLE_EXCLUDES for part in rel.parts[:-1]):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            logger.warning(f"read_agent_visible_filesystem: could not read {path}: {exc}")
            continue
        fs[f"/env/fs/agent-visible/{rel.as_posix()}"] = content
    return fs
