"""Harness-owned READ-ONLY tools for the rubric judge (Stage 4).

These are NOT the agent's tools and NOT filtered by category — they are a fixed
allowlist the judge runs with: read-only DB access, knowledge-base search, a
workspace file reader, and the rubric-derived ``submit_report``. No ``write`` /
``compute`` / mutation surface exists here by construction.

Each tool is a :class:`~tolokaforge.tools.registry.Tool` so the judge can drive
them through the same :class:`~tolokaforge.tools.registry.ToolExecutor` the agent
uses. The DB tools are *synchronous* — they call a :class:`DBReader` the runner
supplies, which bridges to its async DB client off the judge's worker thread (see
``judge.py`` module docstring). ``search_kb`` is :class:`SearchKbTool`, a
harness-owned read-only tool that delegates to the per-trial
:class:`~tolokaforge.core.grading.kb_search.KnowledgeSearch` resolved for the
trial — the SAME index the agent searched. It does NOT reuse the builtin
``SearchKBTool`` (that one re-derives a global rag URL — the bug this fixes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch

__all__ = [
    "GetDbStateTool",
    "QueryDbTool",
    "ReadFileTool",
    "SearchKbTool",
    "SubmitReportTool",
]

#: Cap on file/db output returned to the judge so a huge state can't blow the
#: context window. Truncation is flagged in the output, never silent.
_MAX_OUTPUT_CHARS = 50_000


def _read_only_policy(timeout_s: float = 15.0) -> ToolPolicy:
    return ToolPolicy(timeout_s=timeout_s, category=ToolCategory.READ, visibility=["agent"])


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n... (truncated at {_MAX_OUTPUT_CHARS} chars)"


class GetDbStateTool(Tool):
    """Return the trial's final DB state (read-only) as JSON."""

    def __init__(self, db_reader: DBReader):
        super().__init__(
            name="get_db_state",
            description=(
                "Read the final database state of the trial. Optionally pass a list "
                "of table names to narrow the result; omit to return all tables. "
                "Read-only."
            ),
            policy=_read_only_policy(),
        )
        self._reader = db_reader

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tables": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional table names to return (default: all).",
                        }
                    },
                },
            },
        }

    def execute(self, tables: list[str] | None = None, **_: Any) -> ToolResult:
        try:
            data = self._reader.get_state(tables)
        except Exception as exc:  # surface as a tool error, not a crash
            return ToolResult(success=False, output="", error=f"get_db_state failed: {exc}")
        return ToolResult(success=True, output=_truncate(json.dumps(data, default=str, indent=2)))


class QueryDbTool(Tool):
    """Run a read-only JSONPath query against the trial's final DB state."""

    def __init__(self, db_reader: DBReader):
        super().__init__(
            name="query_db",
            description=(
                "Query the final database state with a JSONPath expression "
                "(e.g. '$.orders[*].status'). Returns matching values. Read-only."
            ),
            policy=_read_only_policy(),
        )
        self._reader = db_reader

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "jsonpath": {
                            "type": "string",
                            "description": "JSONPath expression to evaluate against the state.",
                        }
                    },
                    "required": ["jsonpath"],
                },
            },
        }

    def execute(self, jsonpath: str = "", **_: Any) -> ToolResult:
        if not jsonpath:
            return ToolResult(success=False, output="", error="query_db requires a 'jsonpath'.")
        try:
            data = self._reader.query(jsonpath)
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"query_db failed: {exc}")
        return ToolResult(success=True, output=_truncate(json.dumps(data, default=str, indent=2)))


class ReadFileTool(Tool):
    """Read a UTF-8 text file from the agent's workspace (sandboxed, read-only)."""

    def __init__(self, workspace_dir: Path):
        super().__init__(
            name="read_file",
            description=(
                "Read a text file the agent produced, relative to its workspace. "
                "Read-only; paths are sandboxed to the workspace."
            ),
            policy=_read_only_policy(),
        )
        self._workspace = workspace_dir.resolve()

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the workspace root.",
                        }
                    },
                    "required": ["path"],
                },
            },
        }

    def execute(self, path: str = "", **_: Any) -> ToolResult:
        if not path:
            return ToolResult(success=False, output="", error="read_file requires a 'path'.")
        target = (self._workspace / path).resolve()
        if not target.is_relative_to(self._workspace):
            return ToolResult(success=False, output="", error="path escapes the workspace.")
        if not target.exists() or not target.is_file():
            return ToolResult(success=False, output="", error=f"file not found: {path}")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(success=False, output="", error=f"read_file failed: {exc}")
        return ToolResult(success=True, output=_truncate(content))


class SearchKbTool(Tool):
    """Read-only knowledge-base search delegating to a per-trial backend.

    The judge gets this tool **iff** a :class:`KnowledgeSearch` was resolved for
    the trial (faithful gating: the agent had a KB ⇒ the judge gets the SAME KB).
    The external schema is kept stable (``search_kb(query, top_k, alpha)``) for
    prompt stability; the call delegates to the resolved backend, which points at
    the same per-trial index the agent searched. Fail-loud: a transport error in
    the backend surfaces as a tool error (never silently empty results).
    """

    def __init__(self, kb_search: KnowledgeSearch):
        super().__init__(
            name="search_kb",
            description="Search the knowledge base for relevant information",
            policy=_read_only_policy(),
        )
        self._kb = kb_search

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query to find relevant documents",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results to return (default: 5)",
                            "default": 5,
                        },
                        "alpha": {
                            "type": "number",
                            "description": (
                                "Weight for hybrid search: 0.0=BM25 only (keyword), "
                                "1.0=FAISS only (semantic), 0.5=balanced (default: 0.5). "
                                "Ignored by backends that do not support hybrid search."
                            ),
                            "default": 0.5,
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, query: str = "", top_k: int = 5, alpha: float = 0.5, **_: Any) -> ToolResult:
        if not query:
            return ToolResult(success=False, output="", error="search_kb requires a 'query'.")
        try:
            hits = self._kb.search(query, top_k=top_k, alpha=alpha)
        except Exception as exc:  # surface transport/backend errors loud, not empty
            return ToolResult(success=False, output="", error=f"search_kb failed: {exc}")

        if not hits:
            return ToolResult(
                success=True,
                output="No relevant documents found.",
                metadata={"count": 0},
            )

        lines = [f"Found {len(hits)} relevant documents:\n"]
        for i, hit in enumerate(hits, 1):
            snippet = hit.text[:200]
            ellipsis = "..." if len(hit.text) > 200 else ""
            lines.append(f"\n[{i}] Document: {hit.doc_id}")
            lines.append(f"    Source: {hit.source}")
            lines.append(f"    Score: {hit.score:.3f}")
            lines.append(f"    Content: {snippet}{ellipsis}")
        return ToolResult(
            success=True,
            output="\n".join(lines),
            metadata={"count": len(hits), "top_score": hits[0].score},
        )


class SubmitReportTool(Tool):
    """Terminal tool whose argument schema is derived from the rubric.

    Executing it is a no-op acknowledgement — the loop's termination policy
    captures the arguments and stops; this just gives the model a clean tool
    result so the transcript stays well-formed if the call is inspected.
    """

    def __init__(self, schema: dict[str, Any]):
        func = schema["function"]
        super().__init__(name=func["name"], description=func["description"])
        self._schema = schema

    def get_schema(self) -> dict[str, Any]:
        return self._schema

    def execute(self, **_: Any) -> ToolResult:
        return ToolResult(success=True, output="Report received.")
