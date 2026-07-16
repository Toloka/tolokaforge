"""Consumer-agnostic invokable utilities for anything attached to the OAL gate.

A tool implements :class:`InteractiveTool` — three members: ``name``,
``description``, ``run(args, context) -> ToolResult``. Tools are called by:

* the keyboard REPL (``/name args``),
* an LLM controller (autonomous invocation before drafting),
* an HTTP webhook (``POST /tools/{name}``),
* a scripted controller running a canned scenario,
* a plain post-hoc script inspecting a session snapshot.

The tool doesn't know which of those called it. Callers construct the
:class:`ToolContext` from whatever they have; tools handle missing fields
gracefully.

Third-party tools ship as installable packages that register under
``[project.entry-points."intervener.tools"]``. Discovery is
:meth:`ToolRegistry.with_discovered`.
"""

from intervener.tools.base import InteractiveTool, ToolContext, ToolResult
from intervener.tools.reference import AnalyzeTool, ContextTool
from intervener.tools.registry import ToolRegistry

__all__ = [
    "AnalyzeTool",
    "ContextTool",
    "InteractiveTool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
]
