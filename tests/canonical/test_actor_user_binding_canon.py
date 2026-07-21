"""Pins the resolved user-simulator config for a task authored with the
canonical ``actors.user`` shape, so a regression that silently drops the
``actors.user`` → simulator binding fails loud.
"""

import pytest

from tolokaforge.adapters._task_loader import load_task_yaml

pytestmark = pytest.mark.canonical


def test_resolved_user_simulator_from_actors_user(test_data_dir, canon_snapshot) -> None:
    task, _ = load_task_yaml(test_data_dir / "actor_binding" / "task.yaml")
    snap = canon_snapshot("actor_user_binding")
    snap.assert_match(task.resolve_user_simulator().model_dump(mode="json"), "user_simulator.json")
