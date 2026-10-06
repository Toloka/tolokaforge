"""Discovery + translation tests for the Harbor adapter (no Docker / keys)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_harbor.adapter import HarborAdapter

pytestmark = pytest.mark.unit

# The vendored TB2 pack the adapter discovers through the terminal-bench parser.
_PACK = Path(__file__).resolve().parents[3] / "examples" / "harbor"
_TASK_ID = "write-release-note"


@pytest.fixture
def adapter(tmp_path) -> HarborAdapter:
    return HarborAdapter(
        {
            "harbor_tasks_dir": str(_PACK),
            "agent_model": "anthropic/claude-sonnet-4-5",
            "staging_root": str(tmp_path / "staging"),
        }
    )


def test_discovers_tb2_task_via_terminal_bench_parser(adapter: HarborAdapter):
    assert _TASK_ID in adapter.get_task_ids()
    assert adapter.get_task_dir(_TASK_ID) == _PACK / _TASK_ID


def test_get_task_translates_to_taskconfig(adapter: HarborAdapter):
    task = adapter.get_task(_TASK_ID)
    assert task.adapter_type == "harbor"
    assert task.category == "harbor"
    assert task.grading == "__adapter__"
    assert list(task.tools.agent["enabled"]) == ["bash"]


def test_task_id_filter(tmp_path):
    adapter = HarborAdapter(
        {"harbor_tasks_dir": str(_PACK), "task_ids": ["nope"], "staging_root": str(tmp_path)}
    )
    assert adapter.get_task_ids() == []


def test_to_task_description_builds_terminus_2_command(adapter: HarborAdapter):
    desc = adapter.to_task_description(_TASK_ID)
    assert desc.adapter_type == "harbor"

    command = desc.metadata["agent_harness_command"]
    assert command.startswith("harbor run")
    assert "-p /app/task" in command
    assert "-a terminus-2" in command
    # Terminus keeps the model slug verbatim (its openrouter/ prefix routes
    # through LiteLLM); the adapter never strips it.
    assert "-m anthropic/claude-sonnet-4-5" in command
    assert "-e docker" in command
    # ``--jobs-dir`` targets the environment's identity-mounted (absolute) job
    # dir, not an in-container ``/logs`` path Harbor's sandbox could not
    # bind-mount via the host daemon.
    jobs_dir = adapter._environment(_TASK_ID).harbor_jobs_dir
    assert jobs_dir.name == "harbor_jobs"
    assert f"--jobs-dir {jobs_dir}" in command
    # Unique per trial: the job name expands the engine's per-trial slug inside
    # the container, so concurrent trials of one task never share a job dir.
    assert '--job-name tf-"${TOLOKAFORGE_TRIAL_SLUG}"' in command
    # Real Terminus runs install tooling per trial; the setup budget is lifted.
    assert "--agent-setup-timeout-multiplier 10" in command
    assert "-k 1 -y" in command

    assert desc.metadata["agent_harness"] == "harbor"
    assert desc.metadata["harbor_agent"] == "terminus-2"
    assert desc.metadata["harbor_version"] == "0.23.0"


def test_delegated_contract_one_exec_tool_and_test_execution(adapter: HarborAdapter):
    desc = adapter.to_task_description(_TASK_ID)
    assert len(desc.agent_tools) == 1
    tool = desc.agent_tools[0]
    assert tool.name == "bash"
    assert tool.source.invocation_style.value == "docker_compose_exec"
    assert tool.source.extra["service"] == "main"
    # The single exec must cover the whole budget; the task declares 120s.
    assert tool.timeout_s == 120.0

    assert desc.metadata["agent_harness_command"].strip()
    assert desc.grading.grading_method == "test_execution"
    assert desc.environment_manifest is not None


def test_oracle_agent_omits_model_and_setup_multiplier(tmp_path):
    adapter = HarborAdapter(
        {"harbor_tasks_dir": str(_PACK), "agent": "oracle", "staging_root": str(tmp_path)}
    )
    command = adapter.to_task_description(_TASK_ID).metadata["agent_harness_command"]
    assert "-a oracle" in command
    assert " -m " not in command
    # Oracle runs no agent setup, so the setup-budget flag is omitted — this is
    # what keeps the keyless oracle command (and its e2e) unaffected.
    assert "--agent-setup-timeout-multiplier" not in command


def test_terminus_2_requires_model(tmp_path):
    adapter = HarborAdapter({"harbor_tasks_dir": str(_PACK), "staging_root": str(tmp_path)})
    with pytest.raises(ValueError, match="agent_model"):
        adapter.to_task_description(_TASK_ID)


def test_unsupported_vendor_cli_agent_fails_loud(tmp_path):
    with pytest.raises(ValueError, match="unsupported agent"):
        HarborAdapter(
            {
                "harbor_tasks_dir": str(_PACK),
                "agent": "claude-code",
                "agent_model": "anthropic/claude-sonnet-4-5",
                "staging_root": str(tmp_path),
            }
        )


def test_agent_setup_timeout_multiplier_is_configurable(tmp_path):
    adapter = HarborAdapter(
        {
            "harbor_tasks_dir": str(_PACK),
            "agent_model": "anthropic/claude-sonnet-4-5",
            "agent_setup_timeout_multiplier": 4,
            "staging_root": str(tmp_path),
        }
    )
    command = adapter.to_task_description(_TASK_ID).metadata["agent_harness_command"]
    assert "--agent-setup-timeout-multiplier 4" in command


def test_sandbox_backend_passes_through(tmp_path):
    adapter = HarborAdapter(
        {
            "harbor_tasks_dir": str(_PACK),
            "agent_model": "anthropic/claude-sonnet-4-5",
            "sandbox_backend": "daytona",
            "staging_root": str(tmp_path),
        }
    )
    command = adapter.to_task_description(_TASK_ID).metadata["agent_harness_command"]
    assert "-e daytona" in command


def test_agent_kwargs_become_ak_flags(tmp_path):
    adapter = HarborAdapter(
        {
            "harbor_tasks_dir": str(_PACK),
            "agent_model": "openai/gpt-4o",
            "agent_kwargs": {"api_base": "https://openrouter.ai/api/v1"},
            "staging_root": str(tmp_path),
        }
    )
    command = adapter.to_task_description(_TASK_ID).metadata["agent_harness_command"]
    assert "--ak api_base=https://openrouter.ai/api/v1" in command


def test_docker_stack_requirements_declares_build(adapter: HarborAdapter):
    reqs = adapter.docker_stack_requirements()
    assert reqs.image_builds
    build = reqs.image_builds[0]
    assert build.service == "main"
    assert build.expected_image_ref.startswith("tolokaforge-harbor-write-release-note:")


def test_registered_in_adapter_registry(tmp_path):
    from tolokaforge.adapters import available_adapters, get_adapter

    assert "harbor" in available_adapters()
    built = get_adapter("harbor", {"harbor_tasks_dir": str(_PACK), "staging_root": str(tmp_path)})
    assert isinstance(built, HarborAdapter)
