"""Unit tests for harness adapters."""

import importlib.metadata
import logging
from pathlib import Path

import pytest

from tolokaforge.adapters import available_adapters, get_adapter
from tolokaforge.adapters.base import AdapterEnvironment
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import plugin_registry
from tolokaforge.core.models import RunConfig
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.plugin_registry import DuplicateRegistrationError

pytestmark = pytest.mark.unit


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    """Duck-typed stand-in for :class:`importlib.metadata.EntryPoint`.

    Discovery in :func:`plugin_registry.discover_entry_points` reads
    ``ep.name`` / ``ep.dist`` (for the duplicate message); ``ep.load()`` is
    invoked by ``_discover_adapters`` per surviving entry point.
    """

    def __init__(
        self,
        name: str,
        *,
        target: object = None,
        load_error: Exception | None = None,
        dist: str = "pkg",
    ) -> None:
        self.name = name
        self.dist = _FakeDist(dist)
        self._target = target
        self._load_error = load_error

    def load(self) -> object:
        if self._load_error is not None:
            raise self._load_error
        return self._target


@pytest.fixture
def _isolate_adapter_discovery(monkeypatch: pytest.MonkeyPatch):
    """Reset the plugin_registry discovery cache and adapter dicts around a case.

    Tests that inject entry-points under ``tolokaforge.adapters`` must clear
    the per-group cache so the next scan re-reads their injection instead of
    a stale prior map, and must reset ``_ADAPTERS`` / ``_DISCOVERED`` /
    ``_FAILED_ADAPTERS`` so lazy rediscovery fires.
    """
    import tolokaforge.adapters as adapters_module

    plugin_registry._clear_discovery_cache()
    monkeypatch.setattr(adapters_module, "_ADAPTERS", {})
    monkeypatch.setattr(adapters_module, "_DISCOVERED", False)
    monkeypatch.setattr(adapters_module, "_FAILED_ADAPTERS", {})
    yield
    plugin_registry._clear_discovery_cache()


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

        assert adapter is not None
        # Check class name without importing

    def test_unknown_adapter_raises_error(self):
        """Test that unknown adapter type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown adapter type"):
            get_adapter("unknown_type", {})

    def test_failed_entry_point_is_reported(self, monkeypatch, caplog, _isolate_adapter_discovery):
        """A failing adapter entry-point surfaces its original import error via get_adapter()."""
        broken = _FakeEntryPoint(
            "broken_adapter",
            load_error=ImportError("missing dependency for broken_adapter"),
        )
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda *, group: [broken] if group == "tolokaforge.adapters" else [],
        )
        caplog.set_level(logging.WARNING, logger="tolokaforge.adapters")

        # Drive the production path: get_adapter() triggers lazy discovery,
        # records the failure, and re-raises with the original ImportError chained.
        with pytest.raises(ValueError, match="entry-point was found but failed to load") as excinfo:
            get_adapter("broken_adapter", {})

        assert isinstance(excinfo.value.__cause__, ImportError)
        assert "missing dependency" in str(excinfo.value.__cause__)
        assert "failed to load" in caplog.text

        # A broken external entry-point must not poison the built-in adapters.
        names = available_adapters()
        assert "native" in names
        assert "broken_adapter" not in names

    def test_duplicate_adapter_entry_points_fail_loud(
        self, monkeypatch, _isolate_adapter_discovery
    ):
        """Two adapter entry-points with the same name fail loud before any class loads."""
        first = _FakeEntryPoint("colliding_id", target=object(), dist="pkg-first")
        second = _FakeEntryPoint("colliding_id", target=object(), dist="pkg-second")
        monkeypatch.setattr(
            importlib.metadata,
            "entry_points",
            lambda *, group: [first, second] if group == "tolokaforge.adapters" else [],
        )

        with pytest.raises(DuplicateRegistrationError) as excinfo:
            available_adapters()

        message = str(excinfo.value)
        assert "colliding_id" in message
        assert "pkg-first" in message
        assert "pkg-second" in message
        assert excinfo.value.distributions == ("pkg-first", "pkg-second")

    def test_available_adapters_lists_builtins(self):
        """available_adapters() returns built-ins on a cold call without monkeypatching."""
        names = available_adapters()
        assert "native" in names

    def test_entry_points_load_without_partial_import_errors(self):
        """Adapter entry points should load cleanly, even during discovery."""
        import importlib.metadata

        for ep in importlib.metadata.entry_points(group="tolokaforge.adapters"):
            cls = ep.load()
            assert cls is not None
            assert hasattr(cls, "__name__")


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

        # Each discovered task_id corresponds to a real file under tests/data/tasks/.
        # Flat-layout tasks have task_id == dir name; shared-domain tasks
        # (``<dom>/testcases/<case>/task.yaml``) name their case independently
        # of the dir tree, so we just assert the corresponding file exists.
        for task_id in task_ids:
            task_path = native_adapter._task_files[task_id]
            assert task_path.is_file(), task_id
            assert task_path.is_relative_to(test_project_dir / "tasks"), task_id

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

    # TestTauAdapter and TestOrchestratorWithTauAdapter live in the
    # adapter package's own test suite.


class TestAdapterConfigParsing:
    """Tests for adapter config parsing from YAML."""

    def test_harness_adapter_config_parsing(self):
        """Test HarnessAdapterConfig parses correctly."""
        config_data = {
            "evaluation": {
                "tasks_glob": "tasks/**/*.yaml",
                "output_dir": "output/test",
                "harness_adapter": {
                    "type": "tau",
                    "params": {
                        "task_split": "dev",
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
        assert run_config.evaluation.harness_adapter.type == "tau"
        assert run_config.evaluation.harness_adapter.params.get("task_split") == "dev"

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
