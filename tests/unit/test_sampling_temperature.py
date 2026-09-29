"""The ``temperature`` a request carries, and the user key nothing reads.

Driven through the real client down to the ``completion`` call, so what is
asserted is the keyword set the provider SDK receives. ``models.<role>.temperature:
null`` sends no temperature; the built-in simulator sends 0.2 whatever
``models.user.temperature`` says, and a run that sets that key is told so.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.config_validator import Severity, validate_run_config
from tolokaforge.core.llm.client import BuiltinUserSimulator, LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.core.models.run_config import USER_TEMPERATURE_IGNORED, sets_user_temperature

pytestmark = pytest.mark.unit

_UNSET = object()


def _response(text: str) -> MagicMock:
    """The smallest ``ModelResponse`` shape :meth:`LLMClient.generate` reads."""
    message = MagicMock()
    message.content = text
    message.tool_calls = None
    message.reasoning_content = None
    del message.thinking_blocks
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = "stop"
    response = MagicMock()
    response.choices = [choice]
    response.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return response


def _sent(call: Any) -> dict[str, Any]:
    """The keyword arguments of the one ``completion`` call *call* made."""
    with (
        patch(
            "tolokaforge.core.llm.client.completion",
            return_value=_response("I need to move my booking to Friday."),
        ) as completion,
        patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
    ):
        call()
    assert completion.call_count == 1
    return completion.call_args.kwargs


def _model(temperature: object = _UNSET, **extra: Any) -> ModelConfig:
    fields: dict[str, Any] = {"provider": "openrouter", "name": "openai/gpt-4o-mini", **extra}
    if temperature is not _UNSET:
        fields["temperature"] = temperature
    return ModelConfig(**fields)


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _simulator_request(model: ModelConfig) -> dict[str, Any]:
    sim = BuiltinUserSimulator(mode="llm", llm_config=model)
    agent_turn = [Message(role=MessageRole.ASSISTANT, content="Hi! How can I help you today?")]
    return _sent(lambda: sim.reply(agent_turn))


@pytest.fixture(autouse=True)
def _offline_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-sampling")


class TestAgentTemperature:
    def test_the_default_is_still_zero(self) -> None:
        assert _model().temperature == 0.0
        client = LLMClient(_model())
        assert _sent(lambda: client.generate(messages=[_user("hi")]))["temperature"] == 0.0

    def test_a_declared_value_is_sent(self) -> None:
        client = LLMClient(_model(0.7))
        assert _sent(lambda: client.generate(messages=[_user("hi")]))["temperature"] == 0.7

    def test_null_sends_no_temperature(self) -> None:
        client = LLMClient(_model(None))
        assert "temperature" not in _sent(lambda: client.generate(messages=[_user("hi")]))

    def test_a_preset_fixed_temperature_still_wins_over_null(self) -> None:
        client = LLMClient(_model(None, capabilities={"fixed_temperature": 1.0}))
        assert _sent(lambda: client.generate(messages=[_user("hi")]))["temperature"] == 1.0


class TestSimulatorTemperature:
    @pytest.mark.parametrize("run_value", [_UNSET, 0.0, 0.9, None])
    def test_models_user_temperature_does_not_move_the_simulator(self, run_value: object) -> None:
        """Run configs that set ``models.user.temperature`` have always had 0.2 sent
        in its place; honouring the key would change what those runs send."""
        assert _simulator_request(_model(run_value))["temperature"] == 0.2


class TestTheIgnoredUserKeyIsReported:
    _RUN = {
        "models": {
            "agent": {"provider": "openrouter", "name": "openai/gpt-4o"},
            "user": {"provider": "openrouter", "name": "openai/gpt-4o-mini"},
        },
        "orchestrator": {"workers": 1, "repeats": 1, "max_turns": 10},
        "evaluation": {"tasks_glob": "tasks/**/task.yaml", "output_dir": "out"},
    }

    @pytest.mark.parametrize("value", [0.0, 0.7, None])
    def test_an_explicit_user_temperature_is_detected_whatever_its_value(
        self, value: float | None
    ) -> None:
        models = {"user": _model(value), "agent": _model(0.0)}
        assert sets_user_temperature(models)

    def test_an_unset_user_temperature_is_not(self) -> None:
        assert not sets_user_temperature({"user": _model(), "agent": _model(0.0)})
        assert not sets_user_temperature({"agent": _model(0.0)})

    def test_config_validate_warns_on_the_user_key_only(self) -> None:
        raw = {
            **self._RUN,
            "models": {
                "agent": {**self._RUN["models"]["agent"], "temperature": 0.0},
                "user": {**self._RUN["models"]["user"], "temperature": 0.0},
            },
        }
        issues = validate_run_config(raw).issues
        warned = [i for i in issues if i.path == "models.user.temperature"]
        assert [i.severity for i in warned] == [Severity.WARNING]
        assert warned[0].message == USER_TEMPERATURE_IGNORED
        assert not [i for i in issues if i.path == "models.agent.temperature"]

    def test_config_validate_is_quiet_without_the_key(self) -> None:
        issues = validate_run_config(self._RUN).issues
        assert not [i for i in issues if i.path == "models.user.temperature"]
