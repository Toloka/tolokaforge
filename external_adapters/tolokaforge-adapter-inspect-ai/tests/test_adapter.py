"""Discovery + translation tests for the Inspect AI adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def adapter() -> InspectAiAdapter:
    return InspectAiAdapter(
        {"inspect_task_dir": str(_FIXTURES), "agent_model": "openai/gpt-4o-mini"}
    )


def test_discovers_fixture_task(adapter: InspectAiAdapter):
    assert "poc_smoke" in adapter.get_task_ids()
    assert adapter.get_task_dir("poc_smoke") == _FIXTURES


def test_get_task_translates_to_taskconfig(adapter: InspectAiAdapter):
    task = adapter.get_task("poc_smoke")
    assert task.adapter_type == "inspect_ai"
    assert task.category == "inspect"
    assert task.adapter_settings["inspect_task"] == "poc_smoke"
    assert task.adapter_settings["inspect_file"].endswith("poc_task.py")


def test_task_id_filter():
    adapter = InspectAiAdapter({"inspect_task_dir": str(_FIXTURES), "task_ids": ["does-not-exist"]})
    assert adapter.get_task_ids() == []


def test_to_task_description_not_wired(adapter: InspectAiAdapter):
    with pytest.raises(NotImplementedError, match="run_inspect_eval"):
        adapter.to_task_description("poc_smoke")


def test_grade_not_wired(adapter: InspectAiAdapter):
    with pytest.raises(NotImplementedError, match="run_inspect_eval"):
        adapter.grade("poc_smoke", None, {}, None)


def test_registered_in_adapter_registry():
    from tolokaforge.adapters import available_adapters, get_adapter

    assert "inspect_ai" in available_adapters()
    built = get_adapter("inspect_ai", {"inspect_task_dir": str(_FIXTURES)})
    assert isinstance(built, InspectAiAdapter)
