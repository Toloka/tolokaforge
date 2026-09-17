"""Cohere Command-A+ ``<|START_TEXT|>...<|END_TEXT|>`` marker stripping.

The Command-A+ route wraps every reply in its chat-template markers (observed
2026-08-01). :class:`CohereMarkerAssistantText` is the ``assistant_text_policy``
bound by both Cohere presets. The gateway serving the route strips the markers
itself since 2026-09-08, so the class must be a strict no-op on clean text and a
faithful stripper when the markers are present.

Contract pinned here:

1. Clean text passes through byte-identical (binding the policy never alters a
   reply that carries no markers), including the empty string.
2. One marker pair yields the inner text, whitespace-trimmed.
3. Several pairs keep every region, joined by a single space; template residue
   outside the pairs is dropped.
4. A stray unpaired marker (truncated reply) is removed and the prose is kept.
5. Both Cohere presets resolve to this class; an unrelated preset does not.
"""

from __future__ import annotations

import pytest
from tolokaforge_models.policies.cohere import CohereMarkerAssistantText

from tolokaforge.core.llm.assistant_text_policy import (
    AssistantTextPolicy,
    PassthroughAssistantText,
)
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.models.model_config import ModelConfig

pytestmark = pytest.mark.unit

_COHERE = ModelConfig(provider="openai", name="azure_ai/cohere-command-a-plus-05-2026")


def _strip(text: str) -> str:
    return CohereMarkerAssistantText().parse_assistant_text(text, model_config=_COHERE)


def test_is_an_assistant_text_policy() -> None:
    assert isinstance(CohereMarkerAssistantText(), AssistantTextPolicy)


@pytest.mark.parametrize(
    "text",
    ["", "pong", "two lines\nof plain prose", "<|not_a_marker|> stays", "  spaced  "],
)
def test_text_without_markers_is_returned_unchanged(text: str) -> None:
    assert _strip(text) == text


def test_one_pair_yields_the_inner_text() -> None:
    assert _strip("<|START_TEXT|>pong<|END_TEXT|>") == "pong"
    assert (
        _strip("<|START_TEXT|>\n  Order 42 is confirmed.\n<|END_TEXT|>") == "Order 42 is confirmed."
    )


def test_several_pairs_keep_every_region_and_drop_residue() -> None:
    text = "<|START_TEXT|>first<|END_TEXT|><|END_OF_TURN|><|START_TEXT|>second<|END_TEXT|>"
    assert _strip(text) == "first second"


def test_a_stray_marker_is_removed_and_prose_kept() -> None:
    assert _strip("<|START_TEXT|>The refund was") == "The refund was"
    assert _strip("issued today.<|END_TEXT|>") == "issued today."
    assert _strip("<|START_TEXT|><|END_TEXT|>") == ""


@pytest.mark.parametrize(
    "name",
    [
        "azure_ai/cohere-command-a-plus-05-2026",
        "azure_ai/cohere-command-a-plus",
    ],
)
def test_cohere_presets_bind_the_marker_policy(name: str) -> None:
    caps = build_capabilities(name, "openai")
    assert isinstance(caps.assistant_text_policy, CohereMarkerAssistantText)


def test_an_unrelated_preset_stays_passthrough() -> None:
    caps = build_capabilities("openai/gpt-5.6-sol", "openrouter")
    assert isinstance(caps.assistant_text_policy, PassthroughAssistantText)
