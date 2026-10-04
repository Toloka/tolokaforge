"""Discovery and ``TaskConfig`` translation for the Harbor adapter.

Daemon-free: discovery reads the vendored pack on disk and translation
projects a parsed task into a ``TaskConfig``. No container is built and no
subprocess runs — ``get_task``/``to_task_description`` must stay buildable
from the pack alone (the Docker path is exercised by the integration test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_harbor.adapter import HarborAdapter
from tolokaforge_adapter_harbor.discovery import discover_harbor_tasks

pytestmark = pytest.mark.unit

_HARBOR_TASKS_DIR = Path(__file__).resolve().parents[3] / "examples" / "harbor"
_TASK_ID = "write-release-note"


def _adapter(**overrides: object) -> HarborAdapter:
    params: dict = {
        "harbor_tasks_dir": str(_HARBOR_TASKS_DIR),
        "prebuild_images": False,
    }
    params.update(overrides)
    return HarborAdapter(params)


def test_discover_finds_the_vendored_task() -> None:
    tasks = discover_harbor_tasks(_HARBOR_TASKS_DIR)
    assert _TASK_ID in tasks
    meta = tasks[_TASK_ID]
    assert meta.task_dir == _HARBOR_TASKS_DIR / _TASK_ID
    # task.toml declares the verifier budget; discovery parses it through.
    assert meta.verifier_timeout_sec == 60.0


def test_get_task_ids_lists_the_pack() -> None:
    assert _adapter().get_task_ids() == [_TASK_ID]


def test_task_ids_filter_is_honored() -> None:
    assert _adapter(task_ids=[_TASK_ID]).get_task_ids() == [_TASK_ID]
    assert _adapter(task_ids=["no-such-task"]).get_task_ids() == []


def test_translation_yields_a_valid_task_config() -> None:
    task = _adapter().get_task(_TASK_ID)
    assert task.task_id == _TASK_ID
    assert task.adapter_type == "harbor"
    assert task.grading == "__adapter__"
    # The task's instruction becomes the opening user message.
    assert task.initial_user_message is not None
    assert task.initial_user_message.strip()
    assert "RELEASE_NOTE.txt" in task.initial_user_message
    # The single bash tool is enabled for the agent.
    assert task.tools.agent["enabled"] == ["bash"]


def test_translation_is_daemon_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_task`` must not shell out: any ``subprocess`` call is a regression."""
    import subprocess

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_task must not invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    task = _adapter().get_task(_TASK_ID)
    assert task.adapter_type == "harbor"
