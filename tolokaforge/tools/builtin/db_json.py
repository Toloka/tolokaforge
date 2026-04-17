"""JSON DB tools"""

from typing import Any

import httpx

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class DBQueryTool(Tool):
    """Query JSON database"""

    def __init__(self, db_url: str = "http://json-db:8000"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.READ,
            visibility=["agent"],
        )
        super().__init__(
            name="db_query",
            description="Query the JSON database using JSONPath",
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
        """Execute query"""
        try:
            response = httpx.post(
                f"{self.db_url}/query",
                json={"jsonpath": jsonpath},
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
                error=f"Query failed: {str(e)}",
            )


class DBUpdateTool(Tool):
    """Update JSON database"""

    def __init__(self, db_url: str = "http://json-db:8000"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.WRITE,
            visibility=["agent"],
        )
        super().__init__(
            name="db_update",
            description="Update the JSON database with operations",
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
                        "ops": {
                            "type": "array",
                            "description": "Array of update operations",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "op": {
                                        "type": "string",
                                        "enum": ["replace", "add", "remove"],
                                    },
                                    "path": {"type": "string"},
                                    "value": {},
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
        """Execute update"""
        try:
            response = httpx.post(
                f"{self.db_url}/update",
                json={"ops": ops},
                timeout=self.policy.timeout_s,
            )
            response.raise_for_status()
            data = response.json()

            return ToolResult(
                success=True,
                output=f"Database updated successfully. Version: {data['version']}",
                metadata={"etag": data["etag"], "version": data["version"]},
            )
        except httpx.HTTPError as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Update failed: {str(e)}",
            )


class SQLQueryTool(Tool):
    """Execute SQL queries on the database"""

    def __init__(self, db_url: str = "http://json-db:8000"):
        policy = ToolPolicy(
            timeout_s=30.0,
            category=ToolCategory.READ,
            visibility=["agent"],
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

    def __init__(self, db_url: str = "http://json-db:8000"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.READ,
            visibility=["agent"],
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
