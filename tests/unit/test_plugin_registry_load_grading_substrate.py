"""``plugin_registry.load_grading_substrate`` — fail-loud resolution.

Locks the loader ADR-0040 introduces for the ``tolokaforge.grading_substrates``
entry-point group:

* the two shipped substrates (``in_process``, ``live_callback``) resolve to
  the classes ``pyproject.toml`` registers them against, so a future refactor
  renaming either symbol trips this test before it lands;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_trial_grader``
  and the other loaders use, so third-party graders discovering they've
  mistyped a name see a message that names the alternatives.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.grading.substrate_live import LiveRunnerCallbackGradingSubstrate
from tolokaforge.core.plugin_registry import (
    GRADING_SUBSTRATES_GROUP,
    UnknownImplementationError,
    available_grading_substrates,
    load_grading_substrate,
)

pytestmark = pytest.mark.unit


def test_in_process_resolves_to_the_shipped_class() -> None:
    assert load_grading_substrate("in_process") is InProcessGradingSubstrate


def test_live_callback_resolves_to_the_shipped_class() -> None:
    assert load_grading_substrate("live_callback") is LiveRunnerCallbackGradingSubstrate


def test_available_lists_both_shipped_names() -> None:
    """The two shipped substrates appear in the group listing.

    Sorted, so their order is deterministic; a third-party substrate landing
    an entry-point extends the list without changing this test — the shipped
    two are what tolokaforge itself carries, and their presence is what the
    ADR-0040 default depends on.
    """
    names = available_grading_substrates()
    assert "in_process" in names
    assert "live_callback" in names


def test_unknown_name_raises_unknown_implementation_error() -> None:
    """Unknown names raise the same error class ``load_trial_grader`` uses.

    The message names the group and lists every known name so a caller
    catching the exception can surface a typo diagnosis without importing
    the registry internals.
    """
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_grading_substrate("nonexistent")
    message = str(excinfo.value)
    assert GRADING_SUBSTRATES_GROUP in message
    assert "in_process" in message
    assert "live_callback" in message
    assert excinfo.value.group == GRADING_SUBSTRATES_GROUP
    assert "in_process" in excinfo.value.known
    assert "live_callback" in excinfo.value.known
