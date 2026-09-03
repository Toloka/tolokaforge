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

``docs/GRADING.md`` § Runner-engine version lock is the reader's copy of the subset that
locks an engine to a runner image. It is one table, set-equal to the census rows that
carry a :class:`_DocLock`, with its breadth column rendered from the census's *measured*
``emitted_for`` — so the doc cannot say a key waits on a gate the models do not give it.
Which rows *belong* on it is not left to judgement: every censused key is either locked or
named in :data:`_PREDATES_FLOOR` for predating the support floor, so a grading field added
to any container — not only a new top-level key — must join the table or be argued into
that set.

**Two readers stay two here as well.** The table is read through
``doc_anchors.section``, which bounds a body at any ``^#{1,3} `` line and does not track
code fences: a ``#`` comment inside a fenced block ends that read early and returns a
*short* body with no error (#986). A short read is invisible to a lock that iterates the
rows it was handed — it checks what it sees and passes — so a second, fence-aware reader
finds the same section's end and the two are held to the same table rows before any key
set is compared.

**What is declared and not measured.** These fields carry no measurement behind them,
collected here rather than hedged at each use:

- ``is_leaf_container`` — declared rather than derived on purpose: deriving it would
  need a "is this rendered annotation a model?" predicate only the walk can answer,
  which is the coupling :data:`_WALK_STOPS` exists to avoid. Its honesty is asserted
  instead — a declared leaf must have no census row below it.
- ``since`` — the release whose image first presents the key **in the shape this row's
  lock is about**, not the release the key's name first appeared in. Measurable only by
  scanning tags, which CI cannot do: its jobs check out shallow with no tags. What *is*
  asserted is that the value names a release ``CHANGELOG.md`` records or ``unreleased``,
  that doc and census agree on it, and that it is at or above
  :data:`_SUPPORTED_IMAGE_FLOOR`. What is **not** asserted is that the release named is
  the right one. A row carrying ``since = None`` claims "presented by every image at or
  above the floor", and nothing checks that claim either.
- :data:`_SUPPORTED_IMAGE_FLOOR` — the oldest runner image the table speaks about. A
  judgement, not a derived support policy: nothing in this repository declares how far
  back images are supported.
- ``_DocLock.direction`` — whether the key bites one way or both. Depends on what a past
  image declared. The retired-key half of a both-directions claim is measured by
  :func:`test_a_retired_wire_key_is_declared_by_no_model`; the other half is not.
- ``_RETIRED_KEY_BREADTH`` — the breadth cell for a locked key no current model declares.
  Every other cell in that column is rendered from a measured ``emitted_for``; this one
  is hand-written, because nothing emits the key there is a gate to measure.
- :data:`_PREDATES_FLOOR` — that each of its members predates the floor. Verified at
  authoring time by reading the models at the tag below it; CI checks out shallow with no
  tags and re-derives nothing, so it is the same class of claim as ``since``. What the
  set *is* used for needs no dates: it is a snapshot, so a path neither in it nor locked
  was added after the snapshot and its shape is therefore at or above the floor. What is
  **not** asserted is that any given member truly predates the floor.
- ``_RetiredWireKey.reason``.

What no longer rests on judgement is which rows belong on the table *while the key exists*:
every censused key is locked or exempt, and a key dropped from the census and the table
while a model still declares it reds against the walk. What survives is removal — a key
taken out of the models, the census, the table and :data:`_PREDATES_FLOOR` together leaves
nothing to fire, so nothing forces it into :data:`_RETIRED_WIRE_KEYS` where an older image
still rejecting it would be recorded.

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
import re
import types
import typing
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from tests.utils.doc_anchors import anchor, section
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[2]
_NATIVE_EXAMPLES = _REPO / "examples" / "native"
_DOCS = _REPO / "docs"
_GRADING_DOC = _DOCS / "GRADING.md"
_RUNNER_DOC = _DOCS / "RUNNER.md"
_TROUBLESHOOTING_DOC = _DOCS / "TROUBLESHOOTING.md"
_CHANGELOG = _REPO / "CHANGELOG.md"

_VERSION_LOCK_HEADING = "### Runner-engine version lock"
_UNRELEASED = "unreleased"

_SUPPORTED_IMAGE_FLOOR = "v0.13.1"
"""The oldest runner image the version-lock table speaks about. A key whose locked shape
arrived at or after this belongs on the table; one whose locked shape predates it does
not."""

_PREDATES_FLOOR: frozenset[str] = frozenset(
    {
        "grading",
        "grading.grading_method",
        "grading.llm_judge",
        "grading.llm_judge.customization",
        "grading.llm_judge.customization.disable_knowledge_search",
        "grading.llm_judge.customization.include_agent_system_prompt",
        "grading.llm_judge.customization.system_prompt",
        "grading.llm_judge.rubric",
        "grading.llm_judge.rubric.criteria",
        "grading.llm_judge.rubric.criteria[].description",
        "grading.llm_judge.rubric.criteria[].expected",
        "grading.llm_judge.rubric.criteria[].id",
        "grading.llm_judge.rubric.criteria[].kind",
        "grading.llm_judge.rubric.criteria[].required",
        "grading.llm_judge.rubric.criteria[].weight",
        "grading.llm_judge.rubric.reference",
        "grading.pass_threshold",
        "grading.state_checks",
        "grading.state_checks.db_probes",
        "grading.state_checks.db_probes[].description",
        "grading.state_checks.db_probes[].dsn",
        "grading.state_checks.db_probes[].expect",
        "grading.state_checks.db_probes[].name",
        "grading.state_checks.db_probes[].query",
        "grading.state_checks.golden_actions",
        "grading.state_checks.golden_actions[].arguments",
        "grading.state_checks.golden_actions[].tool_name",
        "grading.state_checks.hash_enabled",
        "grading.state_checks.jsonpath_checks",
        "grading.state_checks.numeric_string_fields",
        "grading.state_checks.relaxed_validation",
        "grading.transcript_rules",
        "grading.transcript_rules.communicate_info",
        "grading.transcript_rules.communicate_info[].info",
        "grading.transcript_rules.communicate_info[].required",
        "grading.transcript_rules.disallow_regex",
        "grading.transcript_rules.max_turns",
        "grading.transcript_rules.must_contain",
        "grading.transcript_rules.required_actions",
        "grading.transcript_rules.required_actions[].action_id",
        "grading.transcript_rules.required_actions[].arguments",
        "grading.transcript_rules.required_actions[].compare_args",
        "grading.transcript_rules.required_actions[].requestor",
        "grading.transcript_rules.tool_expectations.disallowed_tools",
        "grading.transcript_rules.tool_expectations.required_tools",
        "grading.weights",
        "search",
        "search.api_key",
        "search.documents_path",
        "search.domain_name",
        "search.enabled",
        "search.host",
        "search.port",
    }
)
"""Census paths carrying no version lock: each predates :data:`_SUPPORTED_IMAGE_FLOOR`,
so no image the table speaks about can reject one.

A snapshot of the pre-floor census taken when this set was written, which is what makes
the completeness lock decidable without a date per row: a path that is neither exempt nor
locked was, by construction, added after the snapshot, so its shape is at or above the
floor. Nothing in CI re-derives it — see the module docstring's "declared and not
measured".
"""

_RETIRED_KEY_BREADTH = "no current engine"
"""The breadth cell for a locked key no current model declares: nothing emits it, so the
census has no measured ``emitted_for`` to render."""

_CHANGELOG_RELEASE = re.compile(r"^## (v\d+\.\d+\.\d+)")
_FLOOR_IN_PREAMBLE = re.compile(r"runner images from `(v\d+\.\d+\.\d+)` onward")
_ANY_HEADING = re.compile(r"^#{1,3} ")
"""Hand-copied from ``doc_anchors._HEADING`` rather than imported: the whole-or-nothing
check below is only a check while the two readers bound the *same* section, and a copy
that drifts reds there instead of quietly agreeing with a changed original."""

_FENCE = re.compile(r"^\s*(?:```|~~~)")
"""Both fence syntaxes CommonMark defines. A tilde fence is the hole a backtick-only
predicate leaves: both readers would step over neither, agree on a short count, and pass."""

# Every pack under the native example corpus that grades through the native adapter's
# grading contract, all of which build. Pinned so a pack dropping out of the corpus
# fails rather than silently shrinking the walk over it. ``coding_harness`` ships
# a placeholder ``grading.yaml`` — its trial verifier overrides at run time, but the
# static file exists so the standard pre-run gate accepts it and this walk counts it.
_NATIVE_PACK_COUNT = 30


class _Direction(str, Enum):
    """Which pairing a locked key is rejected in."""

    NEW_ENGINE_OLD_IMAGE = "new engine → old image"
    OLD_ENGINE_NEW_IMAGE = "old engine → new image"
    BOTH = "both directions"


@dataclass(frozen=True)
class _DocLock:
    """A key's row on ``docs/GRADING.md`` § Runner-engine version lock."""

    doc_key: str
    """The doc's spelling of the key — rooted at the grading block rather than at the
    task description, and addressing a position below a list with ``[*]`` where the
    census uses ``[]``."""

    direction: _Direction

    since: str | None = None
    """The release whose image first presents this lock's shape. ``None`` reads the
    census row's own ``since`` — the primary lock on any row shares its key's ``since``
    — and a non-``None`` value carries the release the lock names when the row hosts a
    value-domain sub-lock the key's own ``since`` does not fit. That is the case only
    where a leaf container walks a value the census has no path below to date."""

    breadth: str | None = None
    """The doc's ``emitted for`` cell rendered against this lock. ``None`` reads the
    census row's :func:`_doc_breadth`, and a non-``None`` value carries the wording a
    sub-lock the row is not the primary carrier of names. Same reason ``since`` opts
    out — a value-domain sub-lock inside a leaf container does not share the
    container's own gate."""


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
    """The rendered annotation the value crosses as, with the field's declared
    constraints beside it (``int | None [ge=1]``). Rendering resolves ``Literal`` and
    ``str, Enum`` members into the string and reads ``FieldInfo.metadata``, so a change
    to the accepted *value set* is visible here and not only a change of type — a bound
    relaxed on a locked key is as wire-breaking as a renamed field."""

    is_leaf_container: bool = False
    """Whether this row's value is a model the census deliberately does not descend
    into. Declared, not derived."""

    since: str | None = None
    """The release whose image first presents this key in the shape its doc row locks,
    or ``unreleased``. ``None`` on a key the version-lock table does not carry."""

    lock: _DocLock | None = None
    """This key's row on the version-lock table, or ``None`` when it carries none."""

    additional_locks: tuple[_DocLock, ...] = ()
    """Value-domain sub-locks the census would host below this row if the walk did not
    stop here. Every entry carries its own ``since`` and ``breadth`` on the :class:`_DocLock`,
    because a walk-stop leaf's own ``since`` reads the container's arrival — not the day
    an inner enum value was added. Empty on every row that carries only its own key lock."""


@dataclass(frozen=True)
class _RetiredWireKey:
    """A key path a current model must not declare."""

    path: str
    reason: str
    since: str | None = None
    lock: _DocLock | None = None


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
        since="unreleased",
        lock=_DocLock(
            doc_key="search.plane",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
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
        since="v0.13.1",
        lock=_DocLock(
            doc_key="combine_method",
            direction=_Direction.BOTH,
        ),
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
        since="unreleased",
        lock=_DocLock(
            doc_key="state_checks.expect_initial_state",
            direction=_Direction.BOTH,
        ),
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
        since="v0.13.1",
        lock=_DocLock(
            doc_key="state_checks.hash_weight",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
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
        since="v0.16.1",
        lock=_DocLock(
            doc_key="state_checks.id_fields",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
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
        wire_shape="int | None [ge=1]",
    ),
    _WireKey(
        path="grading.transcript_rules.min_assistant_turns",
        emitted_for="grading.transcript_rules",
        wire_shape="int | None [ge=1]",
        since="v0.15.0",
        lock=_DocLock(
            doc_key="transcript_rules.min_assistant_turns",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
    ),
    _WireKey(
        path="grading.transcript_rules.tool_expectations",
        emitted_for="grading.transcript_rules",
        wire_shape="ToolExpectations | None",
        since="v0.13.1",
        lock=_DocLock(
            doc_key="transcript_rules.tool_expectations",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
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
        since="unreleased",
        lock=_DocLock(
            doc_key="transcript_rules.required_actions[*].name",
            direction=_Direction.BOTH,
        ),
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
        since="v0.15.0",
        lock=_DocLock(
            doc_key="trace_checks",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
        additional_locks=(
            _DocLock(
                doc_key='trace_checks.constraints.<kind>.on_missing == "withhold"',
                direction=_Direction.NEW_ENGINE_OLD_IMAGE,
                since=_UNRELEASED,
                breadth="a pack declaring `on_missing: withhold` on a trace constraint",
            ),
            _DocLock(
                doc_key="`trace_checks` negative-text operators (`not_contains`, `not_regex`)",
                direction=_Direction.NEW_ENGINE_OLD_IMAGE,
                since=_UNRELEASED,
                breadth="a pack declaring one under a matcher predicate",
            ),
            _DocLock(
                doc_key="`trace_checks` nullness operators (`is_null`, `omitted`)",
                direction=_Direction.NEW_ENGINE_OLD_IMAGE,
                since=_UNRELEASED,
                breadth="a pack declaring one under a matcher's `args` or `text` predicate",
            ),
            _DocLock(
                doc_key="`trace_checks` date operators (`date_gt`, `date_gte`, `date_lt`, `date_lte`)",
                direction=_Direction.NEW_ENGINE_OLD_IMAGE,
                since=_UNRELEASED,
                breadth="a pack declaring one under a matcher predicate",
            ),
        ),
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
        since="v0.13.1",
        lock=_DocLock(
            doc_key="custom_checks",
            direction=_Direction.NEW_ENGINE_OLD_IMAGE,
        ),
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
        since="v0.13.1",
        lock=_DocLock(
            doc_key="state_checks.env_assertions",
            direction=_Direction.OLD_ENGINE_NEW_IMAGE,
        ),
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


def _contains_model(annotation: object) -> bool:
    """Whether a model is reachable anywhere inside this annotation.

    Read only to refuse an annotation the walk would otherwise record as a leaf while a
    model sits inside it — ``dict[str, M]``, ``M1 | M2``, ``list[M | None]``,
    ``tuple[M, ...]``. The refusal is self-checking where the record would not be:
    :func:`_emitted_key_paths` stops at the same boundary and would agree with the walk,
    and lock 5 reads only rows that *declare* themselves leaves, so an undeclared one is
    invisible to every comparison.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return any(_contains_model(arg) for arg in typing.get_args(annotation))


def _render_constraints(field: FieldInfo) -> str:
    """The declared constraints on a field, rendered beside its annotation.

    Pydantic keeps ``ge`` / ``le`` / ``min_length`` and friends in ``FieldInfo.metadata``
    rather than in the annotation, so a bound relaxed or removed is a wire-visible change
    to the accepted value set that the rendered annotation alone cannot show.
    """
    rendered: list[str] = []
    for constraint in field.metadata:
        if is_dataclass(constraint):
            rendered += [
                f"{member.name}={getattr(constraint, member.name)}"
                for member in dataclass_fields(constraint)
            ]
        else:
            rendered.append(repr(constraint))
    return f" [{', '.join(sorted(rendered))}]" if rendered else ""


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
        rendered = _render_annotation(field.annotation) + _render_constraints(field)
        if not stopped and nested is None and element is None and _contains_model(field.annotation):
            raise AssertionError(
                f"{path}: {rendered} carries a model the walk cannot descend into; "
                "teach the walk this shape rather than letting it record a container "
                "as a leaf"
            )
        yield _WalkedKey(
            path=path,
            emitted_for=gate,
            wire_shape=rendered,
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


def _doc_breadth(row: _WireKey | _RetiredWireKey) -> str:
    """The version-lock table's ``emitted for`` cell for this row.

    The one place the census's measured ``emitted_for`` is rendered into the doc's
    wording, so a doc cell and a model gate cannot drift into two different claims.
    """
    if isinstance(row, _RetiredWireKey):
        return _RETIRED_KEY_BREADTH
    if not row.emitted_for:
        return "every pack"
    return f"a pack declaring `{row.emitted_for.removeprefix('grading.')}`"


def _version_tuple(release: str) -> tuple[int, ...]:
    return tuple(int(part) for part in release.removeprefix("v").split("."))


@lru_cache(maxsize=1)
def _version_lock_section() -> str:
    lines = _GRADING_DOC.read_text(encoding="utf-8").splitlines()
    return section(lines, _VERSION_LOCK_HEADING, _GRADING_DOC.name)


def _table_lines(body: Iterable[str]) -> list[str]:
    return [line for line in body if line.startswith("|")]


def _fence_aware_section_lines() -> list[str]:
    """The heading's body up to the next heading, with fenced blocks stepped over.

    A second reader for the section, and the only reason it exists: ``doc_anchors.section``
    bounds a body at any ``^#{1,3} `` line without tracking code fences, so a ``#`` comment
    inside a fenced block ends its read early and returns a *short* body with no error
    (#986). A short read of a table looks exactly like a doc naming fewer keys, so the two
    readers are held to the same table-row count before any key set is compared.
    """
    lines = _GRADING_DOC.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip() == _VERSION_LOCK_HEADING]
    found = len(starts)
    assert found == 1, f"{_VERSION_LOCK_HEADING} appears {found} times in {_GRADING_DOC.name}"
    body: list[str] = []
    inside_fence = False
    for line in lines[starts[0] + 1 :]:
        if _FENCE.match(line):
            inside_fence = not inside_fence
            continue
        if not inside_fence and _ANY_HEADING.match(line):
            break
        body.append(line)
    assert not inside_fence, f"unclosed code fence under {_VERSION_LOCK_HEADING}"
    return body


def _unwrapped(cell: str) -> str:
    """A cell that is one whole code span, without its backticks.

    Only a cell that is *entirely* a code span is unwrapped: a breadth cell reads
    ``a pack declaring `state_checks``` and must keep the backticks around the key it
    names, or the doc and the census would be compared on different strings.
    """
    if len(cell) > 1 and cell.startswith("`") and cell.endswith("`"):
        return cell[1:-1]
    return cell


@lru_cache(maxsize=1)
def _version_lock_table() -> tuple[dict[str, str], ...]:
    """The table's data rows, keyed by column header.

    Every doc-side lock reads the table through here, so the read is checked against the
    fence-aware reader once rather than at each of them: a short read silently shrinks
    what a lock iterating the rows measures, and passes.
    """
    read = _table_lines(_version_lock_section().splitlines())
    whole = _table_lines(_fence_aware_section_lines())
    assert read == whole, (
        f"the section read carries {len(read)} table lines against {len(whole)} in the "
        "section itself, so this table is short — a truncated read, not a doc naming "
        "fewer keys"
    )
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in read]
    assert len(rows) > 2, f"{_VERSION_LOCK_HEADING} carries no table"
    header, separator, *body = rows
    assert all(set(cell) <= {"-", ":"} for cell in separator), "row 2 is not a table rule"
    return tuple(dict(zip(header, cells, strict=True)) for cells in body)


@lru_cache(maxsize=1)
def _changelog_releases() -> frozenset[str]:
    return frozenset(
        match.group(1)
        for line in _CHANGELOG.read_text(encoding="utf-8").splitlines()
        if (match := _CHANGELOG_RELEASE.match(line))
    )


def _locked_rows() -> tuple[tuple[_WireKey | _RetiredWireKey, _DocLock], ...]:
    """Every census row carrying a version lock, paired with each lock it carries.

    A row hosts one primary ``lock`` and, on a walk-stop leaf, any number of
    :attr:`_WireKey.additional_locks` — each yielded here alongside the row it lives
    on, so a value-domain sub-lock is compared to its doc row the same way the primary
    key lock is.
    """
    rows: list[tuple[_WireKey | _RetiredWireKey, _DocLock]] = []
    for row in (*_WIRE_KEYS, *_RETIRED_WIRE_KEYS):
        if row.lock is not None:
            rows.append((row, row.lock))
        for extra in getattr(row, "additional_locks", ()):
            rows.append((row, extra))
    return tuple(rows)


def _lock_since(row: _WireKey | _RetiredWireKey, lock: _DocLock) -> str | None:
    """The release this row's lock names — ``lock.since`` overrides ``row.since``."""
    return lock.since if lock.since is not None else row.since


def _lock_breadth(row: _WireKey | _RetiredWireKey, lock: _DocLock) -> str:
    """The doc breadth this row's lock names — ``lock.breadth`` overrides the row's."""
    return lock.breadth if lock.breadth is not None else _doc_breadth(row)


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
    unreached = sorted(declared.keys() - walked.keys())
    assert unreached == [], (
        "a census row the walk never reached, so the per-key comparison below skipped "
        "it rather than measuring it"
    )
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
    unreached = sorted(declared.keys() - walked.keys())
    assert unreached == [], (
        "a census row the walk never reached, so the per-key comparison below skipped "
        "it rather than measuring it"
    )
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
    task_files = sorted(
        task_yaml
        for task_yaml in _NATIVE_EXAMPLES.rglob("task.yaml")
        if any((parent / "project.yaml").exists() for parent in task_yaml.parents)
    )
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


def test_the_version_lock_table_names_exactly_the_keys_the_census_locks() -> None:
    locked = {lock.doc_key: lock.direction for _, lock in _locked_rows()}
    assert locked, "no census row carries a doc lock, so this lock asserts nothing"
    tabled = {_unwrapped(row["key"]): row["direction"] for row in _version_lock_table()}

    untabled = sorted(locked.keys() - tabled.keys())
    assert untabled == [], "a census row carries a doc lock with no row on the table"
    uncensused = sorted(tabled.keys() - locked.keys())
    assert uncensused == [], "the version-lock table names a key no census row locks"
    drift = {
        key: (locked[key].value, tabled[key])
        for key in sorted(locked.keys() & tabled.keys())
        if locked[key].value != tabled[key]
    }
    assert drift == {}, "declared direction != the table's, as {key: (declared, table)}"


def test_the_table_states_the_breadth_the_models_actually_emit() -> None:
    measured = {lock.doc_key: _lock_breadth(row, lock) for row, lock in _locked_rows()}
    breadths = set(measured.values())
    assert len(breadths) > 1, "every locked key renders the same breadth"
    drift = {
        key: (measured[key], row["emitted for"])
        for row in _version_lock_table()
        if (key := _unwrapped(row["key"])) in measured and measured[key] != row["emitted for"]
    }
    assert drift == {}, "the table's breadth != the census's measured one, as {key: (census, doc)}"


def test_the_doc_and_the_census_name_the_same_releases_and_the_changelog_records_them() -> None:
    releases = _changelog_releases()
    floor_is_released = _SUPPORTED_IMAGE_FLOOR in releases
    assert floor_is_released, "the support floor names no release CHANGELOG.md records"

    declared = {lock.doc_key: _lock_since(row, lock) for row, lock in _locked_rows()}
    unrecorded = sorted(
        f"{key}={since}"
        for key, since in declared.items()
        if since != _UNRELEASED and since not in releases
    )
    assert unrecorded == [], "a census `since` names no release CHANGELOG.md records"

    drift = {
        key: (declared[key], _unwrapped(row["first declared by"]))
        for row in _version_lock_table()
        if (key := _unwrapped(row["key"])) in declared
        and declared[key] != _unwrapped(row["first declared by"])
    }
    assert drift == {}, "the table's release != the census's, as {key: (census, doc)}"

    stated = _FLOOR_IN_PREAMBLE.search(_version_lock_section())
    assert stated is not None, f"{_VERSION_LOCK_HEADING}'s preamble states no support floor"
    assert stated.group(1) == _SUPPORTED_IMAGE_FLOOR, (
        f"the preamble's floor {stated.group(1)} and _SUPPORTED_IMAGE_FLOOR "
        f"{_SUPPORTED_IMAGE_FLOOR} disagree"
    )


def test_every_dated_key_carries_a_doc_lock_and_clears_the_support_floor() -> None:
    rows = (*_WIRE_KEYS, *_RETIRED_WIRE_KEYS)
    unpaired = sorted(row.path for row in rows if (row.lock is not None) != (row.since is not None))
    assert unpaired == [], (
        "a row declares a `since` without a doc lock or a doc lock without a `since`; "
        "the version-lock table carries exactly the keys dated to a release"
    )

    dated = [row for row in rows if row.since is not None and row.since != _UNRELEASED]
    assert dated, "no row names an exact release, so the floor comparison runs over nothing"
    floor = _version_tuple(_SUPPORTED_IMAGE_FLOOR)
    below = sorted(
        f"{path}={since} < {_SUPPORTED_IMAGE_FLOOR}"
        for path, since in ((row.path, row.since) for row in dated)
        if since is not None and _version_tuple(since) < floor
    )
    assert below == [], "a locked key's shape predates the support floor the table declares"


def test_a_container_is_a_census_leaf_only_while_it_carries_the_lock() -> None:
    leaves = [row for row in _WIRE_KEYS if row.is_leaf_container]
    assert leaves, "no census row declares a leaf container, so this lock asserts nothing"
    unlocked = sorted(row.path for row in leaves if row.lock is None)
    assert unlocked == [], (
        "a container is a census leaf while carrying no version lock; the census stops "
        "there because the container is rejected whole, so its interior needs a census "
        "of its own the day it stops being locked"
    )


def test_the_mirrors_send_the_reader_to_the_one_list() -> None:
    target = anchor(_VERSION_LOCK_HEADING)
    headings = {
        anchor(line)
        for line in _GRADING_DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    }
    into_the_table = f"{_GRADING_DOC.name}#{target}"
    broken: dict[str, str] = {}
    for doc in (_RUNNER_DOC, _TROUBLESHOOTING_DOC):
        if into_the_table not in doc.read_text(encoding="utf-8"):
            broken[doc.name] = f"carries no link to {into_the_table}"
        elif target not in headings:
            broken[doc.name] = f"links to {into_the_table}, which resolves to no heading"
    assert broken == {}, "a mirror does not resolve to the one version-lock table"


def test_every_censused_key_is_locked_or_declared_older_than_the_floor() -> None:
    paths = {row.path for row in _WIRE_KEYS}
    locked = {row.path for row in _WIRE_KEYS if row.lock}
    assert locked, "no census row carries a lock, so this lock asserts nothing"

    unnamed = sorted(_PREDATES_FLOOR - paths)
    assert unnamed == [], (
        "an exemption names no census row; a dead exemption pre-authorises any future "
        "key that reuses the name"
    )
    both = sorted(_PREDATES_FLOOR & locked)
    assert both == [], (
        "a path is both locked and exempt; adding a lock means deleting the exemption, "
        "or the stale one stays ready to re-authorise dropping the key from the table"
    )

    unaccounted = sorted(paths - locked - _PREDATES_FLOOR)
    assert unaccounted == [], (
        "a censused wire key carries neither a version lock nor an exemption; if its "
        "shape arrived at or after the floor, an image lacking it rejects the trial spec"
    )
