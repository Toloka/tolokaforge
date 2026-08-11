"""The three properties the episode-unique-id rule states about itself.

Both the runtime and the grading path derive the same key from the same rule, so
what the rule guarantees is what each of them may assume: that a provider minting
unique ids is left alone, that deriving twice changes nothing, and that the output
is unique even against a provider that emits the disambiguated shape itself.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds, episode_unique_call_ids

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "raw_ids",
    [
        pytest.param((), id="no_calls"),
        pytest.param(("toolu_01",), id="one_call"),
        pytest.param(("call_a", "call_b", "call_c"), id="openai_style_unique_ids"),
        pytest.param(("get_order:0", "get_order:1"), id="positional_ids_that_do_not_collide"),
    ],
)
def test_a_repeat_free_sequence_comes_back_byte_for_byte(raw_ids: tuple[str, ...]) -> None:
    """The no-movement guarantee: every provider that already mints unique ids is
    untouched, so no recorded bundle and no fixture changes meaning."""
    assert episode_unique_call_ids(raw_ids) == raw_ids


def test_a_repeated_id_is_disambiguated_by_its_occurrence_and_only_after_the_first() -> None:
    assert episode_unique_call_ids(("x", "y", "x", "x")) == ("x", "y", "x#2", "x#3")


def test_deriving_twice_is_deriving_once() -> None:
    """Idempotence is what lets a caller derive without knowing whether an upstream
    caller already did — the runtime assigns, and the grading path derives again."""
    raw_ids = ("get_employee:1", "list_cases:2", "get_employee:1", "get_employee:1")

    once = episode_unique_call_ids(raw_ids)

    assert episode_unique_call_ids(once) == once


def test_a_provider_emitting_the_disambiguated_shape_still_leaves_every_call_a_key() -> None:
    """``#2`` is a string a provider may mint itself, so the derived key is checked
    against the keys already handed out rather than assumed free."""
    keys = episode_unique_call_ids(("x", "x#2", "x", "x#2"))

    assert keys == ("x", "x#2", "x#3", "x#2#2")
    assert len(set(keys)) == 4


def test_the_assigner_and_the_batch_function_compute_the_same_thing() -> None:
    """One rule in two shapes: the streaming caller and the batch caller must not be
    able to disagree about what the k-th occurrence of an id is called."""
    raw_ids = ("a", "b", "a", "a#2", "a", "b")
    assigner = EpisodeUniqueCallIds()

    assert tuple(assigner.assign(raw_id) for raw_id in raw_ids) == episode_unique_call_ids(raw_ids)


def test_two_assigners_do_not_share_an_episode() -> None:
    """The assigner is trial-scoped: a second episode's first call carries the
    provider's own id, not a key disambiguated against the first episode's."""
    first = EpisodeUniqueCallIds()
    first.assign("x")

    assert EpisodeUniqueCallIds().assign("x") == "x"
