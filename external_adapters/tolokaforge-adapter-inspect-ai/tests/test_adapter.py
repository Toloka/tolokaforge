"""Discovery + translation tests for the Inspect AI adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def adapter(tmp_path) -> InspectAiAdapter:
    return InspectAiAdapter(
        {
            "inspect_task_dir": str(_FIXTURES),
            "agent_model": "mockllm/model",
            "staging_root": str(tmp_path / "staging"),
        }
    )


def test_discovers_fixture_task(adapter: InspectAiAdapter):
    assert "poc_smoke" in adapter.get_task_ids()
    assert adapter.get_task_dir("poc_smoke") == _FIXTURES


def test_get_task_translates_to_taskconfig(adapter: InspectAiAdapter):
    task = adapter.get_task("poc_smoke")
    assert task.adapter_type == "inspect_ai"
    assert task.category == "inspect"
    assert task.adapter_settings["inspect_task"] == "poc_smoke"


def test_task_id_filter(tmp_path):
    adapter = InspectAiAdapter(
        {"inspect_task_dir": str(_FIXTURES), "task_ids": ["nope"], "staging_root": str(tmp_path)}
    )
    assert adapter.get_task_ids() == []


def test_to_task_description_builds_inspect_command(adapter: InspectAiAdapter):
    desc = adapter.to_task_description("poc_smoke")
    assert desc.adapter_type == "inspect_ai"

    command = desc.metadata["agent_harness_command"]
    assert "inspect eval" in command
    assert "poc_task.py@poc_smoke" in command
    assert "mockllm/model" in command

    assert len(desc.agent_tools) == 1
    tool = desc.agent_tools[0]
    assert tool.name == "bash"
    assert tool.source.invocation_style.value == "docker_compose_exec"
    assert tool.source.extra["service"] == "main"

    assert desc.grading.grading_method == "test_execution"
    assert desc.environment_manifest is not None


def test_to_task_description_requires_model(tmp_path):
    adapter = InspectAiAdapter({"inspect_task_dir": str(_FIXTURES), "staging_root": str(tmp_path)})
    with pytest.raises(ValueError, match="agent_model"):
        adapter.to_task_description("poc_smoke")


def test_docker_stack_requirements_declares_build(adapter: InspectAiAdapter):
    reqs = adapter.docker_stack_requirements()
    assert reqs.image_builds
    build = reqs.image_builds[0]
    assert build.service == "main"
    assert build.expected_image_ref.startswith("tolokaforge-inspect-poc_smoke:")


def test_registered_in_adapter_registry(tmp_path):
    from tolokaforge.adapters import available_adapters, get_adapter

    assert "inspect_ai" in available_adapters()
    built = get_adapter(
        "inspect_ai", {"inspect_task_dir": str(_FIXTURES), "staging_root": str(tmp_path)}
    )
    assert isinstance(built, InspectAiAdapter)
