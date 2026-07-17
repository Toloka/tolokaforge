"""Core types for the tool plug-in surface.

Four types:

* :class:`InteractiveTool` — the Protocol every tool implements.
* :class:`ToolContext` — everything a tool might need, all optional.
* :class:`ToolResult` — the tool's response.
* :class:`LLMCallable` — narrow ``(system, user) → text`` contract for
  agentic tools. Callers construct one however they like (wrapping
  tolokaforge's ``LLMClient``, an in-house HTTP client, a mock). Tools
  never import a specific LLM stack.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rich.console import Console

from intervener.binding import SessionBinding
from tolokaforge.session import TrialEvent

__all__ = ["InteractiveTool", "LLMCallable", "ToolContext", "ToolResult"]


# ``LLMCallable(system, user) -> text``. Minimal by design — the intervener
# package must not depend on any specific LLM stack (see ADR-0019). Callers
# that have credentials + a client wire one up and pass it in; agentic tools
# call it if it's non-None and fall back to a heuristic otherwise.
LLMCallable = Callable[[str, str], str]


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
    * ``llm_call`` — a :data:`LLMCallable` supplied by the caller for
      agentic tools. ``None`` (default) means "no LLM available" and
      agentic tools should fall back to a non-LLM path. The intervener
      package NEVER constructs one itself — that keeps the peer decoupled
      from tolokaforge's LLM stack (see ADR-0019). Callers wrap
      ``tolokaforge.core.llm.LLMClient`` (or any other provider) into a
      simple ``(system, user) → text`` function and pass it in.
    * ``extras`` — future-proofing bag. Consumer-specific state (an HTTP
      request object, a user identity, …) that doesn't fit anywhere else.
    """

    binding: SessionBinding | None = None
    recent_events: list[TrialEvent] = field(default_factory=list)
    task_metadata: dict[str, Any] | None = None
    console: Console | None = None
    llm_call: LLMCallable | None = None
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
