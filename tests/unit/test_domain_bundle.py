"""Unit tests for write_domain_bundle() and TaskConfig adapter fields."""

import json
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.bundle_writer import write_domain_bundle
from tolokaforge.core.models import TaskConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# TaskConfig adapter_type / adapter_settings
# ---------------------------------------------------------------------------


class TestTaskConfigAdapterFields:
    """Test the new adapter_type and adapter_settings fields on TaskConfig."""

    def _minimal_task_config(self, **overrides) -> dict:
        defaults = {
            "task_id": "t-001",
            "name": "Test",
            "category": "unit",
            "description": "desc",
            "initial_state": {},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "user_simulator": {"mode": "scripted"},
            "grading": "grading.yaml",
        }
        defaults.update(overrides)
        return defaults

    def test_adapter_type_defaults_to_native(self):
        """adapter_type should default to 'native' when not specified."""
        cfg = TaskConfig(**self._minimal_task_config())
        assert cfg.adapter_type == "native"

    def test_adapter_settings_defaults_to_none(self):
        """adapter_settings should default to None when not specified."""
        cfg = TaskConfig(**self._minimal_task_config())
        assert cfg.adapter_settings is None

    def test_adapter_type_roundtrip(self):
        """adapter_type should survive dump → load round-trip."""
        cfg = TaskConfig(**self._minimal_task_config(adapter_type="tlk_mcp_core"))
        dumped = cfg.model_dump(mode="json")
        restored = TaskConfig(**dumped)
        assert restored.adapter_type == "tlk_mcp_core"

    def test_adapter_settings_roundtrip(self):
        """adapter_settings should survive dump → load round-trip."""
        settings = {
            "domain_dir": "../_domain",
            "tool_registry": "../_domain/tool_registry.json",
        }
        cfg = TaskConfig(**self._minimal_task_config(adapter_settings=settings))
        dumped = cfg.model_dump(mode="json")
        restored = TaskConfig(**dumped)
        assert restored.adapter_settings == settings

    def test_adapter_fields_in_model_dump(self):
        """Both new fields should appear in model_dump() output."""
        cfg = TaskConfig(**self._minimal_task_config())
        dumped = cfg.model_dump(mode="json")
        assert "adapter_type" in dumped
        assert "adapter_settings" in dumped

    def test_existing_tasks_parse_without_adapter_fields(self):
        """Legacy task YAML (no adapter_type/adapter_settings) should still parse."""
        legacy = self._minimal_task_config()
        # Legacy dicts won't have these keys at all
        assert "adapter_type" not in legacy
        assert "adapter_settings" not in legacy
        cfg = TaskConfig(**legacy)
        assert cfg.adapter_type == "native"
        assert cfg.adapter_settings is None


# ---------------------------------------------------------------------------
# write_domain_bundle
# ---------------------------------------------------------------------------


class TestWriteDomainBundle:
    """Tests for write_domain_bundle() shared domain directory creation."""

    @pytest.fixture()
    def mock_sources(self, tmp_path: Path) -> dict:
        """Create mock mcp_core and tools_library source trees."""
        # mcp_core/src/mcp_core/
        mcp_core_src = tmp_path / "mcp_core_src"
        mcp_core_pkg = mcp_core_src / "mcp_core"
        mcp_core_pkg.mkdir(parents=True)
        (mcp_core_pkg / "__init__.py").write_text("# mcp_core\n")
        (mcp_core_pkg / "db").mkdir()
        (mcp_core_pkg / "db" / "__init__.py").write_text("# db\n")

        # tools_library/src/mcp_tools_library/
        tools_lib_src = tmp_path / "tools_lib_src"
        tools_pkg = tools_lib_src / "mcp_tools_library"
        tools_pkg.mkdir(parents=True)
        (tools_pkg / "__init__.py").write_text("# tools lib\n")

        # Create two toolset dirs
        for toolset in ("retail_toolset", "booking_toolset"):
            ts_dir = tools_pkg / toolset
            ts_dir.mkdir()
            (ts_dir / "__init__.py").write_text(f"# {toolset}\n")
            tools_dir = ts_dir / "tools"
            tools_dir.mkdir()
            (tools_dir / "__init__.py").write_text("# tools\n")
            (tools_dir / "get_item.py").write_text("class GetItemTool: pass\n")

        return {
            "mcp_core_src": mcp_core_src,
            "tools_library_src": tools_lib_src,
        }

    def test_creates_expected_structure(self, tmp_path: Path, mock_sources: dict):
        """write_domain_bundle() creates mcp_core/, tools/, registry, prompt, manifest."""
        domain_dir = tmp_path / "output" / "_domain"
        tool_registry = {
            "get_item": {
                "toolset": "retail_toolset",
                "module_path": "tools.get_item",
                "class_name": "GetItemTool",
                "invocation_style": "mcp_async",
            }
        }

        result = write_domain_bundle(
            mcp_core_src=mock_sources["mcp_core_src"],
            tools_library_src=mock_sources["tools_library_src"],
            tool_registry=tool_registry,
            system_prompt="You are a retail assistant.",
            domain_manifest={"domain": "retail", "adapter": "tlk_mcp_core"},
            output_dir=domain_dir,
        )

        assert result.exists()
        assert (domain_dir / "mcp_core" / "__init__.py").exists()
        assert (domain_dir / "mcp_core" / "db" / "__init__.py").exists()
        assert (domain_dir / "tools" / "mcp_tools_library" / "__init__.py").exists()
        assert (domain_dir / "tool_registry.json").exists()
        assert (domain_dir / "system_prompt.md").exists()
        assert (domain_dir / "domain_manifest.yaml").exists()

    def test_tool_registry_content(self, tmp_path: Path, mock_sources: dict):
        """tool_registry.json contains the provided tool mapping."""
        domain_dir = tmp_path / "_domain"
        tool_registry = {
            "retail_get_item": {
                "toolset": "retail_toolset",
                "module_path": "tools.get_item",
                "class_name": "GetItemTool",
                "invocation_style": "mcp_async",
            }
        }

        write_domain_bundle(
            mcp_core_src=mock_sources["mcp_core_src"],
            tools_library_src=mock_sources["tools_library_src"],
            tool_registry=tool_registry,
            system_prompt="",
            domain_manifest={},
            output_dir=domain_dir,
        )

        with open(domain_dir / "tool_registry.json") as fh:
            loaded = json.load(fh)

        assert "retail_get_item" in loaded
        assert loaded["retail_get_item"]["class_name"] == "GetItemTool"
        assert loaded["retail_get_item"]["invocation_style"] == "mcp_async"

    def test_system_prompt_content(self, tmp_path: Path, mock_sources: dict):
        """system_prompt.md should contain the provided prompt text."""
        domain_dir = tmp_path / "_domain"
        prompt = "You are a helpful retail assistant.\n\nFollow the policies."

        write_domain_bundle(
            mcp_core_src=mock_sources["mcp_core_src"],
            tools_library_src=mock_sources["tools_library_src"],
            tool_registry={},
            system_prompt=prompt,
            domain_manifest={},
            output_dir=domain_dir,
        )

        content = (domain_dir / "system_prompt.md").read_text()
        assert content == prompt

    def test_domain_manifest_content(self, tmp_path: Path, mock_sources: dict):
        """domain_manifest.yaml contains the provided metadata."""
        domain_dir = tmp_path / "_domain"
        manifest = {"domain": "retail_v3", "adapter": "tlk_mcp_core", "version": "1.0"}

        write_domain_bundle(
            mcp_core_src=mock_sources["mcp_core_src"],
            tools_library_src=mock_sources["tools_library_src"],
            tool_registry={},
            system_prompt="",
            domain_manifest=manifest,
            output_dir=domain_dir,
        )

        with open(domain_dir / "domain_manifest.yaml") as fh:
            loaded = yaml.safe_load(fh)

        assert loaded["domain"] == "retail_v3"
        assert loaded["adapter"] == "tlk_mcp_core"

    def test_idempotent_skips_existing(self, tmp_path: Path, mock_sources: dict):
        """If _domain/ already exists, write_domain_bundle() returns without overwriting."""
        domain_dir = tmp_path / "_domain"
        domain_dir.mkdir(parents=True)
        # Place a sentinel file
        sentinel = domain_dir / "sentinel.txt"
        sentinel.write_text("original")

        write_domain_bundle(
            mcp_core_src=mock_sources["mcp_core_src"],
            tools_library_src=mock_sources["tools_library_src"],
            tool_registry={"new_tool": {}},
            system_prompt="overwritten?",
            domain_manifest={},
            output_dir=domain_dir,
        )

        # Sentinel should still be there, no new files created
        assert sentinel.read_text() == "original"
        assert not (domain_dir / "tool_registry.json").exists()

    def test_allowed_toolsets_filtering(self, tmp_path: Path, mock_sources: dict):
        """Only allowed toolset directories should be copied when filter is set."""
        domain_dir = tmp_path / "_domain"

        write_domain_bundle(
            mcp_core_src=mock_sources["mcp_core_src"],
            tools_library_src=mock_sources["tools_library_src"],
            tool_registry={},
            system_prompt="",
            domain_manifest={},
            output_dir=domain_dir,
            allowed_toolsets={"mcp_tools_library.retail_toolset"},
        )

        tools_dir = domain_dir / "tools" / "mcp_tools_library"
        assert (tools_dir / "retail_toolset").exists()
        assert not (tools_dir / "booking_toolset").exists()
        # Top-level __init__.py should still be copied
        assert (tools_dir / "__init__.py").exists()

    def test_handles_missing_mcp_core(self, tmp_path: Path):
        """Should warn but not error if mcp_core source doesn't exist."""
        domain_dir = tmp_path / "_domain"
        nonexistent = tmp_path / "nonexistent"

        # Should not raise
        result = write_domain_bundle(
            mcp_core_src=nonexistent,
            tools_library_src=nonexistent,
            tool_registry={},
            system_prompt="",
            domain_manifest={},
            output_dir=domain_dir,
        )

        assert result.exists()
        assert not (domain_dir / "mcp_core").exists()
        # Registry and prompt should still be written
        assert (domain_dir / "tool_registry.json").exists()
        assert (domain_dir / "system_prompt.md").exists()


# ---------------------------------------------------------------------------
# Conversion output includes adapter fields
# ---------------------------------------------------------------------------


class TestBundleWriterAdapterFields:
    """Test that write_bundle preserves adapter_type and adapter_settings in task.yaml."""

    @pytest.fixture()
    def tlk_bundle(self):
        """Create a NativeTaskBundle simulating tlk_mcp_core conversion output."""
        from tolokaforge.adapters.base import NativeTaskBundle

        return NativeTaskBundle(
            task_config={
                "task_id": "ST001-001",
                "name": "ST001-001",
                "category": "retail",
                "description": "Test task",
                "adapter_type": "tlk_mcp_core",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"enabled": ["get_order"]}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm", "persona": "customer"},
                "grading": "grading.yaml",
                "system_prompt": "system_prompt.md",
                "adapter_settings": {
                    "domain_dir": "../_domain",
                    "tool_registry": "../_domain/tool_registry.json",
                    "system_prompt": "../_domain/system_prompt.md",
                },
            },
            grading_config={
                "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
            },
            initial_state={"orders": []},
            system_prompt="You are a retail agent.",
        )

    def test_task_yaml_contains_adapter_type(self, tmp_path: Path, tlk_bundle):
        """task.yaml should include adapter_type from bundle."""
        from tolokaforge.adapters.bundle_writer import write_bundle

        task_dir = write_bundle(tlk_bundle, tmp_path, "ST001-001")

        with open(task_dir / "task.yaml") as fh:
            data = yaml.safe_load(fh)

        assert data["adapter_type"] == "tlk_mcp_core"

    def test_task_yaml_contains_adapter_settings(self, tmp_path: Path, tlk_bundle):
        """task.yaml should include adapter_settings from bundle."""
        from tolokaforge.adapters.bundle_writer import write_bundle

        task_dir = write_bundle(tlk_bundle, tmp_path, "ST001-001")

        with open(task_dir / "task.yaml") as fh:
            data = yaml.safe_load(fh)

        assert data["adapter_settings"]["domain_dir"] == "../_domain"
        assert data["adapter_settings"]["tool_registry"] == "../_domain/tool_registry.json"

    def test_task_yaml_validates_as_task_config(self, tmp_path: Path, tlk_bundle):
        """Written task.yaml with adapter fields should parse as valid TaskConfig."""
        from tolokaforge.adapters.bundle_writer import write_bundle

        task_dir = write_bundle(tlk_bundle, tmp_path, "ST001-001")

        with open(task_dir / "task.yaml") as fh:
            data = yaml.safe_load(fh)

        cfg = TaskConfig(**data)
        assert cfg.adapter_type == "tlk_mcp_core"
        assert cfg.adapter_settings is not None
        assert cfg.adapter_settings["domain_dir"] == "../_domain"
