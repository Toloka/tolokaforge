"""Substrate-parity guard rail for the grading key manifest.

Fifteen locks over :mod:`tolokaforge.core.grading.key_manifest`:

1. every field either substrate's grading config declares is claimed by exactly
   one manifest entry, and every claimed field resolves; a position below a claimed
   field is addressed by an element path, which is the only such mechanism;
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
   ``GradingConfig`` dump *and* is declared in ``accountable_author_keys()``, the
   per-key human claim that a recording site files it, so a malformed or
   undeclared entry fails here rather than at grade time in production — lock 15
   is what drives the site itself;
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
   author's ``combine.method``, each method pinned to a score written out here;
10. both substrates score one ``trace_checks`` pack to the same component through
    their own grading path — the core engine's ``grade_trajectory``, and the
    runner's ``GradeTrial`` over its real gRPC handlers — and the per-constraint
    facts a reviewer reads off the grade rather than off the score, ``severity``,
    the winning route and ``undecided``, reach the host from both;
11. both substrates read each per-constraint field that shapes how a kind scores
    without carrying a score itself, over packs a build ignoring the field would
    score identically;
12. the constraint vocabulary the manifest addresses, the one the evaluator and
    the runtime ledger read, and the one an author can write are the same set;
13. a state source that declares nothing leaves ``state_checks`` unscored on both
    substrates, and the one asymmetry the rule permits — a probe-only pack, which
    only the runner can read, and which is the whole of what a pack declaring a probe
    may be — is asserted against the manifest's own claim rather than assumed;
14. every scored component a pack produces declares a share of ``combine.weights``
    on both substrates, and a fold with no share to read decides rather than
    defaulting to one;
15. every ledger key's recording site is *driven*: a real
    ``RegisterTrial → ExecuteTool → GradeTrial`` populates the key, lets its
    evaluator run, and the outcome the ledger reports is the one the manifest
    implies. Editing a declaration cannot satisfy it — deleting a recording site,
    downgrading it to a skip, filing ``EVALUATED`` over an evaluation that never
    ran, or driving a config that never populated the key each fail it.

The exemption sets and the differential fixtures are the enforcement mechanism:
adding a grading key to one substrate only cannot pass this suite without an
explicit, reviewable edit to one of the frozen constants below.

Locks 3, 6, 7, 9, 10, 11 and 15 drive a real trial, and each reads it through one
fixture loader, so what a ``grading_parity`` pack can express bounds what they can
prove — for lock 15 that bound covers the keys its driver table sends to a parity
pack, the hash family, the probes and the judge being driven from tasks written out
in this module instead.
That loader's contract — a tool call belongs to the message that requested it, and
carries that call's own result text — is locked at the end of this module.
"""

import ast
import asyncio
import importlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel

from tests.utils.combine_method_verdicts import (
    COMBINE_METHOD_COMPONENTS,
    COMBINE_METHOD_PASS_THRESHOLD,
    COMBINE_METHOD_VERDICTS,
)
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import models as core_models
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.combine_method import COMBINE_METHODS
from tolokaforge.core.grading.combine_weights import MissingComponentWeight
from tolokaforge.core.grading.golden_replay import resolve_initial_state
from tolokaforge.core.grading.judge import JudgeResult, JudgeStatus, JudgeUsage
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
from tolokaforge.core.grading.state_checks import StateChecker, extract_db_state, state_digest
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import TrialTimeline, build_trial_timeline
from tolokaforge.core.grading.transcript import evaluate_transcript_rules
from tolokaforge.core.models import (
    Message,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)
from tolokaforge.runner import grading as runner_grading
from tolokaforge.runner import models as runner_models
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import service as runner_service_module
from tolokaforge.runner.grading import (
    combine_grade_components,
    evaluate_db_probes,
    evaluate_jsonpath_checks,
    resolve_state_checks_component,
)
from tolokaforge.runner.grading_ledger import (
    LEDGER_KEYS,
    LLM_JUDGE_KEY,
    NO_JUDGE_MESSAGES_SKIP,
    accountable_author_keys,
    populated_ledger_keys,
    runner_dump_path,
    skip_note,
    skip_note_prefix,
)
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    KeyAccounting,
    KeyAccountingRecord,
    TraceConstraintExpr,
)
from tolokaforge.runner.service import (
    RunnerServiceImpl,
    TrialContextRuntime,
    _build_runner_check_transcript,
)

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PARITY_GLOB = "grading_parity/**/task.yaml"
_TASKS_GLOB = "tasks/**/task.yaml"
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

# Case name -> the hash verdict it produces against the hash of the state the pack's
# task declares it starts in. Lock 7 asserts core's evaluator really returns these.
_COMPOSITION_HASH_CASES: tuple[tuple[str, float], ...] = (
    ("hash_matching", 1.0),
    ("hash_diverging", 0.0),
)

_JSONPATHS_KEY = "state_checks.jsonpaths"
_PROBES_KEY = "state_checks.db_probes"

# The one in-repo pack whose only state source is a probe, so it is the only fixture
# that can show the RUNNER_ONLY asymmetry lock 13 asserts. It sits under
# ``tests/data/tasks/`` rather than in the parity corpus because that corpus's packs
# each isolate one key, and a probe-only pack isolates this one by construction.
_PROBE_PACK = "db_probe_grading"

_METHOD_KEY = "combine.method"
_METHOD_CASE = "split_components"

_HASH_SCORE_NAME = "hash_score"
_BINARY_HASH_VERDICT = frozenset({0.0, 1.0})

_UNSCORED_COMPONENT = -1.0
"""What ``GradeComponents`` carries for a component the runner did not score.

Every component evaluator in ``tolokaforge/runner/grading.py`` returns it when
handed nothing to evaluate, so a slot holding it means no evaluation happened
whatever the ledger recorded for the keys that feed it.
"""

_HASH_FAMILY_ROOT = "state_checks.hash"
_EXPECT_INITIAL_STATE_KEY = "state_checks.hash.expect_initial_state"
_GOLDEN_ACTIONS_KEY = "state_checks.hash.golden_actions"

# The one in-repo pack that both declares golden actions and gives them a world to be
# replayed in — a JSON initial-state file and an ``mcp_server`` whose tools they call.
# It sits under ``tests/data/tasks/`` for the reason :data:`_PROBE_PACK` does.
_GOLDEN_REPLAY_PACK = "shop_orders_02"

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
        "state_checks.hash.description",
        "llm_judge",
        "grading_method",
    }
)

# Non-BOTH keys that should be both and are not yet. tracking_issue is required.
_DRIFT_EXEMPTIONS: frozenset[str] = frozenset()

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
        "combine.weights",
        "trace_checks",
        "trace_checks.constraints.weight",
        "trace_checks.constraints.on_missing",
        "trace_checks.constraints.severity",
        "trace_checks.constraints.within",
        "trace_checks.constraints.bind",
    }
)

# The five per-constraint fields that shape how a kind scores without scoring
# anything themselves. Each owns a pack whose two trials a build ignoring the
# field would score identically, so discrimination is the field being read.
_TRACE_CONFIG_INPUT_KEYS: tuple[str, ...] = (
    "trace_checks.constraints.weight",
    "trace_checks.constraints.on_missing",
    "trace_checks.constraints.severity",
    "trace_checks.constraints.within",
    "trace_checks.constraints.bind",
)

# FIELD_RESOLUTION_ONLY entries that need no tracking issue: aggregation and
# load-time config inputs, which have no violating trajectory by construction.
_NON_TRACKED_FIELD_RESOLUTION_KEYS = frozenset(
    {
        "combine.pass_threshold",
        "state_checks.hash.description",
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
        "core:StateChecksConfig.hash",
        "core:GradingConfig.transcript_rules",
        "core:GradingConfig.trace_checks",
        "runner:RunnerGradingConfig.state_checks",
        "runner:RunnerGradingConfig.transcript_rules",
        "runner:RunnerGradingConfig.trace_checks",
    }
)

_SUBSTRATE_ROOTS: dict[str, type[BaseModel]] = {
    "core": core_models.GradingConfig,
    "runner": runner_models.RunnerGradingConfig,
}


# --------------------------------------------------------------------------
# Manifest introspection helpers
# --------------------------------------------------------------------------


def _field_of(item: GradingKey, substrate: str) -> str | None:
    return item.core_field if substrate == "core" else item.runner_field


def _element_path_of(item: GradingKey, substrate: str) -> str | None:
    return item.core_element_path if substrate == "core" else item.runner_element_path


def _claimed_fields(substrate: str) -> dict[str, list[str]]:
    """Model field paths claimed directly -> author keys.

    An entry carrying an element path is not claiming the field: it claims one
    place *inside* it, and several such entries share the field. Counting them as
    claims would report the shared field as claimed twice over.
    """
    claims: dict[str, list[str]] = {}
    for item in GRADING_KEYS:
        field = _field_of(item, substrate)
        if field is None or _element_path_of(item, substrate) is not None:
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


def _element_model(annotation: Any) -> type[BaseModel] | None:
    """The element model of a ``list[SomeModel]`` field, which ``_direct_model`` skips.

    That skip is what makes an element path necessary: the walker treats such a
    field as one leaf, so several author keys living inside its elements have no
    address it can resolve until the manifest names one.
    """
    for option in _union_options(annotation):
        if get_origin(option) is not list:
            continue
        (element,) = get_args(option)
        if isinstance(element, type) and issubclass(element, BaseModel):
            return element
    return None


def _resolve_element_path(model: type[BaseModel], element_path: str, *, what: str) -> None:
    """Walk ``element_path`` from ``model``, asserting every segment is a declared field.

    Each segment resolves against the model the one before it holds, so a path
    naming a field of the wrong model fails here rather than addressing nothing.
    A ``list[SomeModel]`` segment continues into its element model, which is how
    ``all_of``'s nested expressions stay walkable.
    """
    segments = element_path.split(".")
    current: type[BaseModel] | None = model
    for index, segment in enumerate(segments):
        assert current is not None, (
            f"{what}: element path {element_path!r} reads {segment!r} out of "
            f"{segments[index - 1]!r}, which holds no model to resolve it against"
        )
        field = current.model_fields.get(segment)
        assert field is not None, (
            f"{what}: element path {element_path!r} does not resolve — {current.__name__} "
            f"declares no field {segment!r}. It declares {sorted(current.model_fields)}"
        )
        current = _direct_model(field.annotation) or _element_model(field.annotation)


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


def _assert_element_paths_resolve(substrate: str, registry: dict[str, type[BaseModel]]) -> None:
    """Every element-addressed entry names a list of models and a path inside it."""
    for item in GRADING_KEYS:
        element_path = _element_path_of(item, substrate)
        field = _field_of(item, substrate)
        if element_path is None or field is None:
            continue
        what = f"{item.author_key} ({substrate})"
        model_name, _, field_name = field.partition(".")
        model = registry.get(model_name)
        assert model is not None, (
            f"{what}: {substrate}_field {field!r} names {model_name!r}, which is not "
            f"reachable from the {substrate} GradingConfig"
        )
        declared = model.model_fields.get(field_name)
        assert declared is not None, (
            f"{what}: {substrate}_field {field!r} does not resolve — {model_name} has no "
            f"field {field_name!r}"
        )
        element = _element_model(declared.annotation)
        assert element is not None, (
            f"{what}: {substrate}_element_path {element_path!r} is walked from the element "
            f"model of {field!r}, which is not a list of models"
        )
        _resolve_element_path(element, element_path, what=what)


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


def _adapter_for(test_data_dir: Path, tasks_glob: str) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": tasks_glob})


def _parity_adapter(test_data_dir: Path) -> NativeAdapter:
    return _adapter_for(test_data_dir, _PARITY_GLOB)


_MISSING = object()


def _yaml_node(data: Any, dotted_path: str) -> Any:
    """What ``dotted_path`` holds in an authored ``grading.yaml``, or ``_MISSING``."""
    node = data
    for segment in dotted_path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return _MISSING
        node = node[segment]
    return node


def _yaml_declares(data: Any, dotted_path: str) -> bool:
    return _yaml_node(data, dotted_path) is not _MISSING


def _yaml_element_declares(node: Any, segments: tuple[str, ...]) -> bool:
    """Whether ``segments`` resolves inside one authored list element.

    A kind written inside a composite counts as declared: where a segment is not
    at this level the walk follows the expressions the kinds hold, so an ``all_of``
    holding a ``before`` declares ``before``. Descent stops outside a constraint
    kind, because a matcher's ``args`` keys are the author's own argument names.
    """
    if not segments:
        return True
    if not isinstance(node, dict):
        return False
    if segments[0] in node:
        return _yaml_element_declares(node[segments[0]], segments[1:])
    return any(
        _yaml_element_declares(nested, segments)
        for kind in TRACE_CONSTRAINT_KINDS
        if kind in node
        for nested in (node[kind] if isinstance(node[kind], list) else [node[kind]])
    )


def _declared_author_keys(grading_yaml: dict[str, Any]) -> set[str]:
    """Every manifest key an authored ``grading.yaml`` declares.

    An element-addressed key lives inside the elements of the list its parent key
    names, so its declaredness is read there rather than at a dotted path of its
    own — a kind is not a YAML key under ``constraints``, it is a key on one of the
    constraints.
    """
    declared: set[str] = set()
    for key in author_keys():
        element_path = entry(key).core_element_path
        if element_path is None:
            if _yaml_declares(grading_yaml, key):
                declared.add(key)
            continue
        elements = _yaml_node(grading_yaml, key.rsplit(".", 1)[0])
        if isinstance(elements, list) and any(
            _yaml_element_declares(element, tuple(element_path.split("."))) for element in elements
        ):
            declared.add(key)
    return declared


_CONSTRAINT_KIND_KEYS = frozenset(
    f"trace_checks.constraints.{kind.value}" for kind in TRACE_CONSTRAINT_KINDS
)


_BLOCK_SWITCH = "enabled"


def _authoring_the_same_key(declared_key: str, author_key: str) -> bool:
    """Whether ``declared_key`` is part of writing ``author_key`` down.

    Four structural cases, none of them a second check. The ancestors a leaf
    lives under: a constraint kind needs the block and the list around it. The
    leaves of a key standing for a list: writing something in the list is what
    authoring the list looks like. And another kind beside a kind — a composite
    is an expression over other kinds and cannot be written without them, which
    is why the sharper assertion on what a pack authors at *top level* is the one
    that keeps a kind's pack about that kind.

    The fourth is the switch that turns a block on. A source inside a block cannot
    be written without it — ``state_checks.hash.expect_initial_state`` under an
    ``enabled: false`` block is read by nothing — and the switch carries no verdict
    of its own: with it off there is no component to discriminate the two trials by,
    so it cannot be what discriminated them.
    """
    block, _, leaf = author_key.rpartition(".")
    return (
        declared_key == author_key
        or author_key.startswith(f"{declared_key}.")
        or declared_key.startswith(f"{author_key}.")
        or {declared_key, author_key} <= _CONSTRAINT_KIND_KEYS
        or (leaf != _BLOCK_SWITCH and declared_key == f"{block}.{_BLOCK_SWITCH}")
    )


def _top_level_constraint_kinds(grading_yaml: dict[str, Any]) -> set[str]:
    """The kind each authored constraint *is*, ignoring what its composites nest."""
    constraints = _yaml_node(grading_yaml, "trace_checks.constraints")
    if not isinstance(constraints, list):
        return set()
    return {
        kind
        for element in constraints
        if isinstance(element, dict) and isinstance(element.get("require"), dict)
        for kind in element["require"]
        if kind in TRACE_CONSTRAINT_KINDS
    }


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
    family: str,
    grading_config: core_models.GradingConfig,
    case: _TrialCase,
    task_dir: Path,
    *,
    task_initial_state: core_models.InitialStateConfig | None,
) -> tuple[float, float]:
    """(component score, combined score) from the core engine's real combine.

    The engine is built the way ``adapters/base.py`` builds it for a real trial, so a
    pack whose grading reads a task-level fact — the state it starts in, which the
    hash block's ``expect_initial_state`` source compares against — is graded here
    against the same fact production would hand it rather than against nothing.
    """
    grade = GradingEngine(
        grading_config, task_dir=task_dir, task_initial_state=task_initial_state
    ).grade_trajectory(case.core_trajectory, case.state)
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


def _write_case_state_into_the_trial_database(
    servicer: RunnerServiceImpl, trial_id: str, tables: Mapping[str, list[dict[str, Any]]]
) -> None:
    """Move the trial's database to ``tables``, one upsert per record.

    Generic over the fixture: every record of every table is written under the
    upsert's own key resolution, so a pack's authored state reaches db-service
    without this helper knowing which tables it holds. Upsert rather than a wholesale
    replace because that is the mutation db-service offers for a record that may or
    may not already be there — a case that expects a record *removed* would need a
    delete this does not write, and the verdict would then disagree with the fixture
    rather than quietly agree with it.
    """
    for table, records in tables.items():
        servicer._run_async(
            servicer.db_client.mutate(
                trial_id, table, [{"op": "upsert", "record": record} for record in records]
            )
        )


def _runner_state_checks_hash_verdict(
    task_description: runner_models.TaskDescription,
    case: _TrialCase,
    servicer: RunnerServiceImpl,
    context: Any,
    *,
    trial_id: str,
) -> tuple[float, float]:
    """(state_checks component, trial score) from the runner's own ``GradeTrial``.

    The hash evaluator reads the trial's database rather than the case's ``state``
    mapping, so the case has to *be* the database: ``RegisterTrial`` provisions it
    from the pack's ``initial_state`` — which is the expected side of the comparison —
    and the trial's own records are written over it before grading.

    The combined score is the runner's own fold off the same response, so both halves
    of the return come from one real trial rather than from a second computation here.
    """
    _register_pack(servicer, context, task_description, trial_id)
    _replay_authored_calls(servicer, context, trial_id, case)
    _write_case_state_into_the_trial_database(servicer, trial_id, case.state["db"])

    response = _grade_registered_trial(
        servicer, context, trial_id, json.dumps(case.runner_messages)
    )
    assert response.success is True, response.error

    component = response.grade.components.state_checks
    assert component != _UNSCORED_COMPONENT, (
        "the runner graded the pack without scoring state_checks, so this cell carries "
        "the unscored sentinel rather than a hash verdict — two of them read as "
        f"agreement between the substrates and prove nothing: {response.grade.reasons!r}"
    )
    return component, response.grade.score


def _runner_verdict(
    family: str,
    task_description: runner_models.TaskDescription,
    case: _TrialCase,
    *,
    servicer: RunnerServiceImpl,
    context: Any,
    trial_id: str,
) -> tuple[float, float]:
    """(component score, combined score) from the runner's real evaluators.

    ``state_checks`` splits on what the pack declares rather than on the key under
    test: a hash block naming a state the trial is compared against is scored by an
    evaluator that resets and hashes a real database, which is reachable only through
    a registered trial. A pack declaring no hash source keeps the in-process JSONPath
    path, so no cell that passed before this arm existed is driven differently now.
    """
    grading = task_description.grading
    if family == "state_checks":
        state_checks = grading.state_checks
        undeclared = runner_models.HashComparisonBasis.UNDECLARED_INITIAL_STATE
        if state_checks.hash_enabled and state_checks.hash_comparison_basis() is not undeclared:
            return _runner_state_checks_hash_verdict(
                task_description, case, servicer, context, trial_id=trial_id
            )
        component, _ = evaluate_jsonpath_checks(state_checks.jsonpath_checks, state=case.state)
        components = {"jsonpath_score": component}
    elif family == "transcript_rules":
        result = evaluate_transcript_rules(case.runner_timeline, grading.transcript_rules)
        component = result.score
        components = {"transcript_score": component}
    elif family == "custom_checks":
        component = _runner_custom_checks_score(task_description, case)
        components = {"custom_checks_score": component}
    elif family == "trace_checks":
        # One evaluator serves both substrates, so the equality this lock ends on
        # holds by construction; what a cell proves is that the pack loads through
        # the adapter onto the runner's own model and that the kind discriminates
        # there. That the runner's GradeTrial really reaches this evaluator is a
        # separate claim, driven over real gRPC by lock 10.
        component = evaluate_trace_checks(case.runner_timeline, grading.trace_checks).score
        components = {"trace_checks_score": component}
    else:
        pytest.fail(
            f"no runner differential driver for the {family!r} family — add one rather "
            "than silently skipping the key"
        )
    combined = combine_grade_components(components, grading.model_dump())
    return component, combined.score


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

        _assert_element_paths_resolve(substrate, registry)

    assert containers == _CONTAINER_FIELDS, (
        "the set of grading config fields walked into as containers changed. Every "
        "container's leaves must be claimed individually; a new container here means "
        "a new key family landed on a substrate."
    )


def test_a_position_inside_a_claimed_field_is_addressed_by_an_element_path():
    """An element path is the manifest's one address for a place below a field.

    An entry whose author key sits under another entry's, on a substrate where that
    parent claims a field, names a position inside that field's value rather than a
    field of its own. Read as a set equality so both halves are findings: a key
    inside a claimed field carrying no element path addresses nothing the suite can
    walk, and an element path under a parent that claims no field is walked from
    nowhere. The field the path starts at is then checked to be the parent's own.
    """
    by_key = {item.author_key: item for item in GRADING_KEYS}
    for substrate in _SUBSTRATE_ROOTS:
        inside_a_claimed_field = {
            item.author_key
            for item in GRADING_KEYS
            if (parent := by_key.get(item.author_key.rpartition(".")[0])) is not None
            and _field_of(parent, substrate) is not None
        }
        element_addressed = {
            item.author_key
            for item in GRADING_KEYS
            if _element_path_of(item, substrate) is not None
        }
        assert element_addressed, (
            f"no {substrate} entry carries an element path, so this lock compares two "
            "empty sets and asserts nothing about how a nested position is addressed"
        )
        assert inside_a_claimed_field == element_addressed, (
            f"{substrate}: the keys sitting inside a claimed field and the keys carrying "
            f"an element path have diverged. Inside a claimed field: "
            f"{sorted(inside_a_claimed_field)}. Element-addressed: "
            f"{sorted(element_addressed)}. A position below a declared field is "
            "addressed by an element path, which the suite walks against the element "
            "model; there is no other way to name one."
        )
        for key in sorted(element_addressed):
            parent_field = _field_of(by_key[key.rpartition(".")[0]], substrate)
            assert _field_of(by_key[key], substrate) == parent_field, (
                f"{key}: {substrate}_element_path is walked from {parent_field!r}, the "
                f"field its parent key claims, but the entry names "
                f"{_field_of(by_key[key], substrate)!r}"
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


@dataclass(frozen=True)
class _SubstrateDifferential:
    """One key's pack, graded on both substrates in both of its two trials."""

    core_ok: float
    core_bad: float
    core_ok_total: float
    core_bad_total: float
    runner_ok: float
    runner_bad: float
    runner_ok_total: float
    runner_bad_total: float


def _drive_both_substrates(
    author_key: str,
    test_data_dir: Path,
    tmp_path: Path,
    servicer: RunnerServiceImpl,
    context: Any,
) -> _SubstrateDifferential:
    """Grade a key's satisfying and violating trials through each substrate's own path.

    The two cases take a trial id apiece: a runner arm that registers a trial mutates
    its database, so grading both cases under one id would compare the second against
    what the first left behind.
    """
    family = author_key.split(".")[0]
    task_id = _task_id_for(author_key)
    pack = _pack_dir(test_data_dir, author_key)
    adapter = _parity_adapter(test_data_dir)
    core_config = adapter.get_grading_config(task_id)
    task_description = adapter.to_task_description(task_id)
    initial_state = adapter.get_task(task_id).initial_state
    satisfying = _load_case(pack, "satisfying")
    violating = _load_case(pack, "violating")
    # The core engine imports the pack's ``checks.py`` from its task dir, which
    # writes ``__pycache__`` beside the fixture; grade from a copy so a canonical
    # run leaves the repo clean.
    core_task_dir = tmp_path / "core_task_dir"
    shutil.copytree(pack, core_task_dir)

    core_ok, core_ok_total = _core_verdict(
        family, core_config, satisfying, core_task_dir, task_initial_state=initial_state
    )
    core_bad, core_bad_total = _core_verdict(
        family, core_config, violating, core_task_dir, task_initial_state=initial_state
    )
    runner_ok, runner_ok_total = _runner_verdict(
        family,
        task_description,
        satisfying,
        servicer=servicer,
        context=context,
        trial_id=f"differential_{task_id}_satisfying:0",
    )
    runner_bad, runner_bad_total = _runner_verdict(
        family,
        task_description,
        violating,
        servicer=servicer,
        context=context,
        trial_id=f"differential_{task_id}_violating:0",
    )
    return _SubstrateDifferential(
        core_ok=core_ok,
        core_bad=core_bad,
        core_ok_total=core_ok_total,
        core_bad_total=core_bad_total,
        runner_ok=runner_ok,
        runner_bad=runner_bad,
        runner_ok_total=runner_ok_total,
        runner_bad_total=runner_bad_total,
    )


def _assert_both_substrates_discriminate(author_key: str, verdict: _SubstrateDifferential) -> None:
    assert verdict.core_ok > verdict.core_bad, (
        f"the core substrate does not discriminate {author_key}: satisfying "
        f"{verdict.core_ok} vs violating {verdict.core_bad}"
    )
    assert verdict.runner_ok > verdict.runner_bad, (
        f"the runner substrate does not discriminate {author_key}: satisfying "
        f"{verdict.runner_ok} vs violating {verdict.runner_bad}"
    )


def _assert_the_substrates_agree(author_key: str, verdict: _SubstrateDifferential) -> None:
    assert verdict.core_ok == pytest.approx(verdict.runner_ok)
    assert verdict.core_bad == pytest.approx(verdict.runner_bad), (
        f"{author_key} claims BOTH_SCORE_PARITY but the substrates score the "
        f"violating trial differently: core {verdict.core_bad} vs runner "
        f"{verdict.runner_bad}"
    )


@pytest.mark.parametrize("author_key", [item.author_key for item in _differential_entries()])
def test_both_substrates_discriminate_each_shared_scored_key(
    author_key, test_data_dir, tmp_path, runner_service, mock_grpc_context
):
    item = entry(author_key)
    pack = _pack_dir(test_data_dir, author_key)

    authored = yaml.safe_load((pack / "grading.yaml").read_text())
    declared = _declared_author_keys(authored)
    unrelated = sorted(
        key
        for key in declared
        if not key.startswith("combine.") and not _authoring_the_same_key(key, author_key)
    )
    assert not unrelated and author_key in declared, (
        f"{author_key}'s fixture declares {unrelated or 'nothing'} beside the key under "
        f"test, so a violating trial could discriminate without that key being read. It "
        f"declares {sorted(declared)}"
    )

    kind_under_test = author_key.removeprefix("trace_checks.constraints.")
    if kind_under_test in TRACE_CONSTRAINT_KINDS:
        top_level = _top_level_constraint_kinds(authored)
        assert top_level == {kind_under_test}, (
            f"{author_key}'s fixture authors the top-level constraints {sorted(top_level)}. "
            "A kind's pack authors that kind alone — another beside it is a second check "
            "the violating trial could be discriminated by, and a composite's sub-terms "
            "belong inside its own expression rather than next to it"
        )

    verdict = _drive_both_substrates(
        author_key, test_data_dir, tmp_path, runner_service, mock_grpc_context
    )
    _assert_both_substrates_discriminate(author_key, verdict)

    assert verdict.core_ok_total > verdict.core_bad_total
    assert verdict.runner_ok_total > verdict.runner_bad_total

    if item.coverage is SubstrateCoverage.BOTH_SCORE_PARITY:
        _assert_the_substrates_agree(author_key, verdict)


# --------------------------------------------------------------------------
# 4. Adapter translation carries every key both substrates declare
# --------------------------------------------------------------------------


_EXPECT_INITIAL_STATE_PACK = _task_id_for(_EXPECT_INITIAL_STATE_KEY)

_TRANSLATION_PACK_GLOBS: Mapping[str, str] = MappingProxyType(
    {
        _ALL_KEYS_TASK: _PARITY_GLOB,
        _PROBE_PACK: _TASKS_GLOB,
        _GOLDEN_REPLAY_PACK: _TASKS_GLOB,
        _EXPECT_INITIAL_STATE_PACK: _PARITY_GLOB,
    }
)
"""The packs whose declared keys together carry the manifest, and the glob loading each.

More than one pack because a key ``all_keys`` may not declare beside the rest still has
to be translated by something. ``state_checks.db_probes`` is exclusive with the hash
block and the JSONPath assertions ``all_keys`` carries; the two hash sources name
different expected states, so at most one of them lives there; and ``golden_actions``
needs a task supplying a JSON initial-state file and an ``mcp_server`` to replay
against, which ``all_keys`` supplies neither of. So the manifest's totality is a union
over packs each of which is authorable on its own, and they do not all live under one
root — hence a glob apiece.
"""

_TRANSLATION_OWNERS: Mapping[str, str] = MappingProxyType(
    {
        _PROBES_KEY: _PROBE_PACK,
        _GOLDEN_ACTIONS_KEY: _GOLDEN_REPLAY_PACK,
        _EXPECT_INITIAL_STATE_KEY: _EXPECT_INITIAL_STATE_PACK,
    }
)
"""The pack that must carry a key to the runner as something other than the field default.

Two reasons put a key here. The first is legality: a key ``all_keys`` cannot declare
beside the rest needs a pack that can. The second is attribution — ``expect_initial_state``
is legal there, but the pack named for it declares that key alone, so nothing else in the
pack could be what put the field off its default. ``all_keys`` owns every key neither
reason claims, so a key added to the manifest has an owner from the start and fails the
lock until some pack declares it. Ownership is per key rather than per pack because
declaring a key is not translating it: ``db_probe_grading`` writes
``combine.method: weighted`` and ``llm_judge: null``, both of which reach the runner as
exactly the field default, so a per-pack claim over it would assert nothing about
translation and be red besides.
"""


def _translation_carriers(
    grading: runner_models.RunnerGradingConfig,
) -> dict[str, BaseModel | None]:
    """The models a manifest ``runner_field`` addresses, keyed by the name it uses."""
    return {
        "RunnerGradingConfig": grading,
        "RunnerStateChecksConfig": grading.state_checks,
        "TranscriptRulesConfig": grading.transcript_rules,
        "TraceChecksConfig": grading.trace_checks,
    }


def _assert_translated_non_default(
    carriers: Mapping[str, BaseModel | None], *, author_key: str, runner_field: str, owner: str
) -> None:
    model_name, _, field_name = runner_field.partition(".")
    carrier = carriers.get(model_name)
    assert carrier is not None, (
        f"{author_key}: adapter translation of {owner} produced no {model_name} to carry "
        f"{runner_field!r}"
    )
    actual = getattr(carrier, field_name)
    default = type(carrier).model_fields[field_name].get_default(call_default_factory=True)
    assert actual != default, (
        f"{author_key} must reach the runner from {owner}/grading.yaml, and arrives as the "
        f"default {default!r} — either that pack does not declare it, or declares it at the "
        f"default, or NativeAdapter.to_task_description drops it on the floor"
    )


def test_adapter_translation_carries_every_runner_key(test_data_dir):
    """Every manifest key is declared by a translation pack, and reaches the runner from it.

    The two claims are read at different grains. Totality is a union over the pack set,
    because no one authorable pack can declare every key. Arrival is per key, against the
    single pack that owns it.
    """
    adapters = {
        task_id: _adapter_for(test_data_dir, tasks_glob)
        for task_id, tasks_glob in _TRANSLATION_PACK_GLOBS.items()
    }
    declared = {
        task_id: _declared_author_keys(
            yaml.safe_load((adapter.get_task_dir(task_id) / "grading.yaml").read_text())
        )
        for task_id, adapter in adapters.items()
    }
    # A family root carries no field of its own — its leaves do — so authorability
    # is "the core config accepts this key", which a root satisfies by being the
    # block its leaves live under.
    authorable = {
        item.author_key for item in GRADING_KEYS if item.core_field is not None or item.family_root
    }
    union: set[str] = set().union(*declared.values())
    assert union == authorable, (
        f"the packs {sorted(declared)} must together declare every manifest key the core "
        f"config accepts and nothing outside it; missing {sorted(authorable - union)}, "
        f"declared but not authorable {sorted(union - authorable)}"
    )

    carriers = {
        task_id: _translation_carriers(adapter.to_task_description(task_id).grading)
        for task_id, adapter in adapters.items()
    }
    # Several element-addressed entries name one field, and the assertion below is
    # about the field arriving — so it is made once per field rather than once per
    # entry, which would report the same translation ten times over.
    carried: set[tuple[str, str]] = set()
    for item in GRADING_KEYS:
        if item.core_field is None or item.runner_field is None:
            continue
        owner = _TRANSLATION_OWNERS.get(item.author_key, _ALL_KEYS_TASK)
        if (owner, item.runner_field) in carried:
            continue
        carried.add((owner, item.runner_field))
        _assert_translated_non_default(
            carriers[owner],
            author_key=item.author_key,
            runner_field=item.runner_field,
            owner=owner,
        )


# --------------------------------------------------------------------------
# 5. Every ledger key resolves in the runner config dump and declares a site
# --------------------------------------------------------------------------

_LEDGER_PROBE_TRACE_CHECKS = runner_models.TraceChecksConfig(
    constraints=[
        runner_models.TraceConstraint(
            id="the_agent_said_something",
            description="a populated block, so the dump carries the constraints field",
            require=TraceConstraintExpr(
                present=runner_models.PresentConstraint(
                    match=runner_models.TraceMatcher(kind="assistant_message")
                )
            ),
        )
    ]
)
"""A populated ``trace_checks`` block for the dump the resolution below walks.

The block is optional and its constraint list is required, so an unset
``trace_checks`` dumps as ``None`` and the walk would resolve the family's keys
against nothing at all — reporting the field as absent from a config that never
declared it.
"""


def test_every_ledger_key_resolves_in_the_runner_config_dump():
    runner_dump = runner_models.RunnerGradingConfig(
        state_checks=runner_models.RunnerStateChecksConfig(),
        transcript_rules=runner_models.TranscriptRulesConfig(),
        trace_checks=_LEDGER_PROBE_TRACE_CHECKS,
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
            f"{item.author_key} reaches the runner but nobody has declared a recording "
            "site for it in grading_ledger.accountable_author_keys, so every task "
            "populating it would fail GradeTrial. Add an evaluate-or-skip site in "
            "_grade_trial_async (or the evaluator it calls) and list the key there. "
            "This asserts the declaration only; lock 15 is what drives the site and "
            "reads the outcome it filed"
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
    validation. Core's hash verdict is the real one: the pack declares its initial
    state as the expected one and its ``hash_matching`` case reproduces it, so
    ``check_hash`` produces the verdict in process. The runner's is handed in,
    because its hash evaluator drives db-service over HTTP — honest only because
    that verdict is binary, which lock 7 holds, and because the runner producing it
    for itself is proven at the integration tier by the hash family's
    ``enforcing_test``.
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

    core_component, core_total = _core_verdict(
        "state_checks",
        core_config,
        trial,
        task_dir,
        task_initial_state=adapter.get_task(task_id).initial_state,
    )
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
    runner_total = combine_grade_components(
        {"hash_score": hash_score, "jsonpath_score": runner_jsonpath},
        runner_grading.model_dump(),
    ).score
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
    What is callable is ``check_hash``, which core's ``expect_initial_state`` branch
    reaches by hashing the pack's declared initial state; the composition fixture's
    two cases are graded through it against a digest computed here the way that
    branch computes it — which pins the two values lock 6 hands the runner as the
    ones core's own evaluator returns for the same states.
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
    task_id = _task_id_for(_COMPOSITION_KEY)
    adapter = _parity_adapter(test_data_dir)
    expected_hash = state_digest(
        resolve_initial_state(
            task_dir=adapter.get_task_dir(task_id),
            initial_state_json_db=adapter.get_task(task_id).initial_state.json_db,
        )
    )
    for case, hash_score in _COMPOSITION_HASH_CASES:
        db_state = extract_db_state(_load_case(pack, case).state)
        actual, _ = StateChecker().check_hash(db_state, expected_hash)
        assert actual == hash_score, (
            f"the composition fixture's {case!r} case scores {actual} against the hash of "
            f"the state its task declares it starts in, not the {hash_score} lock 6 assumes"
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
    distinguishable at all, that lock 9's answer table still spans the declared
    combine methods with one distinct score each, and that lock 14's weight maps still
    span the pair a membership rule is distinguishable over while its zero-share table
    still answers ``all``/``any`` differently from ``weighted``. Membership alone
    enforces nothing: a differential deleted wholesale leaves the escaped set unchanged.
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

    assert {key for key in escaped if key.startswith("trace_checks.")} == set(
        _TRACE_CONFIG_INPUT_KEYS
    ), (
        "a per-constraint config input escaped lock 3 without joining the "
        "parametrisation that drives its differential, so its claim rests on the "
        "membership above and nothing else"
    )
    assert set(family_author_keys("trace_checks")) & reached, (
        "no member of the trace_checks family is differentially proven, so the "
        "field-less family root claims a canonical differential that its leaves — the "
        "only entries carrying a field to run one on — no longer run"
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

    incomplete = {name for name, map_ in _WEIGHT_MAPS.items() if not set(map_) >= _SCORED_PAIR}
    assert incomplete and incomplete != set(_WEIGHT_MAPS), (
        f"_WEIGHT_MAPS {sorted(_WEIGHT_MAPS)} no longer spans both sides of the membership "
        f"question over {sorted(_SCORED_PAIR)}. With only complete maps the refusal is never "
        "reached and with only incomplete ones no fold runs, so either way "
        f"{_WEIGHTS_KEY} would claim a canonical differential over a set of agreeing rows"
    )

    assert {verdict for key, verdict in _ZERO_WEIGHT_VERDICTS.items() if key[0] != "weighted"} - {
        verdict for key, verdict in _ZERO_WEIGHT_VERDICTS.items() if key[0] == "weighted"
    }, (
        "_ZERO_WEIGHT_VERDICTS pins the same verdicts under all/any as under weighted, so the "
        "zero-total-weight rule scoped to every method would satisfy the whole table"
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
        case.runner_timeline, grading.transcript_rules
    ).score
    folded = combine_grade_components(
        {"jsonpath_score": jsonpath_score, "transcript_score": transcript_score},
        grading.model_dump(),
    )
    return _MethodVerdict(
        components={"state_checks": jsonpath_score, "transcript_rules": transcript_score},
        score=folded.score,
        binary_pass=folded.binary_pass,
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
# 10. Both substrates score trace_checks through one shared evaluator
# --------------------------------------------------------------------------

_TRACE_CHECKS_TASK = "trace_checks_constraints"


def _replay_authored_calls(
    servicer: RunnerServiceImpl, context: Any, trial_id: str, case: _TrialCase
) -> None:
    """Execute each authored call through the runner, in the order it was written.

    Nothing stands in for the runner's own recording: the trial's calls arrive as
    ``ExecuteTool`` requests under the call ids the message view declares, so
    ``GradeTrial`` reads a record the runner wrote rather than one handed to it.
    """
    trial = servicer.trials[trial_id]
    for record in case.core_trajectory.tool_log:
        output = record.output

        async def run(_arguments: dict[str, Any], answer: str = output) -> str:
            return answer

        trial.agent_tools[record.tool_name] = run
        executed = servicer.ExecuteTool(
            execute_request(
                trial_id,
                record.tool_name,
                json.dumps(record.arguments),
                executor=record.executor.value,
                call_id=record.call_id,
            ),
            context,
        )
        assert executed.status == pb2.EXECUTION_STATUS_SUCCESS, executed.error_message


def _register_pack(
    servicer: RunnerServiceImpl,
    context: Any,
    task_description: runner_models.TaskDescription,
    trial_id: str,
    *,
    judge_model_config: core_models.ModelConfig | None = None,
) -> None:
    """Put ``task_description`` on the runner through its own ``RegisterTrial``.

    The spec round-trips through JSON, so the runner grades the config it rebuilt
    rather than the caller's object — which is why a caller that needs the graded
    config reads it back off ``servicer.trials[trial_id]``.

    ``judge_model_config`` rides the spec for a task declaring an ``llm_judge``
    rubric, which ``_grade_llm_judge`` refuses to grade without.
    """
    registered = servicer.RegisterTrial(
        register_request(
            trial_spec_json(
                task_description.model_dump(mode="json"),
                trial_id=trial_id,
                judge_model_config=judge_model_config,
            ),
            trial_id=trial_id,
        ),
        context,
    )
    assert registered.success is True, registered.error


def _grade_registered_trial(
    servicer: RunnerServiceImpl, context: Any, trial_id: str, llm_messages_json: str
) -> pb2.GradeTrialResponse:
    """``GradeTrial`` on an already-registered trial, response unasserted."""
    return servicer.GradeTrial(
        pb2.GradeTrialRequest(trial_id=trial_id, llm_messages_json=llm_messages_json),
        context,
    )


def _grade_pack_through_the_runner(
    servicer: RunnerServiceImpl,
    context: Any,
    task_description: runner_models.TaskDescription,
    case: _TrialCase,
    trial_id: str,
) -> pb2.GradeTrialResponse:
    """Register the task, replay ``case``'s calls, grade — the runner's whole path.

    The ``GradeTrialResponse`` comes back unasserted. A caller may be reading the
    RPC's own verdict — a failed audit is an outcome, not a fixture error — so
    asserting ``success`` here would consume the very fact it was called for.
    Registration is asserted, because a trial that never registered is a broken
    fixture whichever outcome the caller wants.
    """
    _register_pack(servicer, context, task_description, trial_id)
    _replay_authored_calls(servicer, context, trial_id, case)
    return _grade_registered_trial(servicer, context, trial_id, json.dumps(case.runner_messages))


def _runner_trace_checks_grade(
    servicer: RunnerServiceImpl,
    task_description: runner_models.TaskDescription,
    case: _TrialCase,
    trial_id: str,
    context: Any,
) -> pb2.Grade:
    """The runner's own ``GradeTrial`` verdict on ``case``, over real gRPC handlers."""
    response = _grade_pack_through_the_runner(servicer, context, task_description, case, trial_id)
    assert response.success is True, response.error
    return response.grade


def test_both_substrates_score_trace_checks_through_their_own_grading_path(
    test_data_dir, tmp_path, runner_service, mock_grpc_context
):
    """One authored pack, two substrates, one component score — each via its own path.

    The core engine's ``grade_trajectory`` and the runner's ``GradeTrial`` are
    driven separately, so the claim is about the two integration points rather
    than about the shared evaluator called twice: a substrate that never reaches
    it, or reaches it with a differently translated config, scores differently
    here. The two trials call the same tools with the same arguments and differ
    only in the order, so the ordering constraint is the only thing that can move
    the score.
    """
    pack = test_data_dir / "grading_parity" / _TRACE_CHECKS_TASK
    adapter = _parity_adapter(test_data_dir)
    core_config = adapter.get_grading_config(_TRACE_CHECKS_TASK)
    task_description = adapter.to_task_description(_TRACE_CHECKS_TASK)
    satisfying = _load_case(pack, "satisfying")
    violating = _load_case(pack, "violating")

    initial_state = adapter.get_task(_TRACE_CHECKS_TASK).initial_state
    core_ok, _ = _core_verdict(
        "trace_checks", core_config, satisfying, tmp_path, task_initial_state=initial_state
    )
    core_bad, _ = _core_verdict(
        "trace_checks", core_config, violating, tmp_path, task_initial_state=initial_state
    )
    runner_ok = _runner_trace_checks_grade(
        runner_service, task_description, satisfying, "trace_ok:0", mock_grpc_context
    )
    runner_bad = _runner_trace_checks_grade(
        runner_service, task_description, violating, "trace_bad:0", mock_grpc_context
    )

    assert (core_ok, core_bad) == (1.0, 0.0), (
        "the core engine does not discriminate the ordering constraint: satisfying "
        f"{core_ok} vs violating {core_bad}"
    )
    assert runner_ok.components.HasField("trace_checks"), (
        "the runner produced no trace_checks component, so the parity comparison "
        "below would hold on a component neither substrate scored"
    )
    assert runner_ok.components.trace_checks == pytest.approx(core_ok)
    assert runner_bad.components.trace_checks == pytest.approx(core_bad)
    # The per-constraint verdicts cross the wire beside the score, so the reviewer
    # reading grade.yaml learns which constraint moved it.
    (crossed,) = runner_bad.trace_checks
    assert (crossed.id, crossed.kind, crossed.passed, crossed.severity) == (
        "payment_looked_up_before_the_case_is_denied",
        "before",
        False,
        "scored",
    )


_TRACE_GATE_TASK = "trace_checks_gate"
_THE_GATE = "the_meter_was_not_reset"


def test_a_trace_gate_fails_the_trial_on_both_substrates(
    test_data_dir, tmp_path, runner_service, mock_grpc_context
):
    """The gate, not the threshold, and not one substrate only.

    The pack declares ``pass_threshold: 0.0``, so every score clears the bar and
    nothing but the gate can fail a trial here; and its scored constraint passes in
    both trials, so nothing but the gate can move the score either. The core
    engine's ``grade_trajectory`` and the runner's real gRPC ``GradeTrial`` are
    driven separately over the same pack, so a substrate that honours the gate at
    only one of the two integration points fails here and names which.
    """
    pack = test_data_dir / "grading_parity" / _TRACE_GATE_TASK
    adapter = _parity_adapter(test_data_dir)
    core_config = adapter.get_grading_config(_TRACE_GATE_TASK)
    task_description = adapter.to_task_description(_TRACE_GATE_TASK)
    satisfying = _load_case(pack, "satisfying")
    violating = _load_case(pack, "violating")

    core_ok = GradingEngine(core_config, task_dir=tmp_path).grade_trajectory(
        satisfying.core_trajectory, satisfying.state
    )
    core_bad = GradingEngine(core_config, task_dir=tmp_path).grade_trajectory(
        violating.core_trajectory, violating.state
    )
    runner_ok = _runner_trace_checks_grade(
        runner_service, task_description, satisfying, "trace_gate_ok:0", mock_grpc_context
    )
    runner_bad = _runner_trace_checks_grade(
        runner_service, task_description, violating, "trace_gate_bad:0", mock_grpc_context
    )

    assert (core_ok.binary_pass, runner_ok.binary_pass) == (True, True), (
        "the gate-clean trial does not pass, so the failure below would not be the "
        f"gate: core {core_ok.binary_pass}, runner {runner_ok.binary_pass}"
    )
    assert (core_bad.binary_pass, runner_bad.binary_pass) == (False, False), (
        "a tripped gate did not fail the trial on both substrates: core "
        f"{core_bad.binary_pass}, runner {runner_bad.binary_pass}"
    )
    # The scores are asserted beside the verdict so a substrate that fails the
    # trial by scoring the component differently is told apart from one honouring
    # the gate: at this threshold a 0.0 score alone would still pass.
    assert (core_ok.score, runner_ok.score) == (1.0, 1.0)
    assert (core_bad.score, runner_bad.score) == (0.0, 0.0)

    assert runner_bad.HasField("trace_checks_summary"), (
        "the runner sent no trace_checks_summary, so the comparisons below would "
        "hold against a default-valued message rather than a reported one"
    )
    assert core_bad.trace_checks_summary is not None
    assert (
        list(runner_bad.trace_checks_summary.failed_gate_ids)
        == core_bad.trace_checks_summary.failed_gate_ids
        == [_THE_GATE]
    )
    assert not runner_ok.trace_checks_summary.failed_gate_ids
    assert runner_bad.trace_checks_summary.gate_failed is True
    # This pack declares no alternatives, so both halves of the summary that name a
    # route are empty by the documented convention — asserted against the literals
    # rather than against each other, which two absences would satisfy. The route
    # halves carrying real values cross the wire in the lock below.
    assert (
        runner_bad.trace_checks_summary.winning_path,
        len(runner_bad.trace_checks_summary.paths),
    ) == ("", 0)
    assert (core_bad.trace_checks_summary.winning_path, core_bad.trace_checks_summary.paths) == (
        "",
        [],
    )
    # A grade that fails a trial has to say why in the field a reviewer reads.
    assert f"FAILED trace gates: {_THE_GATE}" in core_bad.reasons
    assert f"FAILED trace gates: {_THE_GATE}" in runner_bad.reasons

    assert [(item.id, item.severity, item.passed) for item in runner_bad.trace_checks] == [
        ("the_meter_was_read", "scored", True),
        (_THE_GATE, "gate", False),
    ], "severity does not ride the per-constraint verdicts across the wire"


_TRACE_ALTERNATIVES_TASK = "trace_checks_alternatives"


def test_the_winning_route_crosses_the_wire(
    test_data_dir, tmp_path, runner_service, mock_grpc_context
):
    """Both substrates name the same winner, and the losing route's score survives.

    Driven on the two-route pack, where the winner and the per-route scores are
    values a build that synthesised or dropped the summary cannot produce. The
    violating trial takes one step of each route, so the two tie and the winner is
    the first declared — the tie-break, asserted where it crosses the wire rather
    than only inside the evaluator.
    """
    pack = test_data_dir / "grading_parity" / _TRACE_ALTERNATIVES_TASK
    adapter = _parity_adapter(test_data_dir)
    core_config = adapter.get_grading_config(_TRACE_ALTERNATIVES_TASK)
    task_description = adapter.to_task_description(_TRACE_ALTERNATIVES_TASK)
    ledger, cherry_picked = _load_case(pack, "satisfying"), _load_case(pack, "violating")

    core_won = GradingEngine(core_config, task_dir=tmp_path).grade_trajectory(
        ledger.core_trajectory, ledger.state
    )
    runner_won = _runner_trace_checks_grade(
        runner_service, task_description, ledger, "trace_alt_ok:0", mock_grpc_context
    )
    runner_tied = _runner_trace_checks_grade(
        runner_service, task_description, cherry_picked, "trace_alt_tied:0", mock_grpc_context
    )

    assert runner_won.HasField("trace_checks_summary")
    assert core_won.trace_checks_summary is not None
    assert (
        runner_won.trace_checks_summary.winning_path
        == core_won.trace_checks_summary.winning_path
        == "confirmed_against_the_ledger"
    )
    assert [
        (path.id, path.score, path.gate_failed) for path in runner_won.trace_checks_summary.paths
    ] == [
        ("confirmed_against_the_ledger", 1.0, False),
        ("confirmed_against_the_statement", 0.0, False),
    ], "the losing route's own score does not cross the wire, so the max is unauditable"

    assert [(path.id, path.score) for path in runner_tied.trace_checks_summary.paths] == [
        ("confirmed_against_the_ledger", 0.5),
        ("confirmed_against_the_statement", 0.5),
    ]
    assert runner_tied.trace_checks_summary.winning_path == "confirmed_against_the_ledger", (
        "the two routes tie and the winner is not the first declared, so the "
        "tie-break the grade reports depends on something other than declaration order"
    )


_TRACE_UNDECIDED_TASK = "trace_checks_undecided"
_THE_UNDECIDED_CONSTRAINT = "the_case_was_denied_successfully"

_UNDECIDED_LOOKUP = ToolCall(
    id="call_lookup", name="billing_api_get_payment", arguments={"payment_id": "PAY-664306"}
)
_UNDECIDED_DENIAL = ToolCall(
    id="call_denial", name="servicenow_csm_update_case", arguments={"case_id": "CS-1042"}
)

# Both trials ask for both calls. They differ only in what the runner then did
# with the denial, which is the difference the lock is about — so the message
# view is one value rather than two that could drift apart.
_UNDECIDED_VIEW = [
    Message(role="user", content="The customer says this refund is a duplicate. Deny the case."),
    Message(
        role="assistant",
        content="Checking the payment, then denying the case.",
        tool_calls=[_UNDECIDED_LOOKUP, _UNDECIDED_DENIAL],
    ),
]


def _execute_declared_call(
    servicer: RunnerServiceImpl, context: Any, trial_id: str, call: ToolCall, *, succeeds: bool
) -> pb2.ExecuteToolResponse:
    """Run one of the trial's declared calls through the runner's own handler."""

    async def answer(_arguments: dict[str, Any]) -> str:
        if not succeeds:
            raise RuntimeError("the case service rejected the update")
        return '{"case_id": "CS-1042", "state": "closed"}'

    servicer.trials[trial_id].agent_tools[call.name] = answer
    return servicer.ExecuteTool(
        execute_request(trial_id, call.name, json.dumps(call.arguments), call_id=call.id),
        context,
    )


def _runner_undecided_grade(
    servicer: RunnerServiceImpl,
    task_description: runner_models.TaskDescription,
    trial_id: str,
    context: Any,
    *,
    the_denial_ran: bool,
) -> tuple[pb2.Grade, tuple[RecordedToolCall, ...]]:
    """The runner's verdict on the declared pair, and the record it wrote getting there.

    The record comes back with the grade because the core half is driven from it:
    a hand-written counterpart could describe a trial the runner did not have, and
    then the two substrates would be graded on two different trials.
    """
    _register_pack(servicer, context, task_description, trial_id)

    lookup = _execute_declared_call(servicer, context, trial_id, _UNDECIDED_LOOKUP, succeeds=True)
    assert lookup.status == pb2.EXECUTION_STATUS_SUCCESS, lookup.error_message
    if the_denial_ran:
        denial = _execute_declared_call(
            servicer, context, trial_id, _UNDECIDED_DENIAL, succeeds=False
        )
        assert denial.status == pb2.EXECUTION_STATUS_ERROR, (
            "the denial was meant to run and fail, so a success here would make the "
            f"contrast case decidable for the wrong reason: {denial.status}"
        )

    response = servicer.GradeTrial(
        pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps([_wire_message(message) for message in _UNDECIDED_VIEW]),
        ),
        context,
    )
    assert response.success is True, response.error
    return response.grade, tuple(servicer.trials[trial_id].tool_call_history)


def _core_undecided_verdict(
    core_config: core_models.GradingConfig,
    recorded: Sequence[RecordedToolCall],
    task_dir: Path,
) -> core_models.TraceConstraintResult:
    """The core engine's own verdict on the trial the runner just recorded."""
    grade = GradingEngine(core_config, task_dir=task_dir).grade_trajectory(
        Trajectory(
            task_id=_TRACE_UNDECIDED_TASK,
            trial_index=0,
            start_ts=_FIXTURE_TIMESTAMP,
            end_ts=_FIXTURE_TIMESTAMP,
            messages=_UNDECIDED_VIEW,
            tool_log=list(recorded),
        ),
        {},
    )
    (verdict,) = grade.trace_check_results
    return verdict


def test_an_undecided_verdict_crosses_the_wire_as_the_fact_it_is(
    test_data_dir, tmp_path, runner_service, mock_grpc_context
):
    """A constraint nobody can decide reaches the host as undecided, not as false.

    The trial that makes the claim non-vacuous: the runner really ran, and the
    evidence for this constraint is still missing, because the agent declared a
    call the loop never executed. ``passed`` alone cannot tell that apart from the
    agent failing the constraint outright, so the contrast is driven beside it —
    the same declared pair with the denial executed and rejected, which is
    decidably false. Both substrates answer both trials, so a field the runner
    computes and never sends fails here rather than reaching a reviewer as a
    verdict about the agent.
    """
    adapter = _parity_adapter(test_data_dir)
    core_config = adapter.get_grading_config(_TRACE_UNDECIDED_TASK)
    task_description = adapter.to_task_description(_TRACE_UNDECIDED_TASK)

    runner_unrecorded, unrecorded_log = _runner_undecided_grade(
        runner_service,
        task_description,
        "trace_undecided:0",
        mock_grpc_context,
        the_denial_ran=False,
    )
    runner_failed, failed_log = _runner_undecided_grade(
        runner_service, task_description, "trace_denied:0", mock_grpc_context, the_denial_ran=True
    )
    core_unrecorded = _core_undecided_verdict(core_config, unrecorded_log, tmp_path)
    core_failed = _core_undecided_verdict(core_config, failed_log, tmp_path)

    assert [record.tool_name for record in unrecorded_log] == [_UNDECIDED_LOOKUP.name], (
        "the runner recorded the denial, so the undecided trial's evidence is not "
        "missing and the verdict below would be undecided for no reason"
    )
    assert [(record.tool_name, record.status.value) for record in failed_log] == [
        (_UNDECIDED_LOOKUP.name, "success"),
        (_UNDECIDED_DENIAL.name, "error"),
    ]

    (crossed_unrecorded,) = runner_unrecorded.trace_checks
    (crossed_failed,) = runner_failed.trace_checks
    assert (crossed_unrecorded.id, crossed_failed.id) == (
        _THE_UNDECIDED_CONSTRAINT,
        _THE_UNDECIDED_CONSTRAINT,
    )
    assert (crossed_unrecorded.passed, crossed_unrecorded.undecided) == (False, True), (
        "the runner's own verdict on a trial whose evidence it never recorded is "
        f"{(crossed_unrecorded.passed, crossed_unrecorded.undecided)}, and the wire "
        f"says {crossed_unrecorded.message!r}"
    )
    assert (crossed_failed.passed, crossed_failed.undecided) == (False, False), (
        "the executed-and-rejected denial is decidably false, so an undecided verdict "
        f"here would mean the field tracks failure rather than missing evidence: "
        f"{crossed_failed.message!r}"
    )
    assert (core_unrecorded.passed, core_unrecorded.undecided) == (False, True)
    assert (core_failed.passed, core_failed.undecided) == (False, False)


# --------------------------------------------------------------------------
# The runner's own registration path reaches the DB-reading packs
# --------------------------------------------------------------------------

_STATE_READING_PACKS = (
    ("state_checks_jsonpaths", "state_checks"),
    ("custom_checks", "custom_checks"),
)


@pytest.mark.parametrize(("task_id", "component"), _STATE_READING_PACKS)
def test_a_state_reading_pack_grades_through_the_runners_own_registration(
    task_id, component, test_data_dir, runner_service, mock_grpc_context
):
    """Both DB-reading parity packs are reachable through ``RegisterTrial`` and score.

    Every other parity pack grades off the transcript, so these two are the only
    ones whose runner path depends on the trial's DB service having been
    provisioned — and ``RegisterTrial`` provisions it from ``initial_state``, not
    from the trial fixture's ``state:``.

    Only the satisfying case is asserted. The rows come from ``initial_state``, so
    the violating case replays its messages against those same rows and scores
    identically; discriminating the two is lock 3's claim, made over the trial
    fixture's state. What is claimed here is reachability of the runner's own path.
    """
    adapter = _parity_adapter(test_data_dir)
    task_description = adapter.to_task_description(task_id)
    case = _load_case(test_data_dir / "grading_parity" / task_id, "satisfying")

    response = _grade_pack_through_the_runner(
        runner_service,
        mock_grpc_context,
        task_description,
        case,
        f"reachability_{task_id}:0",
    )

    assert response.success is True, response.error
    assert getattr(response.grade.components, component) == pytest.approx(1.0), (
        f"{task_id} reached GradeTrial but scored {component} at "
        f"{getattr(response.grade.components, component)}: the runner graded against "
        "a database RegisterTrial did not provision from initial_state"
    )


# --------------------------------------------------------------------------
# 11. Both substrates read every per-constraint config input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("author_key", _TRACE_CONFIG_INPUT_KEYS)
def test_both_substrates_read_each_per_constraint_config_input(
    author_key, test_data_dir, tmp_path, runner_service, mock_grpc_context
):
    """A field that shapes how a kind scores, driven the way lock 3 drives a scored key.

    Lock 3 selects ``SCORED_CHECK``, so these five escape it — they carry no
    component of their own, they change what one does. Each pack is authored so
    that a build ignoring the field scores its two trials *identically*: the
    weights are the only thing telling one from the other, or the unmatched
    anchor's policy is, or the turn window is, or which constraint is the gate is,
    or the argument the two matchers correlate on is. Discrimination here is
    therefore the field being read, not the constraint around it working.
    """
    verdict = _drive_both_substrates(
        author_key, test_data_dir, tmp_path, runner_service, mock_grpc_context
    )
    _assert_both_substrates_discriminate(author_key, verdict)
    _assert_the_substrates_agree(author_key, verdict)


# --------------------------------------------------------------------------
# 12. The manifest addresses the whole constraint vocabulary and nothing else
# --------------------------------------------------------------------------


def test_the_manifest_addresses_every_constraint_kind_and_no_other():
    """Three sources for the closed vocabulary, two equalities between them.

    The manifest's element paths are what makes a kind's parity claim enforceable,
    ``TRACE_CONSTRAINT_KINDS`` is what the evaluator and the runtime ledger read,
    and ``TraceConstraintExpr``'s fields are what an author can write. A kind
    added to the model alone is a check no fixture pack proves parity for and no
    recording site accounts for; a manifest entry with no field behind it
    addresses nothing.
    """
    addressed: set[str] = set()
    for item in GRADING_KEYS:
        if item.kind is not KeyKind.SCORED_CHECK or not item.author_key.startswith(
            "trace_checks.constraints."
        ):
            continue
        assert item.core_element_path is not None and item.core_element_path.startswith(
            "require."
        ), (
            f"{item.author_key}: a scored constraint kind is addressed under a "
            f"constraint's require expression, not at {item.core_element_path!r}"
        )
        addressed.add(item.core_element_path.removeprefix("require."))

    assert addressed == set(TRACE_CONSTRAINT_KINDS), (
        f"the manifest addresses {sorted(addressed)} where the vocabulary is "
        f"{sorted(kind.value for kind in TRACE_CONSTRAINT_KINDS)}. A kind with no entry has "
        f"no fixture pack "
        "and no parity claim; an entry with no kind addresses a payload nothing evaluates"
    )
    assert set(TraceConstraintExpr.model_fields) == set(TRACE_CONSTRAINT_KINDS), (
        f"TraceConstraintExpr declares {sorted(TraceConstraintExpr.model_fields)} where "
        f"the vocabulary is {sorted(kind.value for kind in TRACE_CONSTRAINT_KINDS)}. Every "
        f"field on that model "
        "is a constraint kind an author can write, so the two are the same set"
    )


# --------------------------------------------------------------------------
# 13. A state source that declares nothing is not scored, on either substrate
# --------------------------------------------------------------------------


def _messageless_trajectory(task_id: str) -> Trajectory:
    """A trial carrying no transcript, so only the state sources can score it."""
    return Trajectory(
        task_id=task_id,
        trial_index=0,
        start_ts=_FIXTURE_TIMESTAMP,
        end_ts=_FIXTURE_TIMESTAMP,
        messages=[],
    )


def _jsonpath_presence(
    assertions: list[dict[str, Any]], state: dict[str, Any]
) -> tuple[float | None, float | None]:
    """Each substrate's ``state_checks`` slot for one JSONPath assertion list.

    One list drives both columns through each substrate's own production evaluator
    and its own composer, so the two differ in nothing but what the author declared.
    """
    core_config = core_models.GradingConfig(
        state_checks={"jsonpaths": assertions},
        combine={"weights": {"state_checks": 1.0}},
    )
    core_component = (
        GradingEngine(core_config)
        .grade_trajectory(_messageless_trajectory("jsonpath_presence"), state)
        .components.state_checks
    )
    runner_jsonpath, _ = evaluate_jsonpath_checks(assertions, state=state)
    runner_component = resolve_state_checks_component(
        hash_score=-1.0,
        jsonpath_score=runner_jsonpath,
        db_probe_score=-1.0,
        hash_weight=None,
    ).component
    return core_component, runner_component


@pytest.mark.parametrize("declared", [True, False])
def test_an_empty_assertion_list_is_unscored_on_both_substrates(declared, test_data_dir):
    """``state_checks.jsonpaths`` at its degenerate boundary: nothing declared.

    The key claims ``BOTH_SCORE_PARITY``, and lock 3 proves it over a satisfying and
    a violating trial — both of which declare assertions. This is the boundary no
    cell of that sweep reaches: with nothing to evaluate, both substrates must leave
    the component absent rather than one of them reporting the fraction of an empty
    set. ``declared=True`` is the non-vacuity half — it reads the pack's own
    assertions off disk, so an implementation scoring nothing at all fails there
    instead of satisfying both rows.

    Built from each substrate's config model rather than from a pack, because the
    authoring gate refuses a ``state_checks`` block whose sources are all
    unevaluable. What still reaches these folds is a directly constructed config, a
    bundle recorded before that rule (which ``retrace`` replays), and the untyped
    ``hash`` block being mutated after validation.
    """
    pack = _pack_dir(test_data_dir, _JSONPATHS_KEY)
    authored = yaml.safe_load((pack / "grading.yaml").read_text())["state_checks"]["jsonpaths"]
    assert authored, (
        f"{pack}/grading.yaml declares no JSONPath assertions, so the declared column "
        "below carries an empty list too and this test compares one cell against itself"
    )
    case = _load_case(pack, "satisfying")

    core, runner = _jsonpath_presence(authored if declared else [], case.state)

    if not declared:
        assert core is None, (
            f"core scored state_checks {core!r} over zero assertions — a fraction of "
            "nothing read as a verdict, which is the whole of what this lock forbids"
        )
        assert runner is None, (
            f"the runner scored state_checks {runner!r} over zero assertions, so its "
            "not-evaluated sentinel is no longer reaching the shared composer"
        )
        return

    assert isinstance(core, float) and isinstance(runner, float), (
        f"a declared assertion list left state_checks unscored — core {core!r}, runner "
        f"{runner!r}. The unscored rows above then hold for every input and prove nothing"
    )
    assert core == pytest.approx(runner), (
        f"{_JSONPATHS_KEY} claims BOTH_SCORE_PARITY but the substrates score the pack's "
        f"own assertions differently: core {core} vs runner {runner}"
    )


def _probe_component(
    probes: list[dict[str, Any]], rows: list[dict[str, Any]], monkeypatch
) -> float | None:
    """The runner's ``state_checks`` slot from its real probe evaluator over ``rows``.

    ``rows`` stands in for the postgres the probe queries, at the seam
    ``_fetch_probe_rows`` exists to provide — the DSN resolves only inside the task's
    docker network, which is why the manifest enforces this key at the integration
    tier. The probe's own ``expect`` assertions and the fold that reads the score are
    the real ones.
    """

    async def _rows(dsn: str, query: str) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(runner_grading, "_fetch_probe_rows", _rows)
    probe_score, _ = asyncio.run(evaluate_db_probes(probes))
    return resolve_state_checks_component(
        hash_score=-1.0,
        jsonpath_score=-1.0,
        db_probe_score=probe_score,
        hash_weight=None,
    ).component


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param([{"reason_code": "CAPA-01", "status": "open"}], 1.0, id="the_probe_holds"),
        pytest.param([{"reason_code": "GUESS", "status": "open"}], 0.0, id="the_probe_fails"),
    ],
)
def test_a_probe_only_pack_is_unscored_core_side_and_scored_by_the_runner(
    rows, expected, test_data_dir, monkeypatch
):
    """The one asymmetry this presence rule is allowed to have, asserted not assumed.

    ``state_checks.db_probes`` is the pack's only state source — enforced rather than
    conventional, since a probe beside a non-empty ``jsonpaths`` or an enabled hash with a
    source loads on neither substrate — and it is ``RUNNER_ONLY``: the probe DSN resolves
    inside the task's docker network, which the runner container joins and the host-side
    engine does not. So core evaluating nothing here is the declared design rather than a
    regression, and the manifest entry is read to say so. Both rows fill the runner's slot
    and they differ, so what sits in it is the probe's verdict rather than a constant.
    """
    assert entry(_PROBES_KEY).coverage is SubstrateCoverage.RUNNER_ONLY, (
        f"{_PROBES_KEY} no longer claims RUNNER_ONLY, so core leaving the component "
        "unscored below is a parity gap rather than the declared asymmetry"
    )
    adapter = _adapter_for(test_data_dir, _TASKS_GLOB)
    grading_config = adapter.get_grading_config(_PROBE_PACK)
    probes = grading_config.state_checks.db_probes
    assert probes, f"{_PROBE_PACK} declares no db_probes, so the runner column scores nothing"

    core = (
        GradingEngine(grading_config)
        .grade_trajectory(_messageless_trajectory(_PROBE_PACK), {})
        .components.state_checks
    )

    assert core is None, (
        f"core scored state_checks {core!r} on a pack whose only state source it cannot "
        "read, so the number can only have come from the empty assertion list beside it"
    )
    assert _probe_component(probes, rows, monkeypatch) == pytest.approx(expected)


# --------------------------------------------------------------------------
# 14. combine.weights: every scored component declares a share, and a fold with
#     no share to read decides rather than defaulting
# --------------------------------------------------------------------------

_WEIGHTS_KEY = "combine.weights"

#: The weight maps the differential drives the same two scored components under, keyed by
#: what each does to the fold. Lock 8 asserts the set still spans a map omitting a scored
#: component *and* one declaring every scored component: hollowed down to complete maps the
#: refusal is never reached, and the differential would prove only that two folds which
#: both aggregate agree.
_SCORED_PAIR = frozenset({"state_checks", "transcript_rules"})

_WEIGHT_MAPS: dict[str, dict[str, float]] = {
    "declares_neither_scored_component": {},
    "omits_transcript_rules": {"state_checks": 1.0},
    "omits_state_checks": {"transcript_rules": 1.0},
    "declares_every_scored_component": {"state_checks": 1.0, "transcript_rules": 1.0},
}

#: What both substrates owe for one component scored at an explicit weight of ``0.0``,
#: per (method, case). Written out rather than derived: ``weighted`` has nothing to average
#: and decides without one, while ``all`` and ``any`` aggregate the component *set* and
#: never read a weight, so a share of ``0.0`` is inert there and the component's own score
#: is still the verdict. The two cases differ under ``all``/``any``, so a rule scoping the
#: zero-weight verdict to every method reds on those four rows.
_ZERO_WEIGHT_VERDICTS: dict[tuple[str, str], tuple[float, bool]] = {
    ("weighted", "satisfying"): (0.0, False),
    ("weighted", "violating"): (0.0, False),
    ("all", "satisfying"): (1.0, True),
    ("all", "violating"): (0.0, False),
    ("any", "satisfying"): (1.0, True),
    ("any", "violating"): (0.0, False),
}

_WEIGHTS_THRESHOLD = 0.8


def _scored_pair_configs(
    assertions: list[dict[str, Any]], weights: dict[str, float], *, method: str = "weighted"
) -> tuple[core_models.GradingConfig, dict[str, Any]]:
    """One authored intent as each substrate's own grading config.

    ``state_checks`` is scored from the pack's own JSONPath assertions, a
    ``BOTH_SCORE_PARITY`` key, so the two columns fold the same number for it and the map is
    the only thing that differs. ``transcript_rules`` is scored by each substrate's own
    evaluator, whose magnitudes are separately tracked — which is why every assertion below
    is about whether the fold *refuses*, never about the number it returns.
    """
    core_config = core_models.GradingConfig(
        state_checks={"jsonpaths": assertions},
        transcript_rules={"max_turns": 5},
        combine={"method": method, "weights": weights, "pass_threshold": _WEIGHTS_THRESHOLD},
    )
    runner_config = {
        "combine_method": method,
        "weights": weights,
        "pass_threshold": _WEIGHTS_THRESHOLD,
        "state_checks": {"jsonpaths": assertions},
        "transcript_rules": {"max_turns": 5},
    }
    return core_config, runner_config


@pytest.mark.parametrize("case", sorted(_WEIGHT_MAPS))
def test_a_scored_component_with_no_declared_weight_refuses_on_both_substrates(case, test_data_dir):
    """The membership question, answered the same way on both substrates.

    Neither may pick a share for a component it scored and the map does not weight: an
    implementation dropping it from core's numerator, its denominator and its aggregation
    while the runner folds it in at ``1.0`` scores the same trial two ways for no reason an
    author could read. Both raise the same type with the same message instead, naming the
    component and both one-line fixes.

    The complete map is the non-vacuity half. Without it an implementation refusing every
    fold satisfies the three refusing rows, and this would prove only that grading is
    broken rather than that the map is read.
    """
    pack = _pack_dir(test_data_dir, _JSONPATHS_KEY)
    authored = yaml.safe_load((pack / "grading.yaml").read_text())["state_checks"]["jsonpaths"]
    trial = _load_case(pack, "violating")
    weights = _WEIGHT_MAPS[case]
    core_config, runner_config = _scored_pair_configs(authored, weights)
    jsonpath_score, _ = evaluate_jsonpath_checks(authored, state=trial.state)
    runner_components = {
        "jsonpath_score": jsonpath_score,
        "transcript_score": evaluate_transcript_rules(
            trial.runner_timeline,
            runner_models.TranscriptRulesConfig(**runner_config["transcript_rules"]),
        ).score,
    }

    if set(weights) >= _SCORED_PAIR:
        core = GradingEngine(core_config).grade_trajectory(trial.core_trajectory, trial.state)
        runner = combine_grade_components(runner_components, runner_config)
        assert isinstance(core.score, float) and isinstance(runner.score, float), (
            "a map declaring every scored component still refused, so the refusing rows "
            "above hold for every map and the fold is proven to read nothing"
        )
        return

    unweighted = sorted(_SCORED_PAIR - set(weights))
    with pytest.raises(MissingComponentWeight) as core_refusal:
        GradingEngine(core_config).grade_trajectory(trial.core_trajectory, trial.state)
    with pytest.raises(MissingComponentWeight) as runner_refusal:
        combine_grade_components(runner_components, runner_config)

    for refusal in (core_refusal, runner_refusal):
        message = str(refusal.value)
        unnamed = f"{_WEIGHTS_KEY} omits {unweighted} and the refusal names none: {message}"
        assert any(name in message for name in unweighted), unnamed
        assert f"{_WEIGHTS_KEY}." in message, message


@pytest.mark.parametrize("case", ["satisfying", "violating"])
@pytest.mark.parametrize("method", COMBINE_METHODS)
def test_a_zero_total_weight_decides_under_weighted_alone_on_both_substrates(
    method, case, test_data_dir
):
    """One component scored at an explicit share of ``0.0``, on every declared method.

    Under ``weighted`` there is nothing to average and the trial earned nothing. An
    implementation answering the zero denominator with a free ``1.0`` instead passes a trial
    whose only scored component the author weighted out. Under ``all`` and ``any`` the
    weight is structurally unread — the shared dispatch aggregates the
    component set — so those verdicts are asserted **unchanged**, which is what stops the
    rule being scoped to every method and breaking an agreement that was already correct.
    """
    pack = _pack_dir(test_data_dir, _JSONPATHS_KEY)
    authored = yaml.safe_load((pack / "grading.yaml").read_text())["state_checks"]["jsonpaths"]
    trial = _load_case(pack, case)
    core_config = core_models.GradingConfig(
        state_checks={"jsonpaths": authored},
        combine={
            "method": method,
            "weights": {"state_checks": 0.0},
            "pass_threshold": _WEIGHTS_THRESHOLD,
        },
    )
    jsonpath_score, _ = evaluate_jsonpath_checks(authored, state=trial.state)

    core = GradingEngine(core_config).grade_trajectory(trial.core_trajectory, trial.state)
    runner = combine_grade_components(
        {"jsonpath_score": jsonpath_score},
        {
            "combine_method": method,
            "weights": {"state_checks": 0.0},
            "pass_threshold": _WEIGHTS_THRESHOLD,
            "state_checks": {"jsonpaths": authored},
        },
    )

    expected = _ZERO_WEIGHT_VERDICTS[(method, case)]
    assert (core.score, core.binary_pass) == expected
    assert (runner.score, runner.binary_pass) == expected
    if method != "weighted":
        return
    for reasons in (core.reasons, runner.reason or ""):
        unnamed = f"the {method} fold counted nothing and named no component: {reasons!r}"
        assert "state_checks" in reasons, unnamed


@pytest.mark.parametrize(
    ("weights", "expected", "named"),
    [
        pytest.param({}, (1.0, True), None, id="asks_for_nothing_and_weights_nothing"),
        pytest.param({"state_checks": 1.0}, (0.0, False), "state_checks", id="a_stray_weight"),
    ],
)
def test_a_config_scoring_nothing_passes_only_when_it_asked_for_nothing(weights, expected, named):
    """The two ways a fold reaches no scored component at all, and they answer differently.

    Nothing requested and nothing weighted is the deliberately non-scoring pack: a pass
    earned by nothing, which is right because nothing was asked for. A weight naming a
    component the config never configured is not that — it claims a component the pack does
    not have, so nothing was scored and the trial fails with the weight key named. Both cells
    are a decision written in one place: an implementation reaching the first from a threshold
    comparison against ``0.0`` answers the second with a pass, and one refusing every
    unscored trial answers the first with a fail.

    Built from each substrate's config rather than from a pack, because the authoring gate
    refuses a stray weight. What still reaches these folds is a directly constructed config
    and a bundle recorded before that rule.
    """
    core_config = core_models.GradingConfig(
        combine={"method": "weighted", "weights": weights, "pass_threshold": _WEIGHTS_THRESHOLD}
    )
    runner_config = {
        "combine_method": "weighted",
        "weights": weights,
        "pass_threshold": _WEIGHTS_THRESHOLD,
    }

    core = GradingEngine(core_config).grade_trajectory(_messageless_trajectory("no_components"), {})
    runner = combine_grade_components({}, runner_config)

    assert (core.score, core.binary_pass) == expected
    assert (runner.score, runner.binary_pass) == expected
    if named is None:
        return
    assert named in core.reasons, core.reasons
    assert named in (runner.reason or ""), runner.reason


# --------------------------------------------------------------------------
# 15. Every ledger key's recording site is driven through a real GradeTrial
# --------------------------------------------------------------------------


class _Driver(str, Enum):
    """How lock 15 gets one ledger key populated with its evaluator able to run."""

    PARITY_PACK = "parity_pack"
    HASH_FAMILY = "hash_family"
    EXPECT_INITIAL_STATE = "expect_initial_state"
    DB_PROBES = "db_probes"
    LLM_JUDGE = "llm_judge"


_LEDGER_KEY_DRIVERS: Mapping[str, _Driver] = MappingProxyType(
    {
        "custom_checks": _Driver.PARITY_PACK,
        "state_checks.jsonpaths": _Driver.PARITY_PACK,
        "transcript_rules.must_contain": _Driver.PARITY_PACK,
        "transcript_rules.disallow_regex": _Driver.PARITY_PACK,
        "transcript_rules.max_turns": _Driver.PARITY_PACK,
        "transcript_rules.min_assistant_turns": _Driver.PARITY_PACK,
        "transcript_rules.tool_expectations": _Driver.PARITY_PACK,
        "transcript_rules.required_actions": _Driver.PARITY_PACK,
        "transcript_rules.communicate_info": _Driver.PARITY_PACK,
        "trace_checks.constraints": _Driver.PARITY_PACK,
        "trace_checks.alternatives": _Driver.PARITY_PACK,
        "trace_checks.constraints.present": _Driver.PARITY_PACK,
        "trace_checks.constraints.absent": _Driver.PARITY_PACK,
        "trace_checks.constraints.count": _Driver.PARITY_PACK,
        "trace_checks.constraints.before": _Driver.PARITY_PACK,
        "trace_checks.constraints.immediately_before": _Driver.PARITY_PACK,
        "trace_checks.constraints.absent_before": _Driver.PARITY_PACK,
        "trace_checks.constraints.absent_between": _Driver.PARITY_PACK,
        "trace_checks.constraints.all_of": _Driver.PARITY_PACK,
        "trace_checks.constraints.any_of": _Driver.PARITY_PACK,
        "trace_checks.constraints.negate": _Driver.PARITY_PACK,
        "state_checks.hash.enabled": _Driver.HASH_FAMILY,
        "state_checks.hash.golden_actions": _Driver.HASH_FAMILY,
        "state_checks.hash.expect_initial_state": _Driver.EXPECT_INITIAL_STATE,
        "state_checks.db_probes": _Driver.DB_PROBES,
        "llm_judge": _Driver.LLM_JUDGE,
    }
)
"""Which driver can populate each ledger key and let its evaluator run.

Written out per key, like ``accountable_author_keys()`` and for the same reason:
comprehended from :data:`LEDGER_KEYS` the totality assertion below would compare
that set against itself, and a key nothing can drive would join the sweep as a row
that silently proves nothing.
"""

_LEDGER_SITE_KEYS = tuple(item.author_key for item in LEDGER_KEYS if item.runner_field is not None)


def test_every_ledger_key_names_a_driver_that_can_populate_it():
    """A ledger key absent from the table would drop out of the sweep, not fail it."""
    undriven = sorted(set(_LEDGER_SITE_KEYS) - set(_LEDGER_KEY_DRIVERS))
    stale = sorted(set(_LEDGER_KEY_DRIVERS) - set(_LEDGER_SITE_KEYS))

    assert not undriven and not stale, (
        f"the lock-15 driver table names no driver for {undriven or 'nothing'} and "
        f"names {stale or 'nothing'} that is not a ledger key with a runner_field. "
        "Every ledger key needs a driver here, or its recording site is unverified"
    )


def _assert_the_site_reported(
    author_key: str,
    grading_config: runner_models.RunnerGradingConfig,
    response: pb2.GradeTrialResponse,
) -> None:
    """Assert the runner reported ``author_key`` the way the manifest implies.

    ``grading_config`` is the config the runner *graded* — read off the registered
    trial, not the test's own copy, because the spec round-trips through JSON and
    the two are siblings.

    The manifest decides which outcome to expect and the ledger supplies the text,
    so neither this module nor a task author can spell an expectation production
    never emits.
    """
    item = entry(author_key)

    assert author_key in populated_ledger_keys(grading_config), (
        f"{author_key}: the config the runner graded does not populate the key, so "
        "the audit never visited it and every claim below would hold over a config "
        "that asked for nothing"
    )
    assert response.success is True, (
        f"{author_key}: GradeTrial failed, which is what the audit does when no "
        f"recording site filed the key — {response.error}"
    )

    assert item.runner_evaluator is not None, (
        f"{author_key}: the manifest gives it no runner evaluator, so this lock cannot "
        "say what its recording site should have filed — a ledger key the runner never "
        "evaluates needs a standing-skip record and a site that files it"
    )
    assert skip_note_prefix(author_key) not in response.grade.reasons, (
        f"{author_key} declares the runner evaluator {item.runner_evaluator}, so its "
        "recording site must file EVALUATED — the grade instead carries a skip note "
        f"for it: {response.grade.reasons!r}"
    )
    component = author_key.split(".")[0]
    assert getattr(response.grade.components, component) != _UNSCORED_COMPONENT, (
        f"{author_key} was recorded as evaluated, but the {component} component it "
        "feeds is the unscored sentinel — the site filed an evaluation that did not "
        "happen"
    )


def _ledger_trial_id(label: str, author_key: str) -> str:
    """A trial id unique to one caller and one key.

    The DB service keeps a trial's rows for the whole session, so two tests naming
    the same trial collide at ``RegisterTrial`` instead of grading. Sibling keys
    sharing a driver — the three hash-family members — still need one trial each.
    """
    return f"ledger_site_{label}_{_task_id_for(author_key)}:0"


def _drive_parity_pack(
    author_key: str,
    test_data_dir: Path,
    servicer: RunnerServiceImpl,
    context: Any,
    *,
    trial_id: str,
) -> tuple[runner_models.RunnerGradingConfig, pb2.GradeTrialResponse]:
    """Grade the key's own pack, and hand back the config the runner actually graded."""
    task_description = _parity_adapter(test_data_dir).to_task_description(_task_id_for(author_key))
    case = _load_case(_pack_dir(test_data_dir, author_key), "satisfying")
    response = _grade_pack_through_the_runner(servicer, context, task_description, case, trial_id)
    return servicer.trials[trial_id].grading_config, response


_HASH_DRIVER_TOOL = "ship_order"

_HASH_DRIVER_TASK: dict[str, Any] = {
    "task_id": "ledger_hash_family",
    "name": "Ledger hash family",
    "category": "test",
    "description": "A hash-graded trial, for the hash family's recording site",
    "adapter_type": "native",
    "system_prompt": "You are a test assistant.",
    "initial_state": {
        "tables": {"orders": [{"order_id": "O1", "status": "pending"}]},
        "schemas": [
            {
                "table_name": "orders",
                "fields": {"order_id": "string", "status": "string"},
                "primary_key": "order_id",
            }
        ],
    },
    # The golden action resolves against the trial's registered callables rather
    # than against declared tool sources, so the tool is bound after registration
    # and declaring it here would only ask the runner to reconstruct it.
    "agent_tools": [],
    "user_tools": [],
    "grading": {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 0.5,
        "state_checks": {
            "hash_enabled": True,
            "golden_actions": [{"tool_name": _HASH_DRIVER_TOOL, "arguments": {"order_id": "O1"}}],
        },
    },
}

_INITIAL_ORDERS = {"orders": [{"order_id": "O1", "status": "pending"}]}

#: The trial's own database, one record away from the state it started in. Both
#: substrates read their two sides from these two mappings — the runner by seeding
#: db-service with the first and mutating it into the second, core by hashing the
#: first as the task's declared initial state and the second as the trial's — so a
#: verdict that differs between them is the grading path's doing.
_ORDERS_AFTER_A_CHANGE = {"orders": [{"order_id": "O1", "status": "shipped"}]}

_EXPECT_INITIAL_STATE_TASK: dict[str, Any] = {
    "task_id": "ledger_expect_initial_state",
    "name": "Ledger expect_initial_state",
    "category": "test",
    "description": "A refusal-shaped trial whose expected final state is its initial state",
    "adapter_type": "native",
    "system_prompt": "You are a test assistant.",
    "initial_state": {
        "tables": _INITIAL_ORDERS,
        "schemas": [
            {
                "table_name": "orders",
                "fields": {"order_id": "string", "status": "string"},
                "primary_key": "order_id",
            }
        ],
    },
    "agent_tools": [],
    "user_tools": [],
    "grading": {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 0.5,
        "state_checks": {"hash_enabled": True, "expect_initial_state": True},
    },
}
"""A second hash-driver task, because ``_HASH_DRIVER_TASK`` cannot carry this key.

That one declares ``golden_actions``, and the authored block refuses
``expect_initial_state`` beside it — so a driver sharing it would fail lock 15's
first claim, the key never being populated on the config the runner graded.
"""

#: What the same pack declares to the core engine, which takes the authored block and
#: the task's own initial state rather than the runner's flattened naming.
_EXPECT_INITIAL_STATE_GRADING: dict[str, Any] = {
    "combine": {"method": "weighted", "weights": {"state_checks": 1.0}, "pass_threshold": 0.5},
    "state_checks": {"hash": {"enabled": True, "expect_initial_state": True}},
}

_JUDGE_DRIVER_TASK: dict[str, Any] = {
    "task_id": "ledger_llm_judge",
    "name": "Ledger llm judge",
    "category": "test",
    "description": "A rubric-graded trial, for the judge's recording site",
    "adapter_type": "native",
    "system_prompt": "You are a test assistant.",
    "initial_state": {},
    "agent_tools": [],
    "user_tools": [],
    "grading": {
        "combine_method": "weighted",
        "weights": {"llm_judge": 1.0},
        "pass_threshold": 0.5,
        "llm_judge": {
            "rubric": {
                "criteria": [
                    {
                        "id": "explained",
                        "description": "the agent explained the outcome",
                        "kind": "binary",
                        "weight": 1.0,
                    }
                ]
            }
        },
    },
}

_JUDGE_TRANSCRIPT = json.dumps(
    [{"role": "assistant", "content": "I shipped order O1, so the request is complete."}]
)
_PROBE_TRANSCRIPT = json.dumps([{"role": "assistant", "content": "Corrective action recorded."}])
_HASH_TRANSCRIPT = json.dumps([{"role": "assistant", "content": "Order O1 is shipped."}])

#: Rows standing in for the postgres the pack's probe queries. The DSN resolves only
#: inside the task's docker network, which is why the manifest enforces this key's
#: *score* at the integration tier — orthogonal to whether its *recording site* can be
#: driven here, which is what lock 15 asks.
_PROBE_ROWS = [{"reason_code": "CAPA-01", "status": "open"}]


class _StubJudge:
    """Stands in for the model provider at the seam ``service.LLMJudge`` names.

    What it replaces is an external LLM call, not a recording site, an evaluator's
    decision, the audit or the combine — all of those stay real on this path.
    """

    def __init__(self, model_config: Any, **_kwargs: Any) -> None:
        self.model_config = model_config

    def run(self, **_kwargs: Any) -> JudgeResult:
        return JudgeResult(
            status=JudgeStatus.COMPLETED, usage=JudgeUsage(), reasons="ok", score=1.0
        )


async def _shipped(arguments: dict[str, Any]) -> str:
    return json.dumps({"order_id": arguments["order_id"], "status": "shipped"})


def _drive_hash_family(
    servicer: RunnerServiceImpl, context: Any, *, trial_id: str
) -> tuple[runner_models.RunnerGradingConfig, pb2.GradeTrialResponse]:
    """Grade a hash-enabled trial whose one golden action names a registered tool.

    Asserts the replay ran whole. The family is accounted for from the basis the
    evaluator returns, and it returns one whenever it completes — including when every
    golden action failed to take effect, which still grades ``success=True`` with
    ``state_checks == 1.0``. All four of lock 15's claims hold on that hollow
    evaluation, so without this guard claim 4 would mean nothing for the hash family.
    """
    task_description = runner_models.TaskDescription.model_validate(_HASH_DRIVER_TASK)
    _register_pack(servicer, context, task_description, trial_id)
    servicer.trials[trial_id].agent_tools[_HASH_DRIVER_TOOL] = _shipped

    response = _grade_registered_trial(servicer, context, trial_id, _HASH_TRANSCRIPT)

    assert "GOLDEN REPLAY ERRORS" not in response.grade.reasons, (
        "the golden replay did not run whole, so the hash verdict was computed "
        f"against a partial golden world and evaluating it proves nothing: "
        f"{response.grade.reasons!r}"
    )
    return servicer.trials[trial_id].grading_config, response


def _drive_expect_initial_state(
    servicer: RunnerServiceImpl, context: Any, *, trial_id: str, changed: bool = False
) -> tuple[runner_models.RunnerGradingConfig, pb2.GradeTrialResponse]:
    """Grade a trial whose declared hash source is the state its task starts in.

    ``changed`` moves the trial's database away from that state through the same
    db-service the evaluator resets and hashes, so the two cells differ in what the
    trial left behind rather than in what the pack declared.
    """
    task_description = runner_models.TaskDescription.model_validate(_EXPECT_INITIAL_STATE_TASK)
    _register_pack(servicer, context, task_description, trial_id)
    if changed:
        servicer._run_async(
            servicer.db_client.mutate(
                trial_id,
                "orders",
                [
                    {
                        "op": "upsert",
                        "record": _ORDERS_AFTER_A_CHANGE["orders"][0],
                        "key": "order_id",
                    }
                ],
            )
        )

    response = _grade_registered_trial(servicer, context, trial_id, _HASH_TRANSCRIPT)
    return servicer.trials[trial_id].grading_config, response


def _drive_db_probes(
    test_data_dir: Path,
    servicer: RunnerServiceImpl,
    context: Any,
    monkeypatch: Any,
    *,
    trial_id: str,
) -> tuple[runner_models.RunnerGradingConfig, pb2.GradeTrialResponse]:
    """Grade the ``db_probe_grading`` pack with the probe's postgres substituted.

    ``_fetch_probe_rows`` is the seam lock 13 already reads this pack through. The
    probe's own ``expect`` assertions, the fold that reads the score, the recording
    site and the audit are all the real ones.
    """

    async def _rows(_dsn: str, _query: str) -> list[dict[str, Any]]:
        return _PROBE_ROWS

    monkeypatch.setattr(runner_grading, "_fetch_probe_rows", _rows)
    task_description = _adapter_for(test_data_dir, _TASKS_GLOB).to_task_description(_PROBE_PACK)
    _register_pack(servicer, context, task_description, trial_id)

    response = _grade_registered_trial(servicer, context, trial_id, _PROBE_TRANSCRIPT)
    return servicer.trials[trial_id].grading_config, response


def _drive_llm_judge(
    servicer: RunnerServiceImpl,
    context: Any,
    monkeypatch: Any,
    *,
    trial_id: str,
    transcript: str = _JUDGE_TRANSCRIPT,
) -> tuple[runner_models.RunnerGradingConfig, pb2.GradeTrialResponse]:
    """Grade a rubric-configured trial with the model provider substituted.

    The spec carries a judge ``ModelConfig``: ``_grade_llm_judge`` raises before it
    ever constructs the judge without one, so the row would otherwise red on claim 2
    for a reason with nothing to do with the recording site.
    """
    monkeypatch.setattr(runner_service_module, "LLMJudge", _StubJudge)
    task_description = runner_models.TaskDescription.model_validate(_JUDGE_DRIVER_TASK)
    _register_pack(
        servicer,
        context,
        task_description,
        trial_id,
        judge_model_config=core_models.ModelConfig(name="test-judge", provider="test"),
    )

    response = _grade_registered_trial(servicer, context, trial_id, transcript)
    return servicer.trials[trial_id].grading_config, response


def _drive_the_key(
    author_key: str,
    test_data_dir: Path,
    servicer: RunnerServiceImpl,
    context: Any,
    monkeypatch: Any,
    *,
    label: str,
) -> tuple[runner_models.RunnerGradingConfig, pb2.GradeTrialResponse]:
    """Whichever driver the table names for ``author_key``."""
    driver = _LEDGER_KEY_DRIVERS[author_key]
    trial_id = _ledger_trial_id(label, author_key)
    if driver is _Driver.PARITY_PACK:
        return _drive_parity_pack(author_key, test_data_dir, servicer, context, trial_id=trial_id)
    if driver is _Driver.HASH_FAMILY:
        return _drive_hash_family(servicer, context, trial_id=trial_id)
    if driver is _Driver.EXPECT_INITIAL_STATE:
        return _drive_expect_initial_state(servicer, context, trial_id=trial_id)
    if driver is _Driver.DB_PROBES:
        return _drive_db_probes(test_data_dir, servicer, context, monkeypatch, trial_id=trial_id)
    if driver is _Driver.LLM_JUDGE:
        return _drive_llm_judge(servicer, context, monkeypatch, trial_id=trial_id)
    raise AssertionError(f"{author_key}: the {driver.value} driver has no implementation")


@pytest.mark.parametrize("author_key", _LEDGER_SITE_KEYS)
def test_every_ledger_keys_recording_site_is_driven(
    author_key, test_data_dir, runner_service, mock_grpc_context, monkeypatch
):
    """Drive a real ``GradeTrial`` per ledger key and read what its site filed.

    Editing ``accountable_author_keys()`` cannot satisfy this: the key has to be
    populated on a config the runner registered, its evaluator has to run, and the
    outcome the ledger reports has to be the one the manifest implies.

    Two rows substitute an external service — the probe's postgres and the judge's
    model provider — and nothing else. A key the manifest enforces at the
    *integration* tier for its **score** is still canonically drivable for its
    **recording site**; the two are orthogonal, and conflating them is what would
    leave a residue here.
    """
    grading_config, response = _drive_the_key(
        author_key, test_data_dir, runner_service, mock_grpc_context, monkeypatch, label="sweep"
    )

    _assert_the_site_reported(author_key, grading_config, response)


def test_a_judge_with_no_transcript_messages_records_its_skip(
    runner_service, mock_grpc_context, monkeypatch
):
    """The judge's other branch, kept beside the sweep rather than in it.

    Every sweep row carries one manifest-derived expectation; this trial populates
    ``llm_judge`` and legitimately skips it, which is the one shape that expectation
    cannot describe. Its note is what tells a task author the rubric scored nothing.
    """
    grading_config, response = _drive_llm_judge(
        runner_service,
        mock_grpc_context,
        monkeypatch,
        trial_id=_ledger_trial_id("skip", LLM_JUDGE_KEY),
        transcript="",
    )

    assert response.success is True, response.error
    assert LLM_JUDGE_KEY in populated_ledger_keys(grading_config)
    assert skip_note(LLM_JUDGE_KEY, NO_JUDGE_MESSAGES_SKIP) in response.grade.reasons, (
        "a judge-configured trial with no transcript to judge must say so on the "
        f"grade: {response.grade.reasons!r}"
    )


# Fault injections. Without them lock 15 is a gate nobody has seen fail, so each
# asserts the helper rejects a trial the runner graded — five through a broken
# runner, one through a config that never asked for the key.

_INJECTION_JSONPATHS = "state_checks.jsonpaths"
_INJECTION_TRACE = "trace_checks.constraints.all_of"
_INJECTION_TRANSCRIPT = "transcript_rules.must_contain"


def test_the_site_lock_rejects_a_config_that_populated_nothing(
    test_data_dir, runner_service, mock_grpc_context
):
    """Claim 1: a real grade beside a config that never asked for the key."""
    _, response = _drive_parity_pack(
        _INJECTION_JSONPATHS,
        test_data_dir,
        runner_service,
        mock_grpc_context,
        trial_id=_ledger_trial_id("unpopulated", _INJECTION_JSONPATHS),
    )

    with pytest.raises(AssertionError, match="does not populate the key"):
        _assert_the_site_reported(
            _INJECTION_JSONPATHS, runner_models.RunnerGradingConfig(), response
        )


def test_the_site_lock_rejects_a_site_that_filed_a_skip_where_it_evaluated(
    test_data_dir, runner_service, mock_grpc_context, monkeypatch
):
    """Claim 3: a recording site downgraded to a skip while its evaluator still ran.

    Patching the module-level ``EVALUATED`` stands in for the class of defect rather
    than reproducing one site's edit: every inline site reads this global, so the
    downgrade lands on all of them at once.
    """
    monkeypatch.setattr(
        runner_service_module,
        "EVALUATED",
        KeyAccountingRecord(outcome=KeyAccounting.SKIPPED, detail="downgraded by injection"),
    )

    grading_config, response = _drive_parity_pack(
        _INJECTION_JSONPATHS,
        test_data_dir,
        runner_service,
        mock_grpc_context,
        trial_id=_ledger_trial_id("downgraded", _INJECTION_JSONPATHS),
    )

    assert response.success is True, response.error
    with pytest.raises(AssertionError, match="must file EVALUATED"):
        _assert_the_site_reported(_INJECTION_JSONPATHS, grading_config, response)


@pytest.mark.parametrize(
    ("evaluator_name", "author_key"),
    [
        ("evaluate_trace_checks", _INJECTION_TRACE),
        ("evaluate_transcript_rules", _INJECTION_TRANSCRIPT),
    ],
)
def test_the_site_lock_rejects_an_evaluator_that_stopped_accounting(
    evaluator_name, author_key, test_data_dir, runner_service, mock_grpc_context, monkeypatch
):
    """Claim 2: the evaluator still scores, but records no key — the audit fails the RPC."""
    real = getattr(runner_service_module, evaluator_name)

    def drifted(*args: Any, **kwargs: Any) -> Any:
        return real(*args, **kwargs).model_copy(update={"accounted_keys": {}})

    monkeypatch.setattr(runner_service_module, evaluator_name, drifted)

    grading_config, response = _drive_parity_pack(
        author_key,
        test_data_dir,
        runner_service,
        mock_grpc_context,
        trial_id=_ledger_trial_id("unaccounted", author_key),
    )

    assert response.success is False, "the audit was meant to fail the RPC"
    with pytest.raises(AssertionError, match="GradeTrial failed"):
        _assert_the_site_reported(author_key, grading_config, response)


def test_the_site_lock_rejects_an_evaluated_record_over_an_evaluation_that_did_not_run(
    test_data_dir, runner_service, mock_grpc_context, monkeypatch
):
    """Claim 4: real accounting filed EVALUATED while the component stayed unscored."""
    real = runner_service_module.evaluate_transcript_rules

    def hollow(*args: Any, **kwargs: Any) -> Any:
        return real(*args, **kwargs).model_copy(update={"score": _UNSCORED_COMPONENT})

    monkeypatch.setattr(runner_service_module, "evaluate_transcript_rules", hollow)

    grading_config, response = _drive_parity_pack(
        _INJECTION_TRANSCRIPT,
        test_data_dir,
        runner_service,
        mock_grpc_context,
        trial_id=_ledger_trial_id("hollow", _INJECTION_TRANSCRIPT),
    )

    assert response.success is True, response.error
    assert skip_note_prefix(_INJECTION_TRANSCRIPT) not in response.grade.reasons, (
        "the key must still be recorded as evaluated, or claim 3 would fire here "
        "and claim 4 would go unmeasured"
    )
    with pytest.raises(AssertionError, match="unscored sentinel"):
        _assert_the_site_reported(_INJECTION_TRANSCRIPT, grading_config, response)


def test_the_site_lock_rejects_hash_accounting_that_files_nothing(
    runner_service, mock_grpc_context, monkeypatch
):
    """Claim 2 for the mechanism the hash family alone uses.

    The whole family reaches the ledger through one ``hash_family_accounting`` call,
    so that call returning nothing is how this family's recording site disappears.
    The assertion is on lock 15's own helper: the mutation also breaks a dozen unit
    tests elsewhere, and a lock that relied on those going red would not be reading
    its own claim.
    """
    monkeypatch.setattr(runner_service_module, "hash_family_accounting", lambda basis: {})

    grading_config, response = _drive_hash_family(
        runner_service,
        mock_grpc_context,
        trial_id=_ledger_trial_id("unaccounted_hash", "state_checks.hash.enabled"),
    )

    assert response.success is False, "the audit was meant to fail the RPC"
    with pytest.raises(AssertionError, match="GradeTrial failed"):
        _assert_the_site_reported("state_checks.hash.enabled", grading_config, response)


# --------------------------------------------------------------------------
# 16. Both substrates score a comparison against the initial state alike
# --------------------------------------------------------------------------


def _core_expect_initial_state_verdict(final_db: dict[str, Any]) -> float:
    """The core engine's ``state_checks`` for a trial ending in ``final_db``.

    Hashes the task's declared initial state in core's own algebra and compares the
    trial's state against it, which is the whole of what the runner does in its
    algebra — so the two verdicts agreeing is a portability claim about the
    proposition rather than about a digest neither substrate could share (#915).
    """
    grade = GradingEngine(
        core_models.GradingConfig(**_EXPECT_INITIAL_STATE_GRADING),
        task_initial_state=core_models.InitialStateConfig(json_db=_INITIAL_ORDERS),
    ).grade_trajectory(_messageless_trajectory("expect_initial_state"), {"db": final_db})
    return grade.components.state_checks


@pytest.mark.parametrize(
    ("changed", "final_db", "expected"),
    [
        pytest.param(False, _INITIAL_ORDERS, 1.0, id="the_trial_left_the_state_as_it_found_it"),
        pytest.param(True, _ORDERS_AFTER_A_CHANGE, 0.0, id="the_trial_changed_a_record"),
    ],
)
def test_both_substrates_score_a_comparison_against_the_initial_state_alike(
    changed, final_db, expected, runner_service, mock_grpc_context
):
    """``state_checks.hash.expect_initial_state``'s ``BOTH_SCORE_PARITY`` claim.

    The runner arm is its whole production path — ``RegisterTrial``, the trial's own
    db-service state, ``GradeTrial`` — so what is compared is the state the evaluator
    reset to rather than a state this module handed it. Neither cell can be the
    unscored sentinel, both being asserted against a verdict of their own.

    One row carries both arms' inputs, because each substrate reaches the trial's
    final state its own way: ``changed`` is the mutation the runner's db-service
    takes, ``final_db`` the same state as core receives it.
    """
    _, response = _drive_expect_initial_state(
        runner_service,
        mock_grpc_context,
        trial_id=_ledger_trial_id(f"differential_{changed}", _EXPECT_INITIAL_STATE_KEY),
        changed=changed,
    )

    assert response.success is True, response.error
    assert response.grade.components.state_checks == pytest.approx(expected)
    assert _core_expect_initial_state_verdict(final_db) == pytest.approx(expected)


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
