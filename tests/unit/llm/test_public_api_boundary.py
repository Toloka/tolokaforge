"""Public-API boundary guardrail for per-model policy subclasses.

Every per-model subclass of a :mod:`tolokaforge.core.llm.*` base must reach
the engine through public API only — no ``_``-prefixed helper imports, no
private-base-method overrides, no ``self._x`` / ``super()._x`` access to a
base's private surface. This module locks that contract via four static /
runtime checks:

* **Test 1 (private-symbol imports)** — no ``from tolokaforge.core.llm[.X]
  import _foo`` on the subclass' host module.
* **Test 2 (private-base-method override)** — no subclass method named
  ``_x`` where ``_x`` exists on any resolvable engine base.
* **Test 3 (private-attr reach)** — no ``self._x`` / ``cls._x`` /
  ``super()._x`` where ``_x`` is a base-class method name and not
  locally defined by the subclass.
* **Test 4 (registry audit)** — every class in
  :data:`tolokaforge.core.llm.presets._POLICY_REGISTRIES` that inherits
  from another in-registry class appears in :data:`PER_MODEL_SUBCLASSES`,
  so a new per-model subclass added to a preset registry cannot slip past
  unlisted.

Tests 1-3 parse the subclass' host-module source via :mod:`ast`; no runtime
import of the subclass is required. Test 4 walks the registries at import
time.

Entries in :data:`PER_MODEL_SUBCLASSES` name concrete-module dotted paths in
:mod:`tolokaforge_models.policies` (e.g. ``tolokaforge_models.policies.gemini``)
where the eight per-model subclasses actually live; the engine's public shim
in ``tolokaforge.core.llm.__init__.py`` still resolves them for a one-release
deprecation window but is not the boundary the AST walk audits.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any, Final

import pytest

pytestmark = pytest.mark.unit


#: Per-model subclasses and composite helpers under the boundary guardrail.
#: Two shapes qualify:
#:
#: (a) subclasses of a concrete engine base already registered in a
#:     ``_POLICY_REGISTRIES`` slot (``StrictSchema``, ``DictMapHints``,
#:     ``OpenAIReasoningCodec``, ``ArrayDictMapResponse``, ...);
#: (b) composite helper classes registered in ``_POLICY_REGISTRIES`` that
#:     inherit only from ``object`` and wire up other in-tree policy
#:     instances internally (``JsonRecursiveCoerceResponse``,
#:     ``ItemRecursiveUnwrapResponse``, ``MinimaxM3TagRecoveryResponse``).
#:
#: Test 4 below is the authoritative audit for shape (a): it walks
#: ``_POLICY_REGISTRIES`` at runtime and asserts every registered class with an
#: in-registry base appears here. Shape (b) entries carry no automatic audit
#: from the registries and must be added manually when a new composite lands.
PER_MODEL_SUBCLASSES: Final[tuple[tuple[str, str], ...]] = (
    ("tolokaforge_models.policies.gemini", "GeminiSchema"),
    ("tolokaforge_models.policies.gemini", "GeminiRecursiveSchema"),
    ("tolokaforge_models.policies.gemini", "ScalarArrayDictMapResponse"),
    ("tolokaforge_models.policies.inkling", "RefResolvingDictMapHints"),
    ("tolokaforge_models.policies.minimax", "MinimaxM3TagRecoveryResponse"),
    ("tolokaforge_models.policies.minimax", "JsonRecursiveCoerceResponse"),
    ("tolokaforge_models.policies.minimax", "ItemRecursiveUnwrapResponse"),
    ("tolokaforge_models.policies.deepseek", "OpenAISummaryReplayReasoningCodec"),
)

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
        base_name = _base_name(base)
        if base_name is None:
            continue
        for dotted in llm_modules:
            module = importlib.import_module(dotted)
            candidate = getattr(module, base_name, None)
            if isinstance(candidate, type):
                bases.append(candidate)
                break
    return bases


def _base_name(node: ast.expr) -> str | None:
    """Extract the trailing identifier from a base-class expression.

    Handles bare identifiers (``StrictSchema``) and dotted attribute chains
    (``tolokaforge.core.llm.schema_sanitizer.StrictSchema``); returns
    ``None`` for anything else (subscripted generics, calls, ...).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


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
        if node.module is None or not (
            node.module == "tolokaforge.core.llm" or node.module.startswith("tolokaforge.core.llm.")
        ):
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
    """Every registered per-model class that lives outside the engine base
    modules must appear in :data:`PER_MODEL_SUBCLASSES`, whether it inherits
    from an in-registry slot base or is a composite inheriting only from
    ``object``.

    Filter shape: engine-owned base classes and slot Protocols
    (``StrictSchema``, ``DictMapHints``, ``ArrayDictMapResponse``, …) live
    under ``tolokaforge.core.llm.*`` and stay engine-side; the AST audit
    does not run against them because they define the abstract surface,
    not implement it. Every OTHER registered class — the per-model
    subclasses and composites the models wheel ships — must be enumerated
    here so tests 1-3 audit each one.

    Composite classes (shape (b) in the module docstring) inherit only
    from ``object`` and wire up sibling policy instances internally. They
    have no in-registry ancestor, so the older "must inherit from a
    registered base" filter would silently exempt them. This walk requires
    every non-engine class to be listed regardless of ancestor shape.
    """
    presets = importlib.import_module("tolokaforge.core.llm.presets")
    listed = set(PER_MODEL_SUBCLASSES)
    missing: list[str] = []
    for slot, registry in presets._POLICY_REGISTRIES.items():
        registered = set(registry.values())
        for cls in registry.values():
            # Engine-owned base classes and slot Protocols are the abstract
            # surface — skip them. Everything else must be enumerated.
            if cls.__module__.startswith("tolokaforge.core.llm."):
                continue
            if cls.__name__ in _SLOT_PROTOCOLS:
                continue
            key = (cls.__module__, cls.__name__)
            if key not in listed:
                in_slot_ancestors = [
                    base for base in cls.__mro__[1:] if base is not object and base in registered
                ]
                shape = (
                    f"inherits from registered slot base(s) "
                    f"{[b.__name__ for b in in_slot_ancestors]!r}"
                    if in_slot_ancestors
                    else "is a composite (inherits only from object)"
                )
                missing.append(
                    f"{slot}: {cls.__module__}.{cls.__name__} {shape} "
                    f"but is not in PER_MODEL_SUBCLASSES. Add it to the "
                    f"list so tests 1-3 audit it against the engine's "
                    f"public API boundary."
                )
    assert not missing, "\n".join(missing)
