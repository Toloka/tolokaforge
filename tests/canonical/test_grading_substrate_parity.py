"""Substrate-parity guard rail for the grading key manifest.

Five locks over :mod:`tolokaforge.core.grading.key_manifest`:

1. every field either substrate's grading config declares is claimed by exactly
   one manifest entry, and every claimed field resolves;
2. the exemption sets are frozen here — in the test module, never beside the
   manifest data they guard — so widening one is a reviewable edit, and every
   entry matching lock 3's predicate names both evaluators and owns a fixture;
3. every key claiming both substrates at ``DIFFERENTIAL_CANONICAL`` demonstrably
   moves both substrates' component scores, through each substrate's real
   production evaluator and its real combine;
4. every key both substrates declare survives adapter translation;
5. every ledger key's ``runner_field`` resolves to a place in the runner
   ``GradingConfig`` dump *and* some recording site in the grading path claims
   it, so a malformed or unclaimed entry fails here rather than at grade time in
   production.

The exemption sets and the differential fixtures are the enforcement mechanism:
adding a grading key to one substrate only cannot pass this suite without an
explicit, reviewable edit to one of the frozen constants below.
"""

import importlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import models as core_models
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.key_manifest import (
    GRADING_KEYS,
    Enforcement,
    GradingKey,
    KeyKind,
    SubstrateCoverage,
    author_keys,
    entry,
)
from tolokaforge.core.models import (
    Message,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)
from tolokaforge.runner import models as runner_models
from tolokaforge.runner.grading import (
    combine_grade_components,
    evaluate_jsonpath_checks,
    evaluate_transcript_rules,
)
from tolokaforge.runner.grading_ledger import (
    LEDGER_KEYS,
    accountable_author_keys,
    runner_dump_path,
)
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PARITY_GLOB = "grading_parity/**/task.yaml"
_ALL_KEYS_TASK = "all_keys"

# --------------------------------------------------------------------------
# Frozen exemption sets — the gate. Each is asserted as set equality against a
# set computed from the manifest, so drift cannot widen an exemption silently.
# --------------------------------------------------------------------------

# Non-BOTH keys that can never be both substrates. tracking_issue must be None.
_ARCHITECTURAL_EXEMPTIONS = frozenset(
    {
        "state_checks.db_probes",
        "llm_judge",
        "grading_method",
    }
)

# Non-BOTH keys that should be both and are not yet. tracking_issue is required.
_DRIFT_EXEMPTIONS = frozenset(
    {
        "combine.method",
        "state_checks.hash.expected_state_hash",
        "state_checks.hash.weight",
    }
)

# Scored keys that claim both substrates but are not differentially proven
# in-process. A key added here is a key whose parity claim rests on field
# resolution alone.
_NON_DIFFERENTIAL_SCORED_KEYS = frozenset(
    {
        "state_checks.hash",
        "state_checks.hash.enabled",
        "state_checks.hash.golden_actions",
    }
)

# FIELD_RESOLUTION_ONLY entries that need no tracking issue: aggregation and
# load-time config inputs, which have no violating trajectory by construction.
_NON_TRACKED_FIELD_RESOLUTION_KEYS = frozenset(
    {
        "combine.weights",
        "combine.pass_threshold",
        "state_checks.id_fields",
        "state_checks.relaxed_validation",
        "grading_method",
    }
)

# Fields the field walker descends into instead of claiming as author keys.
_CONTAINER_FIELDS = frozenset(
    {
        "core:GradingConfig.combine",
        "core:GradingConfig.state_checks",
        "core:GradingConfig.transcript_rules",
        "runner:GradingConfig.state_checks",
        "runner:GradingConfig.transcript_rules",
    }
)

_SUBSTRATE_ROOTS: dict[str, type[BaseModel]] = {
    "core": core_models.GradingConfig,
    "runner": runner_models.GradingConfig,
}


# --------------------------------------------------------------------------
# Manifest introspection helpers
# --------------------------------------------------------------------------


def _field_of(item: GradingKey, substrate: str) -> str | None:
    return item.core_field if substrate == "core" else item.runner_field


def _dict_key_of(item: GradingKey, substrate: str) -> str | None:
    return item.core_dict_key if substrate == "core" else item.runner_dict_key


def _claimed_fields(substrate: str) -> dict[str, list[str]]:
    """Model field paths claimed directly (not via a dict key) -> author keys."""
    claims: dict[str, list[str]] = {}
    for item in GRADING_KEYS:
        field = _field_of(item, substrate)
        if field is None or _dict_key_of(item, substrate) is not None:
            continue
        claims.setdefault(field, []).append(item.author_key)
    return claims


def _union_options(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) in (Union, UnionType):
        return get_args(annotation)
    return (annotation,)


def _direct_model(annotation: Any) -> type[BaseModel] | None:
    """The nested model a field holds directly, or None.

    A ``list[SomeModel]`` field is a leaf: its elements are the shape of one
    author key's value, not separate author keys.
    """
    for option in _union_options(annotation):
        if (
            get_origin(option) is None
            and isinstance(option, type)
            and issubclass(option, BaseModel)
        ):
            return option
    return None


def _is_dict_field(annotation: Any) -> bool:
    return any(get_origin(option) is dict for option in _union_options(annotation))


def _walk(substrate: str) -> tuple[set[str], set[str], dict[str, type[BaseModel]]]:
    """Walk a substrate's grading config from its root ``GradingConfig``.

    Returns leaf field paths, substrate-prefixed container field paths, and the
    reachable model registry (used to resolve manifest ``*_field`` paths).
    """
    claimed = _claimed_fields(substrate)
    leaves: set[str] = set()
    containers: set[str] = set()
    registry: dict[str, type[BaseModel]] = {}
    queue = [_SUBSTRATE_ROOTS[substrate]]
    while queue:
        current = queue.pop()
        if current.__name__ in registry:
            continue
        registry[current.__name__] = current
        for name, field in current.model_fields.items():
            qualified = f"{current.__name__}.{name}"
            nested = _direct_model(field.annotation)
            if nested is not None and qualified not in claimed:
                containers.add(f"{substrate}:{qualified}")
                queue.append(nested)
                continue
            leaves.add(qualified)
    return leaves, containers, registry


def _differential_entries() -> tuple[GradingKey, ...]:
    """Test 3's predicate, stated once and reused by test 2."""
    return tuple(
        item
        for item in GRADING_KEYS
        if item.kind is KeyKind.SCORED_CHECK
        and item.coverage.startswith("BOTH")
        and item.enforcement is Enforcement.DIFFERENTIAL_CANONICAL
    )


def _import_dotted(path: str) -> Any:
    """Resolve a dotted module/attribute path, longest importable prefix first."""
    parts = path.split(".")
    for boundary in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:boundary]))
        except ImportError:
            continue
        resolved = module
        for attribute in parts[boundary:]:
            resolved = getattr(resolved, attribute)
        return resolved
    raise ImportError(f"no importable module prefix in {path!r}")


# --------------------------------------------------------------------------
# Fixture-pack helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _TrialCase:
    """One satisfying-or-violating trial, in each substrate's own input shape."""

    core_trajectory: Trajectory
    runner_messages: list[dict[str, Any]]
    runner_tool_history: list[dict[str, Any]]
    state: dict[str, Any]


def _task_id_for(author_key: str) -> str:
    return author_key.replace(".", "_")


def _pack_dir(test_data_dir: Path, author_key: str) -> Path:
    return test_data_dir / "grading_parity" / _task_id_for(author_key)


def _parity_adapter(test_data_dir: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": _PARITY_GLOB})


def _yaml_declares(data: Any, dotted_path: str) -> bool:
    node = data
    for segment in dotted_path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return False
        node = node[segment]
    return True


def _declared_author_keys(grading_yaml: dict[str, Any]) -> set[str]:
    return {key for key in author_keys() if _yaml_declares(grading_yaml, key)}


def _load_case(pack_dir: Path, case: str) -> _TrialCase:
    fixture = yaml.safe_load((pack_dir / "trial.yaml").read_text())[case]
    messages: list[Message] = []
    for index, raw in enumerate(fixture["messages"]):
        tool_calls = [
            ToolCall(id=f"call_{index}_{position}", name=call["name"], arguments=call["arguments"])
            for position, call in enumerate(raw.get("tool_calls", []))
        ]
        messages.append(
            Message(role=raw["role"], content=raw["content"], tool_calls=tool_calls or None)
        )
    now = "2026-01-01T00:00:00+00:00"
    # One recorded-tool-call list feeds both substrates: the core engine holds it
    # on the Trajectory, the runner's evaluators read its dump. A per-substrate
    # fixture could disagree with itself, which is the divergence this suite exists
    # to catch.
    recorded = [
        RecordedToolCall(
            call_id=f"call_{index}",
            sequence=index,
            tool_name=call["tool_name"],
            arguments=call["arguments"],
            executor=ToolExecutorIdentity(call["executor"]),
            output="",
            status=ToolExecutionStatus(call["status"]),
            latency_seconds=0.0,
            timestamp=now,
        )
        for index, call in enumerate(fixture["tool_calls"])
    ]
    trajectory = Trajectory(
        task_id=pack_dir.name,
        trial_index=0,
        start_ts=now,
        end_ts=now,
        messages=messages,
        tool_log=recorded,
    )
    return _TrialCase(
        core_trajectory=trajectory,
        runner_messages=fixture["messages"],
        runner_tool_history=[call.model_dump() for call in recorded],
        state=fixture["state"],
    )


class _FixtureStateDBClient:
    """Serves a table map where a real trial reads it from the DB service.

    ``StateResponse.data`` is the trial's ``table -> rows`` map, which the fixture
    holds under ``state.db`` — the same level ``build_check_context`` picks for the
    core engine. Both substrates therefore read one set of rows, so a score
    difference can only come from the grading path itself.
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    async def get_state(self, trial_id: str) -> runner_models.StateResponse:
        return runner_models.StateResponse(
            data=self._tables, version=1, full_hash="", stable_hash=""
        )


def _core_verdict(
    family: str, grading_config: core_models.GradingConfig, case: _TrialCase, task_dir: Path
) -> tuple[float, float]:
    """(component score, combined score) from the core engine's real combine."""
    grade = GradingEngine(grading_config, task_dir=task_dir).grade_trajectory(
        case.core_trajectory, case.state
    )
    component = getattr(grade.components, family, None)
    assert component is not None, (
        f"the core engine produced no {family!r} component — either that family has no "
        "core GradeComponents slot or the fixture never exercised it"
    )
    return component, grade.score


def _runner_custom_checks_score(
    task_description: runner_models.TaskDescription, case: _TrialCase
) -> float:
    """The runner's ``custom_checks`` component, via its real delivery + executor.

    ``checks.py`` reaches the runner as a base64 ``tool_artifacts`` entry, so the
    extraction step is part of the path under test: a pack the adapter failed to
    bundle scores nothing here rather than passing on a directory the test handed
    over. ``shutdown`` then removes the temp dir extraction created.
    """
    servicer = RunnerServiceImpl(db_client=_FixtureStateDBClient(case.state["db"]))
    try:
        trial_id = f"{task_description.task_id}:0"
        servicer._extract_tool_artifacts(trial_id, task_description.tool_artifacts)
        context = TrialContextRuntime(trial_id=trial_id, task_description=task_description)
        score, _ = servicer._run_async(
            servicer._grade_custom_checks(trial_id, context, case.runner_messages)
        )
        return score
    finally:
        servicer.shutdown()


def _runner_verdict(
    family: str, task_description: runner_models.TaskDescription, case: _TrialCase
) -> tuple[float, float]:
    """(component score, combined score) from the runner's real evaluators."""
    grading = task_description.grading
    if family == "state_checks":
        component, _ = evaluate_jsonpath_checks(
            grading.state_checks.jsonpath_checks, state=case.state
        )
        components = {"jsonpath_score": component}
    elif family == "transcript_rules":
        result = evaluate_transcript_rules(
            case.runner_messages, case.runner_tool_history, grading.transcript_rules.model_dump()
        )
        component = result.score
        components = {"transcript_score": component}
    elif family == "custom_checks":
        component = _runner_custom_checks_score(task_description, case)
        components = {"custom_checks_score": component}
    else:
        pytest.fail(
            f"no runner differential driver for the {family!r} family — add one rather "
            "than silently skipping the key"
        )
    combined, _ = combine_grade_components(components, grading.model_dump())
    return component, combined


# --------------------------------------------------------------------------
# 1. Every declared config field is claimed by exactly one manifest entry
# --------------------------------------------------------------------------


def test_manifest_covers_every_declared_config_field():
    containers: set[str] = set()
    for substrate in _SUBSTRATE_ROOTS:
        leaves, substrate_containers, registry = _walk(substrate)
        containers |= substrate_containers
        claims = _claimed_fields(substrate)

        unclaimed = sorted(leaf for leaf in leaves if leaf not in claims)
        assert not unclaimed, (
            f"{substrate} grading config declares fields with no key_manifest entry: "
            f"{unclaimed}. Add a GradingKey for each — a field neither substrate's "
            "manifest claims is a key that can silently no-op."
        )

        duplicated = {path: keys for path, keys in claims.items() if len(keys) > 1}
        assert not duplicated, f"{substrate} fields claimed by more than one entry: {duplicated}"

        for path, keys in sorted(claims.items()):
            model_name, _, field_name = path.partition(".")
            model = registry.get(model_name)
            assert model is not None, (
                f"{keys[0]}: {substrate}_field {path!r} names {model_name!r}, which is not "
                f"reachable from the {substrate} GradingConfig"
            )
            assert field_name in model.model_fields, (
                f"{keys[0]}: {substrate}_field {path!r} does not resolve — "
                f"{model_name} has no field {field_name!r}"
            )

        for item in GRADING_KEYS:
            dict_key = _dict_key_of(item, substrate)
            field = _field_of(item, substrate)
            if dict_key is None or field is None:
                continue
            model_name, _, field_name = field.partition(".")
            annotation = registry[model_name].model_fields[field_name].annotation
            assert _is_dict_field(annotation), (
                f"{item.author_key}: {substrate}_dict_key {dict_key!r} requires "
                f"{field!r} to be a dict field, got {annotation}"
            )

    assert containers == _CONTAINER_FIELDS, (
        "the set of grading config fields walked into as containers changed. Every "
        "container's leaves must be claimed individually; a new container here means "
        "a new key family landed on a substrate."
    )


# --------------------------------------------------------------------------
# 2. The exemption sets are frozen and correctly classified
# --------------------------------------------------------------------------


def test_exemption_sets_are_frozen_and_classified(test_data_dir):
    single_substrate = {
        item.author_key for item in GRADING_KEYS if not item.coverage.startswith("BOTH")
    }
    assert single_substrate == _ARCHITECTURAL_EXEMPTIONS | _DRIFT_EXEMPTIONS, (
        "a key stopped (or started) claiming both substrates. Classify it as "
        "architectural (never both) or drift (should be both, with a tracking issue)."
    )
    assert not _ARCHITECTURAL_EXEMPTIONS & _DRIFT_EXEMPTIONS

    for key in sorted(_ARCHITECTURAL_EXEMPTIONS):
        item = entry(key)
        assert item.tracking_issue is None, (
            f"{key} is declared architectural but carries tracking issue "
            f"#{item.tracking_issue} — an exemption that will be fixed is drift"
        )
        assert item.reason.strip(), f"{key}: architectural exemptions must say why"

    for key in sorted(_DRIFT_EXEMPTIONS):
        assert entry(key).tracking_issue is not None, (
            f"{key} is declared drift rather than architectural, so it must name the "
            "GitHub issue that closes the gap"
        )

    unproven_shared_scored = {
        item.author_key
        for item in GRADING_KEYS
        if item.kind is KeyKind.SCORED_CHECK
        and item.coverage.startswith("BOTH")
        and item.enforcement is not Enforcement.DIFFERENTIAL_CANONICAL
    }
    assert unproven_shared_scored == _NON_DIFFERENTIAL_SCORED_KEYS, (
        "a scored key claims both substrates without an in-process differential. "
        "That is the shape that lets a key land on one substrate with a dead field "
        "on the other and still pass CI — widen this set only deliberately."
    )

    untracked_field_resolution = {
        item.author_key
        for item in GRADING_KEYS
        if item.enforcement is Enforcement.FIELD_RESOLUTION_ONLY and item.tracking_issue is None
    }
    assert untracked_field_resolution == _NON_TRACKED_FIELD_RESOLUTION_KEYS, (
        "every FIELD_RESOLUTION_ONLY entry outside this set must carry a tracking "
        "issue naming what would prove the claim"
    )

    for item in GRADING_KEYS:
        if item.enforcement is Enforcement.DIFFERENTIAL_INTEGRATION:
            assert (_REPO_ROOT / item.enforcing_test).is_file(), (
                f"{item.author_key}: enforcing_test {item.enforcing_test!r} does not exist "
                "on disk, so nothing proves the integration differential"
            )
        for evaluator in (item.core_evaluator, item.runner_evaluator):
            if evaluator is None:
                continue
            assert callable(_import_dotted(evaluator)), (
                f"{item.author_key}: declared evaluator {evaluator!r} resolved to "
                "something that is not callable"
            )

    differential = _differential_entries()
    assert differential, "the differential predicate matched nothing — test 3 would be vacuous"
    for item in differential:
        assert item.core_evaluator and item.runner_evaluator, (
            f"{item.author_key} claims a canonical differential but does not name both "
            "substrates' evaluators"
        )
        pack = _pack_dir(test_data_dir, item.author_key)
        missing = [name for name in ("grading.yaml", "trial.yaml") if not (pack / name).is_file()]
        assert not missing, (
            f"{item.author_key} claims a canonical differential but its fixture pack "
            f"{pack} is missing {missing}"
        )


# --------------------------------------------------------------------------
# 3. Both substrates discriminate every shared scored key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("author_key", [item.author_key for item in _differential_entries()])
def test_both_substrates_discriminate_each_shared_scored_key(author_key, test_data_dir, tmp_path):
    item = entry(author_key)
    family = author_key.split(".")[0]
    task_id = _task_id_for(author_key)
    pack = _pack_dir(test_data_dir, author_key)
    adapter = _parity_adapter(test_data_dir)

    declared = _declared_author_keys(yaml.safe_load((pack / "grading.yaml").read_text()))
    assert {key for key in declared if not key.startswith("combine.")} == {author_key}, (
        f"{author_key}'s fixture declares more than the key under test, so a violating "
        f"trial could discriminate without that key being read: {sorted(declared)}"
    )

    core_config = adapter.get_grading_config(task_id)
    task_description = adapter.to_task_description(task_id)
    satisfying = _load_case(pack, "satisfying")
    violating = _load_case(pack, "violating")
    # The core engine imports the pack's ``checks.py`` from its task dir, which
    # writes ``__pycache__`` beside the fixture; grade from a copy so a canonical
    # run leaves the repo clean.
    core_task_dir = tmp_path / "core_task_dir"
    shutil.copytree(pack, core_task_dir)

    core_ok, core_ok_total = _core_verdict(family, core_config, satisfying, core_task_dir)
    core_bad, core_bad_total = _core_verdict(family, core_config, violating, core_task_dir)
    runner_ok, runner_ok_total = _runner_verdict(family, task_description, satisfying)
    runner_bad, runner_bad_total = _runner_verdict(family, task_description, violating)

    assert core_ok > core_bad, (
        f"the core substrate does not discriminate {author_key}: satisfying "
        f"{core_ok} vs violating {core_bad}"
    )
    assert runner_ok > runner_bad, (
        f"the runner substrate does not discriminate {author_key}: satisfying "
        f"{runner_ok} vs violating {runner_bad}"
    )
    assert core_ok_total > core_bad_total
    assert runner_ok_total > runner_bad_total

    if item.coverage is SubstrateCoverage.BOTH_SCORE_PARITY:
        assert core_ok == pytest.approx(runner_ok)
        assert core_bad == pytest.approx(runner_bad), (
            f"{author_key} claims BOTH_SCORE_PARITY but the substrates score the "
            f"violating trial differently: core {core_bad} vs runner {runner_bad}"
        )


# --------------------------------------------------------------------------
# 4. Adapter translation carries every key both substrates declare
# --------------------------------------------------------------------------


def test_adapter_translation_carries_every_runner_key(test_data_dir):
    pack = test_data_dir / "grading_parity" / _ALL_KEYS_TASK
    declared = _declared_author_keys(yaml.safe_load((pack / "grading.yaml").read_text()))
    authorable = {item.author_key for item in GRADING_KEYS if item.core_field is not None}
    assert declared == authorable, (
        f"{pack / 'grading.yaml'} must declare every manifest key the core config "
        f"accepts; missing {sorted(authorable - declared)}"
    )

    grading = _parity_adapter(test_data_dir).to_task_description(_ALL_KEYS_TASK).grading
    owners: dict[str, BaseModel | None] = {
        "GradingConfig": grading,
        "StateChecksConfig": grading.state_checks,
        "TranscriptRulesConfig": grading.transcript_rules,
    }

    for item in GRADING_KEYS:
        if item.core_field is None or item.runner_field is None:
            continue
        model_name, _, field_name = item.runner_field.partition(".")
        owner = owners.get(model_name)
        assert owner is not None, (
            f"{item.author_key}: adapter translation produced no {model_name} to carry "
            f"{item.runner_field!r}"
        )
        actual = getattr(owner, field_name)
        default = type(owner).model_fields[field_name].get_default(call_default_factory=True)
        assert actual != default, (
            f"{item.author_key} is declared in {pack.name}/grading.yaml but arrives at "
            f"the runner as the default {default!r} — NativeAdapter.to_task_description "
            f"drops it on the floor"
        )


# --------------------------------------------------------------------------
# 5. Every ledger key resolves in the runner config dump and has a recording site
# --------------------------------------------------------------------------


def test_every_ledger_key_resolves_in_the_runner_config_dump():
    runner_dump = runner_models.GradingConfig(
        state_checks=runner_models.StateChecksConfig(),
        transcript_rules=runner_models.TranscriptRulesConfig(),
    ).model_dump()

    resolvable = [item for item in LEDGER_KEYS if item.runner_field is not None]
    assert resolvable, (
        "no ledger key names a runner field, so the runtime accounted-keys ledger "
        "would let every populated scored key through unchecked"
    )

    accountable = accountable_author_keys()
    for item in resolvable:
        path = runner_dump_path(item)
        node: Any = runner_dump
        for segment in path[:-1]:
            node = node[segment]
        assert path[-1] in node, (
            f"{item.author_key}: runner_field {item.runner_field!r} resolves to "
            f"{'.'.join(path)}, which the runner GradingConfig dump does not contain — "
            "the ledger would never see the key as populated"
        )
        assert item.author_key in accountable, (
            f"{item.author_key} reaches the runner but no recording site in the grading "
            "path claims it, so every task populating it would fail GradeTrial. Add an "
            "evaluate-or-skip site in _grade_trial_async (or the evaluator it calls) and "
            "list the key in grading_ledger.accountable_author_keys"
        )
