"""Unit tests for the native adapter's coding-harness opt-in.

The native adapter inherits :class:`CodingHarnessAdapterMixin` so a run
declaring ``models.agent.harness`` reaches it. When ``params["agent_harness"]``
is set, ``to_task_description`` emits the four-key harness metadata handshake
the conductor branches on, one ``bash`` :class:`ToolSchema` targeting the
per-trial compose container the CLI execs into, and the ``test_execution``
grading dispatch — replacing the default MCP tool list and grading path.
Byte shapes only; identity to the terminal-bench adapter's output is not
asserted (``toolset``, image tags, and compose synthesis differ).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.adapters.native_harness_synthesis import PROJECT_PREFIX

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "coding_harness"


def _params(**overrides: object) -> dict[str, object]:
    """Base adapter params pointing at the shipped coding-harness pack."""
    base = {
        "tasks_glob": "task.yaml",
        "task_packs": [str(_PACK_ROOT)],
    }
    base.update(overrides)
    return base


class TestCapabilityFlag:
    def test_adapter_class_declares_supports_coding_harness(self) -> None:
        # The orchestrator's config-validation gate reads this attribute to
        # decide whether ``models.agent.harness`` can route to the adapter.
        assert NativeAdapter.supports_coding_harness is True


class TestEngineLoopUnchanged:
    """Under the engine loop the harness-mode branch stays off entirely."""

    def test_agent_harness_absent_keeps_default_task_description(self) -> None:
        adapter = NativeAdapter(_params())
        td = adapter.to_task_description("fix_factorial")
        # Engine loop mints no harness metadata.
        assert "agent_harness_command" not in td.metadata

    def test_docker_stack_requirements_is_empty_under_engine_loop(self) -> None:
        adapter = NativeAdapter(_params())
        req = adapter.docker_stack_requirements()
        assert req.image_builds == []


class TestHarnessModeTaskDescription:
    """With ``agent_harness`` set the description carries the harness shape."""

    @pytest.fixture
    def adapter(self) -> NativeAdapter:
        return NativeAdapter(
            _params(
                agent_harness="claude-code",
                agent_model="openrouter/anthropic/claude-sonnet-4-6",
            )
        )

    def test_agent_tools_is_a_single_docker_compose_exec_bash(self, adapter: NativeAdapter) -> None:
        td = adapter.to_task_description("fix_factorial")
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
            "compose_project_prefix": PROJECT_PREFIX,
        }
        # `toolset="native"` is the native adapter's own identity — a
        # terminal-bench trial keeps ``toolset="terminal_bench"``, so the
        # runner-side dispatch factory can tell them apart.
        assert tool.source.toolset == "native"

    def test_grading_is_test_execution(self, adapter: NativeAdapter) -> None:
        td = adapter.to_task_description("fix_factorial")
        assert td.grading is not None
        assert td.grading.grading_method == "test_execution"
        assert td.grading.combine_method == "weighted"
        assert td.grading.weights == {"custom_checks": 1.0}
        assert td.grading.pass_threshold == 0.5

    def test_metadata_carries_the_four_harness_keys(self, adapter: NativeAdapter) -> None:
        td = adapter.to_task_description("fix_factorial")
        for key in (
            "agent_harness",
            "agent_harness_version",
            "agent_harness_model",
            "agent_harness_command",
        ):
            assert key in td.metadata, f"missing metadata key {key!r}"
        assert td.metadata["agent_harness"] == "claude-code"
        assert td.metadata["agent_harness_model"] == ("openrouter/anthropic/claude-sonnet-4-6")
        # The command is not empty and contains the harness argv — the mixin's
        # exact bytes are locked by test_adapter_support; here we only verify
        # the field is populated by the mixin path rather than empty.
        cmd = td.metadata["agent_harness_command"]
        assert isinstance(cmd, str) and len(cmd) > 0
        assert "claude" in cmd  # the CLI's binary name

    def test_environment_manifest_points_at_synthesised_compose(
        self, adapter: NativeAdapter
    ) -> None:
        td = adapter.to_task_description("fix_factorial")
        assert td.environment_manifest is not None
        compose = td.environment_manifest.compose_file
        assert compose.exists()
        # The staged compose lives under a per-task staging root distinct
        # from the pack: the adapter must synthesise the runner+db-service
        # sidecars, not exec into the pack's own file.
        assert compose.parent != _PACK_ROOT
        # runner_service names the injected sidecar, not the pack's `main`.
        assert td.environment_manifest.runner_service == "runner"


class TestHarnessModeDockerStackRequirements:
    def test_two_builds_base_then_layered(self) -> None:
        adapter = NativeAdapter(
            _params(
                agent_harness="claude-code",
                agent_model="openrouter/anthropic/claude-sonnet-4-6",
            )
        )
        req = adapter.docker_stack_requirements()
        assert len(req.image_builds) == 2
        # Base-image build first so the harness layer can FROM its tag.
        assert req.image_builds[0].service == "main_base"
        assert req.image_builds[1].service == "main"
        # Both entries reference the synthesised compose file, not the pack's own.
        for build in req.image_builds:
            assert build.compose_file.name == "docker-compose.yaml"
            assert build.compose_file.exists()


class TestHarnessSpecValidation:
    def test_empty_agent_model_is_refused(self) -> None:
        # Same refusal terminal-bench emits: the CLI's own default would drive
        # the trial otherwise, silently unpinning the model under measurement.
        with pytest.raises(ValueError, match="requires .*agent_model"):
            NativeAdapter(_params(agent_harness="claude-code", agent_model=""))

    def test_unknown_harness_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            NativeAdapter(_params(agent_harness="not-a-harness"))
