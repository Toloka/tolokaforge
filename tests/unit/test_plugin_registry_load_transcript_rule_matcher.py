"""``plugin_registry.load_transcript_rule_matcher`` — fail-loud resolution.

Locks the loader for the ``tolokaforge.transcript_rule_matchers`` entry-point
group:

* the shipped factory (``default``) resolves to the callable
  ``pyproject.toml`` registers it against, so a future refactor renaming
  the symbol trips this test before it lands;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_grading_substrate``
  and the other loaders use.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.default_transcript_rule_matcher import (
    DefaultTranscriptRuleMatcher,
    _default_transcript_rule_matcher_factory,
)
from tolokaforge.core.plugin_registry import (
    TRANSCRIPT_RULE_MATCHERS_GROUP,
    UnknownImplementationError,
    available_transcript_rule_matchers,
    load_transcript_rule_matcher,
)

pytestmark = pytest.mark.unit


def test_default_resolves_to_the_shipped_factory() -> None:
    assert load_transcript_rule_matcher("default") is _default_transcript_rule_matcher_factory


def test_default_factory_returns_a_default_transcript_rule_matcher_instance() -> None:
    """Locks the two-step "loader -> factory() -> instance" chain end-to-end."""
    factory = load_transcript_rule_matcher("default")
    assert isinstance(factory(), DefaultTranscriptRuleMatcher)


def test_available_lists_the_shipped_name() -> None:
    assert "default" in available_transcript_rule_matchers()


def test_unknown_name_raises_unknown_implementation_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_transcript_rule_matcher("nonexistent")
    message = str(excinfo.value)
    assert TRANSCRIPT_RULE_MATCHERS_GROUP in message
    assert "default" in message
    assert excinfo.value.group == TRANSCRIPT_RULE_MATCHERS_GROUP
    assert "default" in excinfo.value.known
