"""Positive contract for the unified builtin tool registry.

Eliminates drift between adapter/runner/executor by being the single
source of truth for ``name -> (module_path, class_name)`` and
``name -> dispatch wrapper kind``.
"""

from __future__ import annotations

import pytest

from tolokaforge.tools.builtin import registry

pytestmark = pytest.mark.unit


def test_list_builtins_covers_every_consumer_today():
    """The registry must cover the union of every existing builtin
    consumer's vocabulary so this commit can replace them all."""
    expected = {
        # adapter side (was _BUILTIN_TOOL_CLASSES, 3 entries)
        "bash",
        "read_file",
        "write_file",
        # runner side (was _BUILTIN_TOOL_FACTORIES, 7 entries)
        "calculator",
        "browser",
        "http_request",
        "mobile",
        "db_query",
        "db_update",
        # executor side (was builtin_tool_factories, 10 entries)
        "list_dir",
        "search_kb",
        # session-lifetime persistent shell (#566)
        "bash_session",
        # str-replace editor (#567)
        "str_replace_editor",
    }
    assert expected == set(registry.list_builtins())


def test_dispatch_groups_are_disjoint_and_exhaustive():
    generic = registry.list_for_dispatch(registry.Dispatch.GENERIC)
    files = registry.list_for_dispatch(registry.Dispatch.FILES)
    rag = registry.list_for_dispatch(registry.Dispatch.RAG)
    shell = registry.list_for_dispatch(registry.Dispatch.PERSISTENT_SHELL)
    editor = registry.list_for_dispatch(registry.Dispatch.EDITOR)
    groups = [generic, files, rag, shell, editor]
    # Disjoint
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            assert a.isdisjoint(b)
    # Exhaustive
    assert generic | files | rag | shell | editor == registry.list_builtins()


def test_bash_session_routes_to_persistent_shell_dispatch():
    from tolokaforge.tools.persistent_shell import PersistentShellTool

    assert registry.get_dispatch("bash_session") is registry.Dispatch.PERSISTENT_SHELL
    assert registry.get_class("bash_session") is PersistentShellTool


def test_str_replace_editor_routes_to_editor_dispatch():
    from tolokaforge.tools.str_replace_editor import StrReplaceEditorTool

    assert registry.get_dispatch("str_replace_editor") is registry.Dispatch.EDITOR
    assert registry.get_class("str_replace_editor") is StrReplaceEditorTool


def test_bash_is_generic_not_files():
    """Bash was previously listed in both adapter and runner registries.
    The unified registry resolves the dual-listing — bash routes to
    BuiltinGenericToolWrapper, not BuiltinFileToolWrapper."""
    assert registry.get_dispatch("bash") is registry.Dispatch.GENERIC


def test_file_tools_route_to_files_dispatch():
    for name in ("read_file", "write_file", "list_dir"):
        assert registry.get_dispatch(name) is registry.Dispatch.FILES


def test_search_kb_routes_to_rag_dispatch():
    assert registry.get_dispatch("search_kb") is registry.Dispatch.RAG


def test_get_class_imports_real_classes():
    from tolokaforge.tools.builtin.bash import BashTool
    from tolokaforge.tools.builtin.calculator import CalculatorTool
    from tolokaforge.tools.builtin.mobile import MobileTool

    assert registry.get_class("bash") is BashTool
    assert registry.get_class("calculator") is CalculatorTool
    assert registry.get_class("mobile") is MobileTool


def test_get_class_is_cached():
    """Repeated calls don't re-import — the lru_cache is part of the
    contract because some tool modules pull in heavy dependencies
    (Playwright for browser, etc.)."""
    a = registry.get_class("calculator")
    b = registry.get_class("calculator")
    assert a is b


def test_unknown_name_raises_keyerror():
    assert not registry.is_builtin("not_a_real_tool")
    with pytest.raises(KeyError):
        registry.get_dispatch("not_a_real_tool")
    with pytest.raises(KeyError):
        registry.get_class("not_a_real_tool")
