"""Locks the module-level back-compat aliases ``RunnerTranscriptRulesConfig`` and
``RunnerRequiredAction`` on ``tolokaforge.runner.models``: they resolve to the
canonical ``TranscriptRulesConfig`` / ``RequiredAction``, emit exactly one
``DeprecationWarning`` per (message, caller-module) citing the retirement
tracker (#1304), and stay invisible to ``vars()`` / ``dir()``.

Locks:

- Canonical names (``TranscriptRulesConfig``, ``RequiredAction``) resolve
  without emitting any ``DeprecationWarning``.
- The deprecated aliases (``RunnerTranscriptRulesConfig``,
  ``RunnerRequiredAction``) resolve to the *same* class objects and emit
  exactly one ``DeprecationWarning`` naming both the legacy and canonical
  spelling and the ``(tracked in #1304)`` retirement suffix.
- The aliases stay invisible to ``vars()`` / ``dir()`` after resolution —
  the reconcile canonical walks ``vars(module).items()`` and must not
  observe a shadow of a canonical class.
- Genuine typos still raise ``AttributeError`` naming the module.
- A payload built through the alias classes round-trips through the
  canonical class equivalently.
"""

from __future__ import annotations

import warnings

import pytest
from pydantic import BaseModel

from tolokaforge.runner import models as runner_models
from tolokaforge.runner.models import RequiredAction, TranscriptRulesConfig

pytestmark = pytest.mark.unit


def test_canonical_names_import_without_deprecation_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from tolokaforge.runner.models import (  # noqa: F401
            RequiredAction as _RequiredAction,
        )
        from tolokaforge.runner.models import (  # noqa: F401
            TranscriptRulesConfig as _TranscriptRulesConfig,
        )
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    messages = [str(w.message) for w in deprecations]
    assert deprecations == [], f"Canonical imports emitted DeprecationWarning(s): {messages}"


def test_runner_transcript_rules_config_alias_returns_canonical_class() -> None:
    with pytest.warns(
        DeprecationWarning,
        match=r"RunnerTranscriptRulesConfig .*deprecated.*TranscriptRulesConfig.*"
        r"\(tracked in #1304\)",
    ):
        alias = runner_models.RunnerTranscriptRulesConfig
    assert alias is TranscriptRulesConfig
    assert alias.__name__ == "TranscriptRulesConfig"
    assert issubclass(alias, BaseModel)


def test_runner_required_action_alias_returns_canonical_class() -> None:
    with pytest.warns(
        DeprecationWarning,
        match=r"RunnerRequiredAction .*deprecated.*RequiredAction.*" r"\(tracked in #1304\)",
    ):
        alias = runner_models.RunnerRequiredAction
    assert alias is RequiredAction
    assert alias.__name__ == "RequiredAction"
    assert issubclass(alias, BaseModel)


def test_aliases_do_not_leak_into_module_vars_or_dir() -> None:
    """Invisibility invariant — guards a caching regression.

    If the resolver ever writes into ``globals()``, the alias would appear
    in ``vars(module)`` and be picked up by the reconcile canonical's
    ``_basemodel_names`` walker, re-creating a duplicate-name collision the
    reconcile check bans. Resolves both aliases first so a caching
    implementation would have taken effect before the assertions.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _ = runner_models.RunnerTranscriptRulesConfig
        _ = runner_models.RunnerRequiredAction
    module_vars = vars(runner_models)
    assert "RunnerTranscriptRulesConfig" not in module_vars
    assert "RunnerRequiredAction" not in module_vars
    module_dir = dir(runner_models)
    assert "RunnerTranscriptRulesConfig" not in module_dir
    assert "RunnerRequiredAction" not in module_dir


def test_unknown_attribute_still_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match=r"tolokaforge\.runner\.models.*NotAThing"):
        _ = runner_models.NotAThing


def test_alias_built_payload_round_trips_through_canonical_class() -> None:
    """A payload constructed via the alias classes validates identically
    through the canonical class, closing the loop that class identity is
    the whole of the alias contract — no accidental subclass, no shadow
    validator, no divergent JSON shape.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        alias_action_cls = runner_models.RunnerRequiredAction
        alias_rules_cls = runner_models.RunnerTranscriptRulesConfig
    alias_action = alias_action_cls(
        action_id="a1",
        requestor="assistant",
        name="submit",
        arguments={"reason": "done"},
    )
    alias_rules = alias_rules_cls(required_actions=[alias_action])
    canonical_action = RequiredAction(
        action_id="a1",
        requestor="assistant",
        name="submit",
        arguments={"reason": "done"},
    )
    canonical_rules = TranscriptRulesConfig(required_actions=[canonical_action])
    assert alias_rules == canonical_rules
    round_tripped = TranscriptRulesConfig.model_validate_json(alias_rules.model_dump_json())
    assert round_tripped == canonical_rules
