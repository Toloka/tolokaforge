"""Comparison-view rules from outside the engine, and a throwaway distribution to ship them.

:func:`install_rule_distribution` writes a ``<name>.dist-info`` directory with a
``tolokaforge.comparison_view_rules`` entry-point table into a directory and puts
that directory on ``sys.path``. ``importlib.metadata`` then finds the distribution
as it finds an installed package, so a rule registered there resolves through the
same discovery, ``EntryPoint.load`` and contract checks as the built-in rules, with
nothing in the engine patched. The classes below are what such a distribution
registers: :class:`DropField`, a rule no built-in is, and rules that break the
:class:`~tolokaforge.core.grading.comparison_view.ComparisonViewRule` contract one
way each.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal, cast

import pytest
from pydantic import ConfigDict

from tolokaforge.core import plugin_registry
from tolokaforge.core.grading.comparison_view import (
    ComparisonViewRuleConfig,
    RuleApplication,
    RuleOutcome,
)
from tolokaforge.core.plugin_registry import COMPARISON_VIEW_RULES_GROUP

_MODULE = __name__


def target(attribute: str) -> str:
    """The entry-point target of ``attribute`` in this module."""
    return f"{_MODULE}:{attribute}"


def install_rule_distribution(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    rules: Mapping[str, str],
    *,
    distribution: str = "tests-comparison-view-rules",
) -> None:
    """Make a distribution that registers ``rules`` (``name -> "module:attribute"``).

    The discovery cache is replaced by an empty one for the test, so the next lookup
    scans the entry points again; ``monkeypatch`` restores ``sys.path`` and the
    process's own cache on teardown, and no later test sees the distribution.
    """
    dist_info = root / f"{distribution.replace('-', '_')}-0.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 0.0.0\n", encoding="utf-8"
    )
    rows = "".join(f"{name} = {value}\n" for name, value in rules.items())
    (dist_info / "entry_points.txt").write_text(
        f"[{COMPARISON_VIEW_RULES_GROUP}]\n{rows}", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(plugin_registry, "_discovery_cache", {})


class DropFieldConfig(ComparisonViewRuleConfig):
    """``drop_field``: one field to leave out of every row of one table."""

    kind: Literal["drop_field"] = "drop_field"
    table: str
    field: str

    def names(self) -> tuple[str, ...]:
        return (self.table,)


class DropField:
    """Leaves one field out of every row of a table."""

    NAME: ClassVar[str] = "drop_field"
    VERSION: ClassVar[int] = 1
    config_model: ClassVar[type[ComparisonViewRuleConfig]] = DropFieldConfig

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome:
        config = cast(DropFieldConfig, config)
        application = RuleApplication(kind=self.NAME, table=config.table, rows_removed=0)
        if config.table not in state:
            return RuleOutcome(state=state, applied=(application,))
        rows = [
            {key: value for key, value in row.items() if key != config.field}
            for row in state[config.table]
        ]
        return RuleOutcome(state={**state, config.table: rows}, applied=(application,))


class DropFieldVersion2(DropField):
    """:class:`DropField` once what it computes has changed: the same name, the next version."""

    VERSION: ClassVar[int] = 2


class RecordsWhatItIsHanded(DropField):
    """Records the inputs of every application and tries to change the caller's copies."""

    seen: ClassVar[list[dict[str, Any]]] = []

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome:
        type(self).seen.append({"state": state, "initial": initial, "id_fields": id_fields})
        if initial is not None:
            cast(dict[str, Any], initial).setdefault("rows", []).append({"id": "planted"})
        return super().apply(state, initial=initial, id_fields=id_fields, config=config)


class Misnamed(DropField):
    """Registered as ``drop_field`` but named otherwise."""

    NAME: ClassVar[str] = "drop_column"


class Unversioned(DropField):
    """Declares no positive ``VERSION``."""

    VERSION: ClassVar[int] = 0


class _PermissiveConfig(DropFieldConfig):
    model_config = ConfigDict(extra="allow", frozen=True)


class AcceptsAnyKey(DropField):
    """Validates its entry into a model that lets undeclared keys through."""

    config_model: ClassVar[type[ComparisonViewRuleConfig]] = _PermissiveConfig


class NotARuleConfig:
    """Declares a config model that is not a :class:`ComparisonViewRuleConfig`."""

    NAME: ClassVar[str] = "drop_field"
    VERSION: ClassVar[int] = 1
    config_model: ClassVar[type] = dict


class CannotApply:
    """Declares everything a rule declares except ``apply``."""

    NAME: ClassVar[str] = "drop_field"
    VERSION: ClassVar[int] = 1
    config_model: ClassVar[type[ComparisonViewRuleConfig]] = DropFieldConfig


AN_INSTANCE = DropField()
"""A rule instance where the entry point must name the class."""


class ReportsAnotherRulesWork(DropField):
    """Records its application under a built-in's name."""

    def apply(
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> RuleOutcome:
        outcome = super().apply(state, initial=initial, id_fields=id_fields, config=config)
        forged = RuleApplication(kind="exclude_records", table="t", rows_removed=3)
        return RuleOutcome(state=outcome.state, applied=(forged,))


class ReturnsTheStateItself(DropField):
    """Returns the next state where its outcome belongs."""

    def apply(  # type: ignore[override]
        self,
        state: dict[str, Any],
        *,
        initial: Mapping[str, Any] | None,
        id_fields: Mapping[str, str | list[str]],
        config: ComparisonViewRuleConfig,
    ) -> dict[str, Any]:
        return super().apply(state, initial=initial, id_fields=id_fields, config=config).state
