"""Why a stored state hash grades on one substrate and not the other.

Core hashes state with ``consistent_hash(to_hashable(...))``; the runner's db-service
hashes it with ``compute_stable_hash``. The two agree on *which states are equal* — the
``numeric_string_fields`` folding promise holds identically on both — and disagree on the
label they give each equivalence class. A hash an author stores is therefore written in
exactly one of the two algebras, and the other substrate cannot compare against it.

The four groups here are one argument. The first pins the split itself: same partition,
different digests, asserted separately so unifying one without the other fails
distinguishably. The second pins what a pack may no longer do — declare a hash literal
that no substrate grading it will consult, measured over the very literals the recorded
``tau_retail_mini`` bundles still carry. The third pins why the obvious repair is not one:
that literal is a core-algebra digest, so routing it to the runner's evaluator would score
``0.0`` on the state core scores ``1.0``. The fourth pins that there is no longer a wire
field to route it through.

The two functions are deliberately separate (#915). ``compute_stable_hash`` backs
persisted digests — db-service ETags, snapshot hashes, ``ResetTrialResponse.state_hash``
— and core's algebra reproduces the digests recorded bundles carry, so unifying in
either direction invalidates digests that already exist; and with no wire field left to
carry one across, nothing needs the portability unification would buy.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from google.protobuf import descriptor_pb2
from pydantic import ValidationError

from tests.utils.comparison_view_runner import (
    GOLDEN_TOOL,
    Verdict,
    grade_through_the_runner,
    verdict_of,
)
from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.core.grading.comparison_view import ComparisonViewConfig, ComparisonViewError
from tolokaforge.core.grading.golden_replay import GoldenReplayRecord
from tolokaforge.core.grading.hash_grading_result import HashComparisonBasis
from tolokaforge.core.grading.pre_hash import PreHashDeclaration, ViewedPair, view_the_pair
from tolokaforge.core.grading.state_checks import (
    StateChecker,
    consistent_hash,
    extract_db_state,
    state_digest,
    to_hashable,
)
from tolokaforge.core.hash import ColumnCompareRule, compute_stable_hash
from tolokaforge.core.models import GradingConfig
from tolokaforge.runner import models as runner_models
from tolokaforge.runner import runner_pb2
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_PROJECT = "tau_retail_mini"
_RECORDED_BUNDLES = ("0f3b1ff7", "test_001", "test_002")
_RETIRED_WIRE_NUMBER = 3


class _RecordedBundleDefect(Exception):
    """A bundle stopped carrying what these tests read from it.

    Raised rather than asserted so a fixture that no longer supplies the literal reads as
    a broken corpus rather than as the property under test holding.
    """


def _core_digest(state: dict[str, Any], numeric_string_fields: list[str]) -> str:
    """The digest core grading compares against (``state_checks.py`` ``check_hash``)."""
    fields = frozenset(numeric_string_fields) if numeric_string_fields else None
    return consistent_hash(to_hashable(state, fields))


def _runner_digest(state: dict[str, Any], numeric_string_fields: list[str]) -> str:
    """The digest db-service answers with (``json_db_service/app.py`` ``compute_stable_hash``)."""
    return compute_stable_hash(state, numeric_string_fields=numeric_string_fields or None)


def _record(total: Any) -> dict[str, Any]:
    return {"orders": [{"id": "O1", "total": total}]}


_EQUIVALENCE_CASES = (
    pytest.param(_record("130.00"), _record("130.0"), ["total"], True, id="strings-opted-in"),
    pytest.param(_record("130.00"), _record("130.0"), [], False, id="strings-not-opted-in"),
    pytest.param(_record(130), _record(130.0), [], True, id="numbers-no-opt-in"),
    pytest.param(_record(130), _record(130.0), ["total"], True, id="numbers-opted-in"),
)

_LABELLED_STATES = (
    pytest.param(_record("130.00"), ["total"], id="string-opted-in"),
    pytest.param(_record("130.00"), [], id="string-not-opted-in"),
    pytest.param(_record(130), [], id="number"),
)


@pytest.mark.parametrize("left,right,numeric_string_fields,equal", _EQUIVALENCE_CASES)
def test_both_substrates_induce_the_same_equivalence_relation(
    left: dict[str, Any],
    right: dict[str, Any],
    numeric_string_fields: list[str],
    equal: bool,
) -> None:
    """Each substrate folds this pair the way the case says, read off the case not each other.

    Asserting the two columns against a pinned verdict rather than against one another is
    what keeps the lock from passing on a pair both substrates fold wrongly in the same
    direction.
    """
    core_folds = _core_digest(left, numeric_string_fields) == _core_digest(
        right, numeric_string_fields
    )
    runner_folds = _runner_digest(left, numeric_string_fields) == _runner_digest(
        right, numeric_string_fields
    )

    assert core_folds is equal, (
        f"core folded {left} and {right} to equal={core_folds} under "
        f"numeric_string_fields={numeric_string_fields}, expected equal={equal}"
    )
    assert runner_folds is equal, (
        f"the runner folded {left} and {right} to equal={runner_folds} under "
        f"numeric_string_fields={numeric_string_fields}, expected equal={equal}"
    )


@pytest.mark.parametrize("state,numeric_string_fields", _LABELLED_STATES)
def test_the_two_substrates_label_the_same_state_differently(
    state: dict[str, Any], numeric_string_fields: list[str]
) -> None:
    """One state, two digests — which is what makes a stored literal unportable."""
    core = _core_digest(state, numeric_string_fields)
    runner = _runner_digest(state, numeric_string_fields)

    assert core != runner, (
        f"both substrates hashed {state} to {core} under "
        f"numeric_string_fields={numeric_string_fields}: a stored literal is portable and "
        "the argument for retiring one is gone"
    )


# --------------------------------------------------------------------------
# Through a comparison view: one composition, two algebras, one verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ViewCase:
    """One pair through a declared view, and the verdict both substrates owe it.

    ``loads`` is ``False`` for the one declaration the task load refuses: a key built
    from a masked column. Both substrates are then held to the verdict below the load
    gate, where the guarantee the refusal backs up has to hold anyway.
    """

    name: str
    initial: dict[str, list[dict[str, Any]]]
    trial: dict[str, list[dict[str, Any]]]
    golden: dict[str, list[dict[str, Any]]]
    rules: tuple[dict[str, Any], ...]
    verdict: Verdict
    unstable: tuple[tuple[str, str], ...] = ()
    compare_columns: dict[str, dict[str, Any]] = field(default_factory=dict)
    loads: bool = True


def _docs(*rows: tuple[str, str], refs: tuple[str, ...] = ()) -> dict[str, list[dict]]:
    return {
        "documents": [{"id": doc_id, "source_id": source} for doc_id, source in rows],
        "corrections": [
            {"id": f"C{index}", "document_ref": ref} for index, ref in enumerate(refs, 1)
        ],
        "lookup_log": [],
    }


_BY_SOURCE = {
    "kind": "normalize_ids",
    "table": "documents",
    "key": ["source_id"],
    "references": [{"table": "corrections", "field": "document_ref"}],
}
_LOOKUPS = {"kind": "exclude_tables", "tables": ["lookup_log"], "reason": "the read tools'"}
_RELEASED_UNLESS_DISPUTED = {
    "kind": "exclude_records",
    "table": "holds",
    "where": {"status": "released"},
    "unless_referenced_by": [{"table": "disputes", "field": "hold_ref"}],
}
_HOLDS_BY_PLACEMENT = {
    "kind": "normalize_ids",
    "table": "holds",
    "rank_by": ["placed_at"],
    "references": [{"table": "disputes", "field": "hold_ref"}],
}


def _holds(*holds: tuple[str, str, str], disputed: str | None = None) -> dict[str, list[dict]]:
    return {
        "holds": [
            {"id": hold_id, "card": card, "placed_at": placed_at}
            for hold_id, card, placed_at in holds
        ],
        "disputes": [] if disputed is None else [{"id": "X1", "hold_ref": disputed}],
    }


def _released(*holds: tuple[str, str], disputed: str | None = None) -> dict[str, list[dict]]:
    return {
        "holds": [{"id": hold_id, "status": status} for hold_id, status in holds],
        "disputes": [] if disputed is None else [{"id": "X1", "hold_ref": disputed}],
    }


def _decisions(*allocations: dict[str, Any]) -> dict[str, list[dict]]:
    return {"decisions": [{"id": "RD1", "allocations": list(allocations)}]}


_VIEW_CASES: tuple[_ViewCase, ...] = (
    _ViewCase(
        "normalize_ids_with_the_id_declared_unstable_auto_id",
        initial=_docs(("D1", "S1")),
        trial=_docs(("D1", "S1"), ("D3", "S2"), refs=("D3",)),
        golden=_docs(("D1", "S1"), ("D2", "S2"), refs=("D2",)),
        rules=(_BY_SOURCE,),
        unstable=(("documents", "id"),),
        verdict=Verdict.PASS,
    ),
    _ViewCase(
        # Each card's hold re-keys by its rank of placement, which the mask hides, so
        # only the re-keyed id still says which hold is which: dropped as unstable, the
        # dispute naming the other card's hold would pass.
        "an_ordinal_key_over_a_masked_timestamp_names_the_other_cards_hold",
        initial=_holds(),
        trial=_holds(("H5", "B", "t1"), ("H6", "A", "t2"), disputed="H5"),
        golden=_holds(("H1", "A", "t1"), ("H2", "B", "t2"), disputed="H1"),
        rules=(_HOLDS_BY_PLACEMENT,),
        unstable=(("holds", "id"), ("holds", "placed_at")),
        compare_columns={"holds": {"card": {"order": "unordered"}}},
        verdict=Verdict.FAIL,
        loads=False,
    ),
    _ViewCase(
        # #1670 on the view path: the rows re-created under new ids in another order sort
        # by what is left after the unstable filter, not by the generated id.
        "a_permutation_with_an_unstable_id_under_order_unordered",
        initial={"rows": [], "lookup_log": []},
        trial={"rows": [{"a_id": 1, "x": "B"}, {"a_id": 2, "x": "A"}], "lookup_log": []},
        golden={"rows": [{"a_id": 5, "x": "A"}, {"a_id": 6, "x": "B"}], "lookup_log": []},
        rules=(_LOOKUPS,),
        unstable=(("rows", "a_id"),),
        compare_columns={"rows": {"x": {"order": "unordered"}}},
        verdict=Verdict.PASS,
    ),
    _ViewCase(
        # The fixture names the singular; the table is plural. The db-service resolves the
        # name, so the view path on both substrates does.
        "an_unstable_table_name_resolved_as_the_db_service_resolves_it",
        initial=_docs(("D1", "S1")),
        trial={**_docs(("D1", "S1")), "documents": [{"id": "D1", "source_id": "S1", "at": "t9"}]},
        golden={**_docs(("D1", "S1")), "documents": [{"id": "D1", "source_id": "S1", "at": "t5"}]},
        rules=(_LOOKUPS,),
        unstable=(("document", "at"),),
        verdict=Verdict.PASS,
    ),
    _ViewCase(
        "exclude_records_drops_a_released_hold_nothing_references",
        initial=_released(),
        trial=_released(("H1", "active"), ("H2", "released")),
        golden=_released(("H1", "active")),
        rules=(_RELEASED_UNLESS_DISPUTED,),
        verdict=Verdict.PASS,
    ),
    _ViewCase(
        "exclude_records_keeps_a_released_hold_a_dispute_references",
        initial=_released(),
        trial=_released(("H1", "active"), ("H2", "released"), disputed="H2"),
        golden=_released(("H1", "active"), disputed="H2"),
        rules=(_RELEASED_UNLESS_DISPUTED,),
        verdict=Verdict.FAIL,
    ),
    _ViewCase(
        "exclude_records_with_a_path_drops_the_zero_nested_items",
        initial={"decisions": []},
        trial=_decisions({"key": "P1", "amount": "40.00"}, {"key": "P2", "amount": "0.00"}),
        golden=_decisions({"key": "P1", "amount": "40.00"}),
        rules=(
            {
                "kind": "exclude_records",
                "table": "decisions",
                "path": "allocations",
                "where": {"all_zero": ["amount"]},
            },
        ),
        verdict=Verdict.PASS,
    ),
    _ViewCase(
        "exclude_tables_drops_what_the_read_tools_wrote",
        initial=_docs(("D1", "S1")),
        trial={**_docs(("D1", "S1")), "lookup_log": [{"id": "L1", "document": "D1"}]},
        golden=_docs(("D1", "S1")),
        rules=(_LOOKUPS,),
        verdict=Verdict.PASS,
    ),
    _ViewCase(
        "a_trial_whose_documents_share_a_key_collides_and_fails",
        initial=_docs(("D1", "S1")),
        trial=_docs(("D1", "S1"), ("D3", "S2"), ("D4", "S2"), refs=("D3",)),
        golden=_docs(("D1", "S1"), ("D2", "S2"), refs=("D2",)),
        rules=(_BY_SOURCE,),
        verdict=Verdict.FAIL,
    ),
    _ViewCase(
        # Once the golden's view succeeded, a record the view cannot read is the trial's.
        "a_trial_document_without_its_key_field_fails",
        initial=_docs(("D1", "S1")),
        trial={
            **_docs(("D1", "S1"), refs=("D3",)),
            "documents": [{"id": "D1", "source_id": "S1"}, {"id": "D3"}],
        },
        golden=_docs(("D1", "S1"), ("D2", "S2"), refs=("D2",)),
        rules=(_BY_SOURCE,),
        verdict=Verdict.FAIL,
    ),
    _ViewCase(
        "a_golden_document_without_its_key_field_is_a_grading_error",
        initial=_docs(("D1", "S1")),
        trial=_docs(("D1", "S1"), ("D3", "S2"), refs=("D3",)),
        golden={
            **_docs(("D1", "S1"), refs=("D2",)),
            "documents": [{"id": "D1", "source_id": "S1"}, {"id": "D2"}],
        },
        rules=(_BY_SOURCE,),
        verdict=Verdict.GRADING_ERROR,
    ),
    _ViewCase(
        "a_golden_whose_documents_share_a_key_is_a_grading_error",
        initial=_docs(("D1", "S1")),
        trial=_docs(("D1", "S1"), ("D3", "S2"), refs=("D3",)),
        golden=_docs(("D1", "S1"), ("D2", "S2"), ("D5", "S2"), refs=("D2",)),
        rules=(_BY_SOURCE,),
        verdict=Verdict.GRADING_ERROR,
    ),
)


def _view(case: _ViewCase) -> ComparisonViewConfig:
    return ComparisonViewConfig.model_validate({"version": 1, "rules": list(case.rules)})


def _compare_columns(case: _ViewCase) -> dict[str, dict[str, ColumnCompareRule]]:
    return {
        table: {column: ColumnCompareRule(**rule) for column, rule in columns.items()}
        for table, columns in case.compare_columns.items()
    }


def _core_view_verdict(case: _ViewCase) -> Verdict:
    """Core's own hash check over the pair: the composition, then ``state_digest``."""
    try:
        result = StateChecker().check_hash(
            copy.deepcopy(case.trial),
            expected_state=copy.deepcopy(case.golden),
            comparison_view=_view(case),
            initial_state=copy.deepcopy(case.initial),
            unstable_fields=[f"{table}.{name}" for table, name in case.unstable],
            compare_columns=_compare_columns(case),
        )
        score = result.hash_score
    except ComparisonViewError:
        return Verdict.GRADING_ERROR
    return Verdict.PASS if score == 1.0 else Verdict.FAIL


def _view_task(case: _ViewCase) -> dict[str, Any]:
    return {
        "task_id": case.name,
        "name": case.name,
        "category": "test",
        "description": "A pair compared through a comparison view.",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": case.initial,
            "unstable_fields": [
                {"table_name": table, "field_name": name} for table, name in case.unstable
            ],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 0.5,
            "state_checks": {
                "hash_enabled": True,
                "golden_actions": [{"tool_name": GOLDEN_TOOL, "arguments": {}}],
                "compare_columns": case.compare_columns,
                "comparison_view": {"version": 1, "rules": list(case.rules)},
            },
        },
    }


def _runner_view_verdict(case: _ViewCase, servicer: Any, context: Any) -> Verdict:
    """The runner's: ``RegisterTrial`` → the trial's database → ``GradeTrial``.

    A case the load refuses is graded below the gate instead, through the runner's own
    steps 1–5 over the same two states.
    """
    description = runner_models.TaskDescription.model_validate(_view_task(case))
    if not case.loads:
        registered = servicer.RegisterTrial(
            register_request(
                trial_spec_json(description.model_dump(mode="json"), trial_id=f"{case.name}:0"),
                trial_id=f"{case.name}:0",
            ),
            context,
        )
        assert registered.success is False, f"{case.name} is expected to be refused at load"
        assert "masks" in registered.error, registered.error
        try:
            viewed = RunnerServiceImpl._compare_through_the_view(
                SimpleNamespace(task_description=description),
                description.grading.state_checks,
                copy.deepcopy(case.trial),
                copy.deepcopy(case.golden),
                basis=HashComparisonBasis.GOLDEN_REPLAY,
                golden_replay=GoldenReplayRecord(authored=1),
            )
        except ComparisonViewError:
            return Verdict.GRADING_ERROR
        return Verdict.PASS if viewed.hash_match else Verdict.FAIL
    response = grade_through_the_runner(
        servicer,
        context,
        description=_view_task(case),
        trial_id=f"{case.name}:0",
        trial=case.trial,
        golden=case.golden,
    )
    return verdict_of(response)


@pytest.mark.parametrize("case", _VIEW_CASES, ids=[case.name for case in _VIEW_CASES])
def test_both_substrates_reach_the_pinned_verdict_through_a_comparison_view(
    case: _ViewCase, runner_service, mock_grpc_context
) -> None:
    """Each substrate is held to the case's verdict, not to the other's.

    The runner drives its real ``GradeTrial`` over the trial's own db-service, the
    golden written by a replayed action, so the full states it views are the ones it
    read back; core runs ``check_hash``. Both call the one composition for steps 1–3 and
    hash with their own algebra — which still labels the viewed state differently, the
    reason a digest never crosses.
    """
    assert _core_view_verdict(case) is case.verdict, f"core graded {case.name} otherwise"
    runner = _runner_view_verdict(case, runner_service, mock_grpc_context)
    assert runner is case.verdict, f"the runner graded {case.name} otherwise"
    if case.verdict is Verdict.PASS:
        pair = view_the_pair(
            case.trial,
            case.golden,
            initial=case.initial,
            declaration=PreHashDeclaration(
                view=_view(case),
                unstable_fields=tuple(f"{table}.{name}" for table, name in case.unstable),
                compare_columns=_compare_columns(case),
            ),
        )
        assert isinstance(pair, ViewedPair)
        assert compute_stable_hash(pair.trial) != state_digest(pair.trial)


def test_the_view_cases_span_every_verdict_and_every_rule_kind() -> None:
    """A table that pins one verdict, or never names a kind, proves nothing about it."""
    assert {case.verdict for case in _VIEW_CASES} == set(Verdict)
    kinds = {rule["kind"] for case in _VIEW_CASES for rule in case.rules}
    assert kinds == {"exclude_records", "exclude_tables", "normalize_ids"}


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _trial_dir(canonical_project_dir, bundle: str) -> Path:
    return canonical_project_dir(_PROJECT) / "output" / "trials" / bundle / "0"


def _declared_literal(trial_dir: Path) -> str:
    literal = _load_yaml(trial_dir / "task.yaml")["grading_config"]["state_checks"]["hash"][
        "expected_state_hash"
    ]
    if not isinstance(literal, str) or not literal:
        raise _RecordedBundleDefect(f"{trial_dir}: declares no expected_state_hash to grade by")
    return literal


def _hash_the_recorded_verdict_names(trial_dir: Path) -> str:
    reasons = _load_yaml(trial_dir / "grade.yaml")["reasons"]
    named = re.findall(r"\(([0-9a-f]{8,})\.\.\.\)", reasons)
    if len(named) != 1:
        raise _RecordedBundleDefect(
            f"{trial_dir}: recorded reasons name {len(named)} hashes, not one: {reasons!r}"
        )
    return named[0]


def _recorded_db_state(trial_dir: Path) -> dict[str, Any]:
    state = extract_db_state(_load_yaml(trial_dir / "env.yaml"))
    if not state:
        raise _RecordedBundleDefect(f"{trial_dir}: recorded no final state to hash")
    return state


def _pack_declaring(literal: str) -> dict[str, Any]:
    """A pack ``grading.yaml`` whose only state source is the stored literal."""
    return {
        "combine": {
            "method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 1.0,
        },
        "state_checks": {"hash": {"enabled": True, "expected_state_hash": literal}},
    }


def _a_declared_literal_survives_a_pack_load(literal: str) -> bool:
    """Whether the literal reaches a loaded config as a value a substrate would compare against.

    Refusing the pack and dropping the key both answer ``False``, so the predicate below
    holds whichever way the refusal lands.
    """
    try:
        loaded = GradingConfig(**_pack_declaring(literal))
    except ValidationError:
        return False
    return getattr(loaded.state_checks.hash, "expected_state_hash", None) == literal


@pytest.mark.parametrize("bundle", _RECORDED_BUNDLES)
def test_a_pack_cannot_declare_a_hash_literal_no_substrate_will_consult(
    bundle: str, canonical_project_dir
) -> None:
    """The literal these bundles declare is not the hash their recorded verdict names.

    Each bundle records ``score: 1.0`` against a hash that is neither the declared literal
    nor either substrate's hash of the recorded state, so the literal was not what graded
    them — and a pack declaring the same thing reaches no loaded config carrying it.
    """
    trial_dir = _trial_dir(canonical_project_dir, bundle)
    literal = _declared_literal(trial_dir)
    named = _hash_the_recorded_verdict_names(trial_dir)

    if literal.startswith(named):
        raise _RecordedBundleDefect(
            f"{trial_dir}: the recorded verdict names {named}..., which the declared literal "
            f"{literal} begins with — this bundle no longer records an unconsulted literal"
        )

    assert not _a_declared_literal_survives_a_pack_load(literal), (
        f"a pack declaring expected_state_hash: {literal} loads with nothing said, and no "
        "substrate grading it will consult that value"
    )


@pytest.mark.parametrize("bundle", _RECORDED_BUNDLES)
def test_routing_the_declared_literal_to_the_runner_would_score_zero(
    bundle: str, canonical_project_dir
) -> None:
    """The declared literal is a core-algebra digest, so the runner cannot match it.

    This is what a future implementer meets on the way to populating the wire field with
    the stored value: the digest the runner computes over the very state the literal
    describes is a different string, and handing one algebra's digest to the other's binary
    producer scores ``0.0``.
    """
    trial_dir = _trial_dir(canonical_project_dir, bundle)
    literal = _declared_literal(trial_dir)
    state = _recorded_db_state(trial_dir)

    assert _core_digest(state, []) == literal, (
        f"{trial_dir}: the declared literal is no longer core's hash of the recorded state, "
        "so which algebra wrote it is unknown"
    )
    runner_digest = _runner_digest(state, [])
    assert runner_digest != literal, (
        f"{trial_dir}: the runner now hashes the recorded state to the declared literal, so "
        "the literal is portable after all"
    )

    checker = StateChecker()
    result = checker.check_hash(state, literal)
    matched = result.hash_score
    result = checker.check_hash(state, runner_digest)
    crossed = result.hash_score

    assert matched == 1.0, f"{trial_dir}: core no longer scores 1.0 against the declared literal"
    assert crossed == 0.0, (
        f"{trial_dir}: the runner's digest {runner_digest} now scores {crossed} through core's "
        "comparison, so the two algebras are interchangeable"
    )


def _grade_trial_request_schema() -> descriptor_pb2.DescriptorProto:
    """``GradeTrialRequest`` as the *generated* module declares it, not as the ``.proto`` reads.

    A ``runner_pb2`` built from an older schema keeps whatever that schema declared, so an
    engine and a runner image compiled from different generations disagree about the wire
    while both source files read correctly. Only the descriptor says which one shipped.
    """
    schema = descriptor_pb2.DescriptorProto()
    runner_pb2.GradeTrialRequest.DESCRIPTOR.CopyToProto(schema)
    return schema


def test_grade_trial_carries_no_wire_field_for_a_stored_expected_hash() -> None:
    """No route exists for handing the runner a hash it did not compute."""
    declared = {field.name: field.number for field in _grade_trial_request_schema().field}

    assert "precomputed_expected_hash" not in declared, (
        "GradeTrialRequest declares precomputed_expected_hash again, on field "
        f"{declared.get('precomputed_expected_hash')} — a stored digest is written in one "
        "substrate's hash algebra and grades 0.0 through the other (#915)"
    )


def test_the_wire_number_that_carried_a_stored_expected_hash_is_reserved() -> None:
    """A new field on that number would parse an old engine's bytes as its own."""
    schema = _grade_trial_request_schema()
    reserved = {number for span in schema.reserved_range for number in range(span.start, span.end)}

    assert _RETIRED_WIRE_NUMBER in reserved, (
        f"GradeTrialRequest reserves {sorted(reserved)}, which does not include field "
        f"{_RETIRED_WIRE_NUMBER}: whatever is declared there next inherits the bytes an "
        "engine predating this schema still writes"
    )
