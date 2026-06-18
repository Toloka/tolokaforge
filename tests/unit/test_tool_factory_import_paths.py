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

import json
import sys
import textwrap
import types
from unittest.mock import MagicMock

import pytest

from tolokaforge.runner.models import InvocationStyle, ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import (
    MCPAsyncToolWrapper,
    ToolFactory,
    ToolImportError,
)

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


class TestMcpAsyncEndToEnd:
    """End-to-end: a real Python package on disk + sys.path + ``importlib``,
    with no mocks of the import machinery. Proves the engine can load an
    MCP_ASYNC tool from an adapter-supplied package path without any prefix
    coupling — the contract this PR establishes, exercised against real code
    rather than monkeypatched ``import_module``.
    """

    def test_real_package_loads_with_no_prefix(self, factory, tmp_path, monkeypatch):
        # Build a tiny tool package on disk:
        #   tmp/pkg_root/my_adapter_under_test/zendesk/__init__.py
        #   tmp/pkg_root/my_adapter_under_test/zendesk/tools/__init__.py
        #   tmp/pkg_root/my_adapter_under_test/zendesk/tools/create_item.py  (class CreateItem)
        #   tmp/pkg_root/my_adapter_under_test/zendesk/models.py             (no model classes)
        pkg_root = tmp_path / "pkg_root"
        zendesk = pkg_root / "my_adapter_under_test" / "zendesk"
        (zendesk / "tools").mkdir(parents=True)
        (pkg_root / "my_adapter_under_test" / "__init__.py").write_text("")
        (zendesk / "__init__.py").write_text("")
        (zendesk / "tools" / "__init__.py").write_text("")
        (zendesk / "tools" / "create_item.py").write_text(
            textwrap.dedent(
                """
                class CreateItem:
                    pass
                """
            )
        )
        (zendesk / "models.py").write_text("")  # empty: registration is a no-op

        monkeypatch.syspath_prepend(str(pkg_root))

        schema = _schema(
            style=InvocationStyle.MCP_ASYNC,
            toolset="my_adapter_under_test.zendesk",
            module_path="tools.create_item",
            class_name="CreateItem",
        )
        wrapper = factory._create_wrapper(schema)

        # Engine loaded the adapter-supplied path verbatim (no prefix), found
        # the class, and built a real wrapper.
        assert isinstance(wrapper, MCPAsyncToolWrapper)
        assert wrapper.tool_class.__name__ == "CreateItem"
        assert wrapper.tool_class.__module__ == "my_adapter_under_test.zendesk.tools.create_item"

    async def test_full_pipeline_loads_and_invokes_tool_with_no_engine_prefix(
        self, factory, tmp_path, monkeypatch
    ):
        """The most honest in-repo signal for this PR's change. Build a real
        adapter-style tool package, route a serialized TaskDescription tool
        block through ``ToolFactory.reconstruct_tools`` (the public engine
        entry point), and then **invoke** the resulting wrapper through the
        live ``MCPAsyncToolWrapper.execute`` path. Surfaces touched, no mocks:

          * pydantic ``ToolSchema`` validation
          * ``_create_wrapper`` dispatch
          * ``_create_mcp_async_wrapper`` (real ``importlib.import_module``)
          * ``_register_toolset_models`` (real importlib; hits the
            fail-quiet ``ModuleNotFoundError`` branch tightened in this PR
            because the test package ships no ``models.py``)
          * ``MCPAsyncToolWrapper`` construction
          * ``MCPAsyncToolWrapper.execute`` (live, awaited)
          * the test tool class's real ``run_with_validation``

        Only ``db_client`` is mocked, because the test tool deliberately does
        not touch the DB. After this PR, the engine constructs the import
        path verbatim from the adapter-supplied ``toolset`` (no prefix); if
        the change ever regresses, this test fails at the wrapper step.
        """
        # Real MCP-style tool package: my_e2e_adapter_pkg/zendesk_e2e/tools/echo.py
        pkg_root = tmp_path / "pkg"
        zendesk = pkg_root / "my_e2e_adapter_pkg" / "zendesk_e2e"
        (zendesk / "tools").mkdir(parents=True)
        (pkg_root / "my_e2e_adapter_pkg" / "__init__.py").write_text("")
        (zendesk / "__init__.py").write_text("")
        (zendesk / "tools" / "__init__.py").write_text("")
        (zendesk / "tools" / "echo.py").write_text(
            textwrap.dedent(
                """
                class Echo:
                    async def run_with_validation(self, db, arguments):
                        # Mirrors the real MCP runtime contract: take a kwargs
                        # dict, return a JSON-serializable dict. No DB use, so
                        # the mocked db_proxy is irrelevant here.
                        return {"echoed": arguments.get("message"), "count": len(arguments)}
                """
            )
        )
        # Deliberately NO models.py — exercises the fail-quiet
        # ``ModuleNotFoundError`` branch that this PR narrowed (improvement #2).
        monkeypatch.syspath_prepend(str(pkg_root))

        # Serialized TaskDescription tool block, fully-qualified toolset
        # per the post-PR engine contract.
        tool_dict = {
            "name": "echo",
            "description": "Echo arguments back",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            "source": {
                "toolset": "my_e2e_adapter_pkg.zendesk_e2e",
                "module_path": "tools.echo",
                "class_name": "Echo",
                "invocation_style": "mcp_async",
            },
        }

        # The actual engine pipeline — no mocks of dispatch, import, or
        # wrapper construction.
        reconstructed = factory.reconstruct_tools(agent_tools=[tool_dict])

        assert "echo" in reconstructed.agent_tools
        wrapper = reconstructed.agent_tools["echo"]
        assert isinstance(wrapper, MCPAsyncToolWrapper)
        assert wrapper.tool_class.__module__ == ("my_e2e_adapter_pkg.zendesk_e2e.tools.echo")

        # Live invocation — exercises MCPAsyncToolWrapper.execute end-to-end.
        output = await wrapper.execute({"message": "hello", "extra": 42})
        assert json.loads(output) == {"echoed": "hello", "count": 2}

    def test_no_engine_prefix_means_old_bare_subpackage_now_fails_loudly(
        self, factory, tmp_path, monkeypatch
    ):
        """Pre-PR private adapters set toolset=\"zendesk\" relying on the
        engine to prepend a private root. After this PR the bare name is
        imported as-is; if it doesn't exist on sys.path the engine raises
        ToolImportError naming the exact missing path — the clear signal for
        adapter authors to migrate. This test pins the failure mode.
        """
        # Build the package only under a (real) parent; the bare name "zendesk"
        # alone is NOT importable.
        pkg_root = tmp_path / "pkg_root"
        zendesk = pkg_root / "my_adapter_under_test" / "zendesk"
        (zendesk / "tools").mkdir(parents=True)
        (pkg_root / "my_adapter_under_test" / "__init__.py").write_text("")
        (zendesk / "__init__.py").write_text("")
        (zendesk / "tools" / "__init__.py").write_text("")
        (zendesk / "tools" / "create_item.py").write_text("class CreateItem: pass\n")
        monkeypatch.syspath_prepend(str(pkg_root))

        # Sanity: the qualified name imports
        import importlib

        importlib.import_module("my_adapter_under_test.zendesk.tools.create_item")
        # Sanity: the bare name does not
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("zendesk.tools.create_item")
        # Drop any stale cache entry for the bare name from prior test runs.
        sys.modules.pop("zendesk", None)
        sys.modules.pop("zendesk.tools", None)
        sys.modules.pop("zendesk.tools.create_item", None)

        # The engine: bare-name adapter shape now fails loudly with the exact
        # path the adapter supplied — so the author knows what to update.
        schema = _schema(
            style=InvocationStyle.MCP_ASYNC,
            toolset="zendesk",
            module_path="tools.create_item",
            class_name="CreateItem",
        )
        with pytest.raises(ToolImportError) as exc_info:
            factory._create_wrapper(schema)
        msg = str(exc_info.value)
        assert "zendesk.tools.create_item" in msg
        for forbidden in FORBIDDEN_PRIVATE_NAMES:
            assert forbidden not in msg
