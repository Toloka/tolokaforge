"""Core types for the tool plug-in surface.

Three types:

* :class:`InteractiveTool` — the Protocol every tool implements.
* :class:`ToolContext` — everything a tool might need, all optional.
* :class:`ToolResult` — the tool's response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rich.console import Console

from intervener.binding import SessionBinding
from tolokaforge.session import TrialEvent

__all__ = ["InteractiveTool", "ToolContext", "ToolResult"]


@dataclass
class ToolContext:
    """Everything a tool might need to do its work.

    Every field is optional. Tools handle missing pieces gracefully — a
    tool called from a post-hoc script with only ``recent_events`` should
    still return something useful; a tool called from the keyboard REPL
    with a full context can do more.

    * ``binding`` — a live :class:`SessionBinding` when the caller has one.
      Tools may submit interventions through it.
    * ``recent_events`` — bounded window of events observed so far. The
      caller decides how many to include; tools should not assume a
      specific size.
    * ``task_metadata`` — opaque dict populated by the caller from
      whatever it knows about the task (task.yaml, project config, etc).
      Kept as ``dict | None`` to avoid coupling to any specific loader.
    * ``console`` — a Rich :class:`Console` for rendering. Tools that
      need to draw panels/tables use this. Tools that only produce text
      can leave ``ToolResult.output`` for the caller to print.
    * ``extras`` — future-proofing bag. Consumer-specific state (an HTTP
      request object, a user identity, …) that doesn't fit anywhere else.
    """

    binding: SessionBinding | None = None
    recent_events: list[TrialEvent] = field(default_factory=list)
    task_metadata: dict[str, Any] | None = None
    console: Console | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """A tool's response.

    ``output`` is the canonical text; keyboard/CLI callers print it
    directly. ``data`` is an optional structured payload for callers that
    can consume JSON (HTTP endpoint, programmatic consumer).
    ``submitted_interventions`` counts any interventions the tool
    submitted via ``context.binding`` so the caller can surface that
    number to a human.
    """

    output: str
    data: dict[str, Any] | None = None
    submitted_interventions: int = 0


@runtime_checkable
class InteractiveTool(Protocol):
    """The tool contract.

    Attributes ``name`` and ``description`` are class-level or
    instance-level string attributes. ``run`` takes a raw arguments
    string (whatever the caller has after the tool name, or ``""``) and a
    :class:`ToolContext`, and returns a :class:`ToolResult`.
    """

    name: str
    description: str

    def run(self, args: str, context: ToolContext) -> ToolResult: ...
