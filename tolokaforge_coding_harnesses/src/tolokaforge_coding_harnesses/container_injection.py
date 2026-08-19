"""Writing a harness's config files into an already-running container.

This repo's own path builds a trial container, so a
:attr:`~._registry.HarnessSpec.config_files` entry becomes a ``printf`` in the
assembled command. A runtime that instead attaches to a container someone else
started has no such moment, and its only other channel — environment variables
— cannot deliver a file at all.

:class:`ContainerFileInjector` is that missing channel, stated as a contract so
the transport is swappable: :class:`DockerExecInjector` ships here, and a
``kubectl exec`` implementation for a cluster-hosted run implements the same
three lines. The whole premise is that a resolved credential travels from
process memory down a container's stdin and lands nowhere else — not in a temp
file, not in an argument list, not in an environment block.

Stdlib only, and no engine import: the runtimes this exists for are the ones
that do not install one.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "ContainerFileInjector",
    "ContainerInjectionError",
    "DockerExecInjector",
    "FileSpec",
]


@dataclass(frozen=True)
class FileSpec:
    """One file to write inside a container."""

    container_path: str
    """Absolute, and already path-resolved: no ``${HOME}`` / ``${CONFIG_HOME}``
    construct may survive here. The caller runs this package's
    :class:`~.protocols.PathResolver` first. An injector expands nothing — the
    value reaches the container as a quoted positional argument, which writes a
    literal directory named ``${HOME}`` rather than failing."""

    content: str = field(repr=False)
    """File content, written byte-for-byte as UTF-8.

    ``repr=False`` is load-bearing rather than cosmetic: this field holds a
    resolved credential, and a generated ``__repr__`` would print it into
    tracebacks, pytest assertion output, and any ``logger.debug("%s", spec)``.
    An :meth:`ContainerFileInjector.inject` traceback is exactly what a run
    bundle captures."""

    mode: int = 0o600
    """Permission bits the file carries. The CLI's own user reads it; group and
    other do not."""


@runtime_checkable
class ContainerFileInjector(Protocol):
    """Puts files inside a container that is already running."""

    def inject(self, container: str, files: Iterable[FileSpec]) -> None:
        """Write every file in *files* into *container*.

        Raises:
            ContainerInjectionError: one file did not land. The error names
                which one — an implementation that reports only that "something
                failed" leaves the caller with a container it cannot reason
                about.
        """
        ...


class ContainerInjectionError(RuntimeError):
    """One file failed to land, and this says which."""

    def __init__(self, container: str, container_path: str, returncode: int, stderr: str) -> None:
        self.container = container
        self.container_path = container_path
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"container {container!r}: writing {container_path!r} failed with exit "
            f"status {returncode}: {stderr.strip() or '<no stderr>'}"
        )


_WRITE_SCRIPT = 'mkdir -p "$(dirname "$1")" && touch "$1" && chmod "$2" "$1" && cat > "$1"'
"""Fixed literal, never built from a :class:`FileSpec`. The path and mode
arrive as ``$1`` / ``$2``, so a path carrying ``;`` or a space is a filename.

The order is the security property: ``touch`` then ``chmod`` then write means
the content only ever exists on disk under the requested mode. A ``chmod``
after the write would leave the credential world-readable for the length of the
copy. All four steps share one shell because a missing parent directory or an
unwritable target must fail *this* file's exec rather than truncate it
silently.
"""


class DockerExecInjector:
    """:class:`ContainerFileInjector` over ``docker exec``.

    Each exec is bounded by *timeout_s*: an unresponsive daemon or a container
    stuck in restart backoff produces no incremental output under
    ``capture_output``, so without a bound the provisioning call blocks forever
    instead of naming the file that never landed.
    """

    def __init__(self, docker_binary: str = "docker", timeout_s: float = 30.0) -> None:
        self._docker_binary = docker_binary
        self._timeout_s = timeout_s

    def inject(self, container: str, files: Iterable[FileSpec]) -> None:
        """Write each file in its own ``docker exec``.

        One exec per file rather than one for the batch: a batched write cannot
        tell the caller which path failed, and the failing path is the whole
        content of a useful error.
        """
        for spec in files:
            argv = [
                self._docker_binary,
                "exec",
                "-i",
                container,
                "sh",
                "-c",
                _WRITE_SCRIPT,
                "_",
                spec.container_path,
                f"{spec.mode:04o}",
            ]
            try:
                result = subprocess.run(
                    argv,
                    input=spec.content.encode("utf-8"),
                    capture_output=True,
                    check=False,
                    timeout=self._timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                # The exception's own `stderr` is deliberately not interpolated:
                # a wedged exec's captured output is the one place a resolved
                # credential could re-enter this message.
                raise ContainerInjectionError(
                    container=container,
                    container_path=spec.container_path,
                    returncode=-1,
                    stderr=f"docker exec did not return within {self._timeout_s}s",
                ) from exc
            if result.returncode != 0:
                raise ContainerInjectionError(
                    container=container,
                    container_path=spec.container_path,
                    returncode=result.returncode,
                    stderr=result.stderr.decode("utf-8", errors="replace"),
                )
