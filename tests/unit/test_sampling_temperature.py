"""The sampling parameters a request carries, and the values nothing sends.

Driven through the real client down to the ``completion`` call, so what is
asserted is the keyword set the provider SDK receives. ``models.<role>.temperature:
null`` sends no temperature; the built-in simulator sends 0.2 whatever
``models.user.temperature`` says, and a run that sets that key is told so. A preset
declaring ``supports_sampling_params: false`` sends no ``temperature`` / ``top_p``
from any source but a ``fixed_temperature``, and an explicit value on such a model
config is reported by ``config validate`` and at run start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.test_orchestrator_strict_task_load import _make_task, _RaisingStubAdapter
from tolokaforge.core.config_validator import Severity, validate_run_config
from tolokaforge.core.llm.client import BuiltinUserSimulator, LLMClient
from tolokaforge.core.llm.presets import (
    IGNORED_SAMPLING_PARAM,
    ignored_sampling_params,
    set_overlay_path,
)
from tolokaforge.core.models import (
    EvaluationConfig,
    Message,
    MessageRole,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.models.run_config import USER_TEMPERATURE_IGNORED, sets_user_temperature
from tolokaforge.core.orchestrator import Orchestrator

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

    @pytest.mark.parametrize("value", [0.0, None])
    def test_a_run_logs_the_warning_once_its_tasks_load(
        self, value: float | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        records = self._load_tasks({"temperature": value}, caplog)
        warned = [r for r in records if r.getMessage() == USER_TEMPERATURE_IGNORED]
        assert len(warned) == 1
        assert warned[0].levelno == logging.WARNING

    def test_a_run_without_the_key_logs_nothing_about_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        records = self._load_tasks({}, caplog)
        assert not [r for r in records if r.getMessage() == USER_TEMPERATURE_IGNORED]

    @staticmethod
    def _load_tasks(user: dict[str, Any], caplog: pytest.LogCaptureFixture) -> list:
        config = RunConfig(
            models={
                "agent": ModelConfig(provider="openai", name="gpt-4"),
                "user": ModelConfig(provider="openai", name="gpt-4o-mini", **user),
            },
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir="/tmp/user_temperature_warning"),
        )
        orchestrator = Orchestrator(config)
        orchestrator.adapter = _RaisingStubAdapter(
            {}, tasks={"TASK-A": _make_task("TASK-A")}, raises=set()
        )
        with caplog.at_level(logging.WARNING):
            orchestrator.load_tasks()
        return list(caplog.records)


_NO_SAMPLING = "acme/reasoner-1"
_SAMPLING_KEYS = {"temperature", "top_p", "top_k"}


@pytest.fixture
def no_sampling_preset(write_overlay: Callable[[dict], str]) -> None:
    """An operator overlay whose preset declares that ``acme/reasoner*`` takes no sampling."""
    preset = {"match": ["acme/reasoner*"], "params": {"supports_sampling_params": False}}
    set_overlay_path(write_overlay({"presets": {"acme_reasoner": preset}}))


def _sampling_sent(model: ModelConfig, **call: Any) -> dict[str, Any]:
    client = LLMClient(model)
    sent = _sent(lambda: client.generate(messages=[_user("hi")], **call))
    return {key: sent[key] for key in _SAMPLING_KEYS & sent.keys()}


class TestAPresetThatTakesNoSampling:
    @pytest.mark.parametrize(
        ("config", "call"),
        [
            pytest.param({"temperature": 0.7}, {}, id="config-temperature"),
            pytest.param({}, {"temperature": 0.7}, id="per-call-temperature"),
            pytest.param({"temperature": 0.3}, {"temperature": 0.7}, id="both"),
            pytest.param({"top_p": 0.9}, {"temperature": 0.7}, id="config-top-p"),
            pytest.param({}, {"top_p": 0.9}, id="per-call-top-p"),
        ],
    )
    def test_sends_none_from_any_source(
        self, no_sampling_preset: None, config: dict[str, Any], call: dict[str, Any]
    ) -> None:
        assert _sampling_sent(_model(name=_NO_SAMPLING, **config), **call) == {}

    def test_a_fixed_temperature_is_still_sent(self, no_sampling_preset: None) -> None:
        model = _model(0.7, name=_NO_SAMPLING, top_p=0.9, capabilities={"fixed_temperature": 1.0})
        assert _sampling_sent(model) == {"temperature": 1.0}

    def test_the_config_can_take_sampling_back(self, no_sampling_preset: None) -> None:
        model = _model(
            0.7, name=_NO_SAMPLING, top_p=0.9, capabilities={"supports_sampling_params": True}
        )
        assert _sampling_sent(model) == {"temperature": 0.7, "top_p": 0.9}

    def test_a_preset_that_keeps_sampling_still_sends_the_default(
        self, no_sampling_preset: None
    ) -> None:
        assert _sampling_sent(_model()) == {"temperature": 0.0}


class TestTheBundledGpt5EscapeHatch:
    """``capabilities: {supports_sampling_params: true}`` is scoped to its own config."""

    def test_a_provider_openai_config_that_takes_it_sends_its_temperature(self) -> None:
        model = ModelConfig(
            provider="openai",
            name="gpt-5.2",
            temperature=0.7,
            capabilities={"supports_sampling_params": True},
        )
        assert _sampling_sent(model) == {"temperature": 0.7}

    @pytest.mark.parametrize(
        ("provider", "name"), [("openai", "gpt-5.2"), ("openrouter", "openai/gpt-5.2")]
    )
    def test_a_config_without_it_sends_none(self, provider: str, name: str) -> None:
        assert _sampling_sent(ModelConfig(provider=provider, name=name, temperature=0.7)) == {}


def test_thinking_drops_the_config_top_p_too() -> None:
    model = ModelConfig(
        provider="openrouter",
        name="anthropic/claude-opus-4.7",
        top_p=0.9,
        reasoning={"mode": "budget", "budget_tokens": 2000},
    )
    assert _sampling_sent(model) == {}


class TestAnIgnoredSamplingValueIsReported:
    @staticmethod
    def _models(**agent: Any) -> dict[str, ModelConfig]:
        return {
            "agent": _model(name=_NO_SAMPLING, **agent),
            "user": _model(0.7, name=_NO_SAMPLING),
        }

    def test_an_explicit_value_on_the_primary_and_on_a_fallback(
        self, no_sampling_preset: None
    ) -> None:
        models = self._models(
            temperature=0.7,
            fallbacks=[{"provider": "openrouter", "name": _NO_SAMPLING, "top_p": 0.9}],
        )
        found = [(path, f.field) for path, f in ignored_sampling_params(models)]
        assert found == [
            ("models.agent.temperature", "temperature"),
            ("models.agent.fallbacks[0].top_p", "top_p"),
        ]

    @pytest.mark.parametrize(
        "agent",
        [
            pytest.param({}, id="absent"),
            pytest.param({"temperature": None}, id="null"),
            pytest.param(
                {"temperature": 0.7, "capabilities": {"supports_sampling_params": True}},
                id="config-re-enables-sampling",
            ),
            pytest.param(
                {"temperature": 0.7, "capabilities": {"fixed_temperature": 1.0}},
                id="config-pins-fixed-temperature",
            ),
        ],
    )
    def test_silent_when_nothing_written_is_dropped(
        self, no_sampling_preset: None, agent: dict[str, Any]
    ) -> None:
        assert ignored_sampling_params(self._models(**agent)) == []

    def test_silent_on_a_model_whose_preset_keeps_sampling(self, no_sampling_preset: None) -> None:
        assert ignored_sampling_params({"agent": _model(0.7, top_p=0.9)}) == []

    def test_config_validate_and_the_run_report_the_same_paths(
        self, no_sampling_preset: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        agent = {"provider": "openrouter", "name": _NO_SAMPLING, "temperature": 0.7}
        fallback = {"provider": "openrouter", "name": _NO_SAMPLING, "top_p": 0.9}
        raw = {
            **TestTheIgnoredUserKeyIsReported._RUN,
            "models": {"agent": {**agent, "fallbacks": [fallback]}},
        }
        validated = [
            (i.path, i.severity)
            for i in validate_run_config(raw).issues
            if IGNORED_SAMPLING_PARAM in i.message
        ]

        config = RunConfig(
            models={"agent": ModelConfig(**agent, fallbacks=[fallback])},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir="/tmp/ignored_sampling_warning"),
        )
        orchestrator = Orchestrator(config)
        orchestrator.adapter = _RaisingStubAdapter(
            {}, tasks={"TASK-A": _make_task("TASK-A")}, raises=set()
        )
        with caplog.at_level(logging.WARNING):
            orchestrator.load_tasks()
        logged = [r.path for r in caplog.records if r.getMessage() == IGNORED_SAMPLING_PARAM]

        expected = ["models.agent.temperature", "models.agent.fallbacks[0].top_p"]
        assert validated == [(path, Severity.WARNING) for path in expected]
        assert logged == expected

    def test_config_validate_reports_capabilities_that_do_not_build(self) -> None:
        agent = {"provider": "openrouter", "name": _NO_SAMPLING, "temperature": 0.7}
        raw = {
            **TestTheIgnoredUserKeyIsReported._RUN,
            "models": {"agent": {**agent, "capabilities": {"supports_sampling": False}}},
        }
        [issue] = [i for i in validate_run_config(raw).issues if i.severity is Severity.ERROR]
        assert issue.path == "models.agent.capabilities"
        assert "supports_sampling" in issue.message
