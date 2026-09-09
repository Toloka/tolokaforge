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

Two orchestration entry points, one walker: :func:`iter_agent_visible_rel_paths`
yields POSIX rel-paths one at a time (peak memory: one file's bytes), and
:func:`read_agent_visible_filesystem` accumulates the same walk into a
``{'/env/fs/agent-visible/<rel>': content}`` dict. The dict form is for the
runner-side non-harness state factory; the iterator is for the
substrate-service enumeration RPC, where retaining every file's content
just to throw it away would balloon servicer memory on coding-task
workspaces.

Purity: stdlib + logging only. No reach into ``tolokaforge.runner``,
``tolokaforge.grader``, or ``tolokaforge.core.grading.substrate_live`` —
locked by the ``filesystem-view-purity`` contract in ``.importlinter``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

__all__ = [
    "AGENT_VISIBLE_EXCLUDES",
    "is_excluded_rel_path",
    "iter_agent_visible_rel_paths",
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
components only, so :func:`iter_agent_visible_rel_paths`,
:func:`read_agent_visible_filesystem`, and :func:`is_excluded_rel_path`
agree at every emitted path.
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


def _iter_agent_visible_entries(root: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(rel_posix, content)`` for every non-symlink UTF-8-decodable
    file below ``root`` that passes the :data:`AGENT_VISIBLE_EXCLUDES`
    policy. Single-pass walker both public entry points share.

    A caller that only needs paths iterates and discards ``content``: peak
    memory stays at one file's bytes (Python reclaims the string once the
    consumer moves on). A caller building the ``{rel: content}`` dict
    accumulates as usual.
    """
    if not root.is_dir():
        return
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
            logger.warning(f"agent-visible filesystem: skipped {path}: {exc}")
            continue
        yield rel.as_posix(), content


def iter_agent_visible_rel_paths(root: Path) -> Iterator[str]:
    """Yield POSIX rel-paths of every non-symlink UTF-8-decodable file
    below ``root`` that passes the :data:`AGENT_VISIBLE_EXCLUDES` policy.

    Path-only view of the same walk :func:`read_agent_visible_filesystem`
    materialises: identical filter, identical exclusion policy. Reads each
    candidate to enforce the UTF-8-decodable filter, then discards content
    — peak servicer memory during enumeration is one file's bytes, not the
    workspace total. Used by
    :meth:`tolokaforge.runner.substrate_service.SubstrateServicer.ListFilesystemDir`.

    Yield order matches the underlying :meth:`pathlib.Path.rglob` walk;
    callers that need a stable wire order (the substrate-service RPC does)
    sort the emitted iterator.
    """
    for rel_posix, _content in _iter_agent_visible_entries(root):
        yield rel_posix


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
    return {
        f"/env/fs/agent-visible/{rel_posix}": content
        for rel_posix, content in _iter_agent_visible_entries(root)
    }
