"""Single source of truth for builtin tool import paths and dispatch.

Adapters consult :func:`get_class` to extract LLM-facing parameter schemas
at TaskDescription-build time. The runner consults :func:`get_dispatch` to
choose a wrapper class. The executor service consults :func:`get_class`
to instantiate tools with ``env_state``-derived runtime kwargs.

All three previously kept their own list of (name, class) tuples — the
drift between them produced empty parameter schemas for some tools (#121),
unknown-tool TypeErrors at runtime (#110), and inconsistent runtime
contexts across services (#123).
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable
from enum import StrEnum
from functools import cache
from typing import NamedTuple


class BuiltinToolEntry(NamedTuple):
    module_path: str
    class_name: str


class Dispatch(StrEnum):
    """Which runner wrapper handles a given builtin tool.

    The wrapper class is determined by the tool's name alone — neither the
    schema's ``source`` field nor any per-task config influences the
    choice. ``GENERIC`` tools receive ``ToolSchema.tool_config`` as
    constructor kwargs; ``FILES`` tools take ``WORK_DIR`` from the runner
    container layout; ``PERSISTENT_SHELL`` tools are lifecycle
    wrappers that hold a bash session for the trial, selecting a local or
    compose backend from ``tool_config`` without a second dispatch branch.
    ``EDITOR`` tools are stateless file editors matching Anthropic's
    ``str_replace_based_edit_tool``, selecting a local or compose backend from
    ``tool_config``. ``JSON_DB`` tools read and write the trial's own store on
    db-service through the runner factory's ``db_client`` + ``trial_id``, and
    take no ``tool_config``.

    The knowledge-base search tool is not a builtin: the task declares it
    (``initial_state.rag.tool``) and the runner binds it to the trial's search
    index by that declaration, before this registry is consulted (ADR-0054).
    """

    GENERIC = "generic"
    FILES = "files"
    PERSISTENT_SHELL = "persistent_shell"
    EDITOR = "editor"
    JSON_DB = "json_db"


_REGISTRY: dict[str, tuple[BuiltinToolEntry, Dispatch]] = {
    "bash": (
        BuiltinToolEntry("tolokaforge.tools.builtin.bash", "BashTool"),
        Dispatch.GENERIC,
    ),
    "calculator": (
        BuiltinToolEntry("tolokaforge.tools.builtin.calculator", "CalculatorTool"),
        Dispatch.GENERIC,
    ),
    "browser": (
        BuiltinToolEntry("tolokaforge.tools.builtin.browser", "BrowserTool"),
        Dispatch.GENERIC,
    ),
    "http_request": (
        BuiltinToolEntry("tolokaforge.tools.builtin.http_request", "HTTPRequestTool"),
        Dispatch.GENERIC,
    ),
    "build_check": (
        BuiltinToolEntry("tolokaforge.tools.builtin.build_check", "BuildCheckTool"),
        Dispatch.GENERIC,
    ),
    "mobile": (
        BuiltinToolEntry("tolokaforge.tools.builtin.mobile", "MobileTool"),
        Dispatch.GENERIC,
    ),
    "db_query": (
        BuiltinToolEntry("tolokaforge.tools.builtin.db_json", "DBQueryTool"),
        Dispatch.JSON_DB,
    ),
    "db_update": (
        BuiltinToolEntry("tolokaforge.tools.builtin.db_json", "DBUpdateTool"),
        Dispatch.JSON_DB,
    ),
    "read_file": (
        BuiltinToolEntry("tolokaforge.tools.builtin.files", "ReadFileTool"),
        Dispatch.FILES,
    ),
    "write_file": (
        BuiltinToolEntry("tolokaforge.tools.builtin.files", "WriteFileTool"),
        Dispatch.FILES,
    ),
    "list_dir": (
        BuiltinToolEntry("tolokaforge.tools.builtin.files", "ListDirTool"),
        Dispatch.FILES,
    ),
    "bash_session": (
        BuiltinToolEntry("tolokaforge.tools.persistent_shell", "PersistentShellTool"),
        Dispatch.PERSISTENT_SHELL,
    ),
    "str_replace_editor": (
        BuiltinToolEntry("tolokaforge.tools.str_replace_editor", "StrReplaceEditorTool"),
        Dispatch.EDITOR,
    ),
    "submit": (
        BuiltinToolEntry("tolokaforge.tools.builtin.submit", "SubmitTool"),
        Dispatch.GENERIC,
    ),
}

_COMPLETION_TOOLS: frozenset[str] = frozenset({"submit"})
"""Builtins whose call is the agent's own end-of-episode signal.

Orthogonal to :class:`Dispatch`, which names the runner-side wrapper: a
completion tool is reconstructed like any other builtin, and what sets it
apart happens a layer above, in the trial runner's termination policy. Naming
the set here rather than at that policy keeps "which tool ends an episode" a
property of the registry every caller already consults, so a second completion
tool is one entry rather than an edit in two packages.
"""


def is_builtin(name: str) -> bool:
    """Return True if *name* is a known builtin tool."""
    return name in _REGISTRY


def is_completion(name: str) -> bool:
    """Return True if calling *name* is the agent's end-of-episode signal."""
    return name in _COMPLETION_TOOLS


def list_completion_tools() -> frozenset[str]:
    """Return the set of builtin names that end an episode when called."""
    return _COMPLETION_TOOLS


def get_dispatch(name: str) -> Dispatch:
    """Return the runner-side wrapper kind for *name*.

    Raises :class:`KeyError` for unknown names — callers wanting a
    non-fatal probe should use :func:`is_builtin` first.
    """
    return _REGISTRY[name][1]


@cache
def get_class(name: str) -> type:
    """Import and return the tool class for *name*.

    Raises :class:`KeyError` for unknown names; lets ``ImportError`` /
    ``AttributeError`` propagate for broken registry entries — silent
    fallbacks would mask the kind of registry drift this module exists
    to eliminate.
    """
    entry = _REGISTRY[name][0]
    return getattr(importlib.import_module(entry.module_path), entry.class_name)


def list_builtins() -> frozenset[str]:
    """Return the set of all known builtin tool names."""
    return frozenset(_REGISTRY)


def list_for_dispatch(dispatch: Dispatch) -> frozenset[str]:
    """Return the set of names that route to *dispatch*."""
    return frozenset(name for name, (_, d) in _REGISTRY.items() if d is dispatch)


def json_db_tool_config_refusal(name: str, keys: Iterable[str]) -> str:
    """Why the ``JSON_DB`` builtin *name* refuses a ``tool_config`` carrying *keys*."""
    return (
        f"'{name}' reads the trial's own JSON DB and takes no tool_config; "
        f"got keys {sorted(keys)}"
    )
