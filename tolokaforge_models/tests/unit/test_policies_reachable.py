"""Every moved policy subclass is importable and constructible.

Locks the "moves preserved the constructor surface" invariant: each of the
eight subclasses that migrated from :mod:`tolokaforge.core.llm.*` to
:mod:`tolokaforge_models.policies` can be imported from its new module path
and instantiated with its documented ``__init__`` signature (all eight take
no required arguments).
"""

from __future__ import annotations

import importlib
from typing import Final

import pytest

pytestmark = pytest.mark.unit


MOVED_CLASSES: Final[tuple[tuple[str, str], ...]] = (
    ("tolokaforge_models.policies.gemini", "GeminiSchema"),
    ("tolokaforge_models.policies.gemini", "GeminiRecursiveSchema"),
    ("tolokaforge_models.policies.gemini", "ScalarArrayDictMapResponse"),
    ("tolokaforge_models.policies.inkling", "RefResolvingDictMapHints"),
    ("tolokaforge_models.policies.minimax", "JsonRecursiveCoerceResponse"),
    ("tolokaforge_models.policies.minimax", "ItemRecursiveUnwrapResponse"),
    ("tolokaforge_models.policies.minimax", "MinimaxM3TagRecoveryResponse"),
    ("tolokaforge_models.policies.deepseek", "OpenAISummaryReplayReasoningCodec"),
)


@pytest.mark.parametrize(("module_path", "class_name"), MOVED_CLASSES)
def test_moved_class_is_importable_and_instantiable(module_path: str, class_name: str) -> None:
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    assert isinstance(cls, type), f"{module_path}.{class_name} is not a class"
    instance = cls()
    assert instance is not None


def test_policies_package_reexports_every_moved_class() -> None:
    """``from tolokaforge_models.policies import GeminiSchema`` is the alternate
    surface for out-of-tree code that wants a single import site."""
    package = importlib.import_module("tolokaforge_models.policies")
    for _module_path, class_name in MOVED_CLASSES:
        msg = f"tolokaforge_models.policies missing re-export {class_name!r}"
        assert hasattr(package, class_name), msg
