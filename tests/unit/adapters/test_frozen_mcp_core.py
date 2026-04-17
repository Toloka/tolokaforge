"""Unit tests for tolokaforge.adapters.frozen_mcp_core.

Covers:
- ``_discover_model_sources()``  — namespace collision regression
- ``_resolve_model_class()``     — all 5 matching strategies
- ``_sanitize_property_name()``  — module-level helper
- ``_sanitize_schema_properties()`` — recursive schema sanitisation
- ``_resolve_domain_dir()``      — domain directory resolution
- ``_read_domain_from_manifest()`` — YAML manifest parsing
- ``_load_system_prompt()``      — system prompt file loading
- ``_load_tool_registry()``      — tool registry JSON loading
- ``_bundle_domain_artifacts()`` — base64 artifact bundling
- ``_create_database()``         — InMemoryDatabase creation (mcp_core required)
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tolokaforge.adapters.frozen_mcp_core import (
    FrozenMcpCoreAdapter,
    _sanitize_property_name,
    _sanitize_schema_properties,
)

pytestmark = pytest.mark.unit


# =========================================================================
# Helpers
# =========================================================================


def _make_adapter(tmp_path: Path) -> FrozenMcpCoreAdapter:
    """Create a minimal ``FrozenMcpCoreAdapter`` with *base_dir* = *tmp_path*."""
    return FrozenMcpCoreAdapter({"base_dir": str(tmp_path)})


def _build_collision_tree(tools_src: Path) -> None:
    """Build a synthetic filesystem with two toolsets sharing the same final name.

    ::

        tools_src/
        ├── retail_toolset/
        │   └── oms/
        │       ├── models.py
        │       └── initial_data/
        │           └── orders.json
        └── logistics_toolset/
            └── oms/
                ├── models.py
                └── initial_data/
                    └── shipments.json
    """
    for parent, data_file in [
        ("retail_toolset/oms", "orders.json"),
        ("logistics_toolset/oms", "shipments.json"),
    ]:
        toolset = tools_src / parent
        toolset.mkdir(parents=True)
        (toolset / "models.py").write_text("# models\n")
        data_dir = toolset / "initial_data"
        data_dir.mkdir()
        (data_dir / data_file).write_text("{}\n")


def _fake_stem_map(mapping: dict[str, Any]) -> SimpleNamespace:
    """Return a lightweight object whose ``_stem_to_model_cls`` behaves like a dict."""
    return SimpleNamespace(_stem_to_model_cls=mapping)


# =========================================================================
# _discover_model_sources — namespace collision regression
# =========================================================================


class TestDiscoverModelSources:
    """Verify ``_discover_model_sources`` produces unique namespace keys."""

    def test_collision_both_namespaces_preserved(self, tmp_path: Path) -> None:
        """Two directories named ``oms`` under different parents MUST produce two entries."""
        tools_src = tmp_path / "tools_src"
        tools_src.mkdir()
        _build_collision_tree(tools_src)

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        assert len(result) == 2, f"Expected 2 namespaces, got {len(result)}: {list(result.keys())}"

    def test_collision_keys_are_distinct(self, tmp_path: Path) -> None:
        """Namespace keys must embed the parent path, not just the leaf."""
        tools_src = tmp_path / "tools_src"
        tools_src.mkdir()
        _build_collision_tree(tools_src)

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        keys = set(result.keys())
        assert "retail_toolset_oms" in keys, f"Missing retail key; got {keys}"
        assert "logistics_toolset_oms" in keys, f"Missing logistics key; got {keys}"

    def test_module_prefix_includes_full_path(self, tmp_path: Path) -> None:
        """``module_prefix`` should use dotted full path for correct imports."""
        tools_src = tmp_path / "tools_src"
        tools_src.mkdir()
        _build_collision_tree(tools_src)

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        for _namespace, (_data_dir, module_prefix) in result.items():
            assert module_prefix.startswith("mcp_tools_library.")
            assert module_prefix.endswith(".models")

    def test_single_toolset_no_collision(self, tmp_path: Path) -> None:
        """A single toolset should still work normally."""
        tools_src = tmp_path / "tools_src"
        toolset = tools_src / "warehouse" / "inventory"
        toolset.mkdir(parents=True)
        (toolset / "models.py").write_text("# models\n")
        (toolset / "initial_data").mkdir()
        (toolset / "initial_data" / "items.json").write_text("{}\n")

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        assert len(result) == 1
        assert "warehouse_inventory" in result

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty ``tools_src`` returns empty dict."""
        tools_src = tmp_path / "tools_src"
        tools_src.mkdir()

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        assert result == {}

    def test_nonexistent_directory(self, tmp_path: Path) -> None:
        """Non-existent ``tools_src`` returns empty dict."""
        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tmp_path / "nonexistent")

        assert result == {}

    def test_models_dir_instead_of_file(self, tmp_path: Path) -> None:
        """A ``models/`` directory (not ``models.py``) should also be discovered."""
        tools_src = tmp_path / "tools_src"
        toolset = tools_src / "crm" / "contacts"
        toolset.mkdir(parents=True)
        (toolset / "models").mkdir()  # directory, not file
        (toolset / "initial_data").mkdir()
        (toolset / "initial_data" / "contacts.json").write_text("{}\n")

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        assert "crm_contacts" in result

    def test_no_initial_data_skipped(self, tmp_path: Path) -> None:
        """Toolsets without ``initial_data/`` are NOT included in results."""
        tools_src = tmp_path / "tools_src"
        toolset = tools_src / "analytics" / "reports"
        toolset.mkdir(parents=True)
        (toolset / "models.py").write_text("# models\n")
        # No initial_data directory

        adapter = _make_adapter(tmp_path)
        result = adapter._discover_model_sources(tools_src)

        assert len(result) == 0


# =========================================================================
# _resolve_model_class — 5 matching strategies
# =========================================================================


class TestResolveModelClass:
    """Verify all matching strategies in ``_resolve_model_class``."""

    # --- Strategy 1: Direct match ---

    def test_direct_match_positive(self) -> None:
        """Exact table_name in ``_stem_to_model_cls`` returns the model."""
        sentinel = object()
        db = _fake_stem_map({"orders": sentinel})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "orders", "retail")
        assert result is sentinel

    def test_direct_match_negative(self) -> None:
        """No match at all returns ``None``."""
        db = _fake_stem_map({"orders": object()})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "xyz_no_match_xyz", "")
        assert result is None

    # --- Strategy 2: _models_ segment removal ---

    def test_models_segment_removal_positive(self) -> None:
        """``foo_models_bar`` collapses to ``foo_bar`` and matches."""
        sentinel = object()
        db = _fake_stem_map({"oms_orders": sentinel})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "oms_models_orders", "retail")
        assert result is sentinel

    def test_models_segment_removal_negative(self) -> None:
        """If collapsed name doesn't exist either, fall through."""
        db = _fake_stem_map({"something_else": object()})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "oms_models_orders", "retail")
        # Should not match via strategy 2, may match via later strategies
        # If nothing matches at all → None
        assert result is None

    # --- Strategy 3: Domain prefix match ---

    def test_domain_prefix_positive(self) -> None:
        """``{domain}_{table_name}`` matches an existing stem."""
        sentinel = object()
        db = _fake_stem_map({"retail_orders": sentinel})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "orders", "retail")
        assert result is sentinel

    def test_domain_prefix_empty_domain(self) -> None:
        """Empty domain string skips the prefix strategy."""
        db = _fake_stem_map({"_orders": object(), "orders": object()})

        # With empty domain, strategy 3 is skipped; direct match (strategy 1) hits
        result = FrozenMcpCoreAdapter._resolve_model_class(db, "orders", "")
        assert result is not None  # hit via strategy 1

    # --- Strategy 4: Suffix matching (forward) ---

    def test_suffix_match_positive(self) -> None:
        """Stem ending with ``_{table_name}`` matches."""
        sentinel = object()
        db = _fake_stem_map({"external_retail_toolset_oms_orders": sentinel})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "orders", "")
        assert result is sentinel

    def test_suffix_match_negative(self) -> None:
        """Partial suffix that doesn't align on ``_`` boundary doesn't match."""
        db = _fake_stem_map({"bigorders": object()})

        result = FrozenMcpCoreAdapter._resolve_model_class(db, "orders", "")
        assert result is None

    # --- Strategy 5: Reverse suffix matching with _models_ collapse ---

    def test_reverse_suffix_positive(self) -> None:
        """Key ``external_retail_toolset_oms_models_orders`` collapses and matches stem ``oms_orders``."""
        sentinel = object()
        db = _fake_stem_map({"oms_orders": sentinel})

        result = FrozenMcpCoreAdapter._resolve_model_class(
            db, "external_retail_toolset_oms_models_orders", ""
        )
        assert result is sentinel

    def test_reverse_suffix_without_models_segment(self) -> None:
        """Reverse match also works when there's NO ``_models_`` in the key."""
        sentinel = object()
        db = _fake_stem_map({"oms_orders": sentinel})

        result = FrozenMcpCoreAdapter._resolve_model_class(
            db, "external_retail_toolset_oms_orders", ""
        )
        assert result is sentinel


# =========================================================================
# _sanitize_property_name
# =========================================================================


class TestSanitizePropertyName:
    """Verify ``_sanitize_property_name`` handles special characters."""

    def test_dollar_prefix(self) -> None:
        assert _sanitize_property_name("$filter") == "filter"

    def test_spaces_to_underscores(self) -> None:
        assert _sanitize_property_name("name with spaces") == "name_with_spaces"

    def test_already_clean(self) -> None:
        assert _sanitize_property_name("already_clean") == "already_clean"

    def test_multiple_special_chars(self) -> None:
        assert _sanitize_property_name("$special-name.value") == "special-name.value"

    def test_empty_string(self) -> None:
        assert _sanitize_property_name("") == ""

    def test_only_dollar(self) -> None:
        assert _sanitize_property_name("$") == ""

    def test_multiple_dollars_stripped(self) -> None:
        # lstrip("$") removes leading $'s
        assert _sanitize_property_name("$$double") == "double"


# =========================================================================
# _sanitize_schema_properties
# =========================================================================


class TestSanitizeSchemaProperties:
    """Verify ``_sanitize_schema_properties`` recursion and required-list handling."""

    def test_nested_properties_recursion(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "$filter": {"type": "string"},
                "name with spaces": {
                    "type": "object",
                    "properties": {"$inner": {"type": "integer"}},
                },
            },
        }
        result = _sanitize_schema_properties(schema)

        assert "filter" in result["properties"]
        assert "$filter" not in result["properties"]
        assert "name_with_spaces" in result["properties"]
        inner_props = result["properties"]["name_with_spaces"]["properties"]
        assert "inner" in inner_props

    def test_required_field_names_sanitized(self) -> None:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"$id": {"type": "string"}},
            "required": ["$id"],
        }
        result = _sanitize_schema_properties(schema)

        assert result["required"] == ["id"]

    def test_empty_properties(self) -> None:
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        result = _sanitize_schema_properties(schema)

        assert result["properties"] == {}

    def test_missing_properties_key(self) -> None:
        schema: dict[str, Any] = {"type": "string"}
        result = _sanitize_schema_properties(schema)

        assert result == {"type": "string"}

    def test_non_dict_passthrough(self) -> None:
        # Non-dict input is returned as-is
        assert _sanitize_schema_properties("not a dict") == "not a dict"  # type: ignore[arg-type]

    def test_list_items_recursion(self) -> None:
        schema: dict[str, Any] = {
            "anyOf": [
                {"type": "object", "properties": {"$x": {"type": "number"}}},
                {"type": "null"},
            ]
        }
        result = _sanitize_schema_properties(schema)

        first = result["anyOf"][0]
        assert "x" in first["properties"]
        assert result["anyOf"][1] == {"type": "null"}


# =========================================================================
# _resolve_domain_dir
# =========================================================================


class TestResolveDomainDir:
    """Verify ``_resolve_domain_dir`` resolution and error behaviour."""

    def test_resolve_domain_dir_relative(self, tmp_path: Path) -> None:
        """Resolves ``'../_domain'`` (default) relative to *task_dir*."""
        # ../_domain from tasks/task_001 → tasks/_domain
        domain_dir = tmp_path / "tasks" / "_domain"
        domain_dir.mkdir(parents=True)
        task_dir = tmp_path / "tasks" / "task_001"
        task_dir.mkdir(parents=True)

        adapter = _make_adapter(tmp_path)
        result = adapter._resolve_domain_dir(task_dir, {})

        assert result == domain_dir.resolve()

    def test_resolve_domain_dir_custom(self, tmp_path: Path) -> None:
        """Resolves a custom ``domain_dir`` from *adapter_settings*."""
        custom = tmp_path / "custom_domain"
        custom.mkdir()
        task_dir = tmp_path / "tasks" / "task_001"
        task_dir.mkdir(parents=True)

        adapter = _make_adapter(tmp_path)
        result = adapter._resolve_domain_dir(task_dir, {"domain_dir": str(custom)})

        assert result == custom.resolve()

    def test_resolve_domain_dir_missing(self, tmp_path: Path) -> None:
        """Raises ``FileNotFoundError`` when the directory doesn't exist."""
        task_dir = tmp_path / "tasks" / "task_001"
        task_dir.mkdir(parents=True)

        adapter = _make_adapter(tmp_path)
        with pytest.raises(FileNotFoundError, match="Domain directory not found"):
            adapter._resolve_domain_dir(task_dir, {"domain_dir": "/nonexistent/path"})


# =========================================================================
# _read_domain_from_manifest
# =========================================================================


class TestReadDomainFromManifest:
    """Verify ``_read_domain_from_manifest`` YAML parsing."""

    def test_read_domain_from_manifest_present(self, tmp_path: Path) -> None:
        """Reads the ``domain`` key from ``domain_manifest.yaml``."""
        (tmp_path / "domain_manifest.yaml").write_text("domain: retail\n")

        adapter = _make_adapter(tmp_path)
        assert adapter._read_domain_from_manifest(tmp_path) == "retail"

    def test_read_domain_from_manifest_missing(self, tmp_path: Path) -> None:
        """Returns ``'default'`` when the manifest file is absent."""
        adapter = _make_adapter(tmp_path)
        assert adapter._read_domain_from_manifest(tmp_path) == "default"

    def test_read_domain_from_manifest_empty(self, tmp_path: Path) -> None:
        """Returns ``'default'`` when the manifest file is empty."""
        (tmp_path / "domain_manifest.yaml").write_text("")

        adapter = _make_adapter(tmp_path)
        assert adapter._read_domain_from_manifest(tmp_path) == "default"


# =========================================================================
# _load_system_prompt
# =========================================================================


class TestLoadSystemPrompt:
    """Verify ``_load_system_prompt`` file-reading logic."""

    def test_load_system_prompt_exists(self, tmp_path: Path) -> None:
        """Returns file content when the specified path exists."""
        (tmp_path / "prompt.txt").write_text("You are a helpful assistant.")

        adapter = _make_adapter(tmp_path)
        result = adapter._load_system_prompt(tmp_path, {"system_prompt": "prompt.txt"})

        assert result == "You are a helpful assistant."

    def test_load_system_prompt_missing(self, tmp_path: Path) -> None:
        """Returns empty string when the specified file doesn't exist."""
        adapter = _make_adapter(tmp_path)
        result = adapter._load_system_prompt(tmp_path, {"system_prompt": "nonexistent.txt"})

        assert result == ""

    def test_load_system_prompt_no_setting(self, tmp_path: Path) -> None:
        """Returns empty string when ``adapter_settings`` has no ``system_prompt`` key."""
        adapter = _make_adapter(tmp_path)
        result = adapter._load_system_prompt(tmp_path, {})

        assert result == ""


# =========================================================================
# _load_tool_registry
# =========================================================================


class TestLoadToolRegistry:
    """Verify ``_load_tool_registry`` JSON loading."""

    def test_load_tool_registry_valid(self, tmp_path: Path) -> None:
        """Loads valid JSON and returns a dict."""
        registry_data = {
            "get_order": {
                "toolset": "oms",
                "module_path": "tools",
                "class_name": "GetOrder",
            }
        }
        (tmp_path / "tool_registry.json").write_text(json.dumps(registry_data))

        adapter = _make_adapter(tmp_path)
        result = adapter._load_tool_registry(tmp_path)

        assert result == registry_data

    def test_load_tool_registry_missing(self, tmp_path: Path) -> None:
        """Raises ``FileNotFoundError`` when ``tool_registry.json`` is absent."""
        adapter = _make_adapter(tmp_path)
        with pytest.raises(FileNotFoundError, match="tool_registry.json not found"):
            adapter._load_tool_registry(tmp_path)


# =========================================================================
# _bundle_domain_artifacts
# =========================================================================


class TestBundleDomainArtifacts:
    """Verify ``_bundle_domain_artifacts`` base64 encoding and path handling."""

    def test_bundle_artifacts_python_files(self, tmp_path: Path) -> None:
        """``.py`` files are base64-encoded correctly."""
        (tmp_path / "module.py").write_text("print('hello')")

        adapter = _make_adapter(tmp_path)
        result = adapter._bundle_domain_artifacts(tmp_path)

        assert "module.py" in result
        decoded = base64.b64decode(result["module.py"]).decode()
        assert decoded == "print('hello')"

    def test_bundle_artifacts_json_files(self, tmp_path: Path) -> None:
        """``.json`` files are included in the bundle."""
        (tmp_path / "config.json").write_text('{"key": "value"}')

        adapter = _make_adapter(tmp_path)
        result = adapter._bundle_domain_artifacts(tmp_path)

        assert "config.json" in result
        decoded = base64.b64decode(result["config.json"]).decode()
        assert decoded == '{"key": "value"}'

    def test_bundle_artifacts_nested(self, tmp_path: Path) -> None:
        """Nested paths are preserved as posix keys (e.g. ``'mcp_core/db/database.py'``)."""
        nested = tmp_path / "mcp_core" / "db"
        nested.mkdir(parents=True)
        (nested / "database.py").write_text("class DB: pass")

        adapter = _make_adapter(tmp_path)
        result = adapter._bundle_domain_artifacts(tmp_path)

        assert "mcp_core/db/database.py" in result
        decoded = base64.b64decode(result["mcp_core/db/database.py"]).decode()
        assert decoded == "class DB: pass"

    def test_bundle_artifacts_empty_dir(self, tmp_path: Path) -> None:
        """Empty directory returns an empty dict."""
        adapter = _make_adapter(tmp_path)
        result = adapter._bundle_domain_artifacts(tmp_path)

        assert result == {}


# =========================================================================
# _create_database — requires mcp_core at runtime
# =========================================================================


class TestCreateDatabase:
    """Verify ``_create_database`` behaviour.

    These tests require ``mcp_core`` to be importable.  The autouse fixture
    skips the entire class gracefully when the package is unavailable.
    """

    @pytest.fixture(autouse=True)
    def _require_mcp_core(self) -> None:
        pytest.importorskip("mcp_core", reason="mcp_core runtime not available")

    def test_create_database_missing_initial_state(self, tmp_path: Path) -> None:
        """Raises ``FileNotFoundError`` when ``initial_state.json`` is missing."""
        domain_dir = tmp_path / "_domain"
        domain_dir.mkdir()
        task_dir = tmp_path / "task_001"
        task_dir.mkdir()

        adapter = _make_adapter(tmp_path)
        # _setup_sys_path needs the domain on sys.path; mock it to avoid side effects
        adapter._sys_paths_added = True

        with pytest.raises(FileNotFoundError, match="initial_state.json not found"):
            adapter._create_database(task_dir, domain_dir)

    def test_create_database_empty_state(self, tmp_path: Path) -> None:
        """Empty JSON ``{}`` produces a DB with no extra records."""
        domain_dir = tmp_path / "_domain"
        domain_dir.mkdir()
        task_dir = tmp_path / "task_001"
        task_dir.mkdir()
        (task_dir / "initial_state.json").write_text("{}")

        adapter = _make_adapter(tmp_path)
        adapter._sys_paths_added = True

        db = adapter._create_database(task_dir, domain_dir)

        # InMemoryDatabase was created; the state dict was empty so no records added
        assert db is not None

    def test_create_database_unknown_table_logged(self, tmp_path: Path) -> None:
        """Tables not in model registry are silently skipped (logged), not raised."""
        domain_dir = tmp_path / "_domain"
        domain_dir.mkdir()
        task_dir = tmp_path / "task_001"
        task_dir.mkdir()
        # Write state with a table name that won't match any model
        (task_dir / "initial_state.json").write_text(
            json.dumps({"nonexistent_table_xyz": [{"id": 1}]})
        )

        adapter = _make_adapter(tmp_path)
        adapter._sys_paths_added = True

        # Should NOT raise — unknown tables are just logged
        db = adapter._create_database(task_dir, domain_dir)
        assert db is not None
