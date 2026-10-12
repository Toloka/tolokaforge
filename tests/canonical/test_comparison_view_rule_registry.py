"""``tolokaforge.comparison_view_rules`` — a rule registered out of tree is a rule (ADR-0053).

A distribution that registers a class under the entry-point group extends the
comparison view without an engine change: the rule resolves by its ``kind``,
validates its entry into its own ``extra="forbid"`` model, applies, and puts its
``NAME`` and ``VERSION`` into the record. The distribution here is real metadata on
``sys.path`` (:func:`tests.utils.comparison_view_rules.install_rule_distribution`),
read by ``importlib.metadata`` as an installed package's is. Canonical tier because
the built-in rules it lists beside it come from the installed package metadata.

The rule contract and the trust boundary are on
:class:`tolokaforge.core.grading.comparison_view.ComparisonViewRule`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.utils.comparison_view_rules import (
    DropField,
    DropFieldConfig,
    install_rule_distribution,
    target,
)
from tolokaforge.core.grading.comparison_view import (
    ComparisonViewConfig,
    ComparisonViewRule,
    RuleApplication,
    apply_comparison_view,
    resolve_comparison_view_rule,
)
from tolokaforge.core.hash import compute_stable_hash
from tolokaforge.core.plugin_registry import (
    DuplicateRegistrationError,
    available_comparison_view_rules,
)

pytestmark = pytest.mark.canonical

_DROP_NOTE = {"kind": "drop_field", "table": "orders", "field": "note"}
_GOLDEN: dict[str, Any] = {"orders": [{"id": "O1", "total": 5, "note": "as asked"}]}
_TRIAL: dict[str, Any] = {"orders": [{"id": "O1", "total": 5, "note": "left at the door"}]}


def _view(*rules: dict[str, Any]) -> ComparisonViewConfig:
    return ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})


@pytest.fixture
def drop_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_rule_distribution(monkeypatch, tmp_path, {"drop_field": target("DropField")})


def test_the_built_in_rules_are_the_registered_ones() -> None:
    assert available_comparison_view_rules() == [
        "exclude_records",
        "exclude_tables",
        "normalize_ids",
    ]


@pytest.mark.usefixtures("drop_field")
def test_a_rule_registered_out_of_tree_resolves_by_its_kind() -> None:
    assert available_comparison_view_rules() == [
        "drop_field",
        "exclude_records",
        "exclude_tables",
        "normalize_ids",
    ]
    rule = resolve_comparison_view_rule("drop_field")
    assert rule is DropField
    assert isinstance(rule(), ComparisonViewRule)


@pytest.mark.usefixtures("drop_field")
def test_its_entry_validates_into_its_own_model_which_forbids_undeclared_keys() -> None:
    view = _view(_DROP_NOTE, {"kind": "exclude_tables", "tables": ["logs"], "reason": "r"})
    assert type(view.rules[0]) is DropFieldConfig
    with pytest.raises(ValidationError, match=r"rules\[0\] \(drop_field\): column: Extra inputs"):
        _view({**_DROP_NOTE, "column": "note"})
    with pytest.raises(ValidationError, match=r"rules\[0\] \(drop_field\): field: Field required"):
        _view({"kind": "drop_field", "table": "orders"})


@pytest.mark.usefixtures("drop_field")
def test_it_applies_and_its_work_is_recorded_under_its_name() -> None:
    result = apply_comparison_view(_TRIAL, initial=None, view=_view(_DROP_NOTE), id_fields={})
    assert result.state == {"orders": [{"id": "O1", "total": 5}]}
    assert result.record.applied == (
        RuleApplication(kind="drop_field", table="orders", rows_removed=0),
    )


@pytest.mark.usefixtures("drop_field")
def test_it_can_turn_a_failing_hash_into_a_passing_one() -> None:
    """The trust boundary in one case: the rule decides that the two states are equal."""
    view = _view(_DROP_NOTE)

    def digest(state: dict[str, Any]) -> str:
        return compute_stable_hash(
            apply_comparison_view(state, initial=None, view=view, id_fields={}).state
        )

    assert compute_stable_hash(_TRIAL) != compute_stable_hash(_GOLDEN)
    assert digest(_TRIAL) == digest(_GOLDEN)


def _config_sha_with(registered: str, root: Path) -> str:
    with pytest.MonkeyPatch.context() as patch:
        install_rule_distribution(patch, root, {"drop_field": target(registered)})
        return _view(_DROP_NOTE).config_sha256()


def test_its_version_is_in_the_config_sha(tmp_path: Path) -> None:
    """A change to what a registered rule computes changes the sha of every view naming it."""
    first = _config_sha_with("DropField", tmp_path / "v1")
    assert _config_sha_with("DropField", tmp_path / "v1-again") == first
    assert _config_sha_with("DropFieldVersion2", tmp_path / "v2") != first


@pytest.mark.usefixtures("drop_field")
def test_an_unknown_kind_is_refused_at_load_naming_every_registered_kind() -> None:
    with pytest.raises(ValidationError) as caught:
        _view({"kind": "drop_column", "table": "orders", "field": "note"})
    message = str(caught.value)
    assert "rules[0]: unknown comparison_view rule kind 'drop_column'" in message
    assert (
        "registered kinds: ['drop_field', 'exclude_records', 'exclude_tables', 'normalize_ids']"
        in message
    )


def test_a_distribution_registering_a_built_in_name_fails_every_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rule cannot stand in for a built-in: the name has two registrations, so none resolves."""
    install_rule_distribution(
        monkeypatch, tmp_path, {"exclude_records": target("DropField")}, distribution="shadow"
    )
    with pytest.raises(DuplicateRegistrationError) as caught:
        _view({"kind": "exclude_tables", "tables": ["logs"], "reason": "r"})
    assert caught.value.name == "exclude_records"
    assert set(caught.value.distributions) == {"tolokaforge", "shadow"}


@pytest.mark.parametrize(
    ("registered", "fragment"),
    [
        (
            "Misnamed",
            "is Misnamed, whose NAME is 'drop_column'; a rule is registered under its NAME",
        ),
        ("Unversioned", "declares VERSION 0; a rule's VERSION is a positive int"),
        ("AcceptsAnyKey", "which does not forbid extra keys"),
        ("NotARuleConfig", "which does not derive from ComparisonViewRuleConfig"),
        ("CannotApply", "has no apply method"),
        ("AN_INSTANCE", "not a class"),
    ],
)
def test_a_registration_that_breaks_the_rule_contract_is_refused_where_the_kind_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registered: str, fragment: str
) -> None:
    install_rule_distribution(monkeypatch, tmp_path, {"drop_field": target(registered)})
    with pytest.raises(
        TypeError, match="the comparison_view rule registered as 'drop_field' "
    ) as caught:
        _view(_DROP_NOTE)
    assert fragment in str(caught.value)


@pytest.mark.parametrize(
    ("registered", "fragment"),
    [
        ("ReportsAnotherRulesWork", "reports applications under ['exclude_records']"),
        ("ReturnsTheStateItself", "apply returns a RuleOutcome holding the next state"),
    ],
)
def test_an_outcome_that_breaks_the_rule_contract_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registered: str, fragment: str
) -> None:
    install_rule_distribution(monkeypatch, tmp_path, {"drop_field": target(registered)})
    view = _view(_DROP_NOTE)
    with pytest.raises(TypeError, match="comparison_view rule 'drop_field'") as caught:
        apply_comparison_view(_TRIAL, initial=None, view=view, id_fields={})
    assert fragment in str(caught.value)
