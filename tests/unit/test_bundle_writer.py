"""Unit tests for the native bundle writer (tolokaforge.adapters.bundle_writer).

``tolokaforge adapter convert`` imports this module at runtime, so it must exist
and serialise a NativeTaskBundle to the native on-disk layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.base import NativeTaskBundle
from tolokaforge.adapters.bundle_writer import write_bundle

pytestmark = pytest.mark.unit


def _sample_bundle() -> NativeTaskBundle:
    return NativeTaskBundle(
        task_config={"name": "Example", "category": "tool_use"},
        grading_config={"combine": {"method": "weighted", "pass_threshold": 1.0}},
        initial_state={"json_db": {"orders": []}},
        system_prompt="You are a helpful agent.",
        fixtures={
            "tools": [{"name": "get_order"}],
            "golden_actions": [{"tool_name": "get_order", "arguments": {}}],
            "unstable_fields": [{"table_name": "orders", "field_name": "updated_at"}],
            "extra_notes": {"hello": "world"},
        },
        metadata={"source_adapter": "example", "domain": "demo"},
    )


class TestWriteBundle:
    def test_writes_full_native_layout(self, tmp_path: Path):
        task_dir = write_bundle(_sample_bundle(), tmp_path, "task-001")

        assert task_dir == (tmp_path / "task-001").resolve()

        # task.yaml — task_id injected, fields preserved
        task_yaml = yaml.safe_load((task_dir / "task.yaml").read_text())
        assert task_yaml["task_id"] == "task-001"
        assert task_yaml["name"] == "Example"

        # grading.yaml
        grading = yaml.safe_load((task_dir / "grading.yaml").read_text())
        assert grading["combine"]["pass_threshold"] == 1.0

        # initial_state.json + system_prompt.md
        assert json.loads((task_dir / "initial_state.json").read_text()) == {
            "json_db": {"orders": []}
        }
        assert (task_dir / "system_prompt.md").read_text() == "You are a helpful agent."

        # fixtures/
        fx = task_dir / "fixtures"
        assert json.loads((fx / "tools.json").read_text()) == [{"name": "get_order"}]
        assert json.loads((fx / "golden_actions.json").read_text())[0]["tool_name"] == "get_order"
        assert (
            json.loads((fx / "unstable_fields.json").read_text())[0]["field_name"] == "updated_at"
        )
        assert json.loads((fx / "metadata.json").read_text())["source_adapter"] == "example"
        # any extra fixture key gets its own JSON file
        assert json.loads((fx / "extra_notes.json").read_text()) == {"hello": "world"}

    def test_existing_task_id_is_preserved(self, tmp_path: Path):
        bundle = _sample_bundle()
        bundle.task_config["task_id"] = "explicit-id"
        task_dir = write_bundle(bundle, tmp_path, "dir-name")
        # setdefault: an explicit task_config["task_id"] wins over the dir name
        assert yaml.safe_load((task_dir / "task.yaml").read_text())["task_id"] == "explicit-id"

    def test_module_is_importable(self):
        # convert() imports this module at runtime — guard that it exists.
        import tolokaforge.adapters.bundle_writer as bw

        assert hasattr(bw, "write_bundle")
