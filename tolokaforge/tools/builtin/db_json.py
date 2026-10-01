"""JSON DB tools.

``db_query`` and ``db_update`` declare the LLM-facing schema and ``ToolPolicy``
only. The runner serves them against the trial's own store on db-service
(``Dispatch.JSON_DB``), so calling their ``execute`` here is an error.
"""

import os
from typing import Any

import httpx

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

# ``DB_SERVICE_URL`` is set in the runner container (see
# ``tolokaforge/docker/stacks/core.py``) to the tolokaforge-db-service network
# alias on ``runner-net``; the literal is the value outside any docker stack.
_DEFAULT_DB_URL_ENV = "DB_SERVICE_URL"
_DEFAULT_DB_URL_FALLBACK = "http://json-db:8000"

_JSONPATH_EXAMPLE = "$.tickets[0].status"


def _default_db_url() -> str:
    return os.environ.get(_DEFAULT_DB_URL_ENV, _DEFAULT_DB_URL_FALLBACK)


def _unbound(tool_name: str) -> RuntimeError:
    return RuntimeError(
        f"{tool_name} is bound to a trial's JSON DB by the runner's ToolFactory "
        f"(Dispatch.JSON_DB); the tool class itself has no store to reach"
    )


class DBQueryTool(Tool):
    """Query the trial's JSON database."""

    def __init__(self) -> None:
        super().__init__(
            name="db_query",
            description="Query the JSON database using JSONPath",
            policy=ToolPolicy(timeout_s=10.0, category=ToolCategory.READ),
        )

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
                            "description": "JSONPath query (e.g., '$.users[?(@.id==5)]')",
                        }
                    },
                    "required": ["jsonpath"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, jsonpath: str) -> ToolResult:
        raise _unbound(self.name)


class DBUpdateTool(Tool):
    """Update the trial's JSON database."""

    def __init__(self) -> None:
        super().__init__(
            name="db_update",
            description=(
                "Update the JSON database with operations. The batch is all or nothing: "
                "if any operation is refused, none is applied"
            ),
            policy=ToolPolicy(timeout_s=10.0, category=ToolCategory.WRITE),
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ops": {
                            "type": "array",
                            "description": "Array of update operations",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {
                                        "type": "string",
                                        "enum": ["replace", "add", "remove"],
                                        "description": (
                                            "replace: set the value at every match of the "
                                            "path; refused when it matches nothing. "
                                            "add: set the path's last key on the object at its "
                                            "parent (`$.tickets[0].note`); when the parent is a "
                                            "list, append the value to it (`$.audit_log.entry` "
                                            "appends to audit_log). "
                                            "remove: delete every match of the path"
                                        ),
                                    },
                                    "path": {
                                        "type": "string",
                                        "description": (
                                            f"JSONPath, e.g. `{_JSONPATH_EXAMPLE}`. "
                                            "JSON Pointer (`/tickets/0/status`) is not accepted"
                                        ),
                                    },
                                    "value": {"description": "The value to set; unused by remove"},
                                },
                                "required": ["op", "path"],
                            },
                        }
                    },
                    "required": ["ops"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, ops: list) -> ToolResult:
        raise _unbound(self.name)


class SQLQueryTool(Tool):
    """Execute SQL queries on the database"""

    def __init__(self, db_url: str | None = None):
        if db_url is None:
            db_url = _default_db_url()
        policy = ToolPolicy(
            timeout_s=30.0,
            category=ToolCategory.READ,
        )
        super().__init__(
            name="sql_query",
            description="Execute SQL queries on the CRM database. Use standard SQL syntax (SQLite dialect). Tables are automatically created from the database schema.",
            policy=policy,
        )
        self.db_url = db_url

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
                            "description": "SQL query to execute (e.g., 'SELECT * FROM customers WHERE region = \"West\"')",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, query: str) -> ToolResult:
        """Execute SQL query"""
        try:
            response = httpx.post(
                f"{self.db_url}/sql",
                json={"query": query},
                timeout=self.policy.timeout_s,
            )
            response.raise_for_status()
            data = response.json()

            import json

            results_str = json.dumps(data["results"], indent=2)
            return ToolResult(
                success=True,
                output=results_str,
                metadata={"count": data["count"]},
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"SQL query failed: {str(e)}",
            )


class SQLSchemaToolDB(Tool):
    """Get database schema information"""

    def __init__(self, db_url: str | None = None):
        if db_url is None:
            db_url = _default_db_url()
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.READ,
        )
        super().__init__(
            name="get_db_schema",
            description="Get the database schema showing all tables and their columns. Use this to understand what data is available before writing SQL queries.",
            policy=policy,
        )
        self.db_url = db_url

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }

    def execute(self) -> ToolResult:
        """Get schema"""
        try:
            response = httpx.get(
                f"{self.db_url}/schema",
                timeout=self.policy.timeout_s,
            )
            response.raise_for_status()
            data = response.json()

            import json

            schema_str = json.dumps(data["tables"], indent=2)
            return ToolResult(
                success=True,
                output=f"Database Schema:\n{schema_str}",
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to get schema: {str(e)}",
            )
