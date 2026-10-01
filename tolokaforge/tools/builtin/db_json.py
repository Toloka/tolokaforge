"""JSON DB tools.

``db_query`` and ``db_update`` declare the LLM-facing schema and ``ToolPolicy``
only. The runner serves them against the trial's own store on db-service
(``Dispatch.JSON_DB``), so calling their ``execute`` here is an error.
"""

from typing import Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

_JSONPATH_EXAMPLE = "$.tickets[0].status"


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
                                            "add: the path ends in a key name; set that key on "
                                            "the object at its parent (`$.tickets[0].note`), or "
                                            "append the value to a list parent with a path "
                                            "ending in `.-` (`$.tickets.-`), the only form a "
                                            "list parent takes; refused when the parent "
                                            "matches nothing or holds neither. "
                                            "remove: delete every match of the path; a path "
                                            "matching nothing changes nothing. replace and "
                                            "remove refuse the root `$`"
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
                                "additionalProperties": False,
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
