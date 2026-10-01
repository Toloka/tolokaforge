"""Unit tests for tool builtins: db_json, http_request.

Covers: schema structure, constructor configuration, parameter validation,
request construction, and result parsing. All HTTP calls are mocked. The
``db_query`` / ``db_update`` execution path is the runner's trial-bound
wrapper, locked in ``tests/unit/runner/test_json_db_builtins_trial_scope.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from tolokaforge.tools.builtin.db_json import DBQueryTool, DBUpdateTool
from tolokaforge.tools.builtin.http_request import HTTPRequestTool
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
        assert tool.policy.timeout_s == 10.0
        assert tool.policy.category == ToolCategory.READ

    def test_schema_structure(self) -> None:
        tool = DBQueryTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "db_query"
        assert "jsonpath" in func["parameters"]["properties"]
        assert "jsonpath" in func["parameters"]["required"]


# ===================================================================
# DBUpdateTool
# ===================================================================


@pytest.mark.unit
class TestDBUpdateTool:
    """Tests for DBUpdateTool."""

    def test_constructor_defaults(self) -> None:
        tool = DBUpdateTool()
        assert tool.name == "db_update"
        assert tool.policy.timeout_s == 10.0
        assert tool.policy.category == ToolCategory.WRITE

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

    def test_an_op_item_declares_exactly_the_fields_db_service_accepts(self) -> None:
        from tolokaforge.env.json_db_service.app import JSONPathOp

        item_schema = DBUpdateTool().get_schema()["function"]["parameters"]["properties"]["ops"][
            "items"
        ]

        assert set(item_schema["properties"]) == set(JSONPathOp.model_fields)
        assert JSONPathOp.model_config["extra"] == "forbid"
        assert item_schema["additionalProperties"] is False


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
        assert "PATCH" in params["properties"]["method"]["enum"]
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
# Cross-tool: schema format
# ===================================================================


@pytest.mark.unit
class TestSchemaFormat:
    """Tests for consistent schema format across tools."""

    def test_all_schemas_are_function_type(self) -> None:
        tools = [
            DBQueryTool(),
            DBUpdateTool(),
            HTTPRequestTool(),
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
            HTTPRequestTool(),
        ]
        for tool in tools:
            schema = tool.get_schema()
            assert schema["function"]["name"] == tool.name
