"""Unit tests for :mod:`tolokaforge.core.system_prompt`.

Locks the five priority branches of :func:`build_system_prompt` and
guards the ``InProcessConductor._build_system_prompt`` delegation
against silent drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    InitialStateConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    ToolsConfig,
    UserSimulatorConfig,
)
from tolokaforge.core.system_prompt import build_system_prompt

pytestmark = pytest.mark.unit


def _task(**overrides: Any) -> TaskConfig:
    defaults: dict[str, Any] = {
        "task_id": "TASK-001",
        "description": "test task",
        "initial_state": InitialStateConfig(),
        "tools": ToolsConfig(),
        "user_simulator": UserSimulatorConfig(mode="scripted"),
        "grading": "grading.yaml",
    }
    defaults.update(overrides)
    return TaskConfig(**defaults)


def _run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


class TestBuildSystemPromptBranches:
    """Each branch of the priority chain returns the expected string."""

    def test_inline_agent_system_prompt_wins(self, tmp_path: Path) -> None:
        task = _task(policies={"agent_system_prompt": "You are a special assistant."})
        result = build_system_prompt(task=task, task_dir=tmp_path, adapter=None)
        assert result == "You are a special assistant."

    def test_adapter_branch_wraps_in_policy_envelope(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.get_system_prompt.return_value = "Adapter policy content."
        task = _task(system_prompt="__adapter__")

        result = build_system_prompt(task=task, task_dir=tmp_path, adapter=adapter)

        assert "Adapter policy content." in result
        assert result.startswith("<instructions>\n")
        assert "<policy>\nAdapter policy content.\n</policy>" in result
        adapter.get_system_prompt.assert_called_once_with(task.task_id)

    def test_adapter_returns_falsy_falls_through_to_default(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.get_system_prompt.return_value = ""
        task = _task(system_prompt="__adapter__")

        result = build_system_prompt(task=task, task_dir=tmp_path, adapter=adapter)

        assert result == "You are a helpful assistant."

    def test_system_prompt_file_returned_verbatim(self, tmp_path: Path) -> None:
        (tmp_path / "prompt.md").write_text("Custom domain prompt.")
        task = _task(system_prompt="prompt.md")

        result = build_system_prompt(task=task, task_dir=tmp_path, adapter=None)

        assert result == "Custom domain prompt."

    def test_legacy_main_policy_with_additional_composes_xml(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "tasks" / "TASK-001"
        task_dir.mkdir(parents=True)
        (tmp_path / "tasks" / "main_policy.md").write_text("Main policy content.")
        (tmp_path / "tasks" / "additional_policy.md").write_text("Additional policy content.")
        task = _task(system_prompt="additional_policy.md")

        result = build_system_prompt(task=task, task_dir=task_dir, adapter=None)

        assert "<main_policy>\nMain policy content.\n</main_policy>" in result
        assert "<tech_support_policy>\nAdditional policy content.\n</tech_support_policy>" in result

    def test_legacy_main_policy_without_additional_uses_main_only(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "tasks" / "TASK-001"
        task_dir.mkdir(parents=True)
        (tmp_path / "tasks" / "main_policy.md").write_text("Main policy content.")
        task = _task(system_prompt="missing_additional.md")

        result = build_system_prompt(task=task, task_dir=task_dir, adapter=None)

        assert "<main_policy>" not in result
        assert "Main policy content." in result
        assert "<policy>\nMain policy content.\n</policy>" in result

    def test_minimal_default_includes_guidance_and_browser_url(self, tmp_path: Path) -> None:
        task = _task(
            policies={"guidance": ["step one", "step two"]},
            tools=ToolsConfig(agent={"browser": {"initial_url": "http://portal.local:8080"}}),
        )

        result = build_system_prompt(task=task, task_dir=tmp_path, adapter=None)

        assert result.startswith("You are a helpful assistant.")
        assert "- step one" in result
        assert "- step two" in result
        assert "http://portal.local:8080" in result

    def test_minimal_default_bare(self, tmp_path: Path) -> None:
        task = _task()
        result = build_system_prompt(task=task, task_dir=tmp_path, adapter=None)
        assert result == "You are a helpful assistant."


class TestConductorDelegationParity:
    """``InProcessConductor._build_system_prompt`` must call through cleanly."""

    def _conductor(self, adapter: Any) -> Any:
        from tolokaforge.core.conductor import InProcessConductor

        return InProcessConductor(
            adapter=adapter,
            artifact_writer=MagicMock(),
            config=_run_config(),
            logger=MagicMock(),
            verbose=False,
            strict=False,
            agent_client=MagicMock(),
            runtime_backend=MagicMock(),
            trial_grader=MagicMock(),
            output_dir=Path("/tmp"),
            request_limiter=MagicMock(),
        )

    def test_delegator_matches_module_helper_for_inline_prompt(self, tmp_path: Path) -> None:
        task = _task(policies={"agent_system_prompt": "Inline prompt body."})
        adapter = MagicMock()
        conductor = self._conductor(adapter)

        from_method = conductor._build_system_prompt(task, [], tmp_path)
        from_helper = build_system_prompt(task=task, task_dir=tmp_path, adapter=adapter)

        assert from_method == from_helper == "Inline prompt body."

    def test_delegator_matches_module_helper_for_adapter_branch(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.get_system_prompt.return_value = "From adapter."
        task = _task(system_prompt="__adapter__")
        conductor = self._conductor(adapter)

        from_method = conductor._build_system_prompt(task, [], tmp_path)
        from_helper = build_system_prompt(task=task, task_dir=tmp_path, adapter=adapter)

        assert from_method == from_helper
        assert "From adapter." in from_method
