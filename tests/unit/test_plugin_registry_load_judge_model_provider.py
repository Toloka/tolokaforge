"""``plugin_registry.load_judge_model_provider`` — fail-loud resolution.

Locks the loader for the ``tolokaforge.judge_model_providers`` entry-point
group:

* the shipped factory (``litellm``) resolves to the callable
  ``pyproject.toml`` registers it against, so a future refactor renaming
  the symbol trips this test before it lands;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_grading_substrate``
  and the other loaders use.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.default_judge_model_provider import (
    LiteLLMJudgeModelProvider,
    _litellm_judge_model_provider_factory,
)
from tolokaforge.core.plugin_registry import (
    JUDGE_MODEL_PROVIDERS_GROUP,
    UnknownImplementationError,
    available_judge_model_providers,
    load_judge_model_provider,
)

pytestmark = pytest.mark.unit


def test_litellm_resolves_to_the_shipped_factory() -> None:
    assert load_judge_model_provider("litellm") is _litellm_judge_model_provider_factory


def test_litellm_factory_returns_a_litellm_judge_model_provider_instance() -> None:
    """Locks the two-step "loader → factory() → instance" chain end-to-end."""
    factory = load_judge_model_provider("litellm")
    assert isinstance(factory(), LiteLLMJudgeModelProvider)


def test_available_lists_the_shipped_name() -> None:
    assert "litellm" in available_judge_model_providers()


def test_unknown_name_raises_unknown_implementation_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_judge_model_provider("nonexistent")
    message = str(excinfo.value)
    assert JUDGE_MODEL_PROVIDERS_GROUP in message
    assert "litellm" in message
    assert excinfo.value.group == JUDGE_MODEL_PROVIDERS_GROUP
    assert "litellm" in excinfo.value.known
