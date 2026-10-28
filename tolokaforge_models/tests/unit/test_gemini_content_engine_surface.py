"""``GeminiContent`` refuses to load on an engine that cannot honour it.

``minimum_engine_version`` is raised only in the engine release PR
(``docs/RELEASING.md``), so there is a window in which this wheel can land on
an engine whose content policies predate ``wraps_part_reference_tool_results``.
There the registration would succeed and nothing would wrap. The module-level
check in :mod:`tolokaforge_models.policies.gemini` closes that window.
"""

from __future__ import annotations

import pytest
from tolokaforge_models.policies import gemini

from tolokaforge.core.llm.content_policy import OpenAIContent

pytestmark = pytest.mark.unit


def test_the_check_passes_on_an_engine_with_the_wrap_surface() -> None:
    gemini._require_engine_wrap_surface()


def test_an_engine_without_the_wrap_surface_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(OpenAIContent, "wraps_part_reference_tool_results")
    with pytest.raises(RuntimeError, match="wraps_part_reference_tool_results") as info:
        gemini._require_engine_wrap_surface()
    assert "Upgrade tolokaforge" in str(info.value)
