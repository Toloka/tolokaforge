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
* **Test 4 (boundary direction)** — no class registered from
  ``tolokaforge.core.llm.*`` extends another in-registry class. That shape
  is a per-model subclass on the engine side of the boundary, exactly what
  the auto-integration would recreate if a resolve agent wrote into an
  engine module.

Tests 1-3 parse the subclass' host-module source via :mod:`ast`; no runtime
import of the subclass is required. Test 4 walks the registries at import
time.

Entries in :data:`PER_MODEL_SUBCLASSES` name concrete-module dotted paths in
:mod:`tolokaforge_models.policies` (e.g. ``tolokaforge_models.policies.gemini``)
where the per-model subclasses actually live; the engine's public shim
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
#: Two shapes qualify: subclasses of a concrete engine base registered in a
#: ``_POLICY_REGISTRIES`` slot, and composite helpers that inherit only from
#: ``object`` and wire up other policy instances internally.
#:
#: The set is DERIVED from the merged ``_POLICY_REGISTRIES`` at collection time,
#: not hand-maintained. A hand-maintained list would make every models-wheel
#: class an engine-repo edit to add it — which is Bucket B under
#: ``bucket_classifier.py`` and would put the auto-integration pipeline back on
#: the engine release axis for exactly the integrations ADR-0030 moved off it.
#: Registration via the ``tolokaforge.policies`` entry-point group is mandatory
#: for a class to be usable at all, so the registries are a complete source.
#:
#: Derivation is by MODULE, not by registered class. Shape (b) composites are
#: composed into a registered class rather than registered themselves, so a
#: registry-only walk cannot see them — and ``resolve_agent.md`` holds exactly
#: that composition up as the pattern to copy, so unregistered per-model code is
#: the expected case, not an exception worth hardcoding. Taking every public
#: class defined in a contributing module audits the composites for free and
#: keeps growing on its own as new families land.
def _derive_per_model_subclasses() -> tuple[tuple[str, str], ...]:
    """Every public class defined in a module that contributes a registered policy.

    A models-wheel module earns its place by registering at least one class
    through the ``tolokaforge.policies`` entry-point group (mandatory — an
    unregistered class could not be named by a preset). Everything public that
    module defines then ships to the same consumers under the same boundary
    rules, so it is audited too.
    """
    presets = importlib.import_module("tolokaforge.core.llm.presets")
    contributing_modules = {
        cls.__module__
        for registry in presets._POLICY_REGISTRIES.values()
        for cls in registry.values()
        if not cls.__module__.startswith("tolokaforge.core.llm.")
    }
    found: set[tuple[str, str]] = set()
    for module_dotted in contributing_modules:
        module = importlib.import_module(module_dotted)
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isclass(obj):
                continue
            # Imported symbols keep their defining module; only classes this
            # module actually defines are its responsibility.
            if obj.__module__ == module_dotted:
                found.add((module_dotted, name))
    return tuple(sorted(found))


PER_MODEL_SUBCLASSES: Final[tuple[tuple[str, str], ...]] = _derive_per_model_subclasses()

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


def test_per_model_subclasses_covers_every_contributing_module() -> None:
    """Each module that registers a policy must appear in the derived set.

    A count floor would pass on a partial derivation that still cleared it, and
    would fail on a legitimately retired family with a constant edit as the only
    repair. Coverage of the registries is the property actually worth locking.
    """
    presets = importlib.import_module("tolokaforge.core.llm.presets")
    contributing = {
        cls.__module__
        for registry in presets._POLICY_REGISTRIES.values()
        for cls in registry.values()
        if not cls.__module__.startswith("tolokaforge.core.llm.")
    }
    assert contributing, (
        "no module outside tolokaforge.core.llm registers a policy — either the "
        "models wheel is not installed or entry-point loading broke, and the "
        "boundary guardrail below would silently audit nothing"
    )
    audited = {module for module, _ in PER_MODEL_SUBCLASSES}
    assert contributing <= audited, (
        "modules register a policy but contribute no audited class: "
        f"{sorted(contributing - audited)}"
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


def test_no_per_model_subclass_is_registered_engine_side() -> None:
    """No per-model policy class may live under ``tolokaforge.core.llm.*``.

    :data:`PER_MODEL_SUBCLASSES` is derived from the registries, so "is it
    listed" is not a question worth asking. The invariant that still
    needs guarding is the direction of the boundary: per-model policy code
    belongs in the models wheel, and the engine LLM package holds only slot
    Protocols and the concrete *bases* those per-model classes extend.

    A class registered from ``tolokaforge.core.llm.*`` that itself inherits
    from another registered class is a per-model subclass sitting on the
    wrong side. That is the shape this guard forbids, and it is what the auto
    integration would recreate if a resolve agent wrote a new adapter into an
    engine module. `.github/workflows/integrate-model.yml` refuses to commit
    that tree; this test is the same rule enforced against the merged
    registries, so a hand-written engine-side subclass cannot slip past
    either.

    Engine-side classes that inherit only from ``object`` or from a slot
    Protocol are bases, not per-model adaptations, and are left alone.
    """
    presets = importlib.import_module("tolokaforge.core.llm.presets")
    offenders: list[str] = []
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
            if in_slot_ancestors:
                offenders.append(
                    f"{slot}: {cls.__module__}.{cls.__name__} extends registered "
                    f"base(s) {[b.__name__ for b in in_slot_ancestors]!r} but lives "
                    f"in the engine LLM package. Per-model policy classes belong in "
                    f"tolokaforge_models/src/tolokaforge_models/policies/<family>.py, "
                    f"registered via the `tolokaforge.policies` entry-point group. "
                    f"If this really is a new engine base, it must not be a subclass "
                    f"of another registered class."
                )
    assert not offenders, "\n".join(offenders)
