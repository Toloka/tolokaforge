"""The two signals that a run's cost is priced without the rates it needs.

A pricing row that carries no ``cache_read`` / ``cache_write`` makes
``_compute_cost`` bill cache tokens at the *input* rate. On a
cache-read-dominated trial that is a multiple, not a rounding error — and
nothing said so. Two signals now do, deliberately split by what is knowable
when:

* **Before the run** — ``Orchestrator.load_tasks`` warns when a configured
  model resolves to an incomplete row, or to no row at all. It cannot refuse:
  a model with no prompt caching legitimately publishes no cache rate, and
  the table cannot tell that apart from a row that is merely incomplete.
* **After the trial** — ``Metrics.cost_cache_rate_fallback`` records that a
  provider actually reported cache tokens against such a row, which is the
  unambiguous case.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import (
    EvaluationConfig,
    Message,
    MessageRole,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.pricing import reload_pricing, resolve_pricing
from tolokaforge.core.runner import _AgentMetricsSink

pytestmark = pytest.mark.unit

# Two shipped spellings of one model: the dotted row carries both cache
# rates, the dashed row carries neither. See
# tests/unit/test_pricing_known_duplicate_spellings.py for the inventory.
_COMPLETE_ROW_MODEL = "anthropic/claude-sonnet-4.6"
_INCOMPLETE_ROW_MODEL = "anthropic/claude-sonnet-4-6"
_UNPRICED_MODEL = "anthropic/claude-sonnet-0-0-not-a-model"


class _EmptyStubAdapter(BaseAdapter):
    """Adapter with no tasks — ``load_tasks`` reaches only ``get_task_ids``."""

    def get_task_ids(self) -> list[str]:
        return []

    def get_task(self, task_id: str) -> Any:  # pragma: no cover - no task ids
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:  # pragma: no cover - unused
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover - unused
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover - unused
        raise NotImplementedError

    def get_registry_tools(
        self, task_id: str, env: AdapterEnvironment
    ) -> list[Any]:  # pragma: no cover - unused
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def compute_golden_hash(
        self, task_id: str, env: AdapterEnvironment
    ) -> str | None:  # pragma: no cover - unused
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


def _load_tasks_warnings(model_name: str, caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warnings ``load_tasks`` emits for a run whose agent pins ``model_name``."""
    config = RunConfig(
        models={"agent": ModelConfig(provider="openrouter", name=model_name)},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/pricing_cache_rate_signals"),
    )
    orch = Orchestrator(config)
    orch.adapter = _EmptyStubAdapter({})
    with caplog.at_level(logging.WARNING):
        orch.load_tasks()
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


class TestPreflightWarning:
    """``load_tasks`` names the config key and the key that was resolved."""

    def test_incomplete_row_warns_naming_both_sides_and_a_remedy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole defect is that the resolved key differs from what was typed."""
        warnings = _load_tasks_warnings(_INCOMPLETE_ROW_MODEL, caplog)

        matching = [w for w in warnings if "cache" in w]
        assert len(matching) == 1, warnings
        message = matching[0]
        assert "models.agent.name" in message
        assert _INCOMPLETE_ROW_MODEL in message
        assert "cache_read" in message and "cache_write" in message
        assert "pricing_overlay_path" in message

    def test_unpriced_model_warns_about_the_missing_row_instead(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Different failure, different message: cost is absent, not overstated."""
        warnings = _load_tasks_warnings(_UNPRICED_MODEL, caplog)

        matching = [w for w in warnings if "does not carry" in w]
        assert len(matching) == 1, warnings
        assert "models.agent.name" in matching[0]
        assert _UNPRICED_MODEL in matching[0]
        assert "pricing-updater" in matching[0]

    def test_complete_row_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """A run that prices correctly must not train operators to ignore the log."""
        warnings = _load_tasks_warnings(_COMPLETE_ROW_MODEL, caplog)
        assert not [w for w in warnings if "pricing" in w or "cache" in w], warnings

    def test_an_overlay_supplying_the_rates_clears_the_warning(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path
    ) -> None:
        """Why the check sits in ``load_tasks`` rather than earlier.

        ``reload_pricing`` applies ``observability.pricing_overlay_path``
        before the orchestrator is constructed, so an operator who already
        supplied the missing rates must not be warned about them.
        """
        overlay = tmp_path / "overlay.json"
        overlay.write_text(
            json.dumps(
                {"models": {_INCOMPLETE_ROW_MODEL: {"cache_read": 0.3, "cache_write": 3.75}}}
            )
        )
        reload_pricing(overlay_path=overlay)
        try:
            assert resolve_pricing(_INCOMPLETE_ROW_MODEL).missing_cache_rates == ()
            warnings = _load_tasks_warnings(_INCOMPLETE_ROW_MODEL, caplog)
            assert not [w for w in warnings if "cache" in w], warnings
        finally:
            reload_pricing()


def _response_with_cache_reads() -> Any:
    """A litellm-shaped response whose usage is mostly cache reads."""
    message = MagicMock()
    message.content = "ok"
    message.tool_calls = None
    message.reasoning_content = None

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    response.usage = MagicMock()
    response.usage.prompt_tokens = 124_831
    response.usage.completion_tokens = 386
    response.usage.cache_read_input_tokens = 93_282
    response.usage.cache_creation_input_tokens = 31_544
    response.usage.prompt_tokens_details = None
    response.usage.completion_tokens_details = None
    response._hidden_params = {}
    return response


def _response_without_cache_reads() -> Any:
    response = _response_with_cache_reads()
    response.usage.prompt_tokens = 124_831
    response.usage.cache_read_input_tokens = 0
    response.usage.cache_creation_input_tokens = 0
    return response


def _generate(model_name: str, response: Any) -> Any:
    """Drive one generation with litellm's own cost unavailable.

    Local pricing is the fallback path, so the flag can only ever be about
    it — a litellm-priced call is provider-authoritative and already
    cache-aware.
    """
    client = LLMClient(
        ModelConfig(
            provider="openrouter",
            name=model_name,
            temperature=0.0,
            reasoning=ReasoningConfig(mode="off"),
        )
    )
    with (
        patch("tolokaforge.core.llm.client.completion", return_value=response),
        patch("tolokaforge.core.llm.client._litellm_response_cost", return_value=None),
    ):
        return client.generate(
            system="You are helpful.",
            messages=[Message(role=MessageRole.USER, content="Hi")],
        )


class TestPerTrialFlag:
    """``Metrics.cost_cache_rate_fallback`` — the after-the-fact signal."""

    def test_default_is_false(self) -> None:
        """Absent-by-default: every bundle written before this field reads False."""
        assert Metrics().cost_cache_rate_fallback is False

    def test_cache_tokens_against_an_incomplete_row_set_the_flag(self) -> None:
        result = _generate(_INCOMPLETE_ROW_MODEL, _response_with_cache_reads())
        assert result.cost_cache_rate_fallback is True

        metrics = Metrics()
        _AgentMetricsSink(metrics).record_generation(result)
        assert metrics.cost_cache_rate_fallback is True
        # The flag marks the number, it does not correct it: this is the
        # overstated figure the dashed row produces.
        assert metrics.cost_usd == pytest.approx(0.380283, abs=1e-9)

    def test_cache_tokens_against_a_complete_row_do_not(self) -> None:
        result = _generate(_COMPLETE_ROW_MODEL, _response_with_cache_reads())
        assert result.cost_cache_rate_fallback is False
        assert result.cost_usd == pytest.approx(0.1520796, abs=1e-9)

    def test_an_incomplete_row_with_no_cache_tokens_does_not(self) -> None:
        """A model without prompt caching is not a mispriced run."""
        result = _generate(_INCOMPLETE_ROW_MODEL, _response_without_cache_reads())
        assert result.cost_cache_rate_fallback is False

    def test_the_flag_is_sticky_across_calls(self) -> None:
        """One mispriced call makes the trial's summed cost an overestimate."""
        metrics = Metrics()
        sink = _AgentMetricsSink(metrics)
        sink.record_generation(_generate(_INCOMPLETE_ROW_MODEL, _response_with_cache_reads()))
        sink.record_generation(_generate(_INCOMPLETE_ROW_MODEL, _response_without_cache_reads()))
        assert metrics.cost_cache_rate_fallback is True
