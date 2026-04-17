"""Unit tests for NativeTaskBundle and bundle_writer."""

import json

import pytest
import yaml

from tolokaforge.adapters.base import NativeTaskBundle
from tolokaforge.adapters.bundle_writer import write_bundle

pytestmark = pytest.mark.unit


def _make_bundle(**overrides):
    """Create a minimal valid NativeTaskBundle for testing."""
    defaults = {
        "task_config": {
            "task_id": "test-task-001",
            "name": "Test Task",
            "category": "unit_test",
            "description": "A test task for unit testing",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": ["tool_a"]}, "user": {"enabled": []}},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
        "grading_config": {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            },
            "state_checks": {"hash": {"enabled": True, "weight": 1.0}},
        },
        "initial_state": {"orders": [{"id": 1, "status": "pending"}]},
        "system_prompt": "You are a helpful assistant.",
        "fixtures": {
            "tools": [
                {
                    "name": "tool_a",
                    "description": "Does something",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "golden_actions": [
                {"tool_name": "tool_a", "arguments": {"x": 1}},
            ],
            "unstable_fields": [
                {"table_name": "orders", "field_name": "created_at", "reason": "timestamp"},
            ],
        },
        "metadata": {"source_adapter": "test", "env_name": "test_env"},
    }
    defaults.update(overrides)
    return NativeTaskBundle(**defaults)


class TestNativeTaskBundle:
    """Tests for NativeTaskBundle dataclass."""

    @pytest.mark.unit
    def test_create_with_all_fields(self):
        """Test creating a NativeTaskBundle with all fields populated."""
        bundle = _make_bundle()
        assert bundle.task_config["task_id"] == "test-task-001"
        assert bundle.grading_config["combine"]["method"] == "weighted"
        assert len(bundle.initial_state["orders"]) == 1
        assert bundle.system_prompt == "You are a helpful assistant."
        assert len(bundle.fixtures["tools"]) == 1
        assert bundle.metadata["source_adapter"] == "test"


class TestWriteBundle:
    """Tests for write_bundle() disk serialisation."""

    @pytest.mark.unit
    def test_creates_all_expected_files(self, tmp_path):
        """write_bundle() must create all standard files."""
        bundle = _make_bundle()
        task_dir = write_bundle(bundle, tmp_path, "test-task-001")

        assert task_dir.exists()
        assert (task_dir / "task.yaml").exists()
        assert (task_dir / "grading.yaml").exists()
        assert (task_dir / "initial_state.json").exists()
        assert (task_dir / "system_prompt.md").exists()
        assert (task_dir / "fixtures" / "tools.json").exists()
        assert (task_dir / "fixtures" / "golden_actions.json").exists()
        assert (task_dir / "fixtures" / "unstable_fields.json").exists()
        assert (task_dir / "fixtures" / "metadata.json").exists()

    @pytest.mark.unit
    def test_task_id_injected_when_missing(self, tmp_path):
        """task_id should be injected into task_config if not present."""
        bundle = NativeTaskBundle(
            task_config={"name": "no-id"},
            grading_config={},
        )
        task_dir = write_bundle(bundle, tmp_path, "injected-id")

        with open(task_dir / "task.yaml") as fh:
            data = yaml.safe_load(fh)

        assert data["task_id"] == "injected-id"

    @pytest.mark.unit
    def test_roundtrip_read_back(self, tmp_path):
        """Write then read back — all core data should survive the round trip."""
        bundle = _make_bundle()
        task_dir = write_bundle(bundle, tmp_path, "test-task-001")

        with open(task_dir / "task.yaml") as fh:
            task_cfg = yaml.safe_load(fh)
        with open(task_dir / "grading.yaml") as fh:
            grading_cfg = yaml.safe_load(fh)
        with open(task_dir / "initial_state.json") as fh:
            state = json.load(fh)
        prompt = (task_dir / "system_prompt.md").read_text()
        with open(task_dir / "fixtures" / "tools.json") as fh:
            tools = json.load(fh)

        assert task_cfg["task_id"] == bundle.task_config["task_id"]
        assert grading_cfg["combine"]["method"] == bundle.grading_config["combine"]["method"]
        assert state == bundle.initial_state
        assert prompt == bundle.system_prompt
        assert len(tools) == len(bundle.fixtures["tools"])

    @pytest.mark.unit
    def test_idempotent_overwrite(self, tmp_path):
        """Writing the same bundle twice should produce identical output."""
        bundle = _make_bundle()

        write_bundle(bundle, tmp_path, "idem")
        with open(tmp_path / "idem" / "task.yaml") as fh:
            first = fh.read()

        write_bundle(bundle, tmp_path, "idem")
        with open(tmp_path / "idem" / "task.yaml") as fh:
            second = fh.read()

        assert first == second
