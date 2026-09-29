"""The ``temperature`` a request carries, for the agent and for the user simulator.

Driven through the real client down to the ``completion`` call, so what is
asserted is the keyword set the provider SDK receives. ``models.<role>.temperature:
null`` and ``actors.user.sampling: {temperature: null}`` both send no temperature;
the simulator's default stays 0.2 whatever ``models.user.temperature`` says.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from tolokaforge.core.llm.client import LLMClient, UserSimulator
from tolokaforge.core.models import Message, MessageRole, ModelConfig, UserSamplingConfig
from tolokaforge.core.models.task_config import SIMULATOR_TEMPERATURE

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


def _simulator_request(model: ModelConfig, **simulator: Any) -> dict[str, Any]:
    sim = UserSimulator(mode="llm", llm_config=model, **simulator)
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
    def test_models_user_temperature_does_not_move_the_default(self, run_value: object) -> None:
        """Run configs that set ``models.user.temperature`` have always had 0.2 sent
        in its place; honouring the key would change what those runs send."""
        assert _simulator_request(_model(run_value))["temperature"] == SIMULATOR_TEMPERATURE

    def test_a_declared_value_is_sent(self) -> None:
        assert _simulator_request(_model(0.0), temperature=0.7)["temperature"] == 0.7

    def test_null_sends_no_temperature(self) -> None:
        assert "temperature" not in _simulator_request(_model(0.0), temperature=None)

    def test_a_preset_fixed_temperature_still_wins_over_null(self) -> None:
        model = _model(0.0, capabilities={"fixed_temperature": 1.0})
        assert _simulator_request(model, temperature=None)["temperature"] == 1.0

    def test_the_run_config_is_not_changed(self) -> None:
        """The simulator's temperature goes on its own copy of ``models.user``."""
        model = _model(0.0)
        UserSimulator(mode="llm", llm_config=model, temperature=None)
        assert model.temperature == 0.0


class TestUserSamplingConfig:
    def test_temperature_is_required(self) -> None:
        """Required, so ``sampling: {}`` is not read as "send no temperature"."""
        with pytest.raises(ValidationError, match="temperature"):
            UserSamplingConfig()  # type: ignore[call-arg]

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="temprature"):
            UserSamplingConfig(temperature=0.0, temprature=0.0)  # type: ignore[call-arg]
