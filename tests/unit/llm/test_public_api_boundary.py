"""Public-API boundary guardrail for per-model policy subclasses.

Every currently-shipped per-model subclass of a
:mod:`tolokaforge.core.llm.*` base must reach the engine through public API
only — no ``_``-prefixed helper imports, no private-base-method overrides,
no ``self._x`` / ``super()._x`` access to a base's private surface. This
module locks that contract via four static / runtime checks so a regression
fires immediately, before the #938 cutover bakes the violation into
``tolokaforge_models``.

Tests 1-3 operate on the source text of each entry in
:data:`PER_MODEL_SUBCLASSES` — parsed by :mod:`ast`, no runtime import of the
subclass required. Test 4 walks
:data:`tolokaforge.core.llm.presets._POLICY_REGISTRIES` at import time and
asserts that every registered class inheriting from another in-registry class
appears in the guardrail's enumeration, so a new per-model subclass added to
a preset registry cannot slip past unlisted.

The subclass entries name concrete-module dotted paths (e.g.
``tolokaforge.core.llm.schema_sanitizer``) rather than the top-level
``tolokaforge.core.llm`` package: the top-level ``__init__`` uses lazy
``__getattr__`` for a handful of names, and Test 2 imports each base class
directly to inspect its methods.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


#: Every currently-shipped per-model subclass or composite helper that ADR-0030
#: § Follow-up 8 puts on the #938 relocation path. Two shapes qualify:
#:
#: (a) subclasses of a concrete engine base already registered in a
#:     ``_POLICY_REGISTRIES`` slot (``StrictSchema``, ``DictMapHints``,
#:     ``OpenAIReasoningCodec``, ``ArrayDictMapResponse``, ...);
#: (b) composite helper classes that inherit only from ``object`` but wire up
#:     other in-tree policy instances internally
#:     (``JsonRecursiveCoerceResponse``, ``ItemRecursiveUnwrapResponse``,
#:     ``MinimaxM3TagRecoveryResponse``).
#:
#: Test 4 below is the authoritative audit for shape (a): it walks
#: ``_POLICY_REGISTRIES`` at runtime and asserts every registered class with an
#: in-registry base appears here. Shape (b) entries carry no automatic audit
#: from the registries and must be added manually when a new composite lands.
PER_MODEL_SUBCLASSES: list[tuple[str, str]] = [
    ("tolokaforge.core.llm.schema_sanitizer", "GeminiSchema"),
    ("tolokaforge.core.llm.schema_sanitizer", "GeminiRecursiveSchema"),
    ("tolokaforge.core.llm.prompt_policy", "RefResolvingDictMapHints"),
    ("tolokaforge.core.llm.response_policy", "ScalarArrayDictMapResponse"),
    ("tolokaforge.core.llm.response_policy", "MinimaxM3TagRecoveryResponse"),
    ("tolokaforge.core.llm.response_policy", "JsonRecursiveCoerceResponse"),
    ("tolokaforge.core.llm.response_policy", "ItemRecursiveUnwrapResponse"),
    ("tolokaforge.core.llm.reasoning_codec", "OpenAISummaryReplayReasoningCodec"),
]

#: Engine slot Protocols — the abstract slot definitions. They never appear in
#: ``_POLICY_REGISTRIES`` (only concrete classes are registered), but Test 4
#: names them explicitly to document what the registries are *not*
#: enumerating.
_SLOT_PROTOCOLS: frozenset[str] = frozenset(
    {
        "ToolSchemaSanitizer",
        "SystemPromptPolicy",
        "ToolContentPolicy",
        "ResponsePolicy",
        "ReasoningCodec",
        "CachePolicy",
        "ParamsPolicy",
        "MessageAssemblyPolicy",
        "AssistantTextPolicy",
    }
)


def _module_source_and_tree(dotted: str) -> tuple[Path, ast.Module]:
    """Return the source file path and parsed AST for ``dotted``."""
    module = importlib.import_module(dotted)
    source_path = Path(inspect.getfile(module))
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    return source_path, tree


def _find_class_def(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise LookupError(f"class {class_name!r} not found in {ast.dump(tree)[:120]}…")


def _locally_defined_names(class_def: ast.ClassDef) -> set[str]:
    """Names owned by the class: methods, class-level attrs, and any
    ``self.<name> = ...`` assignments in method bodies.
    """
    names: set[str] = set()
    for node in class_def.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    for method in class_def.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(method):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    names.add(target.attr)
    return names


def _iter_engine_base_classes(class_def: ast.ClassDef) -> list[type[Any]]:
    """Resolve every base name to a class in ``tolokaforge.core.llm.*``.

    Bases named as bare identifiers (``StrictSchema``) or dotted attribute
    chains (``tolokaforge.core.llm.schema_sanitizer.StrictSchema``) are looked
    up through the LLM modules; unresolvable bases (``Protocol``, ``object``,
    or anything outside the LLM layer) are skipped.
    """
    bases: list[type[Any]] = []
    llm_modules = [
        "tolokaforge.core.llm.schema_sanitizer",
        "tolokaforge.core.llm.prompt_policy",
        "tolokaforge.core.llm.response_policy",
        "tolokaforge.core.llm.reasoning_codec",
        "tolokaforge.core.llm.cache_policy",
        "tolokaforge.core.llm.content_policy",
        "tolokaforge.core.llm.params_policy",
        "tolokaforge.core.llm.message_assembly_policy",
        "tolokaforge.core.llm.assistant_text_policy",
    ]
    for base in class_def.bases:
        base_name = base.id if isinstance(base, ast.Name) else None
        if base_name is None:
            continue
        for dotted in llm_modules:
            module = importlib.import_module(dotted)
            candidate = getattr(module, base_name, None)
            if isinstance(candidate, type):
                bases.append(candidate)
                break
    return bases


def _base_method_names(base_cls: type[Any]) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(base_cls)
        if callable(member) and not name.startswith("__")
    }


def test_per_model_subclasses_is_populated() -> None:
    assert PER_MODEL_SUBCLASSES, (
        "PER_MODEL_SUBCLASSES must enumerate at least one subclass — an empty "
        "list disables the boundary guardrail."
    )


@pytest.mark.parametrize(("module_dotted", "class_name"), PER_MODEL_SUBCLASSES)
def test_no_private_symbol_imports(module_dotted: str, class_name: str) -> None:
    source_path, tree = _module_source_and_tree(module_dotted)
    # Confirm the class is present so a rename cannot silently defeat Tests 2/3.
    _find_class_def(tree, class_name)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module is None or not node.module.startswith("tolokaforge.core.llm."):
            continue
        for alias in node.names:
            if alias.name.startswith("_"):
                violations.append(
                    f"{source_path}:{node.lineno}: {class_name} module imports "
                    f"private symbol {alias.name!r} from {node.module!r}. Promote "
                    f"{alias.name!r} to public API or reshape."
                )
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(("module_dotted", "class_name"), PER_MODEL_SUBCLASSES)
def test_no_private_base_method_override(module_dotted: str, class_name: str) -> None:
    source_path, tree = _module_source_and_tree(module_dotted)
    class_def = _find_class_def(tree, class_name)
    subclass_private_methods = [
        node
        for node in class_def.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
        and not node.name.startswith("__")
    ]
    if not subclass_private_methods:
        return
    base_names: dict[str, type[Any]] = {}
    for base_cls in _iter_engine_base_classes(class_def):
        for name in _base_method_names(base_cls):
            if name.startswith("_"):
                base_names.setdefault(name, base_cls)
    violations: list[str] = []
    for method in subclass_private_methods:
        base_cls = base_names.get(method.name)
        if base_cls is None:
            continue
        violations.append(
            f"{source_path}:{method.lineno}: {class_name} overrides private "
            f"base method {method.name!r} on "
            f"{base_cls.__module__}.{base_cls.__name__}. Promote {method.name!r} "
            f"to a public overridable hook."
        )
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(("module_dotted", "class_name"), PER_MODEL_SUBCLASSES)
def test_no_private_attr_reach(module_dotted: str, class_name: str) -> None:
    source_path, tree = _module_source_and_tree(module_dotted)
    class_def = _find_class_def(tree, class_name)
    locally_defined = _locally_defined_names(class_def)
    base_method_names: set[str] = set()
    for base_cls in _iter_engine_base_classes(class_def):
        base_method_names.update(_base_method_names(base_cls))
    violations: list[str] = []
    for method in class_def.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(method):
            if not isinstance(sub, ast.Attribute) or not sub.attr.startswith("_"):
                continue
            source = _classify_receiver(sub.value)
            if source is None:
                continue
            if sub.attr in locally_defined:
                continue
            if sub.attr not in base_method_names:
                continue
            violations.append(
                f"{source_path}:{sub.lineno}: {class_name} reaches private "
                f"{source}.{sub.attr!r} not defined locally and matching a "
                f"base-class method name. Either declare {sub.attr!r} in the "
                f"subclass body or use public API."
            )
    assert not violations, "\n".join(violations)


def _classify_receiver(node: ast.expr) -> str | None:
    """Return ``"self"`` / ``"cls"`` / ``"super()"`` for an attribute
    receiver we care about, or ``None`` for anything else.
    """
    if isinstance(node, ast.Name) and node.id in {"self", "cls"}:
        return node.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "super"
        and not node.args
        and not node.keywords
    ):
        return "super()"
    return None


def test_per_model_subclasses_covers_registered_non_base_classes() -> None:
    presets = importlib.import_module("tolokaforge.core.llm.presets")
    listed = set(PER_MODEL_SUBCLASSES)
    missing: list[str] = []
    for slot, registry in presets._POLICY_REGISTRIES.items():
        registered = set(registry.values())
        for cls in registry.values():
            if not cls.__module__.startswith("tolokaforge.core.llm."):
                continue
            if cls.__name__ in _SLOT_PROTOCOLS:
                continue
            in_slot_ancestors = [
                base for base in cls.__mro__[1:] if base is not object and base in registered
            ]
            if not in_slot_ancestors:
                continue
            key = (cls.__module__, cls.__name__)
            if key not in listed:
                missing.append(
                    f"{slot}: {cls.__module__}.{cls.__name__} inherits from "
                    f"registered slot base(s) "
                    f"{[b.__name__ for b in in_slot_ancestors]!r} but is not in "
                    f"PER_MODEL_SUBCLASSES. Add it to the list or promote its "
                    f"private-helper usage to public API."
                )
    assert not missing, "\n".join(missing)
