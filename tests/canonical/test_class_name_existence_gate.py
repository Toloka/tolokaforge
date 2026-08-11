"""Lock the class-name-existence install-time gate.

:func:`tolokaforge.core.llm.presets._check_class_names_resolve` walks the
bundled ``model_presets.yaml`` and refuses to boot on any policy name
that neither the engine defaults nor the ``tolokaforge-models``
entry-point registrations resolve. The gate:

* Names the offending preset / provider block, the slot, the missing
  policy name, and a closest-match suggestion drawn from the live
  registry keyset.
* Fires at :mod:`tolokaforge.core.llm.presets` import time — before any
  ``RunConfig`` load.
* Passes silently on the shipped bundled preset table, since the merged
  ``_POLICY_REGISTRIES`` already contains every referenced class.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm import presets

pytestmark = pytest.mark.canonical


def test_bundled_presets_pass_the_gate_at_import_time() -> None:
    """Import-time gate has already fired (module is loaded); rerunning the
    check against the real bundled + merged state must stay silent."""
    presets._check_class_names_resolve()


def test_unresolved_class_in_preset_raises_naming_all_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bogus_bundle: dict[str, Any] = {
        "default": {},
        "presets": {
            "test_bogus_preset": {
                "match": ["bogus/*"],
                "schema_sanitizer": "NonExistentSchema",
            }
        },
        "providers": {},
    }
    monkeypatch.setattr(presets, "_load_bundled_presets", lambda: bogus_bundle)

    with pytest.raises(RuntimeError) as excinfo:
        presets._check_class_names_resolve()

    message = str(excinfo.value)
    assert "presets.test_bogus_preset" in message, message
    assert "schema_sanitizer" in message, message
    assert "'NonExistentSchema'" in message, message


def test_close_match_suggestion_appears_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo close to a registered name yields a ``did you mean`` hint."""
    bogus_bundle: dict[str, Any] = {
        "default": {},
        "presets": {
            "test_typo_preset": {
                "match": ["typo/*"],
                "schema_sanitizer": "passthrouhg",
            }
        },
        "providers": {},
    }
    monkeypatch.setattr(presets, "_load_bundled_presets", lambda: bogus_bundle)

    with pytest.raises(RuntimeError) as excinfo:
        presets._check_class_names_resolve()

    message = str(excinfo.value)
    assert "did you mean" in message, message
    assert "passthrough" in message, message


def test_registered_class_in_synthetic_preset_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthetic preset that references a real registered class passes."""
    good_bundle: dict[str, Any] = {
        "default": {"schema_sanitizer": "passthrough"},
        "presets": {
            "test_good_preset": {
                "match": ["good/*"],
                "schema_sanitizer": "strict",
                "response_policy": {"name": "standard", "params": {}},
            }
        },
        "providers": {
            "test_provider": {"reasoning_codec": "openai"},
        },
    }
    monkeypatch.setattr(presets, "_load_bundled_presets", lambda: good_bundle)

    presets._check_class_names_resolve()


def test_unresolved_class_in_default_block_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bogus_bundle: dict[str, Any] = {
        "default": {"schema_sanitizer": "ghost"},
        "presets": {},
        "providers": {},
    }
    monkeypatch.setattr(presets, "_load_bundled_presets", lambda: bogus_bundle)

    with pytest.raises(RuntimeError) as excinfo:
        presets._check_class_names_resolve()

    assert "default" in str(excinfo.value)


def test_unresolved_class_in_provider_block_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bogus_bundle: dict[str, Any] = {
        "default": {},
        "presets": {},
        "providers": {"phantom_provider": {"response_policy": "gremlin"}},
    }
    monkeypatch.setattr(presets, "_load_bundled_presets", lambda: bogus_bundle)

    with pytest.raises(RuntimeError) as excinfo:
        presets._check_class_names_resolve()

    assert "providers.phantom_provider" in str(excinfo.value)


def test_dict_slot_value_with_bogus_name_key_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``{name, params}`` mapping with an unregistered ``name`` fails loud."""
    bogus_bundle: dict[str, Any] = {
        "default": {},
        "presets": {
            "test_dict_shape": {
                "match": ["dict/*"],
                "reasoning_codec": {"name": "PhantomCodec", "params": {}},
            }
        },
        "providers": {},
    }
    monkeypatch.setattr(presets, "_load_bundled_presets", lambda: bogus_bundle)

    with pytest.raises(RuntimeError) as excinfo:
        presets._check_class_names_resolve()

    message = str(excinfo.value)
    assert "reasoning_codec" in message, message
    assert "'PhantomCodec'" in message, message
