"""Shared tool libraries delivered through ``tool_artifacts`` (ADR-0056).

A task pins the libraries it needs — ``tool_libraries: [{name, version, apps}]``
in ``task.yaml``, a shared ``domain.yaml`` or a project's ``task_defaults``. A
library is a Python package installed next to the engine that registers a
:class:`ToolLibrary` object under the entry-point group
:data:`~tolokaforge.core.plugin_registry.TOOL_LIBRARIES_GROUP`.

After an adapter has built a task's
:class:`~tolokaforge.runner.models.TaskDescription`,
:func:`merge_tool_libraries` resolves each pin, refuses a library that is not
installed or is installed at another version, and merges the library's bundle
into ``tool_artifacts`` — the one channel by which tool code reaches the runner
and the grader, whatever the adapter. :meth:`BaseAdapter.describe_task
<tolokaforge.adapters.base.BaseAdapter.describe_task>` is the single caller on
the run path.

A bundle runs on the runner's interpreter with the runner's dependencies,
exactly like an adapter's own artefacts, so a library imports only packages
the runner already depends on.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.core.models.task_config import ToolLibraryPin
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    available_tool_libraries,
    load_tool_library,
)

if TYPE_CHECKING:
    from tolokaforge.runner.models import TaskDescription

__all__ = [
    "PROVENANCE_METADATA_KEY",
    "InMemoryToolLibrary",
    "ResolvedToolLibrary",
    "ToolLibrary",
    "ToolLibraryError",
    "merge_tool_libraries",
    "resolve_tool_libraries",
]

PROVENANCE_METADATA_KEY = "tool_libraries"
"""The ``TaskDescription.metadata`` key the merged libraries are recorded under."""

_COLLISIONS_SHOWN = 5


@runtime_checkable
class ToolLibrary(Protocol):
    """What an entry point of ``tolokaforge.tool_libraries`` resolves to."""

    @property
    def name(self) -> str:
        """The name a task pins; equal to the entry-point name."""
        ...

    @property
    def version(self) -> str:
        """The installed version, compared verbatim with the pin's ``version``."""
        ...

    def bundle(self, apps: Sequence[str] | None) -> Mapping[str, bytes]:
        """The library's sources for ``apps`` (every application when ``None``).

        Keys are POSIX paths relative to the trial's artefact root, so a bundle
        rooted at the library's import package (``acme_tools/__init__.py``)
        imports from the extracted directory as an installed package does.
        """
        ...


class ToolLibraryError(ValueError):
    """A pinned library cannot be honoured: absent, another version, or a bad bundle."""


@dataclass(frozen=True)
class ResolvedToolLibrary:
    """A pin together with the installed library that satisfies it."""

    pin: ToolLibraryPin
    library: ToolLibrary


@dataclass
class InMemoryToolLibrary:
    """Deterministic :class:`ToolLibrary` fixture (ADR-0011 Pattern A).

    ``shared`` files ship with every bundle; ``apps`` maps an application name
    to its own files. Asking for an application the library does not ship
    raises :class:`ToolLibraryError`, the failure a real library reports the
    same way. ``calls`` records the ``apps`` argument of every
    :meth:`bundle` call.
    """

    name: str
    version: str
    shared: Mapping[str, bytes] = field(default_factory=dict)
    apps: Mapping[str, Mapping[str, bytes]] = field(default_factory=dict)
    calls: list[tuple[str, ...] | None] = field(default_factory=list)

    def bundle(self, apps: Sequence[str] | None) -> Mapping[str, bytes]:
        self.calls.append(tuple(apps) if apps is not None else None)
        selected = list(self.apps) if apps is None else list(apps)
        unknown = sorted(set(selected) - set(self.apps))
        if unknown:
            raise ToolLibraryError(
                f"tool library {self.name!r} ships no application {unknown} "
                f"(it ships {sorted(self.apps)})"
            )
        files = dict(self.shared)
        for app in selected:
            files.update(self.apps[app])
        return files


def resolve_tool_libraries(pins: Sequence[ToolLibraryPin]) -> list[ResolvedToolLibrary]:
    """The installed library behind each pin, in pin order.

    Raises:
        ToolLibraryError: A name is pinned twice, is not installed (the message
            lists what is), registers an object that is not a
            :class:`ToolLibrary` or that calls itself by another name, or is
            installed at a version other than the pinned one (both named).
    """
    resolved: list[ResolvedToolLibrary] = []
    seen: set[str] = set()
    for pin in pins:
        if pin.name in seen:
            raise ToolLibraryError(f"tool library {pin.name!r} is pinned twice")
        seen.add(pin.name)
        resolved.append(ResolvedToolLibrary(pin, _installed_library(pin)))
    return resolved


def _installed_library(pin: ToolLibraryPin) -> ToolLibrary:
    try:
        library: object = load_tool_library(pin.name)
    except UnknownImplementationError:
        installed = available_tool_libraries()
        raise ToolLibraryError(
            f"tool library {pin.name!r} is pinned but not installed next to the engine "
            f"(installed: {', '.join(installed) if installed else 'none'})"
        ) from None
    if not isinstance(library, ToolLibrary):
        raise ToolLibraryError(
            f"tool library {pin.name!r} registers a {type(library).__name__}, which lacks "
            "name, version or bundle()"
        )
    if library.name != pin.name:
        raise ToolLibraryError(
            f"the entry point {pin.name!r} registers a tool library named {library.name!r}"
        )
    if library.version != pin.version:
        raise ToolLibraryError(
            f"tool library {pin.name!r} is pinned at version {pin.version} but version "
            f"{library.version} is installed; install the pinned version or move the pin"
        )
    return library


def merge_tool_libraries(
    description: TaskDescription, pins: Sequence[ToolLibraryPin]
) -> TaskDescription:
    """``description`` with every pinned library's bundle merged into ``tool_artifacts``.

    A path the description already holds — the adapter's own artefacts or an
    earlier library's — is refused rather than overwritten: a pack that still
    carries a copy of a library drops it when it pins the library. Provenance
    (name, version, applications, file count per library) is recorded under
    ``metadata["tool_libraries"]``. Without pins the description is returned
    as is.

    Raises:
        ToolLibraryError: Any refusal of :func:`resolve_tool_libraries`, an
            empty bundle, a bundle path that is absolute or leaves the
            artefact root, a non-bytes file, or a path collision.
    """
    if not pins:
        return description
    artifacts: dict[str, str] = dict(description.tool_artifacts)
    provenance: list[dict[str, Any]] = []
    for resolved in resolve_tool_libraries(pins):
        files = _checked_bundle(resolved)
        _refuse_collisions(resolved.pin.name, files, artifacts)
        artifacts.update(
            (path, base64.b64encode(content).decode("ascii")) for path, content in files.items()
        )
        provenance.append(
            {
                "name": resolved.pin.name,
                "version": resolved.library.version,
                "apps": list(resolved.pin.apps) if resolved.pin.apps is not None else None,
                "files": len(files),
            }
        )
    metadata = {**description.metadata, PROVENANCE_METADATA_KEY: provenance}
    return description.model_copy(update={"tool_artifacts": artifacts, "metadata": metadata})


def _checked_bundle(resolved: ResolvedToolLibrary) -> dict[str, bytes]:
    pin = resolved.pin
    files = dict(resolved.library.bundle(pin.apps))
    if not files:
        selection = pin.apps if pin.apps is not None else "every application"
        raise ToolLibraryError(
            f"tool library {pin.name!r} produced an empty bundle for {selection}"
        )
    for path, content in files.items():
        posix = PurePosixPath(path)
        if not path or posix.is_absolute() or ".." in posix.parts or "\\" in path:
            raise ToolLibraryError(
                f"tool library {pin.name!r} bundles {path!r}, which is not a relative POSIX "
                "path inside the artefact root"
            )
        if not isinstance(content, bytes):
            raise ToolLibraryError(
                f"tool library {pin.name!r} bundles {path!r} as {type(content).__name__}, not bytes"
            )
    return files


def _refuse_collisions(name: str, files: Mapping[str, bytes], artifacts: Mapping[str, str]) -> None:
    collisions = sorted(set(files) & set(artifacts))
    if not collisions:
        return
    shown = ", ".join(collisions[:_COLLISIONS_SHOWN])
    more = (
        f" and {len(collisions) - _COLLISIONS_SHOWN} more"
        if len(collisions) > _COLLISIONS_SHOWN
        else ""
    )
    raise ToolLibraryError(
        f"tool library {name!r} bundles {len(collisions)} path(s) the task's artefacts "
        f"already hold ({shown}{more}); remove the pack's own copy of the library"
    )
