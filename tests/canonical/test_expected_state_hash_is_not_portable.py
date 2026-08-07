"""Why a stored state hash grades on one substrate and not the other.

Core hashes state with ``consistent_hash(to_hashable(...))``; the runner's db-service
hashes it with ``compute_stable_hash``. The two agree on *which states are equal* — the
``numeric_string_fields`` folding promise holds identically on both — and disagree on the
label they give each equivalence class. A hash an author stores is therefore written in
exactly one of the two algebras, and the other substrate cannot compare against it.

The three groups here are one argument. The first pins the split itself: same partition,
different digests, asserted separately so unifying one without the other fails
distinguishably. The second pins what a pack may no longer do — declare a hash literal
that no substrate grading it will consult, measured over the very literals the recorded
``tau_retail_mini`` bundles still carry. The third pins why the obvious repair is not one:
that literal is a core-algebra digest, so routing it to the runner's evaluator would score
``0.0`` on the state core scores ``1.0``.

See #915 for unifying the two hash functions, which is not in reach here:
``compute_stable_hash`` also backs db-service ETags, snapshot hashes and
``ResetTrialResponse.state_hash``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.core.grading.state_checks import (
    StateChecker,
    consistent_hash,
    extract_db_state,
    to_hashable,
)
from tolokaforge.core.hash import compute_stable_hash
from tolokaforge.core.models import GradingConfig

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_PROJECT = "tau_retail_mini"
_RECORDED_BUNDLES = ("0f3b1ff7", "test_001", "test_002")


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
    matched, _ = checker.check_hash(state, literal)
    crossed, _ = checker.check_hash(state, runner_digest)

    assert matched == 1.0, f"{trial_dir}: core no longer scores 1.0 against the declared literal"
    assert crossed == 0.0, (
        f"{trial_dir}: the runner's digest {runner_digest} now scores {crossed} through core's "
        "comparison, so the two algebras are interchangeable"
    )
