"""Shared tool libraries delivered through ``tool_artifacts`` (ADR-0060).

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
    "checked_bundles",
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
            lists what is), fails to load (the entry point's ImportError or
            AttributeError, chained), registers an object that is not a
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
    except Exception as exc:
        # The entry point names a module attribute; an ImportError or
        # AttributeError raised on loading it is the library's failure, reported
        # like every other refusal of the pin rather than as a raw traceback.
        raise ToolLibraryError(
            f"tool library {pin.name!r} failed to load: {type(exc).__name__}: {exc}"
        ) from exc
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
    earlier library's — is refused rather than overwritten, and so is an
    artefact that would shadow a bundled top-level module on an import path
    of the trial (a copy under ``_shared/``, ``tools/`` or beside an MCP
    server): a pack that still carries a copy of a library drops it when it
    pins the library. Provenance (name, version, applications, file count per
    library) is recorded under ``metadata["tool_libraries"]``. Without pins
    the description is returned as is.

    Raises:
        ToolLibraryError: Any refusal of :func:`resolve_tool_libraries`, an
            empty bundle, a bundle path that is absolute, leaves the artefact
            root or is not normalised, a non-bytes file, a collision, or a
            description whose metadata already carries the provenance key.
    """
    if not pins:
        return description
    if PROVENANCE_METADATA_KEY in description.metadata:
        raise ToolLibraryError(
            f"the adapter's description already sets metadata[{PROVENANCE_METADATA_KEY!r}], "
            "the key the engine records the merged libraries under; an adapter does not merge "
            "libraries itself (see describe_task)"
        )
    artifacts: dict[str, str] = dict(description.tool_artifacts)
    import_roots = _import_roots(description)
    provenance: list[dict[str, Any]] = []
    for resolved, encoded in checked_bundles(pins):
        _refuse_collisions(resolved.pin.name, encoded, artifacts, import_roots)
        artifacts.update(encoded)
        provenance.append(
            {
                "name": resolved.pin.name,
                "version": resolved.library.version,
                "apps": list(resolved.pin.apps) if resolved.pin.apps is not None else None,
                "files": len(encoded),
            }
        )
    metadata = {**description.metadata, PROVENANCE_METADATA_KEY: provenance}
    return description.model_copy(update={"tool_artifacts": artifacts, "metadata": metadata})


def checked_bundles(
    pins: Sequence[ToolLibraryPin],
) -> list[tuple[ResolvedToolLibrary, Mapping[str, str]]]:
    """Each pin's library with its checked bundle, base64-encoded as ``tool_artifacts`` carries it.

    Everything :func:`merge_tool_libraries` does short of a description:
    ``tolokaforge validate`` calls it so an application the library does not
    ship, or a bad bundle path, fails validation with the message a run refuses
    the task with.

    Raises:
        ToolLibraryError: Any refusal of :func:`resolve_tool_libraries`, an
            empty bundle, a bundle path that is absolute, leaves the artefact
            root or is not normalised, or a non-bytes file.
    """
    return [(resolved, _encoded_bundle(resolved)) for resolved in resolve_tool_libraries(pins)]


_ENCODED_BUNDLES: dict[tuple[int, tuple[str, ...] | None], tuple[ToolLibrary, dict[str, str]]] = {}
"""The encoded bundle of each (library object, selection) this process has built.

A run describes every task on the pin, and the orchestrator caches each
description: without this, every task re-ran ``bundle()``, re-encoded the same
files and held its own copy of the strings. The entry keeps the library object
itself, so its ``id`` cannot be reused by another object while the entry lives
— a test that registers a fresh fake library under the same name gets its own
bundle, not a predecessor's.
"""


def _encoded_bundle(resolved: ResolvedToolLibrary) -> dict[str, str]:
    """The checked bundle of ``resolved``, encoded once per library object and selection."""
    apps = tuple(resolved.pin.apps) if resolved.pin.apps is not None else None
    key = (id(resolved.library), apps)
    cached = _ENCODED_BUNDLES.get(key)
    if cached is not None:
        return cached[1]
    encoded = {
        path: base64.b64encode(content).decode("ascii")
        for path, content in _checked_bundle(resolved).items()
    }
    _ENCODED_BUNDLES[key] = (resolved.library, encoded)
    return encoded


def _checked_bundle(resolved: ResolvedToolLibrary) -> dict[str, bytes]:
    pin = resolved.pin
    files = dict(resolved.library.bundle(pin.apps))
    if not files:
        selection = pin.apps if pin.apps is not None else "every application"
        raise ToolLibraryError(
            f"tool library {pin.name!r} produced an empty bundle for {selection}"
        )
    checked: dict[str, bytes] = {}
    for path, content in files.items():
        # ``PurePosixPath`` collapses ``a//x``, ``./a`` and ``a/./x`` silently, so
        # the parts are split by hand: a path that needs normalising is refused,
        # and the one stored is then exactly the one compared.
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ToolLibraryError(
                f"tool library {pin.name!r} bundles {path!r}, which is not a relative POSIX "
                "path inside the artefact root (no leading '/', no empty, '.' or '..' "
                "component, no backslash)"
            )
        if not isinstance(content, bytes):
            raise ToolLibraryError(
                f"tool library {pin.name!r} bundles {path!r} as {type(content).__name__}, not bytes"
            )
        checked[PurePosixPath(path).as_posix()] = content
    return checked


_ALWAYS_IMPORT_ROOTS = frozenset({"", "tools", "_shared"})
"""Artefact directories every trial imports from, relative to the artefact root.

The runner process puts the root and ``tools/`` on ``sys.path`` when it extracts
the artefacts; an MCP server subprocess has its own directory first on its path,
and a shared domain's server lives under ``_shared/``.
"""


def _import_roots(description: TaskDescription) -> frozenset[str]:
    """The artefact directories a module of the trial can be imported from.

    :data:`_ALWAYS_IMPORT_ROOTS` plus the directory of every MCP server script
    the description names: Python starts that subprocess with the script's own
    directory ahead of everything else, so a copy of a library beside the
    server would win over the pinned one at the root.
    """
    roots = set(_ALWAYS_IMPORT_ROOTS)
    for tool in (*description.agent_tools, *description.user_tools):
        source = tool.source
        if source is not None and source.mcp_server_script:
            parent = PurePosixPath(source.mcp_server_script).parent.as_posix()
            roots.add("" if parent == "." else parent)
    return frozenset(roots)


def _import_name(entry: str) -> str:
    """The module a directory entry imports as: ``pkg/`` and ``pkg.py`` are both ``pkg``."""
    return entry[: -len(".py")] if entry.endswith(".py") else entry


def _refuse_collisions(
    name: str,
    files: Mapping[str, str],
    artifacts: Mapping[str, str],
    import_roots: frozenset[str],
) -> None:
    """Refuse an artefact that holds a bundle path or shadows a bundled top-level module.

    Exact path matches are the obvious case. The subtle one is a pack that
    vendored a copy of the library under a directory that comes before the
    artefact root on some import path — ``_shared/<pkg>/…`` beside the MCP
    server, or ``tools/<pkg>/…`` — which the pinned bundle would then never
    be imported over. Both are reported with the artefact paths at fault.
    """
    bundled = {_import_name(PurePosixPath(path).parts[0]) for path in files}
    offending: set[str] = set()
    shadowed: set[str] = set()
    for artifact in artifacts:
        if artifact in files:
            offending.add(artifact)
            shadowed.add(_import_name(PurePosixPath(artifact).parts[0]))
            continue
        parts = PurePosixPath(artifact).parts
        for depth in range(len(parts)):
            if "/".join(parts[:depth]) not in import_roots:
                continue
            module = _import_name(parts[depth])
            if module in bundled:
                offending.add(artifact)
                shadowed.add(module)
    if not offending:
        return
    collisions = sorted(offending)
    shown = ", ".join(collisions[:_COLLISIONS_SHOWN])
    more = (
        f" and {len(collisions) - _COLLISIONS_SHOWN} more"
        if len(collisions) > _COLLISIONS_SHOWN
        else ""
    )
    raise ToolLibraryError(
        f"tool library {name!r} bundles the top-level module(s) {', '.join(sorted(shadowed))}; "
        f"{len(collisions)} path(s) of the task's artefacts hold or shadow them on the import "
        f"path ({shown}{more}); remove the pack's own copy of the library"
    )
