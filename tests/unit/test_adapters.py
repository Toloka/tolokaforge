"""Unit tests for harness adapters."""

from pathlib import Path

import pytest

from tolokaforge.adapters import get_adapter
from tolokaforge.adapters.base import AdapterEnvironment
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import RunConfig
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


class TestGetAdapter:
    """Tests for get_adapter factory function."""

    def test_get_native_adapter_explicitly(self):
        """Test getting native adapter by type name."""
        adapter = get_adapter("native", {"tasks_glob": "tasks/**/*.yaml"})
        assert isinstance(adapter, NativeAdapter)

    def test_get_native_adapter_by_default(self):
        """Test getting native adapter when type is None."""
        adapter = get_adapter(None, {"tasks_glob": "tasks/**/*.yaml"})
        assert isinstance(adapter, NativeAdapter)

    def test_unknown_adapter_raises_error(self):
        """Test that unknown adapter type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown adapter type"):
            get_adapter("unknown_type", {})


class TestNativeAdapter:
    """Tests for NativeAdapter (file-based tasks)."""

    @pytest.fixture
    def test_project_dir(self) -> Path:
        """Get path to non-LFS native task fixtures."""
        return Path(__file__).parent.parent / "data"

    @pytest.fixture
    def native_adapter(self, test_project_dir: Path) -> NativeAdapter:
        """Create adapter for test project."""
        return NativeAdapter(
            {
                "base_dir": str(test_project_dir),
                "tasks_glob": "tasks/**/task.yaml",
            }
        )

    def test_get_task_ids(self, native_adapter: NativeAdapter, test_project_dir: Path):
        """Test native adapter returns task IDs from directory."""
        task_ids = native_adapter.get_task_ids()

        # Should find tasks in the test project
        assert len(task_ids) > 0

        # Check that task IDs match directory names
        tasks_dir = test_project_dir / "tasks"
        expected_ids = [d.name for d in tasks_dir.iterdir() if d.is_dir()]

        for task_id in task_ids:
            assert task_id in expected_ids

    def test_get_task(self, native_adapter: NativeAdapter):
        """Test native adapter loads task config."""
        task_ids = native_adapter.get_task_ids()
        if not task_ids:
            pytest.skip("No tasks in test project")

        task_id = task_ids[0]
        task_config = native_adapter.get_task(task_id)

        assert task_config is not None
        assert task_config.task_id == task_id

    def test_get_task_not_found(self, native_adapter: NativeAdapter):
        """Test native adapter raises error for unknown task."""
        with pytest.raises(ValueError, match="not found"):
            native_adapter.get_task("non_existent_task_id")

    def test_create_environment(self, native_adapter: NativeAdapter):
        """Test native adapter creates environment."""
        task_ids = native_adapter.get_task_ids()
        if not task_ids:
            pytest.skip("No tasks in test project")

        task_id = task_ids[0]
        env = native_adapter.create_environment(task_id)

        assert isinstance(env, AdapterEnvironment)
        assert env.task_dir is not None


class TestAdapterConfigParsing:
    """Tests for adapter config parsing from YAML."""

    def test_harness_adapter_config_parsing(self):
        """Test HarnessAdapterConfig parses correctly."""
        config_data = {
            "evaluation": {
                "tasks_glob": "tasks/**/*.yaml",
                "output_dir": "output/test",
                "harness_adapter": {
                    "type": "frozen_mcp_core",
                    "params": {
                        "base_dir": "/tmp/test",
                    },
                },
            },
            "models": {
                "agent": {"provider": "openai", "name": "gpt-4o-mini"},
            },
            "orchestrator": {
                "workers": 1,
                "repeats": 1,
            },
        }

        run_config = RunConfig(**config_data)

        assert run_config.evaluation.harness_adapter is not None
        assert run_config.evaluation.harness_adapter.type == "frozen_mcp_core"
        assert run_config.evaluation.harness_adapter.params.get("base_dir") == "/tmp/test"

    def test_no_harness_adapter_defaults_to_none(self):
        """Test missing harness_adapter defaults to None (native)."""
        config_data = {
            "evaluation": {
                "tasks_glob": "tasks/**/*.yaml",
                "output_dir": "output/test",
            },
            "models": {
                "agent": {"provider": "openai", "name": "gpt-4o-mini"},
            },
            "orchestrator": {
                "workers": 1,
                "repeats": 1,
            },
        }

        run_config = RunConfig(**config_data)

        assert run_config.evaluation.harness_adapter is None

    def test_task_packs_passed_to_adapter(self, tmp_path: Path):
        """Test task_packs config is forwarded to adapter."""
        pack = tmp_path / "pack"
        (pack / "tasks").mkdir(parents=True)

        config_data = {
            "evaluation": {
                "task_packs": [str(pack)],
                "tasks_glob": "**/task.yaml",
                "output_dir": "output/test",
            },
            "models": {
                "agent": {"provider": "openai", "name": "gpt-4o-mini"},
            },
            "orchestrator": {"workers": 1, "repeats": 1},
        }

        run_config = RunConfig(**config_data)
        orchestrator = Orchestrator(run_config)
        adapter = orchestrator._create_adapter()
        assert [str(path) for path in adapter.task_packs] == [str(pack.resolve())]
