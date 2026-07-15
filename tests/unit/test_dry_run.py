"""Unit tests for :mod:`tolokaforge.core.dry_run`.

Locks the :func:`materialize_dry_run_sample` contract (fields populated,
no HTTP, no LLM client construction), the :func:`load_tasks_for_dry_run`
skip of the TypeSense preflight, and byte-for-byte parity between the
dry-run tool spec and what ``TrialArtifactWriter.write_tools_schemas``
would write for a real trial.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.dry_run import (
    DryRunSample,
    load_tasks_for_dry_run,
    materialize_dry_run_sample,
    tool_schema_to_openai_dict,
)
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TypeSenseConfig,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.unit


TOOL_USE_DATASET = (
    Path(__file__).resolve().parents[2] / "examples" / "native" / "tool_use" / "dataset"
)


def _tool_use_adapter() -> NativeAdapter:
    return NativeAdapter(
        {
            "tasks_glob": "**/task.yaml",
            "task_packs": [str(TOOL_USE_DATASET)],
        }
    )


def _tool_use_run_config(**orchestrator_overrides: Any) -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(
                provider="openrouter",
                name="anthropic/claude-sonnet-4-6",
            ),
        },
        orchestrator=OrchestratorConfig(
            workers=1, repeats=1, auto_start_services=False, **orchestrator_overrides
        ),
        evaluation=EvaluationConfig(
            projects=[str(TOOL_USE_DATASET)],
            tasks_glob="**/task.yaml",
            output_dir="/tmp/dry_run_test",
        ),
    )


class TestMaterializeDryRunSample:
    def test_materialize_returns_expected_dry_run_sample(self) -> None:
        adapter = _tool_use_adapter()
        task = adapter.get_task("tool_use_public_example_01")
        agent = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4-6")

        sample = materialize_dry_run_sample(
            task=task,
            adapter=adapter,
            agent_config=agent,
            judge_config=None,
            runtime_choice="shared",
        )

        assert isinstance(sample, DryRunSample)
        assert sample.task_id == "tool_use_public_example_01"
        assert sample.trial_index == 0
        assert sample.system_prompt.startswith("You are a helpful assistant.")
        assert sample.user_prompt_is_literal is True
        assert "T-100" in sample.user_prompt_text
        assert len(sample.tool_spec) >= 1
        first_tool = sample.tool_spec[0]
        assert first_tool["type"] == "function"
        assert set(first_tool["function"].keys()) == {"name", "description", "parameters"}
        assert sample.agent_model_line == (
            "openrouter/anthropic/claude-sonnet-4-6 · preset: anthropic"
        )
        assert sample.judge_model_line == "(none)"
        assert sample.runtime_line == "shared"

    def test_materialize_placeholder_when_no_initial_user_message(self) -> None:
        adapter = _tool_use_adapter()
        task = adapter.get_task("tool_use_public_example_01")
        task_no_msg = task.model_copy(update={"initial_user_message": None})
        agent = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4-6")

        sample = materialize_dry_run_sample(
            task=task_no_msg,
            adapter=adapter,
            agent_config=agent,
            judge_config=None,
            runtime_choice="shared",
        )

        assert sample.user_prompt_is_literal is False
        assert "generated at runtime by user simulator" in sample.user_prompt_text
        assert f"mode={task.user_simulator.mode}" in sample.user_prompt_text
        assert f"persona={task.user_simulator.persona}" in sample.user_prompt_text

    def test_materialize_no_http_via_respx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No socket opens. Belt-and-braces: patch httpx.Client.send AND
        litellm.completion (both bindings) with raise-on-call sentinels."""
        import httpx
        import litellm

        def _raise_http(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "dry-run must not open an HTTP connection (httpx.Client.send called)"
            )

        def _raise_litellm(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("dry-run must not invoke litellm.completion")

        monkeypatch.setattr(httpx.Client, "send", _raise_http)
        monkeypatch.setattr(litellm, "completion", _raise_litellm)
        monkeypatch.setattr("tolokaforge.core.llm.client.completion", _raise_litellm, raising=False)

        adapter = _tool_use_adapter()
        task = adapter.get_task("tool_use_public_example_01")
        agent = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4-6")

        sample = materialize_dry_run_sample(
            task=task,
            adapter=adapter,
            agent_config=agent,
            judge_config=None,
            runtime_choice="shared",
        )

        assert sample.system_prompt
        assert sample.tool_spec

    def test_tool_spec_sanitization_matches_real_wire(self, tmp_path: Path) -> None:
        """``sample.tool_spec`` equals what ``TrialArtifactWriter.write_tools_schemas``
        would persist for the same task — the audit trail file the production
        run leaves in ``trial_dir/tools_schemas.yaml``."""
        adapter = _tool_use_adapter()
        task = adapter.get_task("tool_use_public_example_01")
        agent = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4-6")

        sample = materialize_dry_run_sample(
            task=task,
            adapter=adapter,
            agent_config=agent,
            judge_config=None,
            runtime_choice="shared",
        )

        task_description = adapter.to_task_description(task.task_id)
        capabilities = build_capabilities(agent.name, agent.provider, overrides=agent.capabilities)
        wire_schemas = list(
            capabilities.schema_sanitizer.sanitize(
                [tool_schema_to_openai_dict(ts) for ts in task_description.agent_tools]
            )
        )
        writer = FileArtifactWriter()
        writer.write_tools_schemas(tmp_path, wire_schemas)
        persisted = yaml.safe_load((tmp_path / "tools_schemas.yaml").read_text())

        assert sample.tool_spec == persisted


class TestLoadTasksForDryRun:
    def test_load_tasks_returns_adapter_and_tasks(self) -> None:
        adapter, tasks = load_tasks_for_dry_run(run_config=_tool_use_run_config())

        assert isinstance(adapter, NativeAdapter)
        task_ids = {t.task_id for t in tasks}
        assert task_ids == {"tool_use_public_example_01", "tool_use_public_example_02"}

    def test_load_tasks_for_dry_run_skips_typesense_preflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with ``orchestrator.typesense.enabled=True``, dry-run must not
        call ``create_typesense_server`` (would attempt to start Docker)."""

        def _raise_typesense(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "dry-run must not start TypeSense — create_typesense_server called"
            )

        monkeypatch.setattr(
            "tolokaforge.core.search.typesense_server.create_typesense_server",
            _raise_typesense,
        )

        run_config = _tool_use_run_config(
            typesense=TypeSenseConfig(enabled=True, mode="local"),
        )

        adapter, tasks = load_tasks_for_dry_run(run_config=run_config)

        assert len(tasks) == 2
        assert isinstance(adapter, NativeAdapter)
