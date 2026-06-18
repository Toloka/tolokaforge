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

import types
from unittest.mock import MagicMock

import pytest

from tolokaforge.runner.models import InvocationStyle, ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import ToolFactory, ToolImportError

pytestmark = pytest.mark.unit

# Private package names the public engine has historically been coupled to.
# The runner must never inject any of these into the import path or error text;
# they may appear only when the *adapter* itself supplied them.
FORBIDDEN_PRIVATE_NAMES = ("mcp_tools_library", "mcp_core", "tau_bench")


@pytest.fixture
def factory():
    return ToolFactory(db_client=MagicMock(), trial_id="test:0")


def _schema(
    *, style: InvocationStyle, toolset: str, module_path: str, class_name: str = "T"
) -> ToolSchema:
    return ToolSchema(
        name="t",
        description="",
        parameters={"type": "object", "properties": {}},
        source=ToolSource(
            toolset=toolset,
            module_path=module_path,
            class_name=class_name,
            invocation_style=style,
        ),
    )


class TestMcpAsyncImportPath:
    """The class-import path: ``{toolset}.{module_path}`` verbatim."""

    def test_uses_adapter_supplied_path_unchanged(self, factory, monkeypatch):
        captured_class: list[str] = []
        captured_register_arg: list[str] = []

        def _fake_import(path):
            captured_class.append(path)
            module = MagicMock()
            module.MyTool = type("MyTool", (), {})
            return module

        monkeypatch.setattr("tolokaforge.runner.tool_factory.importlib.import_module", _fake_import)
        # Pin the boundary: the registration helper receives ``source.toolset``
        # verbatim, with no prefix munging by the dispatch.
        monkeypatch.setattr(
            ToolFactory,
            "_register_toolset_models",
            lambda self, toolset: captured_register_arg.append(toolset),
        )

        schema = _schema(
            style=InvocationStyle.MCP_ASYNC,
            toolset="my_adapter_pkg.zendesk",
            module_path="tools.create_item",
            class_name="MyTool",
        )
        factory._create_wrapper(schema)

        assert captured_class == ["my_adapter_pkg.zendesk.tools.create_item"]
        assert captured_register_arg == ["my_adapter_pkg.zendesk"]


class TestTauSyncImportPath:
    """TAU_SYNC has always used the no-prefix convention; symmetry test that
    the error-message refactor in this PR didn't drift it from that shape.
    """

    def test_unimportable_module_error_names_only_the_configured_path(self, factory):
        schema = _schema(
            style=InvocationStyle.TAU_SYNC,
            toolset="definitely_no_such_pkg_42",
            module_path="x.y",
        )
        with pytest.raises(ToolImportError) as exc_info:
            factory._create_wrapper(schema)

        msg = str(exc_info.value)
        assert "definitely_no_such_pkg_42.x.y" in msg
        for forbidden in FORBIDDEN_PRIVATE_NAMES:
            assert forbidden not in msg, f"Private name {forbidden!r} leaked into TAU_SYNC error"


class TestMcpAsyncErrorMessages:
    """When the configured path is unimportable, the error names exactly what
    the adapter supplied — not any private root the engine used to prepend.
    """

    def test_unimportable_module_error_names_only_the_configured_path(self, factory):
        schema = _schema(
            style=InvocationStyle.MCP_ASYNC,
            toolset="definitely_no_such_pkg_42",
            module_path="x.y",
        )
        with pytest.raises(ToolImportError) as exc_info:
            factory._create_wrapper(schema)

        msg = str(exc_info.value)
        assert "definitely_no_such_pkg_42.x.y" in msg
        for forbidden in FORBIDDEN_PRIVATE_NAMES:
            assert forbidden not in msg, f"Private name {forbidden!r} leaked into MCP_ASYNC error"


class TestModelsRegistrationImportPath:
    """The companion ``{toolset}.models`` import — same no-prefix contract."""

    def test_models_import_uses_adapter_supplied_path_unchanged(self, factory, monkeypatch):
        captured: list[str] = []

        def _fake_import(path):
            captured.append(path)
            # Empty module — the registration loop just iterates dir(); a
            # MagicMock would expose unrelated attributes and trigger the
            # FAIL-LOUD branch downstream, so use a real empty namespace.
            return types.ModuleType(path)

        monkeypatch.setattr("tolokaforge.runner.tool_factory.importlib.import_module", _fake_import)

        factory._register_toolset_models("my_adapter_pkg.zendesk")

        assert captured == ["my_adapter_pkg.zendesk.models"]

    def test_broken_models_module_propagates_not_swallowed(self, factory, monkeypatch):
        """A genuine ImportError *inside* a models module (e.g. missing dep)
        must NOT be silently swallowed at debug level. The fail-quiet path is
        scoped to ``ModuleNotFoundError`` (the toolset has no models module);
        any other ImportError surfaces the bug.
        """

        def _fake_import(path):
            # Plain ImportError, not ModuleNotFoundError — simulates a models
            # module that exists but raises during its own imports.
            raise ImportError("simulated missing transitive dep")

        monkeypatch.setattr("tolokaforge.runner.tool_factory.importlib.import_module", _fake_import)

        with pytest.raises(ImportError, match="simulated missing transitive dep"):
            factory._register_toolset_models("my_adapter_pkg.zendesk")
