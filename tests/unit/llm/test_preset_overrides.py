"""Unit tests for :func:`_apply_config_overrides`.

The AGENTS.md rule #1 says "Surface failures explicitly". A typo in a run
config's ``capabilities:`` block used to silently no-op — these tests pin
the new contract: unknown keys raise :class:`ValueError` with a message
that (a) names every offending key, (b) names the recognised keys, and
(c) points the reader at the contract doc.

The belt-and-braces test ``test_recognised_keys_are_the_documented_set``
additionally prevents the set / body from drifting apart: every key in
:data:`_RECOGNISED_OVERRIDE_KEYS` must appear as a literal inside the
function body, so a future contributor cannot add a key to the allowlist
without also teaching the function how to translate it.
"""

from __future__ import annotations

import inspect

import pytest

from tolokaforge.core.llm.presets import (
    _RECOGNISED_OVERRIDE_KEYS,
    _apply_config_overrides,
)

pytestmark = pytest.mark.unit


class TestApplyConfigOverridesRejectsUnknown:
    def test_unknown_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown capability override keys"):
            _apply_config_overrides({}, {"some_typo": True})

    def test_error_lists_offending_keys(self) -> None:
        # Sorted, so "alpha" comes before "bravo" regardless of dict order.
        with pytest.raises(ValueError, match=r"\['alpha', 'bravo'\]"):
            _apply_config_overrides({}, {"bravo": 1, "alpha": 2})

    def test_error_points_at_contract_doc(self) -> None:
        with pytest.raises(ValueError, match=r"docs/CONFIG\.md § ModelConfig\.capabilities"):
            _apply_config_overrides({}, {"nope": 1})

    def test_recognised_keys_do_not_raise(self) -> None:
        # ``supports_seed`` is in the set; ensure no ValueError.
        _apply_config_overrides({}, {"supports_seed": True})

    def test_empty_overrides_do_not_raise(self) -> None:
        _apply_config_overrides({}, {})

    def test_mixed_known_and_unknown_still_raises(self) -> None:
        # Having one recognised key does NOT suppress the failure on the
        # offending one — the unknown key must still surface.
        with pytest.raises(ValueError, match=r"\['rogue_key'\]"):
            _apply_config_overrides({}, {"supports_seed": True, "rogue_key": "oops"})

    def test_recognised_keys_are_the_documented_set(self) -> None:
        """Every key in the allowlist must be referenced in the body.

        Pins the invariant that the set and the ``if "<key>" in overrides``
        branches cannot drift apart. A future contributor adding a key to
        the set without a translation branch hits this guard.
        """
        src = inspect.getsource(_apply_config_overrides)
        for key in _RECOGNISED_OVERRIDE_KEYS:
            assert f'"{key}"' in src or f"'{key}'" in src, (
                f"{key!r} is in _RECOGNISED_OVERRIDE_KEYS but not referenced "
                f"in _apply_config_overrides body"
            )
