"""Snapshot the agent-visible directory of a harness-mode trial container.

A trial driven under a coding-harness CLI runs inside its own container; the
runner service reaches into that container via a
:class:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper` when it
needs to inspect trial-produced state. This module owns that read.

The snapshot is the file tree the CLI could touch, keyed under the container's
own agent-visible directory (e.g. ``/work/factorial.py``), so a task pack that
grades under ``state_checks`` can assert against ``$.filesystem['/work/...']``
verbatim.

Reads and reads only — the container is not modified. Caps are per-file and
per-run to bound memory when the CLI dropped a large artifact into the tree.
"""

from __future__ import annotations

import base64
import io
import logging
import shlex
import tarfile
from collections.abc import Callable

DEFAULT_MAX_FILE_BYTES = 1_000_000
"""Files strictly larger than this are omitted from the snapshot.

State-checks operators (``contains`` / ``equals`` / ``path_glob``) all read
short-to-medium text; a multi-megabyte file the CLI dropped is almost certainly
a build artifact rather than the code the assertion is targeting."""

DEFAULT_MAX_TOTAL_BYTES = 100_000_000
"""Aggregate raw-bytes ceiling for one snapshot.

The tarball is emitted as base64 over the exec-wrapper's stdout channel, so the
in-memory footprint here is bounded by the receiver of ``_exec_sync`` — the
runner service — rather than by the container's own disk. Exceeding this cap
skips the snapshot rather than truncating it: an assertion that cannot resolve
its target is better graded as a miss than as a match on partial state."""


_TAR_SNAPSHOT_TIMEOUT_S = 60.0
"""Deadline for the single ``tar | base64`` exec inside the container.

Independent of the CLI's own budget; sized for a tree well under
:data:`DEFAULT_MAX_TOTAL_BYTES` on typical alpine / debian images."""

_SIZE_PROBE_TIMEOUT_S = 15.0
"""Deadline for the pre-snapshot ``du`` probe.

Faster than the tar exec — the probe is cheap and refuses expensive work when
the tree is too large to fit under the cap."""


BashExec = Callable[[str, float], str]
"""Callable shape the snapshot expects.

Matches :meth:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper.
_exec_sync`: ``(command, timeout_s) -> stdout``. Kept structural rather than
tied to the concrete class so tests can drive the helper with a stub."""


class _SnapshotAborted(Exception):
    """Signals a size cap or malformed exec output.

    Caught inside :func:`snapshot_container_filesystem` — a snapshot that
    cannot be completed becomes an empty result rather than a grading failure,
    because the assertion consumer already reports "path not found" and the
    trial's *other* grade components (transcript rules, LLM judge) can still
    reach a verdict."""


def snapshot_container_filesystem(
    exec_fn: BashExec,
    agent_visible_dir: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    logger: logging.Logger | None = None,
) -> dict[str, str]:
    """Snapshot the container's *agent_visible_dir* file tree via *exec_fn*.

    Runs three commands inside the container over *exec_fn*:

    1. ``du -sb`` for the tree — refuse if it exceeds *max_total_bytes* before
       any read is issued.
    2. ``tar --exclude=./.git -cf - . | base64`` — emit a base64-encoded
       tarball of the tree over stdout.
    3. Decoded in-process: each POSIX file member with a UTF-8-decodable body
       under *max_file_bytes* is included, keyed as
       ``<agent_visible_dir>/<relative posix path>``.

    Binary files and symlinks are skipped. ``.git/`` is skipped whole — its
    contents are rarely what a state-checks assertion targets, and it is often
    what pushes a tree over the per-run cap.

    Returns an empty dict rather than raising when:

    * *agent_visible_dir* does not exist inside the container;
    * the tree exceeds *max_total_bytes*;
    * the tarball is malformed (the receiver can then fall back — grading a
      subsequent trial or a same-trial retry — instead of crashing the RPC).

    A file skipped for its own reason (oversized, binary) is logged at
    ``warning`` and the rest of the snapshot proceeds.

    Args:
        exec_fn: bash-inside-container execution surface —
            ``(command, timeout_s) -> stdout``.
        agent_visible_dir: absolute container path the CLI edits in.
        max_file_bytes: per-file cap; files strictly larger are dropped.
        max_total_bytes: aggregate cap; the whole snapshot is dropped when
            the tree exceeds this.
        logger: optional structured logger for cap / skip records.
    """
    log = logger or logging.getLogger(__name__)

    try:
        _refuse_if_over_cap(exec_fn, agent_visible_dir, max_total_bytes, log)
    except _SnapshotAborted:
        return {}

    b64 = _tar_and_base64(exec_fn, agent_visible_dir, log)
    if not b64:
        return {}

    try:
        raw = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError) as exc:
        log.warning(
            "harness snapshot: base64 decode failed",
            extra={"agent_visible_dir": agent_visible_dir, "error": str(exc)},
        )
        return {}

    return _decode_tar(raw, agent_visible_dir, max_file_bytes, log)


def _refuse_if_over_cap(
    exec_fn: BashExec,
    agent_visible_dir: str,
    max_total_bytes: int,
    log: logging.Logger,
) -> None:
    """``du`` probe; raise :class:`_SnapshotAborted` when the tree exceeds *max_total_bytes*.

    Also treats an absent directory as a hard skip: the CLI's edit surface not
    existing at snapshot time is the same shape as a task pack declaring the
    wrong path — an empty result is honest, a snapshot from ``/`` would not be.
    """
    probe = (
        f"test -d {shlex.quote(agent_visible_dir)} "
        f"&& du -sb {shlex.quote(agent_visible_dir)} 2>/dev/null "
        "|| echo MISSING"
    )
    output = exec_fn(probe, _SIZE_PROBE_TIMEOUT_S).strip()
    if not output or output.startswith("MISSING") or "MISSING" in output.splitlines()[-1]:
        log.warning(
            "harness snapshot: agent-visible dir not found in container",
            extra={"agent_visible_dir": agent_visible_dir},
        )
        raise _SnapshotAborted
    try:
        total = int(output.splitlines()[-1].split()[0])
    except (ValueError, IndexError):
        # A busybox `du` variant emitting KB blocks reads as a positive small
        # integer, which lands us below the cap incorrectly; but the tar step
        # itself is separately budgeted, so the worst case is a slow-but-honest
        # snapshot rather than an unbounded one.
        return
    if total > max_total_bytes:
        log.warning(
            "harness snapshot: tree exceeds total cap; skipped",
            extra={
                "agent_visible_dir": agent_visible_dir,
                "bytes": total,
                "cap": max_total_bytes,
            },
        )
        raise _SnapshotAborted


def _tar_and_base64(
    exec_fn: BashExec,
    agent_visible_dir: str,
    log: logging.Logger,
) -> str:
    """Run ``tar`` piped through ``base64`` inside the container, return stdout.

    The ``cd`` puts tar's paths under ``./`` so :func:`_decode_tar` can strip
    the leading ``./`` cleanly; ``--exclude=./.git`` drops the SCM directory
    whole — its contents are rarely what an assertion targets and are often
    what pushes a tree past the cap.
    """
    cmd = (
        f"cd {shlex.quote(agent_visible_dir)} && tar --exclude=./.git -cf - . 2>/dev/null | base64"
    )
    try:
        return exec_fn(cmd, _TAR_SNAPSHOT_TIMEOUT_S)
    except Exception as exc:
        log.warning(
            "harness snapshot: tar exec failed",
            extra={"agent_visible_dir": agent_visible_dir, "error": str(exc)},
        )
        return ""


def _decode_tar(
    raw: bytes,
    agent_visible_dir: str,
    max_file_bytes: int,
    log: logging.Logger,
) -> dict[str, str]:
    """Iterate the tarball, emit ``{path: text}`` for eligible file members.

    Binary payloads (any byte that fails UTF-8) are skipped: state-checks
    operators can only match text, and passing base64 through here would give
    an assertion a value neither the author nor a human reader recognises.
    Symlinks are skipped: the vocabulary was not designed to expose whatever
    the link resolves to, whether or not that lands inside the tree.
    """
    root = agent_visible_dir.rstrip("/") or "/"
    result: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for member in tar:
                if not member.isfile() or member.issym() or member.islnk():
                    continue
                if member.size > max_file_bytes:
                    log.warning(
                        "harness snapshot: file exceeds per-file cap; skipped",
                        extra={
                            "path": member.name,
                            "size": member.size,
                            "cap": max_file_bytes,
                        },
                    )
                    continue
                stream = tar.extractfile(member)
                if stream is None:
                    continue
                payload = stream.read()
                try:
                    content = payload.decode("utf-8")
                except UnicodeDecodeError:
                    log.debug(
                        "harness snapshot: skipping non-UTF-8 file",
                        extra={"path": member.name},
                    )
                    continue
                rel = member.name.lstrip("./") or ""
                if not rel:
                    continue
                result[f"{root}/{rel}"] = content
    except tarfile.TarError as exc:
        log.warning(
            "harness snapshot: tar decode failed",
            extra={"agent_visible_dir": agent_visible_dir, "error": str(exc)},
        )
        return {}
    return result
