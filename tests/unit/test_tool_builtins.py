"""Unit tests for tool builtins: db_json, http_request, rag_search.

Covers: schema structure, constructor configuration, parameter validation,
request construction, and result parsing. All HTTP calls are mocked.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tolokaforge.tools.builtin.db_json import (
    DBQueryTool,
    DBUpdateTool,
    SQLQueryTool,
    SQLSchemaToolDB,
)
from tolokaforge.tools.builtin.http_request import HTTPRequestTool
from tolokaforge.tools.builtin.rag_search import SearchKBTool
from tolokaforge.tools.registry import ToolCategory

pytestmark = pytest.mark.unit

# ===================================================================
# DBQueryTool
# ===================================================================


@pytest.mark.unit
class TestDBQueryTool:
    """Tests for DBQueryTool."""

    def test_constructor_defaults(self) -> None:
        tool = DBQueryTool()
        assert tool.name == "db_query"
        assert tool.db_url == "http://json-db:8000"
        assert tool.policy.timeout_s == 10.0
        assert tool.policy.category == ToolCategory.READ

    def test_constructor_custom_url(self) -> None:
        tool = DBQueryTool(db_url="http://localhost:9000")
        assert tool.db_url == "http://localhost:9000"

    def test_schema_structure(self) -> None:
        tool = DBQueryTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "db_query"
        assert "jsonpath" in func["parameters"]["properties"]
        assert "jsonpath" in func["parameters"]["required"]

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_success(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [{"id": 1}], "count": 1}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = DBQueryTool(db_url="http://test:8000")
        result = tool.execute(jsonpath="$.users[0]")

        assert result.success is True
        assert '"id": 1' in result.output
        assert result.metadata["count"] == 1
        mock_post.assert_called_once_with(
            "http://test:8000/query",
            json={"jsonpath": "$.users[0]"},
            timeout=10.0,
        )

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_http_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.HTTPError("Connection refused")

        tool = DBQueryTool()
        result = tool.execute(jsonpath="$.data")

        assert result.success is False
        assert "Query failed" in result.error

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_empty_results(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"results": [], "count": 0}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = DBQueryTool()
        result = tool.execute(jsonpath="$.nonexistent")

        assert result.success is True
        assert result.metadata["count"] == 0


# ===================================================================
# DBUpdateTool
# ===================================================================


@pytest.mark.unit
class TestDBUpdateTool:
    """Tests for DBUpdateTool."""

    def test_constructor_defaults(self) -> None:
        tool = DBUpdateTool()
        assert tool.name == "db_update"
        assert tool.db_url == "http://json-db:8000"
        assert tool.policy.timeout_s == 10.0
        assert tool.policy.category == ToolCategory.WRITE

    def test_constructor_custom_url(self) -> None:
        tool = DBUpdateTool(db_url="http://custom:5000")
        assert tool.db_url == "http://custom:5000"

    def test_schema_structure(self) -> None:
        tool = DBUpdateTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "db_update"
        assert "ops" in func["parameters"]["properties"]
        assert "ops" in func["parameters"]["required"]

        # Check ops schema
        ops_schema = func["parameters"]["properties"]["ops"]
        assert ops_schema["type"] == "array"
        item_schema = ops_schema["items"]
        assert "op" in item_schema["properties"]
        assert "path" in item_schema["properties"]

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_success(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"etag": "abc123", "version": 5}
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = DBUpdateTool(db_url="http://test:8000")
        ops = [{"op": "replace", "path": "$.user.name", "value": "Alice"}]
        result = tool.execute(ops=ops)

        assert result.success is True
        assert "Version: 5" in result.output
        assert result.metadata["etag"] == "abc123"
        assert result.metadata["version"] == 5
        mock_post.assert_called_once_with(
            "http://test:8000/update",
            json={"ops": ops},
            timeout=10.0,
        )

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_http_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.HTTPError("Server error")

        tool = DBUpdateTool()
        result = tool.execute(ops=[{"op": "add", "path": "$.x", "value": 1}])

        assert result.success is False
        assert "Update failed" in result.error


# ===================================================================
# SQLQueryTool
# ===================================================================


@pytest.mark.unit
class TestSQLQueryTool:
    """Tests for SQLQueryTool."""

    def test_constructor_defaults(self) -> None:
        tool = SQLQueryTool()
        assert tool.name == "sql_query"
        assert tool.db_url == "http://json-db:8000"
        assert tool.policy.timeout_s == 30.0
        assert tool.policy.category == ToolCategory.READ

    def test_schema_structure(self) -> None:
        tool = SQLQueryTool()
        schema = tool.get_schema()
        func = schema["function"]
        assert func["name"] == "sql_query"
        assert "query" in func["parameters"]["properties"]
        assert "query" in func["parameters"]["required"]

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_success(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [{"name": "Alice", "age": 30}],
            "count": 1,
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = SQLQueryTool(db_url="http://test:8000")
        result = tool.execute(query="SELECT * FROM users WHERE age > 25")

        assert result.success is True
        parsed = json.loads(result.output)
        assert parsed[0]["name"] == "Alice"
        assert result.metadata["count"] == 1

    @patch("tolokaforge.tools.builtin.db_json.httpx.post")
    def test_execute_http_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.HTTPError("Bad request")

        tool = SQLQueryTool()
        result = tool.execute(query="INVALID SQL")

        assert result.success is False
        assert "SQL query failed" in result.error


# ===================================================================
# SQLSchemaToolDB
# ===================================================================


@pytest.mark.unit
class TestSQLSchemaToolDB:
    """Tests for SQLSchemaToolDB."""

    def test_constructor_defaults(self) -> None:
        tool = SQLSchemaToolDB()
        assert tool.name == "get_db_schema"
        assert tool.db_url == "http://json-db:8000"
        assert tool.policy.category == ToolCategory.READ

    def test_schema_has_no_required_params(self) -> None:
        tool = SQLSchemaToolDB()
        schema = tool.get_schema()
        func = schema["function"]
        assert func["name"] == "get_db_schema"
        params = func["parameters"]
        assert params["properties"] == {}
        # No required params

    @patch("tolokaforge.tools.builtin.db_json.httpx.get")
    def test_execute_success(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "tables": {
                "users": {"columns": ["id", "name", "email"]},
                "orders": {"columns": ["id", "user_id", "total"]},
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        tool = SQLSchemaToolDB(db_url="http://test:8000")
        result = tool.execute()

        assert result.success is True
        assert "Database Schema" in result.output
        assert "users" in result.output

    @patch("tolokaforge.tools.builtin.db_json.httpx.get")
    def test_execute_http_error(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = httpx.HTTPError("Service unavailable")

        tool = SQLSchemaToolDB()
        result = tool.execute()

        assert result.success is False
        assert "Failed to get schema" in result.error


# ===================================================================
# HTTPRequestTool
# ===================================================================


@pytest.mark.unit
class TestHTTPRequestTool:
    """Tests for HTTPRequestTool."""

    def test_constructor_defaults(self) -> None:
        tool = HTTPRequestTool()
        assert tool.name == "http_request"
        assert tool.policy.timeout_s == 20.0
        assert tool.policy.category == ToolCategory.COMPUTE
        assert "mock-web" in tool.allowed_hosts

    def test_constructor_custom_hosts(self) -> None:
        tool = HTTPRequestTool(allowed_hosts=["example.com"])
        assert tool.allowed_hosts == ["example.com"]

    def test_schema_structure(self) -> None:
        tool = HTTPRequestTool()
        schema = tool.get_schema()
        func = schema["function"]
        assert func["name"] == "http_request"
        params = func["parameters"]
        assert "method" in params["properties"]
        assert "url" in params["properties"]
        assert "headers" in params["properties"]
        assert "json" in params["properties"]
        assert set(params["required"]) == {"method", "url"}

    def test_allowed_host_mock_web(self) -> None:
        tool = HTTPRequestTool()
        assert tool._is_allowed_host("http://mock-web:8080/api/data") is True

    def test_allowed_host_localhost(self) -> None:
        tool = HTTPRequestTool()
        assert tool._is_allowed_host("http://localhost:8080/page") is True

    def test_blocked_host(self) -> None:
        tool = HTTPRequestTool()
        assert tool._is_allowed_host("http://evil.com/steal") is False

    def test_blocked_host_external(self) -> None:
        tool = HTTPRequestTool()
        assert tool._is_allowed_host("https://api.openai.com/v1/models") is False

    def test_allowed_host_custom(self) -> None:
        tool = HTTPRequestTool(allowed_hosts=["myhost.local"])
        assert tool._is_allowed_host("http://myhost.local/api") is True
        assert tool._is_allowed_host("http://other.host/api") is False

    def test_scrub_headers_allowed(self) -> None:
        tool = HTTPRequestTool()
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/html",
            "User-Agent": "test-agent",
        }
        scrubbed = tool._scrub_headers(headers)
        assert scrubbed["Content-Type"] == "application/json"
        assert scrubbed["Accept"] == "text/html"
        assert scrubbed["User-Agent"] == "test-agent"

    def test_scrub_headers_removes_sensitive(self) -> None:
        tool = HTTPRequestTool()
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret123",
            "X-API-Key": "key456",
        }
        scrubbed = tool._scrub_headers(headers)
        assert "Content-Type" in scrubbed
        assert "Authorization" not in scrubbed
        assert "X-API-Key" not in scrubbed

    def test_scrub_headers_none(self) -> None:
        tool = HTTPRequestTool()
        assert tool._scrub_headers(None) == {}

    def test_scrub_headers_empty(self) -> None:
        tool = HTTPRequestTool()
        assert tool._scrub_headers({}) == {}

    @patch("tolokaforge.tools.builtin.http_request.httpx.request")
    def test_execute_get_json(self, mock_request: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_success = True
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"data": "value"}
        mock_request.return_value = mock_response

        tool = HTTPRequestTool()
        result = tool.execute(method="GET", url="http://mock-web:8080/api")

        assert result.success is True
        assert "200" in result.output
        assert "value" in result.output

    @patch("tolokaforge.tools.builtin.http_request.httpx.request")
    def test_execute_post_json(self, mock_request: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.is_success = True
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"id": 42}
        mock_request.return_value = mock_response

        tool = HTTPRequestTool()
        result = tool.execute(
            method="POST",
            url="http://mock-web:8080/api",
            json={"name": "test"},
        )

        assert result.success is True
        assert "201" in result.output

    @patch("tolokaforge.tools.builtin.http_request.httpx.request")
    def test_execute_html_response(self, mock_request: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_success = True
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}
        mock_response.text = "<html><body>Hello</body></html>"
        mock_request.return_value = mock_response

        tool = HTTPRequestTool()
        result = tool.execute(method="GET", url="http://mock-web:8080/page")

        assert result.success is True
        assert "HTML" in result.output
        assert "Hello" in result.output

    def test_execute_blocked_host(self) -> None:
        tool = HTTPRequestTool()
        result = tool.execute(method="GET", url="http://evil.com/api")

        assert result.success is False
        assert "not allowed" in result.error.lower()

    @patch("tolokaforge.tools.builtin.http_request.httpx.request")
    def test_execute_timeout(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = httpx.TimeoutException("timed out")

        tool = HTTPRequestTool()
        result = tool.execute(method="GET", url="http://mock-web:8080/slow")

        assert result.success is False
        assert "timed out" in result.error.lower()

    @patch("tolokaforge.tools.builtin.http_request.httpx.request")
    def test_execute_connection_error(self, mock_request: MagicMock) -> None:
        mock_request.side_effect = Exception("Connection refused")

        tool = HTTPRequestTool()
        result = tool.execute(method="GET", url="http://mock-web:8080/down")

        assert result.success is False
        assert "failed" in result.error.lower()

    @patch("tolokaforge.tools.builtin.http_request.httpx.request")
    def test_execute_metadata(self, mock_request: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.is_success = False
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = "Not Found"
        mock_request.return_value = mock_response

        tool = HTTPRequestTool()
        result = tool.execute(method="GET", url="http://mock-web:8080/missing")

        assert result.success is False
        assert result.metadata["status_code"] == 404


# ===================================================================
# SearchKBTool
# ===================================================================


@pytest.mark.unit
class TestSearchKBTool:
    """Tests for SearchKBTool (RAG search)."""

    def test_constructor_defaults(self) -> None:
        tool = SearchKBTool()
        assert tool.name == "search_kb"
        assert tool.rag_url == "http://rag-service:8001"
        assert tool.policy.timeout_s == 15.0
        assert tool.policy.category == ToolCategory.READ

    def test_constructor_custom_url(self) -> None:
        tool = SearchKBTool(rag_url="http://custom-rag:9000")
        assert tool.rag_url == "http://custom-rag:9000"

    def test_schema_structure(self) -> None:
        tool = SearchKBTool()
        schema = tool.get_schema()
        func = schema["function"]
        assert func["name"] == "search_kb"
        params = func["parameters"]
        assert "query" in params["properties"]
        assert "top_k" in params["properties"]
        assert "alpha" in params["properties"]
        assert params["required"] == ["query"]

    def test_schema_alpha_bounds(self) -> None:
        tool = SearchKBTool()
        schema = tool.get_schema()
        alpha_prop = schema["function"]["parameters"]["properties"]["alpha"]
        assert alpha_prop["minimum"] == 0.0
        assert alpha_prop["maximum"] == 1.0

    @patch("tolokaforge.tools.builtin.rag_search.httpx.post")
    def test_execute_success(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "doc_id": "doc-1",
                "source": "policy.md",
                "score": 0.95,
                "text": "This is the relevant policy text about returns...",
                "retrieval_method": "hybrid",
            },
            {
                "doc_id": "doc-2",
                "source": "faq.md",
                "score": 0.82,
                "text": "FAQ about return procedures...",
                "retrieval_method": "hybrid",
            },
        ]
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = SearchKBTool(rag_url="http://test:8001")
        result = tool.execute(query="return policy", top_k=5, alpha=0.5)

        assert result.success is True
        assert "2 relevant documents" in result.output
        assert "doc-1" in result.output
        assert "policy.md" in result.output
        assert result.metadata["count"] == 2
        assert result.metadata["top_score"] == 0.95
        assert result.metadata["method"] == "hybrid"

    @patch("tolokaforge.tools.builtin.rag_search.httpx.post")
    def test_execute_no_results(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = SearchKBTool()
        result = tool.execute(query="nonexistent topic")

        assert result.success is True
        assert "No relevant documents" in result.output
        assert result.metadata["count"] == 0

    @patch("tolokaforge.tools.builtin.rag_search.httpx.post")
    def test_execute_http_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.HTTPError("Service down")

        tool = SearchKBTool()
        result = tool.execute(query="test query")

        assert result.success is False
        assert "Search failed" in result.error

    @patch("tolokaforge.tools.builtin.rag_search.httpx.post")
    def test_execute_default_params(self, mock_post: MagicMock) -> None:
        """Verify default top_k and alpha are passed to the service."""
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = SearchKBTool(rag_url="http://test:8001")
        tool.execute(query="test")

        mock_post.assert_called_once_with(
            "http://test:8001/search",
            json={"query": "test", "top_k": 5, "alpha": 0.5},
            timeout=15.0,
        )

    @patch("tolokaforge.tools.builtin.rag_search.httpx.post")
    def test_execute_custom_params(self, mock_post: MagicMock) -> None:
        """Verify custom top_k and alpha are passed."""
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = SearchKBTool(rag_url="http://test:8001")
        tool.execute(query="test", top_k=10, alpha=0.8)

        mock_post.assert_called_once_with(
            "http://test:8001/search",
            json={"query": "test", "top_k": 10, "alpha": 0.8},
            timeout=15.0,
        )

    @patch("tolokaforge.tools.builtin.rag_search.httpx.post")
    def test_execute_text_truncation(self, mock_post: MagicMock) -> None:
        """Result text is truncated in output."""
        long_text = "x" * 1000
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "doc_id": "doc-1",
                "source": "long.md",
                "score": 0.9,
                "text": long_text,
                "retrieval_method": "semantic",
            },
        ]
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        tool = SearchKBTool()
        result = tool.execute(query="test")

        assert result.success is True
        # Text should be truncated to 200 chars + "..."
        assert "..." in result.output


# ===================================================================
# Cross-tool: visibility configuration
# ===================================================================


@pytest.mark.unit
class TestToolVisibility:
    """Tests for tool visibility configuration."""

    def test_all_tools_have_agent_visibility(self) -> None:
        tools = [
            DBQueryTool(),
            DBUpdateTool(),
            SQLQueryTool(),
            SQLSchemaToolDB(),
            HTTPRequestTool(),
            SearchKBTool(),
        ]
        for tool in tools:
            assert "agent" in tool.policy.visibility, f"{tool.name} missing agent visibility"


# ===================================================================
# Cross-tool: schema format
# ===================================================================


@pytest.mark.unit
class TestSchemaFormat:
    """Tests for consistent schema format across tools."""

    def test_all_schemas_are_function_type(self) -> None:
        tools = [
            DBQueryTool(),
            DBUpdateTool(),
            SQLQueryTool(),
            SQLSchemaToolDB(),
            HTTPRequestTool(),
            SearchKBTool(),
        ]
        for tool in tools:
            schema = tool.get_schema()
            assert schema["type"] == "function", f"{tool.name} missing type=function"
            assert "function" in schema, f"{tool.name} missing function key"
            func = schema["function"]
            assert "name" in func, f"{tool.name} missing name"
            assert "description" in func, f"{tool.name} missing description"
            assert "parameters" in func, f"{tool.name} missing parameters"

    def test_schema_names_match_tool_names(self) -> None:
        tools = [
            DBQueryTool(),
            DBUpdateTool(),
            SQLQueryTool(),
            SQLSchemaToolDB(),
            HTTPRequestTool(),
            SearchKBTool(),
        ]
        for tool in tools:
            schema = tool.get_schema()
            assert schema["function"]["name"] == tool.name
