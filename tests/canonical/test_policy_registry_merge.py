"""Lock the engine ↔ ``tolokaforge-models`` policy-registry merge contract.

The engine's ``_POLICY_REGISTRIES`` initialises with its built-in defaults
only; ``tolokaforge.core.llm.presets`` then calls
:func:`tolokaforge.core.model_data.load_policy_registrations` and merges the
resolved entry-point classes onto each slot's registry. This test locks:

1. Every entry-point declaration in the ``tolokaforge.policies`` group
   resolves to the class at the declared module path AND lands in the
   corresponding slot of ``_POLICY_REGISTRIES`` at engine import time.
2. Bare-name lookups (``_SCHEMA_SANITIZERS["gemini_recursive"]``) return
   the models-wheel class object.
3. An unknown slot on a hypothetical registration raises ``RuntimeError``
   before any silent no-op can land — verified by driving the merge helper
   with a synthetic registration.
4. A collision on an existing engine class raises ``RuntimeError`` naming
   both classes — verified likewise.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import Any

import pytest

from tolokaforge.core.llm.presets import _POLICY_REGISTRIES

pytestmark = pytest.mark.canonical


#: The (slot, policy_name, module_path, class_name) tuples the models wheel
#: registers under the ``tolokaforge.policies`` group. Kept as a literal here
#: (rather than parsed from the metadata) so any drift between the metadata
#: and the engine's expected surface fails loud.
EXPECTED_REGISTRATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("schema_sanitizer", "gemini", "tolokaforge_models.policies.gemini", "GeminiSchema"),
    (
        "schema_sanitizer",
        "gemini_recursive",
        "tolokaforge_models.policies.gemini",
        "GeminiRecursiveSchema",
    ),
    (
        "prompt_policy",
        "dict_map_hints_ref",
        "tolokaforge_models.policies.inkling",
        "RefResolvingDictMapHints",
    ),
    (
        "response_policy",
        "scalar_array_dict_map",
        "tolokaforge_models.policies.gemini",
        "ScalarArrayDictMapResponse",
    ),
    (
        "response_policy",
        "minimax_m3_tags",
        "tolokaforge_models.policies.minimax",
        "MinimaxM3TagRecoveryResponse",
    ),
    (
        "reasoning_codec",
        "openai_summary_replay",
        "tolokaforge_models.policies.deepseek",
        "OpenAISummaryReplayReasoningCodec",
    ),
)


@pytest.mark.parametrize(
    ("slot", "policy_name", "module_path", "class_name"),
    EXPECTED_REGISTRATIONS,
)
def test_registry_contains_models_wheel_class(
    slot: str, policy_name: str, module_path: str, class_name: str
) -> None:
    module = importlib.import_module(module_path)
    expected_cls = getattr(module, class_name)
    got = _POLICY_REGISTRIES[slot].get(policy_name)
    assert got is expected_cls, (
        f"engine registry {slot!r}.{policy_name!r} is {got!r}; "
        f"expected {expected_cls!r} from {module_path!r}"
    )


def test_entry_points_declare_the_expected_registrations() -> None:
    eps = importlib.metadata.entry_points(group="tolokaforge.policies")
    got: set[tuple[str, str, str, str]] = set()
    for ep in eps:
        slot, _, policy_name = ep.name.partition(".")
        module_path, _, class_name = ep.value.partition(":")
        got.add((slot, policy_name, module_path, class_name))
    assert got == set(EXPECTED_REGISTRATIONS), (
        f"entry-point group 'tolokaforge.policies' drifted from the "
        f"expected registrations. Got:\n  {sorted(got)}\nExpected:\n  "
        f"{sorted(EXPECTED_REGISTRATIONS)}"
    )


def test_unknown_slot_raises_at_merge() -> None:
    """A registration in an unknown slot must fail loud, not silent-skip."""
    from tolokaforge.core.llm import presets

    class _Fake:
        pass

    registries = {"schema_sanitizer": {}}
    with pytest.raises(RuntimeError, match="unknown slot"):
        _run_merge(presets, registries, {"nonexistent_slot": {"x": _Fake}})


def test_duplicate_shadowing_engine_class_raises() -> None:
    """A models-wheel registration that shadows an engine class fails loud."""
    from tolokaforge.core.llm import presets

    class _NotStrictSchema:
        pass

    # Seed a registry with the existing engine class and try to overwrite it.
    engine_cls = _POLICY_REGISTRIES["schema_sanitizer"]["strict"]
    registries = {"schema_sanitizer": {"strict": engine_cls}}
    with pytest.raises(RuntimeError, match="shadows"):
        _run_merge(presets, registries, {"schema_sanitizer": {"strict": _NotStrictSchema}})


def test_idempotent_re_registration_of_same_class_is_a_noop() -> None:
    """Re-registering the same class object at the same slot/name is silent."""
    from tolokaforge.core.llm import presets

    engine_cls = _POLICY_REGISTRIES["schema_sanitizer"]["strict"]
    registries = {"schema_sanitizer": {"strict": engine_cls}}
    # Same identity → no shadow, no raise.
    _run_merge(presets, registries, {"schema_sanitizer": {"strict": engine_cls}})
    assert registries["schema_sanitizer"]["strict"] is engine_cls


def _run_merge(
    presets_module: Any,
    registries: dict[str, dict[str, type]],
    to_merge: dict[str, dict[str, type]],
) -> None:
    """Drive the private merge helper against a scratch registry snapshot."""
    original = presets_module._POLICY_REGISTRIES
    presets_module._POLICY_REGISTRIES = registries
    original_loader = presets_module.load_policy_registrations
    presets_module.load_policy_registrations = lambda: to_merge
    try:
        presets_module._merge_out_of_tree_policy_registrations()
    finally:
        presets_module._POLICY_REGISTRIES = original
        presets_module.load_policy_registrations = original_loader
