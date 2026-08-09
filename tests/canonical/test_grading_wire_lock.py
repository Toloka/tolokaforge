"""The grading wire surface is a census, and the declared models are walked against it.

``TrialSpec`` reaches the runner as a plain ``model_dump_json()`` parsed by
``extra="forbid"`` models, so every key the engine emits under ``task.grading`` and
``task.search`` is a key an older runner image must already declare or reject the whole
trial at ``RegisterTrial``. No adapter serialises with ``exclude_none``, which is why a
container is itself a wire key — ``"state_checks": null`` is a key on the wire, and
``grading`` and ``search`` are keys for the same reason.

:data:`_WIRE_KEYS` is that surface written out by hand: one row per emitted key path,
naming the gate the key's emission waits on and the shape its value crosses as. An
independent walk of the declared models produces the same three facts, so a field added
to a runner grading model without a census row fails here naming the path.

**The two sources stay two.** The walk stops at :data:`_WALK_STOPS` and never reads
:data:`_WIRE_KEYS`; the census is never derived from the walk. Were the walk to stop
wherever the census declares a leaf, one hand edit adding a container as a census leaf
would delete that subtree from both sides at once and leave the first three locks green
over an unmeasured surface.

**What is declared and not measured.** ``_WireKey.is_leaf_container`` and
``_RetiredWireKey.reason`` carry no measurement behind them. ``is_leaf_container`` is
declared rather than derived on purpose: deriving it would need a "is this rendered
annotation a model?" predicate only the walk can answer, which is the coupling
:data:`_WALK_STOPS` exists to avoid. Its honesty is asserted instead — a declared leaf
must have no census row below it.

Two surfaces are deliberately outside this census and tracked in #983: the trial spec's
non-grading keys (``initial_state``, ``user_simulator``, ``agent_tools``,
``initialization_actions`` and ``TrialSpec``'s own fields), and the interior of
``grading.trace_checks``, whose constraint-expression union expands to more paths on its
own than the whole rest of the grading contract. The census holds ``trace_checks`` as a
leaf because it is locked whole: an image that rejects the container never validates its
interior.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[2]
_NATIVE_EXAMPLES = _REPO / "examples" / "native"

# Every pack under the native example corpus, all of which build. Pinned so a pack
# dropping out of the corpus fails rather than silently shrinking the walk over it.
_NATIVE_PACK_COUNT = 29


@dataclass(frozen=True)
class _WireKey:
    """One key path the trial spec's grading contract puts on the wire."""

    path: str
    """The dotted key path inside a serialised ``TaskDescription``, rooted at the
    description. A position inside a list's elements is addressed with ``[]``, the one
    way to name a position below a list field, following the ``*_element_path``
    convention ``tolokaforge/core/grading/key_manifest.py`` sets."""

    emitted_for: str
    """The path of the nearest ancestor a pack must declare for this key to appear;
    ``""`` when every pack emits it."""

    wire_shape: str
    """The rendered annotation the value crosses as. Rendering resolves ``Literal`` and
    ``str, Enum`` members into the string, so a change to a value *domain* is visible
    here and not only a change of type."""

    is_leaf_container: bool = False
    """Whether this row's value is a model the census deliberately does not descend
    into. Declared, not derived."""


@dataclass(frozen=True)
class _RetiredWireKey:
    """A key path a current model must not declare."""

    path: str
    reason: str


_WALK_STOPS: tuple[str, ...] = ("grading.trace_checks", "environment_manifest")
"""The paths the model walk records without descending into. Read by the walk and by
nothing else — the census never reads it, and it never reads the census."""

_EXCLUDED_FROM_THE_WIRE = "environment_manifest"
"""The one walk stop that is not a wire key at all: ``conductor.py`` drops it from the
serialised spec, because it describes how the orchestrator materialises the substrate
the runner already runs inside."""

_CENSUS_ROOTS: tuple[str, ...] = ("grading", "search")
"""The two ``TaskDescription`` fields whose emitted keys this census speaks for."""

_WIRE_KEYS: tuple[_WireKey, ...] = (
    _WireKey(
        path="search",
        emitted_for="",
        wire_shape="SearchConfig",
    ),
    _WireKey(
        path="search.enabled",
        emitted_for="",
        wire_shape="bool",
    ),
    _WireKey(
        path="search.plane",
        emitted_for="",
        wire_shape="Literal['typesense', 'rag_service'] | None",
    ),
    _WireKey(
        path="search.domain_name",
        emitted_for="",
        wire_shape="str | None",
    ),
    _WireKey(
        path="search.documents_path",
        emitted_for="",
        wire_shape="str | None",
    ),
    _WireKey(
        path="search.host",
        emitted_for="",
        wire_shape="str | None",
    ),
    _WireKey(
        path="search.port",
        emitted_for="",
        wire_shape="int | None",
    ),
    _WireKey(
        path="search.api_key",
        emitted_for="",
        wire_shape="str | None",
    ),
    _WireKey(
        path="grading",
        emitted_for="",
        wire_shape="RunnerGradingConfig",
    ),
    _WireKey(
        path="grading.combine_method",
        emitted_for="",
        wire_shape="Literal['weighted', 'all', 'any']",
    ),
    _WireKey(
        path="grading.weights",
        emitted_for="",
        wire_shape="dict[str, float]",
    ),
    _WireKey(
        path="grading.pass_threshold",
        emitted_for="",
        wire_shape="float",
    ),
    _WireKey(
        path="grading.grading_method",
        emitted_for="",
        wire_shape="Literal['hash', 'test_execution', 'transcript', 'llm'] | None",
    ),
    _WireKey(
        path="grading.state_checks",
        emitted_for="",
        wire_shape="RunnerStateChecksConfig | None",
    ),
    _WireKey(
        path="grading.state_checks.hash_enabled",
        emitted_for="grading.state_checks",
        wire_shape="bool",
    ),
    _WireKey(
        path="grading.state_checks.expect_initial_state",
        emitted_for="grading.state_checks",
        wire_shape="bool",
    ),
    _WireKey(
        path="grading.state_checks.golden_actions",
        emitted_for="grading.state_checks",
        wire_shape="list[GoldenAction]",
    ),
    _WireKey(
        path="grading.state_checks.golden_actions[].tool_name",
        emitted_for="grading.state_checks.golden_actions",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.state_checks.golden_actions[].arguments",
        emitted_for="grading.state_checks.golden_actions",
        wire_shape="dict[str, Any]",
    ),
    _WireKey(
        path="grading.state_checks.hash_weight",
        emitted_for="grading.state_checks",
        wire_shape="float | None",
    ),
    _WireKey(
        path="grading.state_checks.numeric_string_fields",
        emitted_for="grading.state_checks",
        wire_shape="list[str]",
    ),
    _WireKey(
        path="grading.state_checks.id_fields",
        emitted_for="grading.state_checks",
        wire_shape="dict[str, str | list[str]]",
    ),
    _WireKey(
        path="grading.state_checks.relaxed_validation",
        emitted_for="grading.state_checks",
        wire_shape="bool",
    ),
    _WireKey(
        path="grading.state_checks.jsonpath_checks",
        emitted_for="grading.state_checks",
        wire_shape="list[dict[str, Any]]",
    ),
    _WireKey(
        path="grading.state_checks.db_probes",
        emitted_for="grading.state_checks",
        wire_shape="list[DbProbe]",
    ),
    _WireKey(
        path="grading.state_checks.db_probes[].name",
        emitted_for="grading.state_checks.db_probes",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.state_checks.db_probes[].dsn",
        emitted_for="grading.state_checks.db_probes",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.state_checks.db_probes[].query",
        emitted_for="grading.state_checks.db_probes",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.state_checks.db_probes[].expect",
        emitted_for="grading.state_checks.db_probes",
        wire_shape="list[dict[str, Any]]",
    ),
    _WireKey(
        path="grading.state_checks.db_probes[].description",
        emitted_for="grading.state_checks.db_probes",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.transcript_rules",
        emitted_for="",
        wire_shape="TranscriptRulesConfig | None",
    ),
    _WireKey(
        path="grading.transcript_rules.must_contain",
        emitted_for="grading.transcript_rules",
        wire_shape="list[str]",
    ),
    _WireKey(
        path="grading.transcript_rules.disallow_regex",
        emitted_for="grading.transcript_rules",
        wire_shape="list[str]",
    ),
    _WireKey(
        path="grading.transcript_rules.max_turns",
        emitted_for="grading.transcript_rules",
        wire_shape="int | None",
    ),
    _WireKey(
        path="grading.transcript_rules.min_assistant_turns",
        emitted_for="grading.transcript_rules",
        wire_shape="int | None",
    ),
    _WireKey(
        path="grading.transcript_rules.tool_expectations",
        emitted_for="grading.transcript_rules",
        wire_shape="ToolExpectations | None",
    ),
    _WireKey(
        path="grading.transcript_rules.tool_expectations.required_tools",
        emitted_for="grading.transcript_rules.tool_expectations",
        wire_shape="list[str]",
    ),
    _WireKey(
        path="grading.transcript_rules.tool_expectations.disallowed_tools",
        emitted_for="grading.transcript_rules.tool_expectations",
        wire_shape="list[str]",
    ),
    _WireKey(
        path="grading.transcript_rules.required_actions",
        emitted_for="grading.transcript_rules",
        wire_shape="list[RequiredAction]",
    ),
    _WireKey(
        path="grading.transcript_rules.required_actions[].action_id",
        emitted_for="grading.transcript_rules.required_actions",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.transcript_rules.required_actions[].requestor",
        emitted_for="grading.transcript_rules.required_actions",
        wire_shape="Literal['assistant', 'user']",
    ),
    _WireKey(
        path="grading.transcript_rules.required_actions[].name",
        emitted_for="grading.transcript_rules.required_actions",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.transcript_rules.required_actions[].arguments",
        emitted_for="grading.transcript_rules.required_actions",
        wire_shape="dict[str, Any]",
    ),
    _WireKey(
        path="grading.transcript_rules.required_actions[].compare_args",
        emitted_for="grading.transcript_rules.required_actions",
        wire_shape="list[str] | None",
    ),
    _WireKey(
        path="grading.transcript_rules.communicate_info",
        emitted_for="grading.transcript_rules",
        wire_shape="list[CommunicateInfo]",
    ),
    _WireKey(
        path="grading.transcript_rules.communicate_info[].info",
        emitted_for="grading.transcript_rules.communicate_info",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.transcript_rules.communicate_info[].required",
        emitted_for="grading.transcript_rules.communicate_info",
        wire_shape="bool",
    ),
    _WireKey(
        path="grading.trace_checks",
        emitted_for="",
        wire_shape="TraceChecksConfig | None",
        is_leaf_container=True,
    ),
    _WireKey(
        path="grading.llm_judge",
        emitted_for="",
        wire_shape="LLMJudgeConfig | None",
    ),
    _WireKey(
        path="grading.llm_judge.rubric",
        emitted_for="grading.llm_judge",
        wire_shape="Rubric",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria",
        emitted_for="grading.llm_judge",
        wire_shape="list[Criterion]",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria[].id",
        emitted_for="grading.llm_judge.rubric.criteria",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria[].description",
        emitted_for="grading.llm_judge.rubric.criteria",
        wire_shape="str",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria[].weight",
        emitted_for="grading.llm_judge.rubric.criteria",
        wire_shape="float",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria[].kind",
        emitted_for="grading.llm_judge.rubric.criteria",
        wire_shape="Literal['binary', 'graded']",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria[].required",
        emitted_for="grading.llm_judge.rubric.criteria",
        wire_shape="bool",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.criteria[].expected",
        emitted_for="grading.llm_judge.rubric.criteria",
        wire_shape="str | None",
    ),
    _WireKey(
        path="grading.llm_judge.rubric.reference",
        emitted_for="grading.llm_judge",
        wire_shape="str | None",
    ),
    _WireKey(
        path="grading.llm_judge.customization",
        emitted_for="grading.llm_judge",
        wire_shape="JudgeCustomization | None",
    ),
    _WireKey(
        path="grading.llm_judge.customization.disable_knowledge_search",
        emitted_for="grading.llm_judge.customization",
        wire_shape="bool | None",
    ),
    _WireKey(
        path="grading.llm_judge.customization.system_prompt",
        emitted_for="grading.llm_judge.customization",
        wire_shape="str | None",
    ),
    _WireKey(
        path="grading.llm_judge.customization.include_agent_system_prompt",
        emitted_for="grading.llm_judge.customization",
        wire_shape="bool | None",
    ),
    _WireKey(
        path="grading.custom_checks",
        emitted_for="",
        wire_shape="dict[str, Any] | None",
    ),
)

_RETIRED_WIRE_KEYS: tuple[_RetiredWireKey, ...] = (
    _RetiredWireKey(
        path="grading.state_checks.expected_hash",
        reason="the expected hash is computed per trial, never authored (#693)",
    ),
    _RetiredWireKey(
        path="grading.state_checks.env_assertions",
        reason="environment assertions were removed from the state-check block",
    ),
    _RetiredWireKey(
        path="grading.transcript_rules.required_actions[].tool_name",
        reason="the element spells the tool it names `name` (#685)",
    ),
)
"""Paths no current model may declare. Beside the core-side ``RETIRED_STATE_CHECK_KEYS``
in ``tolokaforge/core/models/task_config.py``, which refuses the *authored* spellings;
this table is about the wire."""


@dataclass(frozen=True)
class _WalkedKey:
    """One key path the declared models put on the wire, as the walk found it."""

    path: str
    emitted_for: str
    wire_shape: str
    descended: bool


def _render_annotation(annotation: object) -> str:
    if annotation is type(None):
        return "None"
    if annotation is Any:
        return "Any"
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return f"Literal[{', '.join(repr(arg) for arg in typing.get_args(annotation))}]"
    if origin in (typing.Union, types.UnionType):
        return " | ".join(_render_annotation(arg) for arg in typing.get_args(annotation))
    if origin is not None:
        args = ", ".join(_render_annotation(arg) for arg in typing.get_args(annotation))
        return f"{origin.__name__}[{args}]"
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return f"Literal[{', '.join(repr(member.value) for member in annotation)}]"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation)


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    """The annotation with ``None`` stripped, and whether it carried one."""
    origin = typing.get_origin(annotation)
    if origin not in (typing.Union, types.UnionType):
        return annotation, False
    args = typing.get_args(annotation)
    without_none = [arg for arg in args if arg is not type(None)]
    optional = len(without_none) != len(args)
    if len(without_none) == 1:
        return without_none[0], optional
    return annotation, optional


def _nested_model(annotation: object) -> type[BaseModel] | None:
    inner, _ = _unwrap_optional(annotation)
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner
    return None


def _element_model(annotation: object) -> type[BaseModel] | None:
    inner, _ = _unwrap_optional(annotation)
    if typing.get_origin(inner) is not list:
        return None
    (element,) = typing.get_args(inner)
    if isinstance(element, type) and issubclass(element, BaseModel):
        return element
    return None


def _walk_model(model: type[BaseModel], prefix: str, gate: str) -> Iterator[_WalkedKey]:
    """Every wire key path under ``model``, with the gate its emission waits on.

    ``gate`` is the nearest ancestor a pack must declare — the nearest optional field or
    list above this one — which is a property of the ancestors and never of the field
    itself: an optional container is emitted unconditionally as ``null`` and only its
    children wait on it.
    """
    for name, field in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        nested = _nested_model(field.annotation)
        element = _element_model(field.annotation)
        stopped = path in _WALK_STOPS
        yield _WalkedKey(
            path=path,
            emitted_for=gate,
            wire_shape=_render_annotation(field.annotation),
            descended=not stopped and (nested is not None or element is not None),
        )
        if stopped:
            continue
        _, optional = _unwrap_optional(field.annotation)
        if nested is not None:
            yield from _walk_model(nested, path, path if optional else gate)
        elif element is not None:
            yield from _walk_model(element, f"{path}[]", path)


@lru_cache(maxsize=1)
def _walked_wire_keys() -> tuple[_WalkedKey, ...]:
    """The grading and search key paths the declared models emit."""
    return tuple(
        key
        for key in _walk_model(TaskDescription, prefix="", gate="")
        if key.path.split(".")[0] in _CENSUS_ROOTS
    )


def _descended_paths() -> frozenset[str]:
    """The paths the walk went below, plus the element positions under them.

    What the corpus lock reads to stop descending a serialised pack where the walk
    stopped descending the models, so the two are compared at one granularity.
    """
    paths = {""}
    for key in _walked_wire_keys():
        if not key.descended:
            continue
        paths.add(key.path)
        paths.add(f"{key.path}[]")
    return frozenset(paths)


def _emitted_key_paths(value: object, path: str, descended: frozenset[str]) -> dict[str, object]:
    """Key paths inside a serialised value, stopping where the model walk stopped."""
    if path not in descended:
        return {}
    if isinstance(value, dict):
        emitted: dict[str, object] = {}
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            emitted[child] = item
            emitted.update(_emitted_key_paths(item, child, descended))
        return emitted
    if isinstance(value, list):
        emitted = {}
        for item in value:
            emitted.update(_emitted_key_paths(item, f"{path}[]", descended))
        return emitted
    return {}


def _pack_adapter(task_yaml: Path) -> tuple[str, NativeAdapter]:
    """The task's id and an adapter over it, wired the orchestrator's way.

    The project's ``default_environment`` is passed because a task that declares no
    ``environment_manifest`` of its own resolves its substrate from it, and without it
    such a pack raises at ``to_task_description`` rather than building.
    """
    enclosing = [
        parent / "project.yaml"
        for parent in task_yaml.parents
        if (parent / "project.yaml").exists()
    ]
    assert enclosing, f"{task_yaml} has no enclosing project.yaml"
    project_yaml = enclosing[0]
    project = load_project_config(project_yaml)
    adapter = NativeAdapter(
        {
            "tasks_glob": str(task_yaml.relative_to(project_yaml.parent)),
            "task_packs": [str(project_yaml.parent)],
            "project_task_defaults": project.task_defaults.model_dump(exclude_defaults=True)
            or None,
            "project_default_environment": project.default_environment,
        }
    )
    task_ids = adapter.get_task_ids()
    assert len(task_ids) == 1, f"{task_yaml} resolved to {task_ids}, not one task"
    return task_ids[0], adapter


def _emitted_grading_keys(description: TaskDescription) -> dict[str, object]:
    """The grading and search key paths a built pack actually puts on the wire.

    Serialised the way ``conductor.py`` serialises a trial spec — no ``exclude_none``,
    the environment manifest dropped — so a container the models declare is a key here
    even when the pack declares nothing under it.
    """
    payload = json.loads(description.model_dump_json(exclude={_EXCLUDED_FROM_THE_WIRE}))
    descended = _descended_paths()
    emitted: dict[str, object] = {}
    for root in _CENSUS_ROOTS:
        emitted[root] = payload[root]
        emitted.update(_emitted_key_paths(payload[root], root, descended))
    return emitted


def test_the_census_names_every_grading_wire_key_the_engine_emits() -> None:
    walked = {key.path for key in _walked_wire_keys()}
    declared = {row.path for row in _WIRE_KEYS}
    assert sorted(walked - declared) == [], (
        "a runner grading model declares a wire key the census does not name; "
        "add a row to _WIRE_KEYS for each path"
    )
    assert sorted(declared - walked) == [], (
        "the census names a wire key no runner grading model declares; "
        "remove the row or retire the path in _RETIRED_WIRE_KEYS"
    )


def test_every_censused_key_declares_the_gate_its_emission_waits_on() -> None:
    walked = {key.path: key.emitted_for for key in _walked_wire_keys()}
    declared = {row.path: row.emitted_for for row in _WIRE_KEYS}
    drift = {
        path: (declared[path], walked[path])
        for path in sorted(declared.keys() & walked.keys())
        if declared[path] != walked[path]
    }
    assert drift == {}, "declared emitted_for != walked emitted_for, as {path: (declared, walked)}"
    assert declared["grading.trace_checks"] == "", (
        "grading.trace_checks is emitted by every pack, declared or not — a gate here "
        "would mean an older image never sees the key that breaks it"
    )


def test_every_censused_key_declares_the_shape_it_crosses_as() -> None:
    walked = {key.path: key.wire_shape for key in _walked_wire_keys()}
    declared = {row.path: row.wire_shape for row in _WIRE_KEYS}
    drift = {
        path: (declared[path], walked[path])
        for path in sorted(declared.keys() & walked.keys())
        if declared[path] != walked[path]
    }
    assert drift == {}, "declared wire_shape != walked wire_shape, as {path: (declared, walked)}"


def test_a_retired_wire_key_is_declared_by_no_model() -> None:
    walked = {key.path for key in _walked_wire_keys()}
    retired = {row.path for row in _RETIRED_WIRE_KEYS}
    assert retired, "_RETIRED_WIRE_KEYS is empty, so this lock asserts nothing"

    unmeasured = sorted(
        row.path
        for row in _RETIRED_WIRE_KEYS
        if row.path.rsplit(".", 1)[0].removesuffix("[]") not in walked
    )
    assert unmeasured == [], (
        "a retired path is addressed below a container the walk never reaches, so its "
        "absence from the walk says nothing about the models"
    )
    assert sorted(retired & walked) == [], "a runner grading model re-declares a retired wire key"
    censused = sorted(retired & {row.path for row in _WIRE_KEYS})
    assert censused == [], "a path is both censused and retired"


def test_the_walk_stops_where_the_census_declares_a_leaf() -> None:
    stops = set(_WALK_STOPS) - {_EXCLUDED_FROM_THE_WIRE}
    declared_leaves = {row.path for row in _WIRE_KEYS if row.is_leaf_container}
    assert declared_leaves, "no census row declares a leaf container, so this lock asserts nothing"
    assert stops == declared_leaves, (
        "the walk's stops and the census's declared leaf containers disagree; both are "
        "written by hand and neither is derived from the other"
    )

    below = {
        leaf: sorted(
            row.path
            for row in _WIRE_KEYS
            if row.path.startswith(f"{leaf}.") or row.path.startswith(f"{leaf}[")
        )
        for leaf in sorted(declared_leaves)
    }
    contradicted = {leaf: rows for leaf, rows in below.items() if rows}
    assert contradicted == {}, "a declared leaf container has census rows below it"


def test_the_example_corpus_emits_what_the_census_says_it_emits() -> None:
    task_files = sorted(_NATIVE_EXAMPLES.rglob("task.yaml"))
    assert len(task_files) == _NATIVE_PACK_COUNT, (
        "the native example corpus changed size; a pack dropping out would otherwise "
        "shrink this lock's reach silently"
    )

    unconditional = {row.path for row in _WIRE_KEYS if row.emitted_for == ""}
    gated = {row.path: row.emitted_for for row in _WIRE_KEYS if row.emitted_for}
    assert gated, "no census row is gated, so this lock would compare one constant set"

    drift: dict[str, dict[str, list[str]]] = {}
    shapes: set[frozenset[str]] = set()
    for task_yaml in task_files:
        task_id, adapter = _pack_adapter(task_yaml)
        emitted = _emitted_grading_keys(adapter.to_task_description(task_id))
        declared_gates = {
            path
            for path, value in emitted.items()
            if isinstance(value, dict) or (isinstance(value, list) and value)
        }
        expected = unconditional | {path for path, gate in gated.items() if gate in declared_gates}
        shapes.add(frozenset(expected))
        if set(emitted) != expected:
            drift[task_id] = {
                "emitted but not censused": sorted(set(emitted) - expected),
                "censused but not emitted": sorted(expected - set(emitted)),
            }
    assert drift == {}, "a built pack's wire keys disagree with what the census says it emits"
    assert len(shapes) > 1, (
        "every pack expects the same key set, so the gated rows were never told apart "
        "from the unconditional ones"
    )
