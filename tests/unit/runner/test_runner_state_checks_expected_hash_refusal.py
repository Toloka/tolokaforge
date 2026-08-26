"""Locks the wire-layer refusal of ``expected_hash`` on ``RunnerStateChecksConfig``.

The retired key ``expected_hash`` used to carry a stored digest; the current wire
schema replaces it with ``expect_initial_state: bool`` (a comparison-basis
selector), a different concept on the same name. A caller that even declares the
key is speaking the retired schema, so ``RunnerStateChecksConfig`` refuses it at
the wire layer with a message that names the retirement, the two current sources
(``golden_actions``, ``expect_initial_state``), and the retirement tracker
(#1304). The generic Pydantic ``extra_forbidden`` message it replaces does not
appear.

Locks:

- Presence of ``expected_hash`` under any value — a populated digest string, a
  bool, ``None`` — fires the same actionable refusal, with a single
  ``value_error`` naming the retirement, the two migration targets and #1304.
- The refusal message does not carry Pydantic's generic ``Extra inputs are not
  permitted`` / ``extra_forbidden`` string — the specific migration message wins.
- ``expect_initial_state=True`` and default construction validate and round-trip
  — the field the retirement points at is unchanged.
- An unrelated unknown key still raises the pre-existing generic
  ``extra_forbidden`` (the migration path is scoped, not leaking).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.runner.models import RunnerStateChecksConfig

pytestmark = pytest.mark.unit

_REQUIRED_SUBSTRINGS: tuple[str, ...] = (
    "expected_hash",
    "golden_actions",
    "expect_initial_state",
    "#1304",
)
_RETIREMENT_SYNONYMS: tuple[str, ...] = ("retired", "deleted", "removed")


def _assert_actionable_refusal(exc: ValidationError) -> None:
    errors = exc.errors()
    assert len(errors) == 1, f"expected single error, got {errors!r}"
    (error,) = errors
    assert error["type"] == "value_error", error
    msg = error["msg"]
    for substring in _REQUIRED_SUBSTRINGS:
        assert substring in msg, f"{substring!r} missing from refusal message: {msg!r}"
    matched = [word for word in _RETIREMENT_SYNONYMS if word in msg]
    assert matched, f"none of {_RETIREMENT_SYNONYMS!r} appear in refusal message: {msg!r}"


def test_expected_hash_digest_string_raises_actionable_refusal() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RunnerStateChecksConfig(expected_hash="deadbeef" * 8)
    _assert_actionable_refusal(exc_info.value)


@pytest.mark.parametrize("value", [True, False, None])
def test_expected_hash_key_presence_under_any_value_refuses(value: object) -> None:
    with pytest.raises(ValidationError) as exc_info:
        RunnerStateChecksConfig(expected_hash=value)
    _assert_actionable_refusal(exc_info.value)


def test_refusal_message_does_not_leak_generic_extra_forbidden() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RunnerStateChecksConfig(expected_hash="deadbeef")
    (error,) = exc_info.value.errors()
    msg = error["msg"]
    assert "Extra inputs are not permitted" not in msg, msg
    assert "extra_forbidden" not in msg, msg


def test_expect_initial_state_true_validates_and_round_trips() -> None:
    config = RunnerStateChecksConfig(expect_initial_state=True)
    assert config.expect_initial_state is True


def test_default_construction_validates_and_round_trips() -> None:
    config = RunnerStateChecksConfig()
    assert config.expect_initial_state is False
    assert config.hash_enabled is False


def test_unrelated_unknown_key_still_raises_generic_extra_forbidden() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RunnerStateChecksConfig(hash_weightz=0.5)
    (error,) = exc_info.value.errors()
    assert error["type"] == "extra_forbidden", error
    assert "#1304" not in error["msg"], error
