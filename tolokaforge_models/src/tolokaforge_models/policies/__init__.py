"""Per-model policy subclasses registered with the engine.

Each module under this package hosts the policy subclasses for one model
family; the classes are registered with the engine's policy registries via
the ``tolokaforge.policies`` entry-point group declared in
``tolokaforge_models/pyproject.toml``. The engine's
:func:`tolokaforge.core.model_data.load_policy_registrations` reads those
entries at :mod:`tolokaforge.core.llm.presets` import time and merges the
classes onto ``_POLICY_REGISTRIES``.

Out-of-tree code that wants the classes directly imports them from the
family module (``from tolokaforge_models.policies.gemini import
GeminiSchema``) or from this package (``from tolokaforge_models.policies
import GeminiSchema``).
"""

from __future__ import annotations

from tolokaforge_models.policies.deepseek import OpenAISummaryReplayReasoningCodec
from tolokaforge_models.policies.gemini import (
    GeminiRecursiveSchema,
    GeminiSchema,
    ScalarArrayDictMapResponse,
)
from tolokaforge_models.policies.inkling import RefResolvingDictMapHints
from tolokaforge_models.policies.minimax import (
    ItemRecursiveUnwrapResponse,
    JsonRecursiveCoerceResponse,
    MinimaxM3TagRecoveryResponse,
)

__all__ = [
    "GeminiRecursiveSchema",
    "GeminiSchema",
    "ItemRecursiveUnwrapResponse",
    "JsonRecursiveCoerceResponse",
    "MinimaxM3TagRecoveryResponse",
    "OpenAISummaryReplayReasoningCodec",
    "RefResolvingDictMapHints",
    "ScalarArrayDictMapResponse",
]
