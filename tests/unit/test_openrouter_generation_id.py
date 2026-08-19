"""OpenRouter generation-id capture, from response header to persisted bundle.

The id is the only key that joins a call we made to the routing decision
OpenRouter made for it: querying
``https://openrouter.ai/api/v1/generation?id=<id>`` afterwards reports which
upstream provider actually served the call. Without it persisted, a result
suspected of being a routing artefact can only be re-run, never checked.

Header name is ``x-generation-id`` — verified by live probe 2026-08-19, which
returned exactly that and no ``x-openrouter-generation-id``. litellm re-keys
raw provider headers as ``llm_provider-<name>`` into
``_hidden_params['additional_headers']``, so the tests below drive the
prefixed form the engine actually sees, and the bare form a direct-httpx
caller would hand it.

Every non-OpenRouter route (Anthropic direct, Google direct, …) sends no such
header, so the absent path is pinned at every layer alongside the present one:
absence must persist as ``None`` / an empty list, never as a crash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.llm.usage import (
    OPENROUTER_GENERATION_ID_HEADER,
    UsageExtractor,
    extract_openrouter_generation_id,
)
from tolokaforge.core.models import Message, MessageRole, Metrics, ModelConfig, Trajectory
from tolokaforge.core.output_writer import OutputWriter
from tolokaforge.core.runner import _AgentMetricsSink

pytestmark = pytest.mark.unit


_GEN_ID = "gen-1787132417-e6DthuPJjrFMFf46ae5F"


def _response(
    *,
    headers: dict[str, Any] | None = None,
    hidden_params: dict[str, Any] | None = None,
    with_usage: bool = True,
) -> Any:
    """A litellm-shaped response, optionally carrying provider headers."""
    mock_message = MagicMock()
    mock_message.content = "ok"
    mock_message.tool_calls = None
    mock_message.reasoning_content = None

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    response = MagicMock()
    response.choices = [mock_choice]
    if with_usage:
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50
        response.usage.cache_read_input_tokens = 0
        response.usage.cache_creation_input_tokens = 0
        response.usage.prompt_tokens_details = None
        response.usage.completion_tokens_details = None
    else:
        response.usage = None

    params = dict(hidden_params or {})
    if headers is not None:
        params["additional_headers"] = headers
    response._hidden_params = params
    return response


def _client(provider: str = "openrouter", name: str = "moonshotai/kimi-k3") -> LLMClient:
    cfg = ModelConfig(
        provider=provider,
        name=name,
        temperature=0.0,
        reasoning=ReasoningConfig(mode="off"),
    )
    return LLMClient(cfg)


def _generate(client: LLMClient, response: Any) -> Any:
    with patch("tolokaforge.core.llm.client.completion", return_value=response):
        return client.generate(
            system="You are helpful.",
            messages=[Message(role=MessageRole.USER, content="Hi")],
        )


class TestExtractFromResponseHeaders:
    """The extractor reads the header litellm actually surfaces."""

    def test_reads_litellm_prefixed_header(self) -> None:
        response = _response(headers={f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": _GEN_ID})
        assert extract_openrouter_generation_id(response) == _GEN_ID

    def test_reads_unprefixed_header(self) -> None:
        """A direct-httpx caller hands us the bare provider name."""
        response = _response(headers={OPENROUTER_GENERATION_ID_HEADER: _GEN_ID})
        assert extract_openrouter_generation_id(response) == _GEN_ID

    def test_header_match_is_case_insensitive(self) -> None:
        """HTTP header names are case-insensitive; libraries normalise differently."""
        response = _response(headers={"LLM_PROVIDER-X-Generation-Id": _GEN_ID})
        assert extract_openrouter_generation_id(response) == _GEN_ID

    def test_absent_header_yields_none(self) -> None:
        """The normal state for Anthropic direct / Google direct."""
        response = _response(headers={"llm_provider-content-type": "application/json"})
        assert extract_openrouter_generation_id(response) is None

    def test_no_additional_headers_yields_none(self) -> None:
        response = _response(hidden_params={"response_cost": 0.001})
        assert extract_openrouter_generation_id(response) is None

    def test_no_hidden_params_yields_none(self) -> None:
        response = MagicMock(spec=[])
        assert extract_openrouter_generation_id(response) is None

    def test_empty_header_value_yields_none(self) -> None:
        """An empty id joins to nothing, so it is absence, not a value."""
        response = _response(headers={f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": ""})
        assert extract_openrouter_generation_id(response) is None

    def test_wrong_header_name_is_not_matched(self) -> None:
        """Guards the name itself: the header is NOT x-openrouter-generation-id."""
        response = _response(headers={"llm_provider-x-openrouter-generation-id": _GEN_ID})
        assert extract_openrouter_generation_id(response) is None


class TestUsageExtractorCarriesTheId:
    """The per-call record carries the id alongside cost / latency."""

    def test_call_record_carries_id(self) -> None:
        response = _response(headers={f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": _GEN_ID})
        usage = UsageExtractor().extract(response)
        assert len(usage.calls) == 1
        assert usage.calls[0].openrouter_generation_id == _GEN_ID

    def test_call_record_id_is_none_without_header(self) -> None:
        usage = UsageExtractor().extract(_response(headers={}))
        assert usage.calls[0].openrouter_generation_id is None


class TestGenerationResultCarriesTheId:
    """``LLMClient.generate`` surfaces the id on the result it returns."""

    def test_generate_surfaces_id(self) -> None:
        response = _response(headers={f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": _GEN_ID})
        result = _generate(_client(), response)
        assert result.openrouter_generation_id == _GEN_ID
        assert result.usage.calls[0].openrouter_generation_id == _GEN_ID

    def test_generate_without_header_yields_none(self) -> None:
        """A direct-Anthropic route must not crash and must not invent an id."""
        result = _generate(_client(provider="anthropic", name="claude-opus-4-7"), _response())
        assert result.openrouter_generation_id is None

    def test_id_survives_a_response_with_no_usage_block(self) -> None:
        """No usage block means no call record, but the routing is still recorded."""
        response = _response(
            headers={f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": _GEN_ID},
            with_usage=False,
        )
        result = _generate(_client(), response)
        assert result.usage.calls == ()
        assert result.openrouter_generation_id == _GEN_ID


class TestMetricsAccumulation:
    """One id per agent call, in call order, on the trial's metrics."""

    def test_ids_accumulate_in_call_order(self) -> None:
        metrics = Metrics()
        recorder = _AgentMetricsSink(metrics)
        client = _client()
        for suffix in ("a", "b"):
            headers = {f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": f"gen-{suffix}"}
            recorder.record_generation(_generate(client, _response(headers=headers)))

        assert metrics.openrouter_generation_ids == ["gen-a", "gen-b"]

    def test_unrouted_calls_contribute_no_entry(self) -> None:
        """The list is shorter than ``api_calls`` rather than padded with nulls."""
        metrics = Metrics()
        recorder = _AgentMetricsSink(metrics)
        client = _client()
        recorder.record_generation(
            _generate(
                client,
                _response(headers={f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": _GEN_ID}),
            )
        )
        recorder.record_generation(_generate(client, _response(headers={})))

        assert metrics.api_calls == 2
        assert metrics.openrouter_generation_ids == [_GEN_ID]


class TestPersistedBundle:
    """The id reaches disk in both artifacts and survives the round-trip."""

    @staticmethod
    def _trajectory(generation_id: str | None) -> Trajectory:
        from datetime import datetime, timezone

        ts = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
        metrics = Metrics()
        recorder = _AgentMetricsSink(metrics)
        headers = (
            {f"llm_provider-{OPENROUTER_GENERATION_ID_HEADER}": generation_id}
            if generation_id is not None
            else {}
        )
        result = _generate(_client(), _response(headers=headers))
        recorder.record_generation(result)
        return Trajectory(
            task_id="openrouter-gen-id-001",
            trial_index=0,
            start_ts=ts,
            end_ts=ts,
            messages=[
                Message(role=MessageRole.USER, content="Hi", ts=ts),
                Message(
                    role=MessageRole.ASSISTANT,
                    content=result.text,
                    openrouter_generation_id=result.openrouter_generation_id,
                    ts=ts,
                ),
            ],
            metrics=metrics,
        )

    def test_metrics_yaml_carries_flat_list_and_per_call_id(self, tmp_path: Path) -> None:
        OutputWriter(tmp_path).write_metrics(self._trajectory(_GEN_ID))

        persisted = yaml.safe_load((tmp_path / "metrics.yaml").read_text())
        assert persisted["openrouter_generation_ids"] == [_GEN_ID]
        assert persisted["usage"]["calls"][0]["openrouter_generation_id"] == _GEN_ID

    def test_metrics_yaml_omits_nothing_when_route_is_not_openrouter(self, tmp_path: Path) -> None:
        OutputWriter(tmp_path).write_metrics(self._trajectory(None))

        persisted = yaml.safe_load((tmp_path / "metrics.yaml").read_text())
        assert persisted["openrouter_generation_ids"] == []
        assert persisted["usage"]["calls"][0]["openrouter_generation_id"] is None

    def test_trajectory_yaml_attaches_id_to_the_assistant_message(self, tmp_path: Path) -> None:
        OutputWriter(tmp_path).write_trajectory(self._trajectory(_GEN_ID))

        persisted = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        by_role = {m["role"]: m for m in persisted["messages"]}
        assert by_role["assistant"]["openrouter_generation_id"] == _GEN_ID
        assert by_role["user"]["openrouter_generation_id"] is None

    def test_bundle_round_trips_through_model_validate(self, tmp_path: Path) -> None:
        """Judge replay and trace replay both re-validate a persisted bundle."""
        writer = OutputWriter(tmp_path)
        trajectory = self._trajectory(_GEN_ID)
        writer.write_trajectory(trajectory)
        writer.write_metrics(trajectory)

        raw = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
        raw["metrics"] = yaml.safe_load((tmp_path / "metrics.yaml").read_text())
        raw["metrics"].pop("schema_version", None)
        raw["metrics"].pop("tool_usage_detail", None)
        restored = Trajectory.model_validate(raw)

        assert restored.metrics.openrouter_generation_ids == [_GEN_ID]
        assert restored.metrics.usage.calls[0].openrouter_generation_id == _GEN_ID
        assert restored.messages[1].openrouter_generation_id == _GEN_ID
