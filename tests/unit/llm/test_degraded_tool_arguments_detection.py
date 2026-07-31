"""Content-based detection of degraded LLM tool-call responses.

Provider-native finish reasons are not sufficient to identify every broken
tool call. Anthropic, for example, can return a normal ``tool_use`` envelope
whose argument object is empty. These tests pin the schema-driven guard that
rejects such turns before they can become trajectory entries.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.client import LLMClient, _should_retry_exception
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.unit


_ABSENT = object()


def _tool_schema(parameters: dict[str, Any] | object = _ABSENT) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": "bash",
        "description": "Run a command.",
    }
    if parameters is not _ABSENT:
        function["parameters"] = parameters
    return {"type": "function", "function": function}


def _parameters(*, required: list[str] | object = _ABSENT) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer"},
        },
    }
    if required is not _ABSENT:
        parameters["required"] = required
    return parameters


def _tool_call_response(arguments: Any) -> MagicMock:
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    tool_call = MagicMock()

    tool_call.id = "toolu_degraded_1"
    tool_call.function = MagicMock()
    tool_call.function.name = "bash"
    tool_call.function.arguments = arguments

    message.content = None
    message.tool_calls = [tool_call]
    message.thinking_blocks = None
    message.reasoning_content = None
    message.provider_specific_fields = None
    choice.message = message
    choice.finish_reason = "tool_calls"
    choice.provider_specific_fields = {"native_finish_reason": "tool_use"}
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=20,
        completion_tokens=5,
        total_tokens=25,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _make_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-degraded-tool-arguments")
    return LLMClient(
        ModelConfig(
            provider="openrouter",
            name="anthropic/claude-opus-4.8",
        )
    )


def _disable_tenacity_sleep(client: LLMClient) -> None:
    client.generate.retry.sleep = lambda *args, **kwargs: None  # type: ignore[attr-defined]


def _generate(client: LLMClient, tools: list[dict[str, Any]]):
    return client.generate(
        system="Use the supplied tool.",
        messages=[Message(role=MessageRole.USER, content="Run pwd")],
        tools=tools,
    )


def test_empty_required_tool_arguments_raise_and_are_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    _disable_tenacity_sleep(client)
    client.logger = MagicMock()
    bad = _tool_call_response({})
    tools = [_tool_schema(_parameters(required=["command"]))]

    with patch("tolokaforge.core.llm.client.completion", return_value=bad) as completion_mock:
        with pytest.raises(RuntimeError, match="empty/missing required tool arguments") as error:
            _generate(client, tools)

    assert _should_retry_exception(error.value) is True
    assert completion_mock.call_count == 5
    warning = client.logger.warning.call_args
    assert "Discarding degraded tool-call response" in warning.args[0]
    assert warning.kwargs["tool"] == "bash"
    assert warning.kwargs["missing_required_parameters"] == ["command"]
    assert warning.kwargs["raw_arguments_type"] == "dict"
    assert warning.kwargs["raw_arguments_count"] == 0


@pytest.mark.parametrize(
    "parameters",
    [
        _parameters(),
        _parameters(required=[]),
        _ABSENT,
    ],
    ids=["required-absent", "required-empty", "parameters-absent"],
)
def test_empty_optional_tool_arguments_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
    parameters: dict[str, Any] | object,
) -> None:
    client = _make_client(monkeypatch)
    response = _tool_call_response({})
    tools = [_tool_schema(parameters)]

    with (
        patch("tolokaforge.core.llm.client.completion", return_value=response),
        patch("tolokaforge.core.llm.client._litellm_response_cost", return_value=0.0),
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        result = _generate(client, tools)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {}


def test_well_formed_required_tool_arguments_are_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    response = _tool_call_response('{"command": "pwd"}')
    tools = [_tool_schema(_parameters(required=["command"]))]

    with (
        patch("tolokaforge.core.llm.client.completion", return_value=response),
        patch("tolokaforge.core.llm.client._litellm_response_cost", return_value=0.0),
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        result = _generate(client, tools)

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "bash"
    assert result.tool_calls[0].arguments == {"command": "pwd"}


def test_partial_tool_arguments_report_the_missing_required_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    _disable_tenacity_sleep(client)
    response = _tool_call_response('{"command": "pwd"}')
    tools = [_tool_schema(_parameters(required=["command", "timeout"]))]

    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        with pytest.raises(RuntimeError, match=r"missing_required_parameters=\['timeout'\]"):
            _generate(client, tools)


def test_retry_exhaustion_names_empty_missing_required_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    _disable_tenacity_sleep(client)
    bad = _tool_call_response("{}")
    tools = [_tool_schema(_parameters(required=["command"]))]

    with patch("tolokaforge.core.llm.client.completion", return_value=bad) as completion_mock:
        with pytest.raises(RuntimeError) as error:
            _generate(client, tools)

    assert "empty/missing required tool arguments" in str(error.value)
    assert "tool='bash'" in str(error.value)
    assert completion_mock.call_count == 5


def test_degraded_tool_arguments_never_return_a_trajectory_visible_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(monkeypatch)
    _disable_tenacity_sleep(client)
    bad = _tool_call_response({})
    tools = [_tool_schema(_parameters(required=["command"]))]
    caller_visible_results: list[Any] = []

    def call_and_record_result() -> None:
        caller_visible_results.append(_generate(client, tools))

    with patch("tolokaforge.core.llm.client.completion", return_value=bad):
        with pytest.raises(RuntimeError, match="empty/missing required tool arguments"):
            call_and_record_result()

    assert caller_visible_results == []


@pytest.mark.parametrize(
    ("raw_arguments", "expected_type"),
    [
        (None, "NoneType"),
        (42, "int"),
        ("   ", "str"),
        ("[1, 2, 3]", "str"),
        ("\x00", "str"),
    ],
)
def test_empty_tool_arguments_parser_paths_log_payload_shape(
    monkeypatch: pytest.MonkeyPatch,
    raw_arguments: Any,
    expected_type: str,
) -> None:
    client = _make_client(monkeypatch)
    client.logger = MagicMock()

    assert client._parse_tool_arguments("bash", raw_arguments) == {}

    client.logger.warning.assert_called_once()
    assert client.logger.warning.call_args.kwargs["tool"] == "bash"
    assert client.logger.warning.call_args.kwargs["raw_arguments_type"] == expected_type
    if isinstance(raw_arguments, str):
        assert client.logger.warning.call_args.kwargs["raw_arguments_length"] == len(raw_arguments)
