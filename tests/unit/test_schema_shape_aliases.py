"""Unit tests for the schema-shape deprecation aliases in
``tolokaforge.core.deprecations``.

Schema-shape aliases rename or relocate a YAML *key* at the parse
boundary: uppercase ``network_policy`` enum names lowercase to the
canonical values, ``SecurityContext`` accepts ``user``/``group`` for
``run_as_user``/``run_as_group``, ``evaluation.task_packs`` lifts into
``projects``, flat ``compose_file``/``runner_service`` fold under
``stack``, and a run config under ``run_config/`` (singular) warns. Each
accepts the legacy shape with a ``DeprecationWarning`` and normalises to
the canonical field; the canonical shape warns none.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from tolokaforge.core.deprecations import (
    coerce_flat_stack_fields,
    coerce_task_packs_alias,
    warn_legacy_run_config_dir,
)
from tolokaforge.runner.models import EnvironmentPatch, NetworkPolicy, SecurityContext

pytestmark = pytest.mark.unit


class TestNetworkPolicyCaseAlias:
    def test_uppercase_lowercases_and_warns(self) -> None:
        with pytest.warns(DeprecationWarning, match="network_policy: NO_INTERNET"):
            patch = EnvironmentPatch(network_policy="NO_INTERNET")
        assert patch.network_policy is NetworkPolicy.NO_INTERNET

    def test_lowercase_canonical_emits_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            patch = EnvironmentPatch(network_policy="no_internet")
        assert patch.network_policy is NetworkPolicy.NO_INTERNET
        assert not any(
            issubclass(w.category, DeprecationWarning) and "network_policy" in str(w.message)
            for w in caught
        )


class TestSecurityContextAliases:
    def test_legacy_user_group_rename_and_warn(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ctx = SecurityContext(user=1000, group=1000)
        assert ctx.run_as_user == 1000
        assert ctx.run_as_group == 1000
        messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
        assert any("SecurityContext.user" in m for m in messages)
        assert any("SecurityContext.group" in m for m in messages)

    def test_canonical_run_as_fields_emit_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ctx = SecurityContext(run_as_user=1000, run_as_group=1000)
        assert ctx.run_as_user == 1000
        assert ctx.run_as_group == 1000
        assert not any(
            issubclass(w.category, DeprecationWarning) and "SecurityContext" in str(w.message)
            for w in caught
        )

    def test_legacy_and_canonical_disagree_fails_loud(self) -> None:
        with pytest.raises(
            (ValueError, ValidationError),
            match="legacy 'user'=1000 conflicts with 'run_as_user'=1001",
        ):
            SecurityContext(user=1000, run_as_user=1001)


class TestRelocatedAliasesUnchanged:
    """The three aliases moved into ``deprecations.py`` keep their exact
    messages so downstream consumers pinning them do not regress."""

    def test_task_packs_alias_message_unchanged(self) -> None:
        with pytest.warns(DeprecationWarning, match="task_packs is deprecated"):
            out = coerce_task_packs_alias({"task_packs": ["/tmp/pack_a"], "output_dir": "x"})
        assert out["projects"] == ["/tmp/pack_a"]
        assert out["task_packs"] == []

    def test_flat_stack_alias_message_unchanged(self) -> None:
        with pytest.warns(
            DeprecationWarning,
            match="flat compose_file / runner_service at the",
        ):
            out = coerce_flat_stack_fields({"compose_file": "./environment.compose.yaml"})
        assert out["stack"]["compose_file"] == "./environment.compose.yaml"

    def test_run_config_dir_alias_message_unchanged(self) -> None:
        with pytest.warns(DeprecationWarning, match=r"Legacy 'run_config/' \(singular\) directory"):
            warn_legacy_run_config_dir(Path("run_config/dev.yaml"))
