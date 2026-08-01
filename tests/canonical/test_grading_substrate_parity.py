"""Substrate-parity guard rail for the grading key manifest.

Nine locks over :mod:`tolokaforge.core.grading.key_manifest`:

1. every field either substrate's grading config declares is claimed by exactly
   one manifest entry, and every claimed field resolves;
2. the exemption sets are frozen here — in the test module, never beside the
   manifest data they guard — so widening one is a reviewable edit, every entry
   matching lock 3's predicate names both evaluators and owns a fixture, and every
   ``DIFFERENTIAL_INTEGRATION`` claim's ``enforcing_test`` nodeid resolves to a test
   function pytest would collect;
3. every key claiming both substrates at ``DIFFERENTIAL_CANONICAL`` demonstrably
   moves both substrates' component scores, through each substrate's real
   production evaluator and its real combine;
4. every key both substrates declare survives adapter translation;
5. every ledger key's ``runner_field`` resolves to a place in the runner
   ``GradingConfig`` dump *and* some recording site in the grading path claims
   it, so a malformed or unclaimed entry fails here rather than at grade time in
   production;
6. both substrates fold a hash verdict and a JSONPath score into one
   ``state_checks`` component by the author's weight, pinned cell by cell to
   arithmetic this module computes for itself;
7. the hash verdict either substrate can produce is binary, which is what makes
   lock 6's canonical-tier hash inputs the only values that path yields rather
   than a stand-in for it;
8. every ``DIFFERENTIAL_CANONICAL`` claim lock 3's predicate cannot reach is
   enumerated here, and the two tables those claims rest on — lock 6's weight sweep
   and lock 9's method answers — stay substantive;
9. both substrates aggregate one split pair of deterministic components by the
   author's ``combine.method``, each method pinned to a score written out here.

The exemption sets and the differential fixtures are the enforcement mechanism:
adding a grading key to one substrate only cannot pass this suite without an
explicit, reviewable edit to one of the frozen constants below.

Locks 3, 6, 7 and 9 drive a real trial, and each reads it through one fixture
loader, so what a ``grading_parity`` pack can express bounds what they can prove.
That loader's contract — a tool call belongs to the message that requested it, and
carries that call's own result text — is locked at the end of this module.
"""

import ast
import importlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel

from tests.utils.combine_method_verdicts import (
    COMBINE_METHOD_COMPONENTS,
    COMBINE_METHOD_PASS_THRESHOLD,
    COMBINE_METHOD_VERDICTS,
)
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import models as core_models
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.combine_method import COMBINE_METHODS
from tolokaforge.core.grading.key_manifest import (
    GRADING_KEYS,
    Enforcement,
    GradingKey,
    KeyKind,
    SubstrateCoverage,
    author_keys,
    entry,
    family_author_keys,
)
from tolokaforge.core.grading.state_checks import StateChecker, extract_db_state
from tolokaforge.core.grading.trace_timeline import TrialTimeline, build_trial_timeline
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
    resolve_state_checks_component,
)
from tolokaforge.runner.grading_ledger import (
    LEDGER_KEYS,
    accountable_author_keys,
    runner_dump_path,
)
from tolokaforge.runner.service import (
    RunnerServiceImpl,
    TrialContextRuntime,
    _build_runner_check_transcript,
)

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PARITY_GLOB = "grading_parity/**/task.yaml"
_ALL_KEYS_TASK = "all_keys"

COMPOSITION_PARITY_WEIGHTS: tuple[float, ...] = (0.0, 0.25, 0.6, 1.0)
"""The ``state_checks.hash.weight`` values lock 6 drives both substrates at.

The endpoints pin the two single-source limits — ``0.0`` scores the JSONPath
assertions alone, ``1.0`` the hash verdict alone. The interior weights are where a
fold that *selects* the dominant source instead of mixing the two diverges from
``j(1-w) + hw``, and the endpoints cannot see that: a rule returning ``j`` below
``w=0.5`` and ``h`` above it reproduces the blend at both ends. Lock 8 holds the
sweep to at least two of them.
"""

_COMPOSITION_KEY = "state_checks.hash.weight"

# The fixture satisfies one of its two assertions on both trial cases, so the
# JSONPath half of the fold is strictly partial and the blend is distinguishable
# from every rule that agrees with it at 0 and 1.
_COMPOSITION_JSONPATH_SCORE = 0.5

# Case name -> the hash verdict it produces against the pack's committed
# expected_state_hash. Lock 7 asserts core's evaluator really returns these.
_COMPOSITION_HASH_CASES: tuple[tuple[str, float], ...] = (
    ("hash_matching", 1.0),
    ("hash_diverging", 0.0),
)

_METHOD_KEY = "combine.method"
_METHOD_CASE = "split_components"

_HASH_SCORE_NAME = "hash_score"
_BINARY_HASH_VERDICT = frozenset({0.0, 1.0})

_HASH_FAMILY_ROOT = "state_checks.hash"

# Every function that can hand a hash verdict to the shared composer, as
# (repo-relative module, function name). Asserted as set equality against the
# hash family's declared evaluators, so a fourth producer forces an edit here
# instead of landing with lock 7 green and lock 6's binariness premise false.
_HASH_VERDICT_PRODUCERS = frozenset(
    {
        ("tolokaforge/core/grading/state_checks.py", "check_hash"),
        ("tolokaforge/core/grading/state_checks.py", "check_hash_against_golden_replay"),
        ("tolokaforge/runner/service.py", "_execute_hash_grading"),
    }
)

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
_DRIFT_EXEMPTIONS = frozenset({"state_checks.hash.expected_state_hash"})

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

# DIFFERENTIAL_CANONICAL entries lock 3's predicate does not reach, because it
# selects kind: SCORED_CHECK and these carry no component score of their own. Each
# one needs a differential of its own in this module; lock 8 holds the set.
_CANONICAL_DIFFERENTIALS_OUTSIDE_LOCK_3 = frozenset(
    {
        "state_checks.hash.weight",
        "combine.method",
    }
)

# FIELD_RESOLUTION_ONLY entries that need no tracking issue: aggregation and
# load-time config inputs, which have no violating trajectory by construction.
_NON_TRACKED_FIELD_RESOLUTION_KEYS = frozenset(
    {
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


def _assert_enforcing_test_is_collectable(item: GradingKey) -> None:
    """The named integration test exists as a function pytest would collect.

    Resolved by parsing the module, never by importing it: the integration tier
    pulls testcontainers and a docker daemon, neither of which the canonical tier
    has. That is also the limit of what this can prove — the nodeid resolves and is
    collectable; whether it *passes* is what running the integration tier answers.
    """
    module_path, separator, function_name = item.enforcing_test.partition("::")
    assert separator, (
        f"{item.author_key}: enforcing_test {item.enforcing_test!r} is a module path, not a "
        "pytest nodeid, so it names no test function"
    )
    module_file = _REPO_ROOT / module_path
    assert module_file.is_file(), (
        f"{item.author_key}: enforcing_test module {module_path!r} does not exist on disk, "
        "so nothing proves the integration differential"
    )
    declared = {
        node.name
        for node in ast.parse(module_file.read_text()).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    collectable = sorted(name for name in declared if name.startswith("test_"))
    assert function_name in declared, (
        f"{item.author_key}: enforcing_test names {function_name!r}, which {module_path} does "
        f"not declare at module level. It declares {collectable}"
    )
    assert function_name.startswith("test_"), (
        f"{item.author_key}: enforcing_test names {function_name!r}, which pytest does not "
        "collect as a test"
    )


def _split_dotted(path: str) -> tuple[Any, list[str]]:
    """A dotted path's longest importable module prefix, and the attributes after it."""
    parts = path.split(".")
    for boundary in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:boundary]))
        except ImportError:
            continue
        return module, parts[boundary:]
    raise ImportError(f"no importable module prefix in {path!r}")


def _import_dotted(path: str) -> Any:
    """Resolve a dotted module/attribute path, longest importable prefix first."""
    module, attributes = _split_dotted(path)
    resolved: Any = module
    for attribute in attributes:
        resolved = getattr(resolved, attribute)
    return resolved


def _evaluator_source(evaluator: str) -> tuple[str, str]:
    """A declared evaluator's source location: (repo-relative module, function name)."""
    module, attributes = _split_dotted(evaluator)
    assert attributes, f"{evaluator!r} names a module, not a function with source to read"
    return str(Path(module.__file__).resolve().relative_to(_REPO_ROOT)), attributes[-1]


def _declared_hash_verdict_producers() -> frozenset[tuple[str, str]]:
    """Every evaluator the manifest names for a *scored* member of the hash family.

    ``state_checks.hash.weight`` is ``CONFIG_INPUT`` — it names the composer that
    consumes a verdict, not a function that produces one — so the ``SCORED_CHECK``
    filter is what keeps the fold itself out of the audit.
    """
    return frozenset(
        _evaluator_source(evaluator)
        for author_key in family_author_keys(_HASH_FAMILY_ROOT)
        for evaluator in (entry(author_key).core_evaluator, entry(author_key).runner_evaluator)
        if evaluator is not None and entry(author_key).kind is KeyKind.SCORED_CHECK
    )


# --------------------------------------------------------------------------
# Fixture-pack helpers
# --------------------------------------------------------------------------

# The one shape a ``trial.yaml`` case may take. Every key is read; anything else
# is rejected, so a pack cannot carry a field the loader silently drops.
_CASE_KEYS = frozenset({"messages", "state"})
_MESSAGE_KEYS = frozenset({"role", "content", "tool_calls"})
_CALL_KEYS = frozenset({"tool_name", "executor", "status", "arguments", "output"})

_FIXTURE_TIMESTAMP = "2026-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class _TrialCase:
    """One satisfying-or-violating trial, in each substrate's own input shape."""

    core_trajectory: Trajectory
    runner_messages: list[dict[str, Any]]
    runner_timeline: TrialTimeline
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


def _reject_unknown(authored: dict[str, Any], allowed: frozenset[str], *, what: str) -> None:
    """A fixture key nothing reads expresses less than its author wrote — so it is an error."""
    unknown = sorted(set(authored) - allowed)
    assert not unknown, f"{what} declares {unknown}; the loader reads only {sorted(allowed)}"


def _authored_call(raw_call: dict[str, Any], *, sequence: int) -> tuple[ToolCall, RecordedToolCall]:
    """One authored call, as the message view declares it and as the record kept it.

    ``latency_seconds`` is not authorable and is pinned at ``0.0``: wall time is
    not compared across substrates, so a fixture varying it would pin a number no
    parity claim reads.
    """
    call_id = f"call_{sequence}"
    return (
        ToolCall(id=call_id, name=raw_call["tool_name"], arguments=raw_call["arguments"]),
        RecordedToolCall(
            call_id=call_id,
            sequence=sequence,
            tool_name=raw_call["tool_name"],
            arguments=raw_call["arguments"],
            executor=ToolExecutorIdentity(raw_call["executor"]),
            output=raw_call.get("output", ""),
            status=ToolExecutionStatus(raw_call["status"]),
            latency_seconds=0.0,
            timestamp=_FIXTURE_TIMESTAMP,
        ),
    )


def _wire_message(message: Message) -> dict[str, Any]:
    """One turn as the runner receives it, in ``llm_messages_json``'s OpenAI shape."""
    wire: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    return wire


def _load_case(pack_dir: Path, case: str) -> _TrialCase:
    """One authored trial, in the input shape each substrate really takes.

    A call is authored inside the message that requested it, so a fixture places
    its calls across turns and the timeline's ``turn_index`` and event order follow
    what the author wrote.
    """
    fixture = yaml.safe_load((pack_dir / "trial.yaml").read_text())[case]
    where = f"{pack_dir.name}/trial.yaml case {case!r}"
    _reject_unknown(fixture, _CASE_KEYS, what=where)

    # One recorded-tool-call list feeds both substrates: the core engine holds it
    # on the Trajectory, the runner's evaluators read its dump. A per-substrate
    # fixture could disagree with itself, which is the divergence this suite exists
    # to catch. The message view declares every one of them, or the trial's two
    # views disagree and neither substrate will grade it.
    view: list[Message] = []
    recorded: list[RecordedToolCall] = []
    for index, raw in enumerate(fixture["messages"]):
        _reject_unknown(raw, _MESSAGE_KEYS, what=f"{where} message {index}")
        declared: list[ToolCall] = []
        for raw_call in raw.get("tool_calls", ()):
            _reject_unknown(raw_call, _CALL_KEYS, what=f"{where} message {index} tool call")
            call, record = _authored_call(raw_call, sequence=len(recorded))
            declared.append(call)
            recorded.append(record)
        view.append(Message(role=raw["role"], content=raw["content"], tool_calls=declared or None))

    trajectory = Trajectory(
        task_id=pack_dir.name,
        trial_index=0,
        start_ts=_FIXTURE_TIMESTAMP,
        end_ts=_FIXTURE_TIMESTAMP,
        messages=view,
        tool_log=recorded,
    )
    return _TrialCase(
        core_trajectory=trajectory,
        runner_messages=[_wire_message(message) for message in view],
        runner_timeline=build_trial_timeline(view, recorded, None),
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
            case.runner_timeline, grading.transcript_rules.model_dump()
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
            _assert_enforcing_test_is_collectable(item)
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


# --------------------------------------------------------------------------
# 6. Both substrates fold two state sources by the author's weight
# --------------------------------------------------------------------------


def _interior_composition_weights() -> tuple[float, ...]:
    """The sweep's weights strictly inside ``(0, 1)``."""
    return tuple(weight for weight in COMPOSITION_PARITY_WEIGHTS if 0.0 < weight < 1.0)


@dataclass(frozen=True)
class _CompositionVerdict:
    """One (weight, hash verdict) cell of the composition sweep, both substrates."""

    core_component: float | None
    core_total: float
    core_jsonpath: float
    runner_component: float | None
    runner_total: float
    runner_jsonpath: float


def _composition_verdict(
    test_data_dir: Path, tmp_path: Path, *, weight: float, case: str, hash_score: float
) -> _CompositionVerdict:
    """Drive both substrates over the composition pack at one weight.

    The pack is copied and its ``state_checks.hash.weight`` rewritten per cell, so
    every cell crosses the whole load path — YAML, the shared load gate, and
    ``NativeAdapter.to_task_description`` — rather than a config mutated after
    validation. Core's hash verdict is the real one: the pack commits the
    ``expected_state_hash`` of its ``hash_matching`` state, so ``check_hash``
    produces the verdict in process. The runner's is handed in, because its hash
    evaluator drives db-service over HTTP — honest only because that verdict is
    binary, which lock 7 holds, and because the runner producing it for itself is
    proven at the integration tier by the hash family's ``enforcing_test``.
    """
    pack = _pack_dir(test_data_dir, _COMPOSITION_KEY)
    root = tmp_path / f"weight_{weight}_{case}"
    task_dir = root / "grading_parity" / pack.name
    shutil.copytree(pack, task_dir)
    grading_path = task_dir / "grading.yaml"
    authored = yaml.safe_load(grading_path.read_text())
    authored["state_checks"]["hash"]["weight"] = weight
    grading_path.write_text(yaml.safe_dump(authored))

    adapter = _parity_adapter(root)
    task_id = _task_id_for(_COMPOSITION_KEY)
    core_config = adapter.get_grading_config(task_id)
    runner_grading = adapter.to_task_description(task_id).grading
    trial = _load_case(pack, case)

    core_component, core_total = _core_verdict("state_checks", core_config, trial, task_dir)
    core_jsonpath, _ = StateChecker().check_jsonpaths(
        trial.state, core_config.state_checks.jsonpaths
    )
    runner_jsonpath, _ = evaluate_jsonpath_checks(
        runner_grading.state_checks.jsonpath_checks, state=trial.state
    )
    runner_component = resolve_state_checks_component(
        hash_score=hash_score,
        jsonpath_score=runner_jsonpath,
        db_probe_score=-1.0,
        hash_weight=runner_grading.state_checks.hash_weight,
    ).component
    runner_total, _ = combine_grade_components(
        {"hash_score": hash_score, "jsonpath_score": runner_jsonpath},
        runner_grading.model_dump(),
    )
    return _CompositionVerdict(
        core_component=core_component,
        core_total=core_total,
        core_jsonpath=core_jsonpath,
        runner_component=runner_component,
        runner_total=runner_total,
        runner_jsonpath=runner_jsonpath,
    )


@pytest.mark.parametrize("weight", COMPOSITION_PARITY_WEIGHTS)
@pytest.mark.parametrize(("case", "hash_score"), _COMPOSITION_HASH_CASES)
def test_both_substrates_compose_one_state_checks_score(
    weight, case, hash_score, test_data_dir, tmp_path
):
    """Each substrate's composite is ``j(1-w) + hw``, computed here rather than compared.

    One shared composer means cross-substrate equality holds by construction and
    proves nothing on its own — two substrates calling one constant-returning
    function agree perfectly. What a cell proves is routing: that each substrate's
    production path reaches that function with the right state root, the right
    not-evaluated mapping, and the author's weight out of config.
    """
    verdict = _composition_verdict(
        test_data_dir, tmp_path, weight=weight, case=case, hash_score=hash_score
    )
    expected = _COMPOSITION_JSONPATH_SCORE * (1.0 - weight) + hash_score * weight

    for substrate, jsonpath_score in (
        ("core", verdict.core_jsonpath),
        ("runner", verdict.runner_jsonpath),
    ):
        assert 0.0 < jsonpath_score < 1.0, (
            f"the {substrate} substrate scored the composition fixture's assertions "
            f"{jsonpath_score}, not strictly inside (0, 1). A saturated JSONPath half makes "
            "the fold indistinguishable from the rules it must be told apart from — and a "
            "0.0 usually means the assertions no longer resolve against $.db.<table>"
        )
        assert jsonpath_score == pytest.approx(_COMPOSITION_JSONPATH_SCORE), (
            f"the {substrate} substrate scores the fixture's assertions {jsonpath_score}, so "
            "the blend this test pins each composite to is computed from the wrong j"
        )

    for substrate, component, total in (
        ("core", verdict.core_component, verdict.core_total),
        ("runner", verdict.runner_component, verdict.runner_total),
    ):
        assert isinstance(component, float), (
            f"the {substrate} substrate left the state_checks component {component!r} with "
            "both sources configured, so an equality against the other substrate would "
            "compare two unscored components and pass"
        )
        assert 0.0 <= component <= 1.0, (
            f"the {substrate} substrate scored state_checks {component}, which is outside "
            "[0, 1] and therefore not a score any combine can normalise"
        )
        assert component == pytest.approx(expected), (
            f"the {substrate} substrate folded hash {hash_score} with jsonpath "
            f"{_COMPOSITION_JSONPATH_SCORE} at weight {weight} into {component}, not the "
            f"blend {expected}"
        )
        assert total == pytest.approx(expected), (
            f"the {substrate} substrate reports state_checks {component} but scores the "
            f"trial {total} — the composite does not reach the final score"
        )


def test_the_composite_moves_with_the_weight_at_a_fixed_hash_verdict(test_data_dir, tmp_path):
    """The one assertion that survives this module's own arithmetic being wrong.

    Lock 6 pins each composite against a blend computed here, so a fold rule
    reverted in production *and* in that computation together passes every cell.
    This observes only that the composite responds to the weight, which no
    weight-independent rule satisfies however the test-side arithmetic drifts.

    Driven at the sweep's interior weights: ``0.0`` and ``1.0`` collapse the blend
    onto a single source, which a rule merely selecting the dominant source
    reproduces exactly.
    """
    interior = _interior_composition_weights()
    assert len(interior) >= 2, (
        f"COMPOSITION_PARITY_WEIGHTS {COMPOSITION_PARITY_WEIGHTS} holds fewer than two "
        "weights strictly inside (0, 1), so this test compares nothing"
    )

    for case, hash_score in _COMPOSITION_HASH_CASES:
        verdicts = [
            _composition_verdict(
                test_data_dir, tmp_path, weight=weight, case=case, hash_score=hash_score
            )
            for weight in interior
        ]
        for substrate, composites in (
            ("core", [verdict.core_component for verdict in verdicts]),
            ("runner", [verdict.runner_component for verdict in verdicts]),
        ):
            assert len(set(composites)) == len(composites), (
                f"the {substrate} substrate scored {case} identically at weights {interior}: "
                f"{composites}. The weight the author wrote does not reach the fold"
            )


# --------------------------------------------------------------------------
# 7. The hash verdict is binary, on both substrates
# --------------------------------------------------------------------------


def _verdict_constants(expression: ast.expr) -> frozenset[float] | None:
    """The values a hash-score expression can hold, or ``None`` if it computes one.

    ``None`` is the interesting answer: a producer that derives a hash score instead
    of choosing between two literals would make lock 6's ``0.0``/``1.0`` runner
    inputs a stand-in for a value the path never yields.
    """
    if isinstance(expression, ast.Constant) and isinstance(expression.value, (int, float)):
        return None if isinstance(expression.value, bool) else frozenset({float(expression.value)})
    if isinstance(expression, ast.IfExp):
        branches = (_verdict_constants(expression.body), _verdict_constants(expression.orelse))
        if any(branch is None for branch in branches):
            return None
        return frozenset().union(*branches)
    if isinstance(expression, ast.Name) and expression.id == _HASH_SCORE_NAME:
        return frozenset()
    return None


def _verdict_expression(node: ast.AST) -> ast.expr | None:
    """The expression ``node`` puts in the hash-score position, or ``None``.

    Three shapes carry a verdict out of a producer: the first element of a returned
    tuple (the ``(score, reason)`` pair both core producers return), a ``hash_score``
    keyword argument (the runner returns its verdict inside a model), and an
    assignment to ``hash_score``, which the other two then hand on.
    """
    if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
        return node.value.elts[0]
    named = isinstance(node, ast.keyword) and node.arg == _HASH_SCORE_NAME
    assigned = (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == _HASH_SCORE_NAME
    )
    return node.value if named or assigned else None


def _sole_function(module_path: str, function_name: str) -> ast.AST:
    tree = ast.parse((_REPO_ROOT / module_path).read_text())
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    assert len(found) == 1, (
        f"{module_path} declares {len(found)} functions named {function_name!r}, so the "
        "hash-verdict audit cannot say which one produces the verdict"
    )
    return found[0]


def _carries_a_verdict(exit_node: ast.Return) -> bool:
    """Whether a ``return`` puts its verdict somewhere this audit can read it."""
    return any(_verdict_expression(node) is not None for node in ast.walk(exit_node))


def _reachable_hash_verdicts(module_path: str, function_name: str) -> frozenset[float]:
    """Every value the named producer can hand on as a hash score.

    Fails when a score position holds a computed expression rather than a choice
    between literals: a derived partial verdict would make lock 6's ``0.0``/``1.0``
    runner inputs a stand-in for values that path never yields. Fails too when the
    producer leaves by a ``return`` whose verdict sits outside the three positions
    :func:`_verdict_expression` reads — otherwise a refactor to ``return result``
    routes the verdict past the audit while the literals it left behind keep the
    binariness assertion green.
    """
    producer = _sole_function(module_path, function_name)
    constants: set[float] = set()
    for node in ast.walk(producer):
        expression = _verdict_expression(node)
        if expression is None:
            continue
        reachable = _verdict_constants(expression)
        assert reachable is not None, (
            f"{module_path}::{function_name} computes a hash score at line "
            f"{expression.lineno} instead of choosing between literals"
        )
        constants |= reachable

    unaudited = [
        node.lineno
        for node in ast.walk(producer)
        if isinstance(node, ast.Return) and not _carries_a_verdict(node)
    ]
    assert not unaudited, (
        f"{module_path}::{function_name} returns at lines {unaudited} without putting a "
        "verdict in a position this audit reads — the first element of a returned tuple, "
        f"an assignment to {_HASH_SCORE_NAME}, or a {_HASH_SCORE_NAME}= keyword. The "
        "literals it leaves behind would keep the binariness assertion green while the "
        "verdict it actually returns went unread"
    )
    return frozenset(constants)


def test_the_hash_verdict_is_binary_on_both_substrates(test_data_dir):
    """Read from the source, because neither remaining producer is callable here.

    The runner's hash evaluator drives db-service over HTTP and core's golden-replay
    producer needs a task's MCP server, so a canonical-tier call reaches neither.
    What is callable is core's ``expected_state_hash`` branch, and the composition
    fixture's two cases are graded through it — which pins the two values lock 6
    hands the runner as the ones core's own evaluator returns for the same states.
    """
    producers = _declared_hash_verdict_producers()
    assert producers == _HASH_VERDICT_PRODUCERS, (
        "the set of functions the manifest names as hash-verdict producers changed. Every "
        "one is audited below, and lock 6 hands the runner's fold a 0.0/1.0 verdict on the "
        "strength of that audit — so widening this set is an edit a reviewer sees"
    )
    for module_path, function_name in sorted(producers):
        reachable = _reachable_hash_verdicts(module_path, function_name)
        assert reachable == _BINARY_HASH_VERDICT, (
            f"{module_path}::{function_name} can produce hash scores {sorted(reachable)}, "
            f"not {sorted(_BINARY_HASH_VERDICT)}"
        )

    pack = _pack_dir(test_data_dir, _COMPOSITION_KEY)
    expected_hash = yaml.safe_load((pack / "grading.yaml").read_text())["state_checks"]["hash"][
        "expected_state_hash"
    ]
    for case, hash_score in _COMPOSITION_HASH_CASES:
        db_state = extract_db_state(_load_case(pack, case).state)
        actual, _ = StateChecker().check_hash(db_state, expected_hash)
        assert actual == hash_score, (
            f"the composition fixture's {case!r} case scores {actual} against the pack's "
            f"committed expected_state_hash, not the {hash_score} lock 6 assumes"
        )


# --------------------------------------------------------------------------
# 8. Canonical differentials lock 3's predicate cannot reach
# --------------------------------------------------------------------------


def test_canonical_differentials_outside_lock_3_are_enumerated_and_substantive():
    """Lock 3 selects ``kind: SCORED_CHECK``, so ``CONFIG_INPUT`` and ``AGGREGATION`` escape it.

    Naming the test that proves such a claim would be a citation rather than a
    proof — the enforcement level would rest on a nodeid resolving while the test it
    names asserted nothing. So this asserts the property each escaped claim depends
    on instead: that lock 6's sweep still spans the weights where a fold rule is
    distinguishable at all, and that lock 9's answer table still spans the declared
    combine methods with one distinct score each. Membership alone enforces nothing:
    a differential deleted wholesale leaves the escaped set unchanged.
    """
    reached = {item.author_key for item in _differential_entries()}
    escaped = {
        item.author_key
        for item in GRADING_KEYS
        if item.enforcement is Enforcement.DIFFERENTIAL_CANONICAL and item.author_key not in reached
    }
    assert escaped == _CANONICAL_DIFFERENTIALS_OUTSIDE_LOCK_3, (
        "the set of DIFFERENTIAL_CANONICAL claims lock 3's predicate does not reach "
        "changed. Every entry here needs its own differential in this module — a claim "
        "that reaches neither lock 3 nor a lock named here is enforced by nothing"
    )

    interior = _interior_composition_weights()
    assert len(set(interior)) >= 2, (
        f"COMPOSITION_PARITY_WEIGHTS {COMPOSITION_PARITY_WEIGHTS} holds fewer than two "
        "distinct weights strictly inside (0, 1). At 0.0 and 1.0 the blend collapses onto a "
        f"single source, so {_COMPOSITION_KEY} would claim a canonical differential that a "
        "fold merely selecting the dominant source passes"
    )

    assert set(COMBINE_METHOD_VERDICTS) == set(COMBINE_METHODS), (
        f"COMBINE_METHOD_VERDICTS answers for {sorted(COMBINE_METHOD_VERDICTS)} but "
        f"COMBINE_METHODS declares {sorted(COMBINE_METHODS)}. A declared method with no "
        f"row here is a method with no cross-substrate evidence, so {_METHOD_KEY}'s "
        "canonical differential would span less than the domain it claims"
    )
    scores = {score for score, _ in COMBINE_METHOD_VERDICTS.values()}
    assert len(scores) == len(COMBINE_METHODS), (
        f"the declared methods are pinned to {sorted(scores)} — fewer scores than methods. "
        "An implementation returning one aggregation for every method satisfies a table "
        "whose rows agree"
    )


# --------------------------------------------------------------------------
# 9. Both substrates aggregate by the author's combine.method
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _MethodVerdict:
    """One substrate's aggregation of the combine-method pack at one method."""

    components: dict[str, float]
    score: float
    binary_pass: bool


def _author_method(test_data_dir: Path, root: Path, *, method: str) -> None:
    """Copy the combine-method pack under ``root`` with ``method`` authored into it.

    Rewriting the YAML rather than a loaded model puts the whole load path under test
    for both substrates — the author's key, the shared load gate, and each
    substrate's own translation of it — and hands them one file to disagree over.
    """
    shutil.copytree(_pack_dir(test_data_dir, _METHOD_KEY), _pack_dir(root, _METHOD_KEY))
    grading_path = _pack_dir(root, _METHOD_KEY) / "grading.yaml"
    authored = yaml.safe_load(grading_path.read_text())
    authored["combine"]["method"] = method
    grading_path.write_text(yaml.safe_dump(authored))


def _core_method_verdict(test_data_dir: Path, root: Path, *, method: str) -> _MethodVerdict:
    """Core's verdict on the authored pack, aggregated by the method it declares."""
    grading_config = _parity_adapter(root).get_grading_config(_task_id_for(_METHOD_KEY))
    assert grading_config.combine.method == method, (
        f"the core config loaded combine.method {grading_config.combine.method!r} from a "
        f"grading.yaml declaring {method!r}, so this cell measures a method nobody wrote"
    )
    assert grading_config.combine.pass_threshold == COMBINE_METHOD_PASS_THRESHOLD, (
        f"the pack's authored pass_threshold is {grading_config.combine.pass_threshold}, so "
        f"the flags pinned against {COMBINE_METHOD_PASS_THRESHOLD} answer a different question"
    )
    case = _load_case(_pack_dir(test_data_dir, _METHOD_KEY), _METHOD_CASE)
    grade = GradingEngine(grading_config, task_dir=_pack_dir(root, _METHOD_KEY)).grade_trajectory(
        case.core_trajectory, case.state
    )
    return _MethodVerdict(
        components={
            "state_checks": grade.components.state_checks,
            "transcript_rules": grade.components.transcript_rules,
        },
        score=grade.score,
        binary_pass=grade.binary_pass,
    )


def _runner_method_verdict(test_data_dir: Path, root: Path, *, method: str) -> _MethodVerdict:
    """The runner's verdict on the authored pack, from its real evaluators and combine.

    The method reaches the combine the way production sends it: translated onto
    ``TaskDescription.grading.combine_method`` and read off that model's dump.
    """
    grading = _parity_adapter(root).to_task_description(_task_id_for(_METHOD_KEY)).grading
    assert grading.combine_method == method, (
        f"the adapter translated combine.method {method!r} into combine_method "
        f"{grading.combine_method!r}, so this cell measures a method nobody wrote"
    )
    assert grading.pass_threshold == COMBINE_METHOD_PASS_THRESHOLD, (
        f"the adapter translated pass_threshold {grading.pass_threshold}, so "
        f"the flags pinned against {COMBINE_METHOD_PASS_THRESHOLD} answer a different question"
    )
    case = _load_case(_pack_dir(test_data_dir, _METHOD_KEY), _METHOD_CASE)
    jsonpath_score, _ = evaluate_jsonpath_checks(
        grading.state_checks.jsonpath_checks, state=case.state
    )
    transcript_score = evaluate_transcript_rules(
        case.runner_timeline, grading.transcript_rules.model_dump()
    ).score
    score, binary_pass = combine_grade_components(
        {"jsonpath_score": jsonpath_score, "transcript_score": transcript_score},
        grading.model_dump(),
    )
    return _MethodVerdict(
        components={"state_checks": jsonpath_score, "transcript_rules": transcript_score},
        score=score,
        binary_pass=binary_pass,
    )


@pytest.mark.parametrize("method", COMBINE_METHODS)
def test_both_substrates_aggregate_by_the_declared_combine_method(method, test_data_dir, tmp_path):
    """Each substrate folds one split pair of components by the method, to a pinned answer.

    One shared dispatch makes cross-substrate agreement hold by construction — two
    substrates calling one function that returned the mean for every method agree
    perfectly — so the equality at the end is the weakest assertion here. What carries
    the lock is that each substrate's answer is the one written out per method, over
    components a fold cannot mistake for each other.

    The fixture is deterministic on purpose. A judge- or probe-weighted pack would
    score nothing core-side, so the two component maps would differ before any method
    was read and the cell would measure that instead.
    """
    expected_score, expected_pass = COMBINE_METHOD_VERDICTS[method]
    root = tmp_path / f"method_{method}"
    _author_method(test_data_dir, root, method=method)
    core = _core_method_verdict(test_data_dir, root, method=method)
    runner = _runner_method_verdict(test_data_dir, root, method=method)
    for substrate, verdict in (("core", core), ("runner", runner)):
        assert verdict.components == COMBINE_METHOD_COMPONENTS, (
            f"the {substrate} substrate scored the pack's components {verdict.components}, "
            f"not {COMBINE_METHOD_COMPONENTS} — the answers pinned below aggregate other numbers"
        )
        assert verdict.score == pytest.approx(expected_score), (
            f"the {substrate} substrate aggregated {COMBINE_METHOD_COMPONENTS} by {method!r} into "
            f"{verdict.score}, not {expected_score}"
        )
        assert verdict.binary_pass is expected_pass, (
            f"the {substrate} substrate aggregated {COMBINE_METHOD_COMPONENTS} by {method!r} to "
            f"binary_pass {verdict.binary_pass}, not {expected_pass}"
        )

    assert (core.score, core.binary_pass) == (runner.score, runner.binary_pass), (
        f"the substrates disagree on {method!r}: core {(core.score, core.binary_pass)} vs "
        f"runner {(runner.score, runner.binary_pass)}"
    )


# --------------------------------------------------------------------------
# The fixture loader every lock above reads its trials through
# --------------------------------------------------------------------------

_MULTI_TURN_CASE = "two_turns"

_MULTI_TURN_FIXTURE: dict[str, Any] = {
    _MULTI_TURN_CASE: {
        "messages": [
            {"role": "user", "content": "Refund PAY-1 if it is a duplicate."},
            {
                "role": "assistant",
                "content": "Looking that up.",
                "tool_calls": [
                    {
                        "tool_name": "billing_api_get_payment",
                        "executor": "agent",
                        "status": "success",
                        "arguments": {"payment_id": "PAY-1"},
                        "output": '{"amount": 10}',
                    }
                ],
            },
            {
                "role": "assistant",
                "content": "Denying it.",
                "tool_calls": [
                    {
                        "tool_name": "servicenow_csm_update_case",
                        "executor": "agent",
                        "status": "success",
                        "arguments": {"u_resolution_code": "denied_ineligible"},
                    }
                ],
            },
        ],
        "state": {},
    }
}

# What the fixture above must build, event by event. The two calls sit on
# different assistant turns, so a loader gathering them onto one turn produces
# different ``turn_index`` values here; the first call authors an ``output`` and
# the second omits one, so the two result rows pin the text read and the default.
_MULTI_TURN_EVENTS: tuple[tuple[int, int, str, str | None, str | None], ...] = (
    (0, 0, "user_message", None, None),
    (1, 0, "assistant_message", None, None),
    (2, 0, "tool_call", "billing_api_get_payment", None),
    (3, 0, "tool_result", "billing_api_get_payment", '{"amount": 10}'),
    (4, 1, "assistant_message", None, None),
    (5, 1, "tool_call", "servicenow_csm_update_case", None),
    (6, 1, "tool_result", "servicenow_csm_update_case", ""),
)


def _write_pack(tmp_path: Path, fixture: dict[str, Any]) -> Path:
    pack = tmp_path / "authored_pack"
    pack.mkdir()
    (pack / "trial.yaml").write_text(yaml.safe_dump(fixture))
    return pack


def _produced_events(
    timeline: TrialTimeline,
) -> tuple[tuple[int, int, str, str | None, str | None], ...]:
    return tuple(
        (event.position, event.turn_index, event.kind.value, event.tool_name, event.result)
        for event in timeline.events
    )


def test_the_fixture_loader_places_each_turns_calls_where_the_author_wrote_them(tmp_path):
    """A call authored under a turn is that turn's call, with its own result text.

    Every lock above reads its trial through this loader, so what a pack can say is
    the limit of what they can prove. A loader that gathered a trial's calls onto
    one turn would leave ordering across turns — and any window over ``turn_index``
    — with no expressible fixture at all.
    """
    pack = _write_pack(tmp_path, _MULTI_TURN_FIXTURE)
    trial = _load_case(pack, _MULTI_TURN_CASE)

    assert _produced_events(trial.runner_timeline) == _MULTI_TURN_EVENTS

    assert [
        (message.role.value, [call.name for call in message.tool_calls or []])
        for message in trial.core_trajectory.messages
    ] == [
        ("user", []),
        ("assistant", ["billing_api_get_payment"]),
        ("assistant", ["servicenow_csm_update_case"]),
    ], "the core substrate's trajectory does not carry the placement the timeline shows"


def test_the_fixture_loader_hands_the_runner_wire_shaped_tool_calls(tmp_path):
    """The runner's own decoder reads the authored calls back off ``runner_messages``.

    ``_grade_custom_checks`` takes the wire ``llm_messages``, so a fixture-shaped
    call there decodes to an empty tool name and a check reads a trial that made no
    call — the parity suite's own silent divergence.
    """
    pack = _write_pack(tmp_path, _MULTI_TURN_FIXTURE)
    transcript = _build_runner_check_transcript(_load_case(pack, _MULTI_TURN_CASE).runner_messages)

    assert [
        (call.name, call.arguments)
        for message in transcript.messages
        for call in message.tool_calls
    ] == [
        ("billing_api_get_payment", {"payment_id": "PAY-1"}),
        ("servicenow_csm_update_case", {"u_resolution_code": "denied_ineligible"}),
    ]


@pytest.mark.parametrize(
    ("what", "mutate", "rejected"),
    [
        (
            "a trial-wide call list",
            lambda case: case.update(
                tool_calls=[{"tool_name": "cancel_order", "executor": "agent", "status": "success"}]
            ),
            "['tool_calls']",
        ),
        (
            "a misspelled message key",
            lambda case: case["messages"][1].update(
                tool_call=case["messages"][1].pop("tool_calls")
            ),
            "['tool_call']",
        ),
        (
            "an authored latency",
            lambda case: case["messages"][1]["tool_calls"][0].update(latency_seconds=1.5),
            "['latency_seconds']",
        ),
    ],
)
def test_the_fixture_loader_rejects_a_key_it_would_not_read(what, mutate, rejected, tmp_path):
    """One shape, no modes: a key the loader does not read fails the pack that wrote it.

    The trial-wide list is the shape this loader replaced, and it is the one a pack
    copied from an older fixture would carry — accepted silently, it would place
    every call on the last assistant turn again.
    """
    fixture = yaml.safe_load(yaml.safe_dump(_MULTI_TURN_FIXTURE))
    mutate(fixture[_MULTI_TURN_CASE])
    pack = _write_pack(tmp_path, fixture)

    with pytest.raises(AssertionError, match=re.escape(rejected)):
        _load_case(pack, _MULTI_TURN_CASE)
