"""Unit tests for :class:`tolokaforge.tools.builtin.build_check.BuildCheckTool`.

Covers: constructor validation, schema shape, endpoint URL composition,
successful invocation, HTTP error responses, timeout / network-error
paths, and the "no per-invocation args" contract. All HTTP calls are
mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from tolokaforge.tools.builtin.build_check import BuildCheckTool
from tolokaforge.tools.registry import ToolCategory

pytestmark = pytest.mark.unit


class TestBuildCheckToolConstructor:
    def test_constructor_defaults(self) -> None:
        tool = BuildCheckTool(service="grader")
        assert tool.name == "build_check"
        assert tool.endpoint_url == "http://grader:8001/build_check"
        assert tool.policy.category == ToolCategory.COMPUTE

    def test_constructor_overrides(self) -> None:
        tool = BuildCheckTool(
            service="peer",
            port=9002,
            path="/probe",
            method="GET",
            timeout_s=60.0,
        )
        assert tool.endpoint_url == "http://peer:9002/probe"

    def test_constructor_normalises_relative_path(self) -> None:
        tool = BuildCheckTool(service="peer", path="probe")
        assert tool.endpoint_url.endswith("/probe")

    def test_missing_service_raises(self) -> None:
        with pytest.raises(ValueError, match="service"):
            BuildCheckTool(service="")

    def test_non_string_service_raises(self) -> None:
        with pytest.raises(ValueError, match="service"):
            BuildCheckTool(service=None)  # type: ignore[arg-type]

    def test_invalid_method_raises(self) -> None:
        with pytest.raises(ValueError, match="method"):
            BuildCheckTool(service="peer", method="DELETE")

    def test_non_positive_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="timeout_s"):
            BuildCheckTool(service="peer", timeout_s=0)

    def test_non_int_port_raises(self) -> None:
        """A YAML typo like ``port: "eighty"`` must fail loud at trial
        registration with a diagnostic message, not surface as an
        opaque ``ValueError`` from ``int(port)`` deep inside execute."""
        with pytest.raises(ValueError, match="port"):
            BuildCheckTool(service="peer", port="8001")  # type: ignore[arg-type]

    def test_non_string_path_raises(self) -> None:
        with pytest.raises(ValueError, match="path"):
            BuildCheckTool(service="peer", path=None)  # type: ignore[arg-type]


class TestBuildCheckToolSchema:
    def test_schema_shape(self) -> None:
        schema = BuildCheckTool(service="peer").get_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "build_check"
        params = schema["function"]["parameters"]
        assert params["properties"] == {}
        assert params["required"] == []
        assert params["additionalProperties"] is False


class TestBuildCheckToolExecute:
    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_success_returns_body_verbatim(self, mock_post: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        response.text = '{"configured": true, "run_error": ""}'
        mock_post.return_value = response

        tool = BuildCheckTool(service="grader")
        result = tool.execute()

        assert result.success is True
        assert result.output == '{"configured": true, "run_error": ""}'
        assert result.metadata["status_code"] == 200
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == "http://grader:8001/build_check"

    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_post_wire_contract(self, mock_post: MagicMock) -> None:
        """POST carries ``Content-Type: application/json`` + empty JSON
        body. Locks the wire contract so a refactor to ``data=`` /
        ``json=`` / different content-type would fail loudly."""
        response = MagicMock()
        response.status_code = 200
        response.text = "ok"
        mock_post.return_value = response

        BuildCheckTool(service="peer").execute()

        kwargs = mock_post.call_args.kwargs
        assert kwargs["headers"] == {"Content-Type": "application/json"}
        assert kwargs["content"] == b"{}"

    @patch("tolokaforge.tools.builtin.build_check.httpx.get")
    def test_execute_get_method(self, mock_get: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        response.text = "ok"
        mock_get.return_value = response

        tool = BuildCheckTool(service="peer", method="GET")
        result = tool.execute()

        assert result.success is True
        mock_get.assert_called_once()

    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_ignores_arguments(self, mock_post: MagicMock) -> None:
        """Schema advertises no arguments; extra kwargs are silently ignored
        so a call site that misreads the schema still gets a response."""
        response = MagicMock()
        response.status_code = 200
        response.text = "ok"
        mock_post.return_value = response

        tool = BuildCheckTool(service="peer")
        result = tool.execute(extra="value", another=42)

        assert result.success is True

    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_redirect_status_is_error(self, mock_post: MagicMock) -> None:
        """3xx (redirects, not-modified) is not success. httpx does not
        follow redirects here, so a 301 from a misconfigured peer is a
        defect that should surface as an error — not a masquerading
        success with a redirect body."""
        response = MagicMock()
        response.status_code = 301
        response.text = "Moved Permanently"
        mock_post.return_value = response

        result = BuildCheckTool(service="peer").execute()

        assert result.success is False
        assert result.output == "Moved Permanently"
        assert "HTTP 301" in result.error
        assert result.metadata["status_code"] == 301

    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_http_error_hands_body_back(self, mock_post: MagicMock) -> None:
        """Non-2xx still returns the body so the agent can read diagnostic
        detail — but marks ``success=False`` so the loop records ERROR."""
        response = MagicMock()
        response.status_code = 500
        response.text = "internal server error: config load failed"
        mock_post.return_value = response

        tool = BuildCheckTool(service="peer")
        result = tool.execute()

        assert result.success is False
        assert result.output == "internal server error: config load failed"
        assert "HTTP 500" in result.error
        assert result.metadata["status_code"] == 500

    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_timeout(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.ReadTimeout("timeout")

        tool = BuildCheckTool(service="peer", timeout_s=5.0)
        result = tool.execute()

        assert result.success is False
        assert result.output == ""
        assert "timed out" in result.error
        assert "5.0s" in result.error

    @patch("tolokaforge.tools.builtin.build_check.httpx.post")
    def test_execute_connection_error(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = httpx.ConnectError("no route to host")

        tool = BuildCheckTool(service="peer")
        result = tool.execute()

        assert result.success is False
        assert result.output == ""
        assert "ConnectError" in result.error
        assert "no route to host" in result.error
