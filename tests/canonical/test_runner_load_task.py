"""Behaviour lock for the ``tolokaforge.runner.load_task`` helper.

Three locks on the public loader:

1. **Golden snapshot** — ``load_task(<fixture task.yaml>)`` produces a
   ``TaskConfig`` whose ``model_dump(mode="json")`` matches a committed golden
   (regenerable via the dev MCP ``update_canonical_snapshots``).
2. **Equivalence + source-dir preservation** — the config equals the one
   ``load_task_yaml`` returns, and its ``.source_dir`` equals the effective
   task dir the tuple carries. ``source_dir`` is absent from
   ``model_dump(mode="json")`` (an in-process locator, not a schema field), so
   the model-dump equivalence alone cannot catch a dropped dir.
3. **Import-path resolution** — the eager ``from``-import and attribute access
   resolve to the same callable object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.adapters._task_loader import load_task, load_task_yaml

pytestmark = pytest.mark.canonical

_TASK_YAML = (
    Path(__file__).resolve().parents[2]
    / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)


def test_load_task_matches_golden(canon_snapshot):
    task = load_task(_TASK_YAML)
    canon_snapshot("runner_load_task").assert_match(
        task.model_dump(mode="json"), "tool_use_public_example_01.json"
    )


def test_load_task_equals_loader_and_preserves_source_dir():
    task = load_task(_TASK_YAML)
    tuple_task, effective_dir = load_task_yaml(_TASK_YAML)

    assert task.model_dump(mode="json") == tuple_task.model_dump(mode="json")
    assert task.source_dir == effective_dir


def test_load_task_import_paths_resolve_to_same_object():
    import importlib

    from tolokaforge.runner import load_task as eager_load_task

    runner = importlib.import_module("tolokaforge.runner")
    assert eager_load_task is runner.load_task
