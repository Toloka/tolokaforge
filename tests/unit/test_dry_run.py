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
from rich.console import Console

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.adapter_registry import CompositeAdapter
from tolokaforge.core.dry_run import (
    DryRunSample,
    DryRunUnit,
    load_harness_entry_units_for_dry_run,
    load_tasks_for_dry_run,
    materialize_dry_run_sample,
    tool_schema_to_openai_dict,
)
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.models import (
    ActorSpec,
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TypeSenseConfig,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.dx.dry_run_render import render_dry_run_sample
from tolokaforge_coding_harnesses import ENGINE_LOOP

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

        sim = task.resolve_user_simulator()
        assert sample.user_prompt_is_literal is False
        assert "generated at runtime by user simulator" in sample.user_prompt_text
        assert f"mode={sim.mode}" in sample.user_prompt_text
        assert f"persona={sim.persona}" in sample.user_prompt_text

    def test_materialize_shows_the_agent_opening_line(self) -> None:
        adapter = _tool_use_adapter()
        task = adapter.get_task("tool_use_public_example_01")
        opened = task.model_copy(
            update={
                "actors": {
                    **(task.actors or {}),
                    "user": ActorSpec(first_agent_message="Hi! How can I help you today?"),
                }
            }
        )
        agent = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4-6")

        sample = materialize_dry_run_sample(
            task=opened,
            adapter=adapter,
            agent_config=agent,
            judge_config=None,
            runtime_choice="shared",
        )
        console = Console(record=True, width=120)
        render_dry_run_sample(sample=sample, console=console)

        assert sample.agent_opening_line == "Hi! How can I help you today?"
        rendered = console.export_text()
        assert rendered.index("Agent opening line:") < rendered.index("User prompt:")
        assert "Hi! How can I help you today?" in rendered

    def test_materialize_has_no_opening_line_by_default(self) -> None:
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

        assert sample.agent_opening_line is None

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


def _coding_harness_run_config() -> RunConfig:
    """Single native adapter with a coding-harness agent model.

    ``models.agent.harness`` is the canonical coding-harness selector; a real
    run injects it (and ``models.agent.name``) into the adapter's construction
    params as ``agent_harness`` / ``agent_model``. The single-adapter dry-run
    path must resolve the same injection.
    """
    return RunConfig(
        models={
            "agent": ModelConfig(
                provider="openrouter",
                name="anthropic/claude-sonnet-4-6",
                harness="claude-code",
            ),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            projects=[str(TOOL_USE_DATASET)],
            tasks_glob="**/task.yaml",
            output_dir="/tmp/dry_run_harness",
        ),
    )


class TestLoadTasksForDryRunSharesRealAssembly:
    """The single-adapter dry-run resolves adapter params through the
    orchestrator's own ``_create_adapter``, so a coding-harness run's
    ``agent_harness`` / ``agent_model`` injection is surfaced exactly as a real
    run assembles it."""

    def test_coding_harness_injection_is_surfaced(self) -> None:
        adapter, tasks = load_tasks_for_dry_run(run_config=_coding_harness_run_config())

        assert isinstance(adapter, NativeAdapter)
        assert tasks
        # The gap this closes: the hand-rolled builder omitted this injection, so
        # a coding-harness dry-run previewed engine-loop wiring, not the CLI's.
        assert adapter.agent_harness == "claude-code"
        assert adapter.agent_model == "anthropic/claude-sonnet-4-6"

    def test_dry_run_adapter_matches_the_real_run_adapter(self) -> None:
        """Parity with what a live single-adapter run assembles — the dry-run and
        the real run now build the adapter through the same code."""
        from tolokaforge.core.orchestrator import Orchestrator

        run_config = _coding_harness_run_config()
        dry_adapter, _ = load_tasks_for_dry_run(run_config=run_config)
        real_adapter = Orchestrator(run_config)._create_adapter()

        assert isinstance(real_adapter, NativeAdapter)
        assert (dry_adapter.agent_harness, dry_adapter.agent_model) == (
            real_adapter.agent_harness,
            real_adapter.agent_model,
        )

    def test_plain_native_dry_run_is_unchanged(self) -> None:
        """Regression: a run with no ``models.agent.harness`` injects nothing —
        the adapter stays on the engine loop, exactly as before."""
        adapter, _ = load_tasks_for_dry_run(run_config=_tool_use_run_config())

        assert isinstance(adapter, NativeAdapter)
        assert adapter.agent_harness == ENGINE_LOOP
        assert adapter.agent_model == ""


def _multi_harness_run_config() -> RunConfig:
    """Two native entries over the tool_use dataset, each pinned to one task.

    Two entries of the same adapter type with different ``task_ids`` allow-lists:
    the dry-run must resolve each entry's own task through the composite rather
    than discovering the dataset's whole task list once.
    """
    return RunConfig(
        models={"agent": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4-6")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            projects=[str(TOOL_USE_DATASET)],
            tasks_glob="**/task.yaml",
            output_dir="/tmp/dry_run_multi",
        ),
        harnesses={
            "entries": [
                {"name": "first", "adapter": "native", "task_ids": ["tool_use_public_example_01"]},
                {"name": "second", "adapter": "native", "task_ids": ["tool_use_public_example_02"]},
            ]
        },
    )


class TestLoadHarnessEntryUnitsForDryRun:
    def test_resolves_one_unit_per_entry_without_native_fallthrough(self) -> None:
        units = load_harness_entry_units_for_dry_run(run_config=_multi_harness_run_config())

        assert [u.entry for u in units] == ["first", "second"]
        # Each entry's allow-list is honoured: one task per entry, not the
        # whole dataset discovered once and shared.
        assert [u.task.task_id for u in units] == [
            "tool_use_public_example_01",
            "tool_use_public_example_02",
        ]
        for unit in units:
            assert isinstance(unit, DryRunUnit)
            # Per-entry adapters, never the composite — each resolves its task.
            assert isinstance(unit.adapter, NativeAdapter)
            assert not isinstance(unit.adapter, CompositeAdapter)
            assert unit.agent_config.name == "anthropic/claude-sonnet-4-6"

    def test_both_entries_materialize_first_turn_wiring(self) -> None:
        """Each resolved unit renders a full first-turn sample (the issue's crash
        point): system prompt + sanitized tool spec, tagged with its entry."""
        units = load_harness_entry_units_for_dry_run(run_config=_multi_harness_run_config())

        samples = [
            materialize_dry_run_sample(
                task=unit.task,
                adapter=unit.adapter,
                agent_config=unit.agent_config,
                judge_config=unit.judge_config,
                runtime_choice="shared",
                entry=unit.entry,
            )
            for unit in units
        ]

        assert {s.entry for s in samples} == {"first", "second"}
        for sample in samples:
            assert sample.system_prompt
            assert sample.tool_spec

        console = Console(record=True, width=120)
        for sample in samples:
            render_dry_run_sample(sample=sample, console=console)
        rendered = console.export_text()
        # The entry qualifies the panel title so the same task id under two
        # entries never collides.
        assert "first/tool_use_public_example_01" in rendered
        assert "second/tool_use_public_example_02" in rendered

    def test_single_adapter_sample_title_is_unqualified(self) -> None:
        """Regression: a single-adapter sample carries no entry and renders the
        bare ``Task <id>`` title — the multi-harness entry tag never leaks in."""
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

        assert sample.entry is None
        console = Console(record=True, width=120)
        render_dry_run_sample(sample=sample, console=console)
        rendered = console.export_text()
        assert "Task tool_use_public_example_01" in rendered
        assert "/tool_use_public_example_01" not in rendered
