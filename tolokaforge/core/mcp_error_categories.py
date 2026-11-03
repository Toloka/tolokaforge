"""The failure categories an MCP tool declares in ``CallToolResult._meta``.

A tool answering ``isError: true`` says *why* through
``_meta["tolokaforge/error_category"]``; an untyped ``isError`` is recorded as
``environment_error``. A leaf module with no imports, so an MCP server — this
repository's ``DomainToolRegistry`` or a third-party one — and the runner that
reads the categories share them without pulling in either side.
"""

from __future__ import annotations

__all__ = [
    "INVALID_ARGUMENTS_CATEGORY",
    "MCP_ERROR_CATEGORY_META",
    "TOOL_ERROR_CATEGORY",
]

#: ``CallToolResult._meta`` key through which an MCP tool declares its failure's category.
MCP_ERROR_CATEGORY_META = "tolokaforge/error_category"

#: The tool refused the caller's arguments; the runner records ``invalid_arguments``.
INVALID_ARGUMENTS_CATEGORY = "invalid_arguments"

#: The tool ran and refused the request (a ``ToolError``); the runner records ``tool_error``.
TOOL_ERROR_CATEGORY = "tool_error"
