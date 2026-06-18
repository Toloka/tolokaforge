"""Runner tool factory must not assume a private package layout.

The public engine cannot hardcode any non-public package name into the import
path it uses for adapter-supplied tools. ``_create_mcp_async_wrapper`` and its
sibling ``_register_toolset_models`` now import ``{toolset}.{module_path}``
and ``{toolset}.models`` directly — the adapter supplies the fully-qualified
package; the runner adds no prefix. This mirrors how ``_create_tau_sync_wrapper``
has always worked.

These tests pin both ends of that contract:
  1. the import path the runner *constructs* contains no extra prefix;
  2. the error path *reports* surfaced via ``ToolImportError`` contains the
     exact module path the adapter supplied — and crucially nothing else.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.runner.models import InvocationStyle, ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import ToolFactory, ToolImportError

pytestmark = pytest.mark.unit


@pytest.fixture
def factory():
    return ToolFactory(db_client=MagicMock(), trial_id="test:0")


def _mcp_async_schema(*, toolset: str, module_path: str, class_name: str = "T") -> ToolSchema:
    return ToolSchema(
        name="t",
        description="",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            toolset=toolset,
            module_path=module_path,
            class_name=class_name,
            invocation_style=InvocationStyle.MCP_ASYNC,
        ),
    )


class TestMcpAsyncImportPath:
    """The class-import path: ``{toolset}.{module_path}`` verbatim."""

    def test_uses_adapter_supplied_path_unchanged(self, factory, monkeypatch):
        captured: list[str] = []

        def _fake_import(path):
            captured.append(path)
            module = MagicMock()
            module.MyTool = type("MyTool", (), {})
            return module

        monkeypatch.setattr("tolokaforge.runner.tool_factory.importlib.import_module", _fake_import)
        # Skip the models-registration side effect; it has its own test below.
        monkeypatch.setattr(ToolFactory, "_register_toolset_models", lambda self, toolset: None)

        schema = _mcp_async_schema(
            toolset="my_adapter_pkg.zendesk",
            module_path="tools.create_item",
            class_name="MyTool",
        )
        factory._create_wrapper(schema)

        assert captured == ["my_adapter_pkg.zendesk.tools.create_item"]


class TestMcpAsyncErrorMessages:
    """When the configured path is unimportable, the error names exactly what
    the adapter supplied — not any private root the engine used to prepend.
    """

    def test_unimportable_module_error_names_only_the_configured_path(self, factory):
        schema = _mcp_async_schema(
            toolset="definitely_no_such_pkg_42",
            module_path="x.y",
        )
        with pytest.raises(ToolImportError) as exc_info:
            factory._create_wrapper(schema)

        msg = str(exc_info.value)
        assert "definitely_no_such_pkg_42.x.y" in msg
        # Regression guard: no private root may sneak back into the error text.
        assert "mcp_tools_library" not in msg


class TestModelsRegistrationImportPath:
    """The companion ``{toolset}.models`` import — same no-prefix contract."""

    def test_models_import_uses_adapter_supplied_path_unchanged(self, factory, monkeypatch):
        captured: list[str] = []

        def _fake_import(path):
            captured.append(path)
            # Empty module — the registration loop just iterates dir(); a
            # MagicMock would expose unrelated attributes and trigger the
            # FAIL-LOUD branch downstream, so use a real empty namespace.
            import types

            return types.ModuleType(path)

        monkeypatch.setattr("tolokaforge.runner.tool_factory.importlib.import_module", _fake_import)

        factory._register_toolset_models("my_adapter_pkg.zendesk")

        assert captured == ["my_adapter_pkg.zendesk.models"]
