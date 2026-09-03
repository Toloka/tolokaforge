"""Unit tests for the coding-harness driver applied to a staged native task.

The native adapter carries no coding-harness state: ``NativeAdapter.stage_task``
materialises a per-task compose staging root, and
:class:`~tolokaforge.core.drivers.coding_harness.CodingHarnessDriver` layers
onto it. The driver's ``decorate_task_description`` emits the four-key
harness metadata handshake the conductor branches on, one ``bash``
:class:`ToolSchema` targeting the per-trial compose container the CLI execs
into, and the ``test_execution`` grading dispatch — replacing the default MCP
tool list and grading path. Byte shapes only; identity to the
terminal-bench adapter's output is not asserted (``toolset``, image tags, and
compose synthesis differ).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.drivers.coding_harness import CodingHarnessDriver, HarnessSelection

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "coding_harness"


def _pack_adapter() -> NativeAdapter:
    return NativeAdapter({"tasks_glob": "task.yaml", "task_packs": [str(_PACK_ROOT)]})


def _driver(**overrides: object) -> CodingHarnessDriver:
    kwargs: dict[str, object] = {
        "agent_harness": "claude-code",
        "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
    }
    kwargs.update(overrides)
    return CodingHarnessDriver(HarnessSelection(**kwargs))


class TestEngineLoopUnchanged:
    """The default adapter carries no harness-mode state at all."""

    def test_default_task_description_has_no_harness_metadata(self) -> None:
        adapter = _pack_adapter()
        td = adapter.to_task_description("fix_factorial")
        assert "agent_harness_command" not in td.metadata

    def test_docker_stack_requirements_is_empty(self) -> None:
        adapter = _pack_adapter()
        req = adapter.docker_stack_requirements()
        assert req.image_builds == []


class TestStageTask:
    def test_stages_pack_with_compose_stack(self) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        assert staged.agent_service == "main"
        assert staged.base_build_service == "main_base"
        assert staged.base_image == "tolokaforge-native-fix_factorial-base:local"
        assert staged.compose_project_prefix == "tfnative_"
        assert staged.staging_dir != _PACK_ROOT
        assert staged.compose_file.exists()
        assert staged.compose_file.parent == staged.staging_dir

    def test_repeated_calls_land_in_the_same_staging_dir(self) -> None:
        adapter = _pack_adapter()
        first = adapter.stage_task("fix_factorial")
        second = adapter.stage_task("fix_factorial")
        assert first is not None
        assert second is not None
        assert first.staging_dir == second.staging_dir

    def test_returns_none_for_task_with_no_compose_stack(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "tasks" / "no_stack"
        task_dir.mkdir(parents=True)
        write_yaml_file(
            task_dir / "task.yaml",
            {
                "task_id": "no_stack",
                "name": "no stack",
                "category": "tool_use",
                "description": "no stack",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "actors": {"user": {"mode": "llm", "persona": "cooperative"}},
                "grading": "grading.yaml",
            },
        )
        write_yaml_file(
            task_dir / "grading.yaml",
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0},
                    "pass_threshold": 0.5,
                },
                "components": {"state_checks": {"jsonpaths": []}},
            },
        )
        adapter = NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})
        assert adapter.stage_task("no_stack") is None


class TestDriverDecoratesStagedTaskDescription:
    """Applying the driver to a staged native task reproduces the old
    harness-mode ``TaskDescription`` shape."""

    @pytest.fixture
    def staged(self):
        return _pack_adapter().stage_task("fix_factorial")

    def test_agent_tools_is_a_single_docker_compose_exec_bash(self, staged) -> None:
        adapter = _pack_adapter()
        base = adapter.to_task_description("fix_factorial")
        td = _driver().decorate_task_description(base, staged=staged)
        assert len(td.agent_tools) == 1
        tool = td.agent_tools[0]
        assert tool.name == "bash"
        assert tool.category == "compute"
        # The runner's ``DockerComposeExecToolWrapper`` reads
        # ``source.extra['service']`` + ``compose_project_prefix`` to resolve
        # the per-trial container name; the two must land verbatim.
        assert tool.source is not None
        assert tool.source.invocation_style == "docker_compose_exec"
        assert tool.source.extra == {
            "service": "main",
            "compose_project_prefix": "tfnative_",
        }
        assert tool.source.toolset == "coding_harness"

    def test_grading_is_test_execution(self, staged) -> None:
        adapter = _pack_adapter()
        base = adapter.to_task_description("fix_factorial")
        td = _driver().decorate_task_description(base, staged=staged)
        assert td.grading is not None
        assert td.grading.grading_method == "test_execution"
        assert td.grading.combine_method == "weighted"
        assert td.grading.weights == {"custom_checks": 1.0}
        assert td.grading.pass_threshold == 0.5

    def test_metadata_carries_the_four_harness_keys(self, staged) -> None:
        adapter = _pack_adapter()
        base = adapter.to_task_description("fix_factorial")
        td = _driver().decorate_task_description(base, staged=staged)
        for key in (
            "agent_harness",
            "agent_harness_version",
            "agent_harness_model",
            "agent_harness_command",
        ):
            assert key in td.metadata, f"missing metadata key {key!r}"
        assert td.metadata["agent_harness"] == "claude-code"
        assert td.metadata["agent_harness_model"] == "openrouter/anthropic/claude-sonnet-4-6"
        # The command is not empty and contains the harness argv — the
        # registry's exact bytes are locked elsewhere; here we only verify
        # the field is populated rather than empty.
        cmd = td.metadata["agent_harness_command"]
        assert isinstance(cmd, str) and len(cmd) > 0
        assert "claude" in cmd  # the CLI's binary name

    def test_command_embeds_the_real_per_task_instruction(self, staged) -> None:
        """The driver substitutes the task's own instruction into the
        command it builds — not a literal, never-expanded placeholder."""
        adapter = _pack_adapter()
        base = adapter.to_task_description("fix_factorial")
        td = _driver().decorate_task_description(base, staged=staged)
        cmd = td.metadata["agent_harness_command"]
        assert "TOLOKAFORGE_HARNESS_INSTRUCTION" not in cmd
        assert "factorial" in cmd


class TestDriverContainerLayers:
    def test_two_builds_base_then_layered(self) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        layers = _driver().apply_container_layers(staged=staged)
        assert len(layers.stack_requirements) == 2
        # Base-image build first so the harness layer can FROM its tag.
        assert layers.stack_requirements[0].service == "main_base"
        assert layers.stack_requirements[1].service == "main"
        for build in layers.stack_requirements:
            assert build.compose_file == staged.compose_file
            assert build.compose_file.exists()


class TestHarnessSpecValidation:
    def test_empty_agent_model_is_refused(self) -> None:
        # Same refusal terminal-bench emits: the CLI's own default would
        # drive the trial otherwise, silently unpinning the model under
        # measurement.
        with pytest.raises(ValueError, match="requires .*agent_model"):
            _driver(agent_model="")

    def test_unknown_harness_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            _driver(agent_harness="not-a-harness")
