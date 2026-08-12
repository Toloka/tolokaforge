"""Lock the ``tolokaforge.core.llm`` deprecation shim for the moved subclasses.

Eight per-model policy subclasses moved to :mod:`tolokaforge_models.policies`;
``tolokaforge.core.llm.__init__`` keeps a one-release ``__getattr__`` shim so
``from tolokaforge.core.llm import GeminiRecursiveSchema`` still resolves,
emitting a ``DeprecationWarning`` on first access per name and caching the
warned name so subsequent accesses are silent (Python's default warnings
filter dedupes on ``(message, category, module, lineno)``, which would otherwise
let two callsites on different source lines each re-warn).

Contract locked:

1. First access to a moved name emits exactly one ``DeprecationWarning`` whose
   message names the new import path.
2. A second access to the same name emits zero additional warnings.
3. A different moved name fires its own first-time warning independently
   (per-name caching, not global-once).
4. The resolved class is identity-equal to the class at the new path.
"""

from __future__ import annotations

import importlib
import warnings

import pytest

from tolokaforge.core.llm import _MOVED_SUBCLASSES, _WARNED

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_shim_state() -> None:
    _WARNED.clear()


def _resolve_from_shim(name: str):
    """Resolve ``name`` through the shim, returning the class and any caught warnings."""
    llm = importlib.import_module("tolokaforge.core.llm")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cls = getattr(llm, name)
    return cls, [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_first_access_emits_deprecation_naming_new_path() -> None:
    cls, warned = _resolve_from_shim("GeminiRecursiveSchema")
    assert len(warned) == 1, f"expected exactly one DeprecationWarning; got {warned!r}"
    assert "tolokaforge_models.policies.gemini.GeminiRecursiveSchema" in str(warned[0].message)
    from tolokaforge_models.policies.gemini import GeminiRecursiveSchema

    assert cls is GeminiRecursiveSchema


def test_second_access_to_same_name_is_silent() -> None:
    _resolve_from_shim("GeminiRecursiveSchema")
    _, warned = _resolve_from_shim("GeminiRecursiveSchema")
    assert warned == [], f"expected zero additional warnings on second access; got {warned!r}"


def test_per_name_caching_not_global_once() -> None:
    _resolve_from_shim("GeminiRecursiveSchema")
    _, warned = _resolve_from_shim("RefResolvingDictMapHints")
    assert len(warned) == 1
    assert "tolokaforge_models.policies.inkling.RefResolvingDictMapHints" in str(warned[0].message)


@pytest.mark.parametrize(("name", "module_path"), sorted(_MOVED_SUBCLASSES.items()))
def test_every_moved_name_resolves_to_class_at_new_path(name: str, module_path: str) -> None:
    cls, warned = _resolve_from_shim(name)
    assert len(warned) == 1
    target_module = importlib.import_module(module_path)
    assert cls is getattr(target_module, name)


def test_unknown_name_raises_attribute_error() -> None:
    llm = importlib.import_module("tolokaforge.core.llm")
    with pytest.raises(AttributeError, match="NoSuchThing"):
        _ = llm.NoSuchThing  # type: ignore[attr-defined]
