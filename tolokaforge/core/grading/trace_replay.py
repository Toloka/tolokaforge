"""Trace-check replay — re-evaluate a pack's constraints over recorded bundles.

Replay is a *caller* of the one production evaluator
(:func:`~tolokaforge.core.grading.trace_checks.evaluate_trace_checks`), never a
second implementation. It reads a trial bundle written by the eval flow, rebuilds
the timeline from what the bundle persisted — ``trajectory.yaml`` for the message
view, ``tool_log.yaml`` for the tool-call record — and scores the pack's
``trace_checks`` block against it again.

Nothing here runs an agent, an environment or a judge, so a replay costs no
tokens and starts no container. That is a structural property of the imports, not
a promise: this module reaches the evaluator, the bundle reader, the authoring
gate and the outcome classifier a run's own attribution uses, and stops there.

Constraints can come from a supplied file instead of the bundle, which is how an
author iterates on one without editing the pack. Such a block is checked against
each bundle's recorded tool set — through the same
:func:`~tolokaforge.core.grading.config_validation.inspect_grading_authoring` a
pack meets before a run — *before* any trial is re-checked, so a misspelled tool
is one defect in one file rather than a corpus of trials that all failed.

The source run is left as it was found. Artifacts land under
``<source>/trace_replay/<replay_id>/…``, a sibling of judge replay's ``replays/``
so neither command's discovery walks the other's output.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from tolokaforge.core.failure_attribution import TrialOutcomeClass, classify_trial_outcome
from tolokaforge.core.grading.config_validation import (
    AuthoringReport,
    ToolInventory,
    inspect_grading_authoring,
)
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    TrialTimeline,
    build_trial_timeline,
)
from tolokaforge.core.models import (
    TraceChecksConfig,
    TraceChecksResult,
    TraceChecksSummary,
    TraceConstraintResult,
    Trajectory,
)
from tolokaforge.core.output.artifacts import read_recorded_tool_log

__all__ = [
    "JUDGE_REPLAY_DIRNAME",
    "RESERVED_DIRNAMES",
    "TRACE_CHECKS_RESULT_FILENAME",
    "TRACE_REPLAY_DIRNAME",
    "TRACE_REPLAY_REPORT_FILENAME",
    "BundleEvidence",
    "ConstraintDiscrimination",
    "ConstraintDiscriminationRow",
    "ConstraintProvenance",
    "MissingTraceReplayInputError",
    "TraceChecksOverride",
    "TraceChecksOverrideError",
    "TraceReplayEligibility",
    "TraceReplayEvidence",
    "TraceReplayFailure",
    "TraceReplayInputs",
    "TraceReplayOutcomeStatus",
    "TraceReplayOverrideNotes",
    "TraceReplayReport",
    "TraceReplayReportError",
    "TrialTraceReplay",
    "TrialTraceReplayOutcome",
    "build_trace_replay_report",
    "classify_trace_trial",
    "declared_trace_checks",
    "discover_trace_bundles",
    "emit_trace_replay_report",
    "load_trace_checks_override",
    "read_trace_replay_inputs",
    "recorded_grade",
    "recorded_task",
    "recorded_task_id",
    "replay_trace_checks",
    "run_trace_replay_batch",
    "tool_inventory_from_bundle",
    "trace_replay_root",
]

#: Subdirectory replay artifacts are written under; excluded from discovery so a
#: source pointed at a previous run's output never re-checks bundles nested there.
TRACE_REPLAY_DIRNAME = "trace_replay"
#: Judge replay's output subtree, excluded from discovery beside this command's own.
#: A local literal rather than an import of ``replay.REPLAYS_DIRNAME``: that module
#: loads the judge and its LLM client at import time, and the import boundary that
#: makes this one incapable of spending forbids reaching it. A shared leaf module
#: carrying the layout for both is #787.
JUDGE_REPLAY_DIRNAME = "replays"
#: Directory names reserved anywhere under a source: a bundle sitting beneath one is
#: not discovered, at any depth, because a previously-replayed subtree can be nested
#: arbitrarily. The trade is deliberate — a *task* named ``replays`` would hide its
#: own trials — and it is what keeps the two replay commands from reading each
#: other's output whatever either one writes into its tree.
RESERVED_DIRNAMES = frozenset({TRACE_REPLAY_DIRNAME, JUDGE_REPLAY_DIRNAME})
#: Per-bundle artifact name. Deliberately one no trial bundle already holds, so a
#: write that escaped the output subtree creates a file rather than clobbering one.
TRACE_CHECKS_RESULT_FILENAME = "trace_checks_result.yaml"
#: Run-level artifact name, beside the per-bundle results under the same subtree.
TRACE_REPLAY_REPORT_FILENAME = "trace_replay_report.yaml"

_BUNDLE_MARKERS = ("task.yaml", "trajectory.yaml")
_TOOLS_SCHEMAS_FILENAME = "tools_schemas.yaml"
#: The denominator a route-scoped row is read against, carried in the report so a
#: reader never takes a route's unanimity for a corpus-wide claim.
_ROUTE_SCOPING_NOTE = (
    "a path constraint is measured only over the trials its path won; "
    "trials_evaluated is that count"
)


class TraceReplayEligibility(str, Enum):
    """Whether a recorded trial has trace constraints to re-check."""

    ELIGIBLE = "eligible"
    NOT_APPLICABLE = "not_applicable"


class TraceReplayOutcomeStatus(str, Enum):
    """Per-trial disposition in a batch replay.

    ``REPLAYED`` — recomputed and the artifact written. ``WOULD_REPLAY`` — a
    ``dry_run`` trial that is eligible and reconstructable. ``SKIPPED_NOT_APPLICABLE``
    — the bundle declares no ``trace_checks`` and no override was supplied, which is
    a declared skip, never a silent one. ``SKIPPED_NO_TASK`` — the bundle carries no
    ``task.yaml``, so nothing says what the trial was graded against; it is kept
    apart from ``SKIPPED_NOT_APPLICABLE``, which asserts something about the pack
    that a bundle without one cannot support. ``FAILED`` — the bundle could not be
    classified or reconstructed; ``reason`` names the file and the defect, and the
    batch continues.
    """

    REPLAYED = "replayed"
    WOULD_REPLAY = "would_replay"
    SKIPPED_NOT_APPLICABLE = "skipped_not_applicable"
    SKIPPED_NO_TASK = "skipped_no_task"
    FAILED = "failed"


class TraceReplayFailure(str, Enum):
    """Why a bundle could not be re-checked, as the run-level evidence counts it.

    ``PREDATES_CALL_IDS`` is separated from every other unreadable input because it
    is the one defect that is a property of the corpus's *age* rather than of a
    broken file: an operator reading ``NEVER_DECIDED`` off a corpus that lost
    bundles to it is reading a report about the harness, not about the constraint.
    """

    UNREADABLE_INPUT = "unreadable_input"
    PREDATES_CALL_IDS = "predates_call_ids"


class ConstraintDiscrimination(str, Enum):
    """How much a constraint separated the trials it was measured over.

    The boundaries are ``all decided`` / ``some decided`` / ``none decided`` — no
    threshold anywhere. ``ALWAYS_TRUE`` and ``ALWAYS_FALSE`` therefore mean
    unanimous *on complete evidence*, and ``UNDECIDED_IN_PART`` carries the verdict
    the decided trials agreed on so a claim resting on one observation cannot read
    as a corpus-wide one. Disagreement wins outright: a constraint that passed one
    trial and failed another is ``DISCRIMINATING`` however many were undecided,
    because it is proven to discriminate.

    ``NOT_MEASURED`` is decided before any other member, not last: at zero
    evaluated trials the ``ALWAYS_TRUE`` and ``ALWAYS_FALSE`` conditions are both
    vacuously satisfied, so a classifier checking them in declaration order would
    report a constraint no trial ever evaluated as unanimously passing.
    """

    DISCRIMINATING = "discriminating"
    ALWAYS_TRUE = "always_true"
    ALWAYS_FALSE = "always_false"
    UNDECIDED_IN_PART = "undecided_in_part"
    NEVER_DECIDED = "never_decided"
    NOT_MEASURED = "not_measured"


class ConstraintProvenance(str, Enum):
    """Where the constraint block a trial was re-checked against came from.

    Deliberately not judge replay's ``ProvenanceSource``: importing that module
    would pull the judge and its LLM client into a command that spends nothing,
    which is the property the replay import boundary exists to keep.
    """

    RECORDED = "recorded"
    OVERRIDE = "override"


class MissingTraceReplayInputError(ValueError):
    """A bundle cannot be re-checked because an input is missing or invalid.

    The message names the file and the defect. Never raised for a bundle that
    declares no ``trace_checks`` — that is a declared skip. ``failure`` classifies
    the defect for the run-level evidence block, which counts an age-related
    rejection apart from a broken file; the message stays the operator's answer.
    """

    def __init__(
        self, message: str, *, failure: TraceReplayFailure = TraceReplayFailure.UNREADABLE_INPUT
    ) -> None:
        super().__init__(message)
        self.failure = failure


class TraceChecksOverrideError(ValueError):
    """An operator-supplied constraint file cannot be used, and the message says why.

    Distinct from :class:`MissingTraceReplayInputError`, which is a defect in a
    recorded bundle: a bundle defect fails that trial and the batch runs on, while
    an override is wrong for every trial at once, so it stops the batch before
    anything is re-checked against a block already known to be mis-authored.
    """


class TraceReplayReportError(ValueError):
    """One report cannot be built over these outcomes, and the message says which pair.

    Every cause is a mismatch between the batch and the constraint blocks it is being
    reported against, never a defect in a bundle — those are already per-trial
    failures by the time a report is built. A row keyed by ``(task_id,
    constraint_id)`` is a claim about one pack's block, so a corpus in which two
    bundles claim one task while declaring different blocks has no single row to make
    it in, and a ``declared`` mapping that did not come from this batch cannot say
    what a trial was measured against.
    """


@dataclass(frozen=True)
class TraceChecksOverride:
    """A constraint block supplied on the command line, and the file it came from.

    ``block`` is the mapping as authored, because the authoring gate addresses a
    defect by where the operator wrote it; ``config`` is that same mapping
    validated, derived here so the two cannot drift. The block's own rejections
    describe what is wrong with a ``trace_checks`` block but not *which* file
    carries it, so they are re-raised naming the path.

    Frozen, and ``block`` is stored as a read-only copy of what the caller passed, so
    a mapping mutated after construction cannot make ``block`` and ``config``
    disagree. Carrying a mapping also makes the type unhashable despite being frozen:
    it is compared by value and never used as a key.
    """

    path: Path
    block: Mapping[str, Any]
    config: TraceChecksConfig = field(init=False)

    def __post_init__(self) -> None:
        try:
            config = TraceChecksConfig.model_validate(self.block)
        except ValidationError as exc:
            raise TraceChecksOverrideError(
                f"constraint override {self.path} cannot be used as written: {exc}"
            ) from exc
        object.__setattr__(self, "block", MappingProxyType(dict(self.block)))
        object.__setattr__(self, "config", config)


def load_trace_checks_override(path: Path) -> TraceChecksOverride:
    """Read a constraint block off an operator-supplied file.

    Accepts a grading document carrying a ``trace_checks:`` key or the bare block,
    so the file can be a pack's ``grading.yaml`` or a snippet written to iterate on
    one constraint. Whichever it is, it replaces a bundle's block wholesale —
    nothing is merged, because two constraint lists folded together assert
    something neither was written to assert.
    """
    path = Path(path)
    if not path.exists():
        raise TraceChecksOverrideError(f"constraint override {path} does not exist")
    try:
        document = _load_yaml_mapping(path)
    except MissingTraceReplayInputError as exc:
        raise TraceChecksOverrideError(f"constraint override {path} is unreadable: {exc}") from exc
    if document is None:
        raise TraceChecksOverrideError(f"constraint override {path} is not a YAML mapping")
    block = document.get("trace_checks")
    if isinstance(block, Mapping):
        return TraceChecksOverride(path=path, block=block)
    if "constraints" in document or "alternatives" in document:
        return TraceChecksOverride(path=path, block=document)
    raise TraceChecksOverrideError(
        f"constraint override {path} declares nothing to re-check: it carries neither a "
        "'trace_checks:' block nor a top-level 'constraints:' / 'alternatives:' list"
    )


def tool_inventory_from_bundle(bundle: Path) -> ToolInventory:
    """The tool set a recorded trial actually had, read off its wire record.

    ``tools_schemas.yaml`` is the post-policy list handed to the provider, so it is
    what the trial's actor could call whichever adapter assembled it — a stronger
    reading than the pack's declaration, which says what the task asked for. A
    bundle that recorded none is :meth:`ToolInventory.unresolvable`, which routes
    every schema-dependent rule into the report's ``unchecked`` channel: absent is
    not empty, and an empty inventory would make every tool name in an override
    wrong.
    """
    path = Path(bundle) / _TOOLS_SCHEMAS_FILENAME
    if not path.exists():
        return ToolInventory.unresolvable()
    try:
        recorded = yaml.safe_load(_bundle_text(path))
    except yaml.YAMLError as exc:
        raise MissingTraceReplayInputError(f"unreadable YAML at {path}: {exc}") from exc
    if not isinstance(recorded, list):
        raise MissingTraceReplayInputError(
            f"{path} holds {type(recorded).__name__} where the recorded wire tool list "
            "belongs, so the trial cannot say which tools its actor could call"
        )
    recorded_tools = [_recorded_tool(path, index, entry) for index, entry in enumerate(recorded)]
    return ToolInventory(
        declared=frozenset(name for name, _ in recorded_tools),
        parameters={name: schema for name, schema in recorded_tools if schema is not None},
        known=True,
    )


def _recorded_tool(path: Path, index: int, entry: Any) -> tuple[str, Mapping[str, Any] | None]:
    """One recorded tool: its name, and its parameter schema where it recorded one.

    Both sit under ``function`` — the conductor builds the OpenAI function envelope
    for every provider, so that is the one shape a bundle carries. A tool recording
    no parameters mapping is left out of the inventory's schemas rather than given
    an empty one, so its argument names classify as unknown rather than as wrong.
    """
    function = entry.get("function") if isinstance(entry, Mapping) else None
    name = function.get("name") if isinstance(function, Mapping) else None
    if not isinstance(name, str) or not name:
        raise MissingTraceReplayInputError(
            f"{path} entry {index} records no function.name, so the tool it stands for "
            "cannot be matched against a constraint"
        )
    schema = function.get("parameters")
    return name, schema if isinstance(schema, Mapping) else None


@dataclass(frozen=True)
class BundleEvidence:
    """What the bundle itself says about how much of the trial it recorded.

    ``tool_log_present`` is the reader's file-presence answer, never
    :attr:`~tolokaforge.core.grading.trace_timeline.TrialTimeline.records_present`:
    a trial that called no tool writes ``tool_log.yaml`` empty, so the bundle is
    fully recorded while the timeline it produces reports no record view. Reading
    the timeline's flag here would report a recorded corpus as record-less.

    ``schema_version`` is the bundle's stamp, ``None`` where the bundle predates
    it. Evidence only — a stamp says which files to expect, and both stamped and
    unstamped bundles re-check fine, so nothing gates on it.
    """

    tool_log_present: bool
    schema_version: int | None


@dataclass(frozen=True)
class TraceReplayInputs:
    """Everything one bundle contributes to a re-check.

    ``recorded_constraints`` and ``recorded_summary`` are what the live run wrote
    into ``grade.yaml``, held for comparison against the recomputation and never
    read as the replay's own verdict — a bundle written by a runner predating
    ``undecided`` records ``False`` on verdicts that were genuinely undecided, so
    the replay's ``undecided`` has to come from the fold it just performed.
    ``None`` means the bundle records no trace-check verdict to compare against.

    ``task_id`` is what a discrimination row is keyed on beside the constraint id:
    ids are unique only within one pack's block, so a run spanning tasks folds two
    unrelated predicates into one row without it. ``recorded_binary_pass`` is the
    trial-level verdict the live run wrote, ``None`` where the bundle was never
    graded — a second, independent source from the recomputation.
    """

    task_id: str
    config: TraceChecksConfig
    provenance: ConstraintProvenance
    timeline: TrialTimeline
    evidence: BundleEvidence
    recorded_constraints: tuple[TraceConstraintResult, ...] | None
    recorded_summary: TraceChecksSummary | None
    recorded_binary_pass: bool | None


@dataclass(frozen=True)
class TrialTraceReplayOutcome:
    """Outcome of one trial in a batch replay.

    ``evidence`` is ``None`` exactly where the bundle was never read — a declared
    skip or a failure — which is a different statement from an unstamped,
    record-less bundle.

    ``override_authoring`` is what the authoring gate found checking the supplied
    block against this bundle's recorded tool set, ``None`` where no override was
    supplied. Errors never reach here — they stop the batch — so it carries the
    advisories and the rules the bundle's tool record could not answer. A caller
    that reports the outcome without it reports a block checked against nothing as
    a block checked and found clean.

    ``config`` is the block this trial was re-checked against, carried so the
    discrimination report reads the declared constraint universe off the batch it
    reports on rather than re-reading ``task.yaml`` as a second source; a route
    that won no trial is only reportable from it. ``failure`` classifies a
    ``FAILED`` disposition for the evidence block.

    ``recorded_constraints`` is what the live run concluded per constraint, the
    independent source the report joins each recomputed verdict to by id;
    ``recorded_binary_pass`` is the trial-level pass beside it, reported per trial
    rather than per constraint because a trial fails for reasons beyond any one
    constraint. Both are ``None`` where the bundle was never graded.
    """

    bundle: Path
    status: TraceReplayOutcomeStatus
    reason: str | None = None
    provenance: ConstraintProvenance | None = None
    evidence: BundleEvidence | None = None
    result: TraceChecksResult | None = None
    override_authoring: AuthoringReport | None = None
    task_id: str | None = None
    config: TraceChecksConfig | None = None
    failure: TraceReplayFailure | None = None
    recorded_constraints: tuple[TraceConstraintResult, ...] | None = None
    recorded_binary_pass: bool | None = None


def _bundle_text(path: Path) -> str:
    """The text of one file inside a bundle, or why this bundle cannot supply it.

    Every read of a bundle's bytes goes through here. Both replay commands net this
    module's own error per bundle and let everything else out, so a file the
    filesystem refuses or a byte that is not UTF-8 would otherwise abort the whole
    batch over one trial's artifact — the failure mode a per-bundle net exists to
    prevent.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MissingTraceReplayInputError(f"unreadable file at {path}: {exc}") from exc


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(_bundle_text(path))
    except yaml.YAMLError as exc:
        raise MissingTraceReplayInputError(f"unreadable YAML at {path}: {exc}") from exc


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    """A mapping off *path*, ``None`` where the file is absent *or* holds anything else.

    For the inputs whose absence and whose wrong shape are one refusal at the call
    site: a bundle with no ``task.yaml`` and one whose ``task.yaml`` is a list are
    both "not a trial bundle", and the caller names both possibilities.
    """
    if not path.exists():
        return None
    loaded = _load_yaml(path)
    return loaded if isinstance(loaded, dict) else None


def _carried_mapping(path: Path) -> dict[str, Any] | None:
    """The mapping a bundle carries at *path*, ``None`` only where it carries none.

    The strict reading, for the two inputs whose *absence* is a supported state: an
    ungraded trial has no ``grade.yaml``, and a bundle older than the stamp has no
    ``schema_version`` in ``metrics.yaml``. Folding a wrong-shaped file into that
    ``None`` would report a bundle that cannot say what the live run concluded as one
    nobody graded — dropping it out of the agreement denominator silently — and an
    unreadable stamp as an unstamped bundle.
    """
    if not path.exists():
        return None
    loaded = _load_yaml(path)
    if isinstance(loaded, dict):
        return loaded
    raise MissingTraceReplayInputError(
        f"{path} holds {type(loaded).__name__} where a mapping belongs, so the bundle "
        "cannot say what it recorded — which is a different state from not carrying it"
    )


def _is_bundle(path: Path) -> bool:
    return path.is_dir() and all((path / marker).exists() for marker in _BUNDLE_MARKERS)


def discover_trace_bundles(source: Path) -> list[Path]:
    """Discover re-checkable trial bundles under ``source``, layout-agnostic.

    A directory is a bundle iff it directly contains ``task.yaml`` +
    ``trajectory.yaml``. Not ``grade.yaml``: a trial is worth re-checking whether
    or not it was ever graded. Handles the three recorded layouts uniformly — a run
    dir with a ``trials/<task>/<idx>/`` subtree, a flat collection of bundle dirs,
    or a single bundle dir. Returned sorted for stable batches.

    Nothing beneath a :data:`RESERVED_DIRNAMES` directory is discovered, at any
    depth: a source re-pointed at a run that already holds either replay command's
    output re-checks the trials, never the artifacts.
    """
    source = Path(source)
    if _is_bundle(source):
        return [source]
    bundles = {
        marker.parent
        for marker in source.rglob("trajectory.yaml")
        if RESERVED_DIRNAMES.isdisjoint(marker.relative_to(source).parts)
        and _is_bundle(marker.parent)
    }
    return sorted(bundles)


def recorded_task(bundle: Path) -> dict[str, Any]:
    """The bundle's ``task.yaml``, read once per bundle and threaded onward.

    Every reader of it — the eligibility classification, the constraint block, the
    task id — takes the mapping rather than the path, because parsing it per reader
    costs more than the whole re-check it feeds. Public for the readers outside this
    module that need the trial's own account of what it was graded against: the
    differential reads the rubric each bundle recorded off it.
    """
    task = _load_yaml_mapping(bundle / "task.yaml")
    if task is None:
        raise MissingTraceReplayInputError(
            f"not a trial bundle: {bundle / 'task.yaml'} is missing or not a mapping"
        )
    return task


def classify_trace_trial(
    bundle: Path, *, override: TraceChecksOverride | None = None
) -> TraceReplayEligibility:
    """Classify a recorded trial as having constraints to re-check, or not.

    Eligible iff the bundle's ``task.yaml`` declares a ``grading_config.trace_checks``
    block, or an override supplies one — an override replaces the block wholesale,
    so it makes a trial that declared none re-checkable. Raises
    :class:`MissingTraceReplayInputError` when ``task.yaml`` is missing or is not a
    mapping: that is not a constraint-less trial, it is not a trial bundle.
    """
    bundle = Path(bundle)
    return _classify_trace_trial(recorded_task(bundle), override)


def _classify_trace_trial(
    task: dict[str, Any], override: TraceChecksOverride | None
) -> TraceReplayEligibility:
    if override is not None or _declared_trace_checks(task) is not None:
        return TraceReplayEligibility.ELIGIBLE
    return TraceReplayEligibility.NOT_APPLICABLE


def _declared_trace_checks(task: dict[str, Any]) -> Any:
    return (task.get("grading_config") or {}).get("trace_checks")


def _resolve_trace_checks(
    bundle: Path, task: dict[str, Any], override: TraceChecksOverride | None
) -> tuple[TraceChecksConfig, ConstraintProvenance]:
    if override is not None:
        return override.config, ConstraintProvenance.OVERRIDE
    declared = _declared_trace_checks(task)
    if declared is None:
        raise MissingTraceReplayInputError(
            f"no trace_checks: {bundle / 'task.yaml'} declares no "
            "grading_config.trace_checks and no override was supplied"
        )
    try:
        return TraceChecksConfig.model_validate(declared), ConstraintProvenance.RECORDED
    except ValidationError as exc:
        raise MissingTraceReplayInputError(
            f"{bundle / 'task.yaml'} declares a trace_checks block that does not validate: {exc}"
        ) from exc


def _is_a_call_id_defect(error: ErrorDetails) -> bool:
    """Whether one validation error is a persisted tool call carrying no usable id.

    Two shapes, because ``ToolCall`` states the rule twice: ``id`` is required, so a
    call omitting it errors at ``…tool_calls.<n>.id``; and the empty string is
    rejected by the model's only validator, which errors one level up at
    ``…tool_calls.<n>``.
    """
    location = error["loc"]
    if "tool_calls" not in location:
        return False
    return location[-1] == "id" or error["type"] == "value_error"


def _unreadable_trajectory(bundle: Path, error: ValidationError) -> MissingTraceReplayInputError:
    detail = f"{bundle / 'trajectory.yaml'} did not validate: {error}"
    if not any(_is_a_call_id_defect(item) for item in error.errors()):
        return MissingTraceReplayInputError(detail)
    return MissingTraceReplayInputError(
        f"the bundle at {bundle} predates call-id threading: a tool call it persisted "
        "carries no id, and the id is the only key joining a call to the result it "
        f"produced, so the trial cannot be re-checked at all. {detail}",
        failure=TraceReplayFailure.PREDATES_CALL_IDS,
    )


def _load_trajectory(bundle: Path) -> tuple[Trajectory, bool]:
    """The trial as its bundle persisted it, and whether the bundle held a record.

    Both halves come off disk — the message view from ``trajectory.yaml``, the
    tool-call record from the ``tool_log.yaml`` sidecar — so what this returns is
    whatever the writer wrote, never a reconstruction of it.
    """
    persisted = _load_yaml_mapping(bundle / "trajectory.yaml")
    if persisted is None:
        raise MissingTraceReplayInputError(
            f"no transcript: {bundle / 'trajectory.yaml'} is missing or not a mapping"
        )
    try:
        record, tool_log_present = read_recorded_tool_log(bundle)
    except ValueError as exc:
        raise MissingTraceReplayInputError(str(exc)) from exc
    try:
        trajectory = Trajectory.model_validate({**persisted, "tool_log": record})
    except ValidationError as exc:
        raise _unreadable_trajectory(bundle, exc) from exc
    return trajectory, tool_log_present


@dataclass(frozen=True)
class _RecordedGrade:
    """What a bundle's ``grade.yaml`` froze at write time, or nothing where it has none.

    ``binary_pass`` is the trial-level verdict, held apart from the per-constraint
    ones because it is what the report counts constraint agreement against.
    """

    constraints: tuple[TraceConstraintResult, ...] | None
    summary: TraceChecksSummary | None
    binary_pass: bool | None


def _recorded_binary_pass(bundle: Path, grade: dict[str, Any]) -> bool | None:
    recorded = grade.get("binary_pass")
    if recorded is None or isinstance(recorded, bool):
        return recorded
    raise MissingTraceReplayInputError(
        f"{bundle / 'grade.yaml'} records binary_pass {recorded!r}, which is neither a "
        "pass nor a fail, so the bundle cannot say what the live run concluded"
    )


def _recorded_constraints(bundle: Path, recorded: Any) -> tuple[TraceConstraintResult, ...] | None:
    """The per-constraint verdicts a live run froze, read as the model that wrote them.

    A non-list is refused rather than iterated: it is the bundle's account of what the
    live run concluded, and the report joins the recomputation to it by constraint id.
    Read through :class:`TraceConstraintResult` itself, which forbids unknown keys —
    the same strictness the sibling summary and ``binary_pass`` are read with, because
    a verdict this reader cannot fully account for is not a verdict to compare against.
    """
    if recorded is None:
        return None
    if not isinstance(recorded, list):
        raise MissingTraceReplayInputError(
            f"{bundle / 'grade.yaml'} holds {type(recorded).__name__} where the live run's "
            "per-constraint verdicts belong, so the bundle cannot say what it concluded"
        )
    return tuple(TraceConstraintResult.model_validate(item) for item in recorded)


def recorded_grade(bundle: Path) -> dict[str, Any] | None:
    """The bundle's ``grade.yaml`` as the live run wrote it, ``None`` where none graded it.

    Public because the differential joins the *judge's* per-criterion verdicts to a
    recomputed constraint verdict, and those verdicts are a different question from the
    trace-check ones :class:`TraceReplayInputs` carries. A file present but holding
    anything other than a mapping is refused rather than folded into the ``None``: a
    bundle that cannot say what it concluded is not one nobody graded.
    """
    return _carried_mapping(bundle / "grade.yaml")


def _recorded_grade(bundle: Path) -> _RecordedGrade:
    grade = recorded_grade(bundle)
    if grade is None:
        return _RecordedGrade(constraints=None, summary=None, binary_pass=None)
    summary = grade.get("trace_checks_summary")
    try:
        return _RecordedGrade(
            constraints=_recorded_constraints(bundle, grade.get("trace_check_results")),
            summary=None if summary is None else TraceChecksSummary.model_validate(summary),
            binary_pass=_recorded_binary_pass(bundle, grade),
        )
    except ValidationError as exc:
        raise MissingTraceReplayInputError(
            f"{bundle / 'grade.yaml'} records trace-check verdicts that do not validate: {exc}"
        ) from exc


def _schema_version(bundle: Path) -> int | None:
    """The bundle's schema stamp, ``None`` where the bundle predates the stamp."""
    stamped = (_carried_mapping(bundle / "metrics.yaml") or {}).get("schema_version")
    if stamped is None or (isinstance(stamped, int) and not isinstance(stamped, bool)):
        return stamped
    raise MissingTraceReplayInputError(
        f"{bundle / 'metrics.yaml'} stamps schema_version {stamped!r}, which is not a "
        "version number, so the bundle cannot say which artifacts it carries"
    )


def recorded_task_id(bundle: Path, task: dict[str, Any]) -> str:
    """Which task a bundle's trial belongs to, half of a discrimination row's key.

    Public because the differential resolves a bundle's *pack* through it, and a second
    reading of the field would answer a bundle that names none with a different refusal.
    """
    declared = task.get("task_id")
    if isinstance(declared, str) and declared:
        return declared
    raise MissingTraceReplayInputError(
        f"{bundle / 'task.yaml'} names no task_id, so nothing attributes the trial's "
        "verdicts to a task — and a constraint id is unique only inside one pack's "
        "block, so an unattributed verdict folds into whatever other pack reused the id"
    )


def read_trace_replay_inputs(
    bundle: Path, *, override: TraceChecksOverride | None = None
) -> TraceReplayInputs:
    """Reconstruct one bundle's re-check inputs from what it persisted.

    Raises :class:`MissingTraceReplayInputError`, naming the file and the defect,
    for every unreadable or invalid input — including a bundle written before tool
    calls carried ids, which fails validation rather than producing a timeline
    nothing can join. Callers must classify the trial first; this is only valid for
    an eligible one.
    """
    bundle = Path(bundle)
    return _read_trace_replay_inputs(bundle, recorded_task(bundle), override)


def _read_trace_replay_inputs(
    bundle: Path, task: dict[str, Any], override: TraceChecksOverride | None
) -> TraceReplayInputs:
    config, provenance = _resolve_trace_checks(bundle, task, override)
    trajectory, tool_log_present = _load_trajectory(bundle)
    recorded = _recorded_grade(bundle)
    return TraceReplayInputs(
        task_id=recorded_task_id(bundle, task),
        config=config,
        provenance=provenance,
        timeline=build_trial_timeline(
            trajectory.messages, trajectory.tool_log, trajectory.termination_reason
        ),
        evidence=BundleEvidence(
            tool_log_present=tool_log_present, schema_version=_schema_version(bundle)
        ),
        recorded_constraints=recorded.constraints,
        recorded_summary=recorded.summary,
        recorded_binary_pass=recorded.binary_pass,
    )


def replay_trace_checks(inputs: TraceReplayInputs) -> TraceChecksResult:
    """Score the resolved constraint block against the reconstructed timeline.

    Drives the one production evaluator, so a re-check and the live grading it is
    compared against are the same computation over two readings of one trial.
    """
    return evaluate_trace_checks(inputs.timeline, inputs.config)


def _bundle_rel(bundle: Path, source: Path) -> Path:
    """A bundle's path under the source, and what the report and the output key on.

    A single-bundle ``source`` has no structure to preserve — the bundle *is* the
    source — so it stands for itself under its own directory name, as judge
    replay's mapping does for the same layout.
    """
    try:
        rel = bundle.relative_to(source)
    except ValueError:
        return Path(bundle.name)
    return Path(bundle.name) if rel == Path(".") else rel


def trace_replay_root(source: Path, replay_id: str) -> Path:
    """The subtree one replay owns, and the only place under the source it writes.

    Public because the command reports the path it wrote to, and composing that
    layout a second time in the CLI would make the two disagree on a rename.
    """
    return source / TRACE_REPLAY_DIRNAME / replay_id


def _trace_replay_destination(source: Path, bundle: Path, replay_id: str) -> Path:
    return trace_replay_root(source, replay_id) / _bundle_rel(bundle, source)


def _dump_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(payload, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _write_trace_checks_result(destination: Path, result: TraceChecksResult) -> None:
    _dump_yaml(destination / TRACE_CHECKS_RESULT_FILENAME, result.model_dump(mode="json"))


def _failed(
    bundle: Path,
    reason: str,
    failure: TraceReplayFailure = TraceReplayFailure.UNREADABLE_INPUT,
) -> TrialTraceReplayOutcome:
    return TrialTraceReplayOutcome(
        bundle=bundle,
        status=TraceReplayOutcomeStatus.FAILED,
        reason=reason,
        failure=failure,
    )


def _check_override_against_bundles(
    override: TraceChecksOverride, bundles: Sequence[Path]
) -> tuple[dict[Path, AuthoringReport], dict[Path, str]]:
    """Check the supplied block against every bundle's recorded tool set.

    Once per *distinct* recorded tool list rather than once per bundle: the gate's
    answer is a function of the inventory alone, and a run repeats one task's list
    byte for byte across its trials. A bundle whose list is present but unreadable
    is returned as a failure instead — its tool set is unknown, not unresolvable,
    and treating the two alike would report an unchecked block as checked.
    """
    grading = {"trace_checks": override.block}
    checked: dict[str | None, AuthoringReport] = {}
    reports: dict[Path, AuthoringReport] = {}
    unreadable: dict[Path, str] = {}
    for bundle in bundles:
        try:
            recorded = _recorded_tool_schemas(bundle)
            if recorded not in checked:
                checked[recorded] = inspect_grading_authoring(
                    grading, tool_inventory_from_bundle(bundle)
                )
        except MissingTraceReplayInputError as exc:
            unreadable[bundle] = str(exc)
            continue
        reports[bundle] = checked[recorded]
    return reports, unreadable


def _recorded_tool_schemas(bundle: Path) -> str | None:
    path = bundle / _TOOLS_SCHEMAS_FILENAME
    return _bundle_text(path) if path.exists() else None


def _refuse_mis_authored_override(
    override: TraceChecksOverride, reports: Mapping[Path, AuthoringReport]
) -> None:
    """Stop the batch on a block that cannot be graded against a bundle's tools.

    Before any trial is re-checked, and carrying every finding that tool set raised
    rather than the first: a misspelled tool name is one defect in one file, and a
    batch that ran anyway would report it as every trial failing the constraint.
    """
    for bundle, report in reports.items():
        if not report.errors:
            continue
        written = "\n".join(f"  - {item.where}: {item.message}" for item in report.errors)
        raise TraceChecksOverrideError(
            f"constraint override {override.path} cannot be graded against the tools "
            f"{bundle} recorded:\n{written}"
        )


def _task_less_disposition(bundle: Path) -> TrialTraceReplayOutcome:
    """What a bundle carrying no ``task.yaml`` is, read off its own trajectory.

    A trial the substrate killed before it ran is written by the executor alone —
    the trajectory and the metrics, no task snapshot — so refusing every task-less
    bundle would report the most common abort shape as a defective input. Anything
    else task-less recorded a real episode and lost what it was graded against,
    which *is* a defective input and stays a failure naming the file.

    The reason names the outcome class, not the operational cause — for a provision
    failure that is ``error_reason`` in the same bundle's ``metrics.yaml``, which
    replay does not read. Raises :class:`MissingTraceReplayInputError` for both
    failing arms, so the batch's per-bundle net reports them.
    """
    trajectory, _ = _load_trajectory(bundle)
    if classify_trial_outcome(trajectory) is not TrialOutcomeClass.INFRASTRUCTURE_ABORT:
        raise MissingTraceReplayInputError(
            f"not a trial bundle: {bundle / 'task.yaml'} is missing, so nothing says what "
            "the trial recorded here was graded against"
        )
    termination = trajectory.termination_reason.value if trajectory.termination_reason else "none"
    return TrialTraceReplayOutcome(
        bundle=bundle,
        status=TraceReplayOutcomeStatus.SKIPPED_NO_TASK,
        reason=(
            "the trial was aborted before it was measured "
            f"(termination_reason: {termination}), so it recorded no task.yaml and "
            "there are no trace checks to re-check it against"
        ),
    )


def _replay_one_bundle(
    source: Path,
    bundle: Path,
    *,
    replay_id: str,
    override: TraceChecksOverride | None,
    dry_run: bool,
    authoring: AuthoringReport | None,
) -> TrialTraceReplayOutcome:
    try:
        if not (bundle / "task.yaml").exists():
            return _task_less_disposition(bundle)
        task = recorded_task(bundle)
        if _classify_trace_trial(task, override) is TraceReplayEligibility.NOT_APPLICABLE:
            return TrialTraceReplayOutcome(
                bundle=bundle, status=TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
            )
        inputs = _read_trace_replay_inputs(bundle, task, override)
    except MissingTraceReplayInputError as exc:
        return _failed(bundle, str(exc), exc.failure)
    except TimelineInconsistencyError as exc:
        return _failed(bundle, str(exc))

    if dry_run:
        return TrialTraceReplayOutcome(
            bundle=bundle,
            status=TraceReplayOutcomeStatus.WOULD_REPLAY,
            provenance=inputs.provenance,
            evidence=inputs.evidence,
            override_authoring=authoring,
            task_id=inputs.task_id,
            config=inputs.config,
            recorded_constraints=inputs.recorded_constraints,
            recorded_binary_pass=inputs.recorded_binary_pass,
        )

    result = replay_trace_checks(inputs)
    _write_trace_checks_result(_trace_replay_destination(source, bundle, replay_id), result)
    return TrialTraceReplayOutcome(
        bundle=bundle,
        status=TraceReplayOutcomeStatus.REPLAYED,
        provenance=inputs.provenance,
        evidence=inputs.evidence,
        result=result,
        override_authoring=authoring,
        task_id=inputs.task_id,
        config=inputs.config,
        recorded_constraints=inputs.recorded_constraints,
        recorded_binary_pass=inputs.recorded_binary_pass,
    )


def run_trace_replay_batch(
    source: Path,
    *,
    replay_id: str,
    trial: Path | None = None,
    override: TraceChecksOverride | None = None,
    dry_run: bool = False,
) -> list[TrialTraceReplayOutcome]:
    """Re-check every eligible trial under ``source`` sequentially.

    An ``override`` is checked against every discovered bundle's recorded tool set
    first and raises :class:`TraceChecksOverrideError` on a defect, so a
    mis-authored block stops the batch rather than arriving as a corpus of failing
    trials. What the gate could not check travels on each outcome.

    A bundle declaring no ``trace_checks`` and given no override is reported
    skipped, and so is one carrying no ``task.yaml`` whose own trajectory calls it
    an infrastructure abort; one that cannot be read or reconstructed — including a
    task-less bundle that did record an episode — is a named per-trial failure and
    the batch continues. With ``dry_run`` the inputs are still resolved — the
    reconstruction is the thing worth checking for free — and nothing is written.
    Otherwise each bundle's recomputed result is written to
    ``<source>/trace_replay/<replay_id>/…``; no file the source already held is
    opened for write.
    """
    source = Path(source)
    bundles = [Path(trial)] if trial is not None else discover_trace_bundles(source)
    reports: dict[Path, AuthoringReport] = {}
    unreadable: dict[Path, str] = {}
    if override is not None:
        reports, unreadable = _check_override_against_bundles(override, bundles)
        _refuse_mis_authored_override(override, reports)

    outcomes: list[TrialTraceReplayOutcome] = []
    for bundle in bundles:
        if bundle in unreadable:
            outcomes.append(_failed(bundle, unreadable[bundle]))
            continue
        outcomes.append(
            _replay_one_bundle(
                source,
                bundle,
                replay_id=replay_id,
                override=override,
                dry_run=dry_run,
                authoring=reports.get(bundle),
            )
        )
    return outcomes


class ConstraintDiscriminationRow(BaseModel):
    """How one declared constraint separated the trials it was measured over.

    A row is keyed by ``(task_id, constraint_id)``, never by the id alone: ids are
    unique only inside one pack's block, so a run spanning tasks would fold two
    unrelated predicates over two unrelated corpora into one row — and every pack's
    constraints would read as unmeasured on every other pack's trials.

    ``trials_evaluated`` counts the trials that reached this constraint, which for a
    route-scoped one is the trials its route won. Undecided trials are excluded from
    ``passed_trials`` / ``failed_trials`` and counted on their own, so a
    classification resting on a single observation is visible as such.
    ``decided_verdict`` is set only for ``UNDECIDED_IN_PART``, where the decided
    trials agreed but the evidence was incomplete.

    ``trials_labelled`` counts the trials on which *this constraint* was decided
    both now and by the live run that wrote the bundle, and
    ``agreed_with_recorded_pass`` how many of those the two agree on. The join is by
    constraint id: comparing a constraint's verdict against the trial-level
    ``binary_pass`` would count a disagreement whenever a trial failed for any other
    reason. Where either side is undecided there is nothing to compare, so the trial
    is not labelled — and a bundle that recorded no verdict for the constraint, an
    ungraded trial or one re-checked against an override naming constraints its pack
    never had, is not labelled either.
    """

    task_id: str
    constraint_id: str
    route: str
    trials_evaluated: int
    trials_decided: int
    passed_trials: int
    failed_trials: int
    undecided_trials: int
    verdict: ConstraintDiscrimination
    decided_verdict: bool | None
    trials_labelled: int
    agreed_with_recorded_pass: int

    model_config = {"extra": "forbid"}


class TrialTraceReplay(BaseModel):
    """One re-checked trial, as the report lists it.

    ``tool_log_present`` is the reader's file-presence answer, so a fully-recorded
    trial that happened to call no tool is not reported as record-less.
    ``provenance`` says whether this trial was measured against its own pack's block
    or a supplied one, per trial rather than per run: a batch given an override
    re-checks every bundle against it, including ones whose pack declared nothing.

    ``recorded_binary_pass`` is the trial-level verdict the live run wrote, ``None``
    on an ungraded trial. It sits here rather than in a constraint row because it is
    a statement about the trial — the per-constraint agreement is on the rows.
    """

    bundle: str
    provenance: ConstraintProvenance
    score: float
    winning_path: str
    gate_failed: bool
    tool_log_present: bool
    recorded_binary_pass: bool | None

    model_config = {"extra": "forbid"}


class TraceReplayEvidence(BaseModel):
    """What the corpus carried, so no verdict is read without it.

    An operator reading ``NEVER_DECIDED`` needs to know whether the corpus is old
    before concluding anything about the constraint: a record-less bundle cannot
    decide a matcher reading a field only the tool-call record carries, and one
    predating call ids cannot be re-checked at all.

    ``bundles_read`` is the denominator of ``bundles_with_tool_log`` and of
    ``schema_versions`` — the bundles whose inputs were reconstructed, which a dry
    run also does. ``schema_versions`` counts the stamps seen, under ``unstamped``
    where a bundle predates the stamp; it is evidence and never a gate.

    ``bundles_skipped`` counts the bundles that declared no ``trace_checks`` and
    nothing else. A bundle carrying no ``task.yaml`` is counted by
    ``bundles_no_task`` instead: what an aborted trial could not say about a pack
    and what a pack chose not to declare are two facts, and one number carrying
    both is a number nobody can act on.
    """

    bundles_read: int
    bundles_with_tool_log: int
    bundles_skipped: int
    bundles_no_task: int
    bundles_failed: int
    bundles_predating_call_ids: int
    schema_versions: dict[str, int]

    model_config = {"extra": "forbid"}


class TraceReplayOverrideNotes(BaseModel):
    """What the authoring gate said about a supplied constraint block.

    Errors never reach a report — they stop the batch before anything is
    re-checked — so these are the advisories and the rules no bundle's recorded
    tool set could answer. Unioned over the bundles and deduplicated: the gate's
    answer is a function of the recorded tool list, and a run repeats one task's
    list byte for byte across its trials. ``None`` on the report where the
    constraints came from the bundles, so an empty pair of lists means the gate ran
    and found nothing rather than that it never ran.
    """

    advisories: list[str]
    unchecked: list[str]

    model_config = {"extra": "forbid"}


class TraceReplayReport(BaseModel):
    """The run-level answer to "is this constraint worth shipping".

    Rows are ordered by task and then by the order the block declares them, so a
    single-task run reads as one contiguous block.
    """

    replay_id: str
    trials: list[TrialTraceReplay]
    discrimination: list[ConstraintDiscriminationRow]
    evidence: TraceReplayEvidence
    override_authoring: TraceReplayOverrideNotes | None = None
    route_scoping: str = _ROUTE_SCOPING_NOTE

    model_config = {"extra": "forbid"}


def declared_trace_checks(
    outcomes: Sequence[TrialTraceReplayOutcome],
) -> dict[Path, TraceChecksConfig]:
    """The block each re-checked bundle was measured against, keyed by bundle.

    What :func:`build_trace_replay_report` takes as ``declared``, read off the batch
    that produced *outcomes*. Re-reading ``task.yaml`` for it would be a second
    source of truth for what a trial was graded on, and would answer the pack's
    block for a trial re-checked against an override.
    """
    return {outcome.bundle: outcome.config for outcome in outcomes if outcome.config is not None}


@dataclass(frozen=True)
class _TrialVerdict:
    """One trial's verdict on one constraint, beside the one the live run recorded.

    ``recorded_pass`` is that constraint's recorded verdict, ``None`` where the live
    run recorded none for it or recorded it undecided — in both cases there is no
    second opinion to agree or disagree with.
    """

    passed: bool
    undecided: bool
    recorded_pass: bool | None


@dataclass
class _RowTally:
    """One discrimination row under construction, over the trials that reached it."""

    task_id: str
    constraint_id: str
    route: str
    position: int
    config: TraceChecksConfig
    verdicts: list[_TrialVerdict] = field(default_factory=list)


def _declared_constraints(config: TraceChecksConfig) -> list[tuple[str, str]]:
    """Every constraint the block declares, with the route it sits in (``""`` shared)."""
    return [(constraint.id, "") for constraint in config.constraints] + [
        (constraint.id, path.id)
        for path in config.alternatives or ()
        for constraint in path.constraints
    ]


def _classify_discrimination(
    verdicts: Sequence[_TrialVerdict],
) -> tuple[ConstraintDiscrimination, bool | None]:
    """The six-valued verdict over one row's trials, and the decided one where split.

    ``NOT_MEASURED`` is answered before anything else: over zero trials "every
    evaluated trial was decided and all passed" is vacuously true and so is its
    mirror, so any other order reports a constraint no trial evaluated as
    unanimously passing.
    """
    if not verdicts:
        return ConstraintDiscrimination.NOT_MEASURED, None
    passed = sum(1 for verdict in verdicts if verdict.passed)
    failed = sum(1 for verdict in verdicts if not verdict.passed and not verdict.undecided)
    if passed and failed:
        return ConstraintDiscrimination.DISCRIMINATING, None
    if not passed and not failed:
        return ConstraintDiscrimination.NEVER_DECIDED, None
    if len(verdicts) > passed + failed:
        return ConstraintDiscrimination.UNDECIDED_IN_PART, bool(passed)
    if passed:
        return ConstraintDiscrimination.ALWAYS_TRUE, None
    return ConstraintDiscrimination.ALWAYS_FALSE, None


def _discrimination_row(tally: _RowTally) -> ConstraintDiscriminationRow:
    verdict, decided_verdict = _classify_discrimination(tally.verdicts)
    decided = [item for item in tally.verdicts if not item.undecided]
    labelled = [item for item in decided if item.recorded_pass is not None]
    return ConstraintDiscriminationRow(
        task_id=tally.task_id,
        constraint_id=tally.constraint_id,
        route=tally.route,
        trials_evaluated=len(tally.verdicts),
        trials_decided=len(decided),
        passed_trials=sum(1 for item in decided if item.passed),
        failed_trials=sum(1 for item in decided if not item.passed),
        undecided_trials=len(tally.verdicts) - len(decided),
        verdict=verdict,
        decided_verdict=decided_verdict,
        trials_labelled=len(labelled),
        agreed_with_recorded_pass=sum(1 for item in labelled if item.passed == item.recorded_pass),
    )


def _seed_declared_rows(
    tallies: dict[tuple[str, str], _RowTally],
    bundle: Path,
    task_id: str,
    config: TraceChecksConfig,
) -> None:
    """Give every constraint the block declares a row before a verdict is counted.

    Refuses a task whose bundles were measured against *different* blocks. A row is
    one claim about one pack's block; two revisions of a pack folded into it would
    tally two predicates under one id and could read ``DISCRIMINATING`` off the
    difference between the blocks rather than between the trials.
    """
    for position, (constraint_id, route) in enumerate(_declared_constraints(config)):
        seeded = tallies.setdefault(
            (task_id, constraint_id), _RowTally(task_id, constraint_id, route, position, config)
        )
        if seeded.config != config:
            raise TraceReplayReportError(
                f"{bundle} re-checks task {task_id!r} against a different trace_checks block "
                f"than an earlier bundle of the same task did, so constraint {constraint_id!r} "
                "has no single row to be reported in. Point --source at one revision of the "
                "pack, or supply --constraints to measure one block over all of them"
            )


def _recorded_verdicts(outcome: TrialTraceReplayOutcome) -> dict[str, bool]:
    """The live run's decided verdict per constraint id, for the report to join against.

    An undecided recorded verdict is left out: it is ``passed=False`` on the wire and
    counting it as a recorded failure would report missing evidence as a disagreement.
    """
    return {
        item.id: item.passed for item in outcome.recorded_constraints or () if not item.undecided
    }


def _declared_block(declared: Mapping[Path, TraceChecksConfig], bundle: Path) -> TraceChecksConfig:
    """The block a re-checked bundle was measured against, refusing a foreign mapping."""
    config = declared.get(bundle)
    if config is None:
        raise TraceReplayReportError(
            f"no constraint block for {bundle}: declared must come from "
            "declared_trace_checks(outcomes) over the same batch, which reads each "
            "bundle's block off the outcome that was re-checked against it"
        )
    return config


def _row_for_verdict(
    tallies: Mapping[tuple[str, str], _RowTally], bundle: Path, task_id: str, constraint_id: str
) -> _RowTally:
    """The seeded row a verdict belongs in, refusing one the declared block never had."""
    tally = tallies.get((task_id, constraint_id))
    if tally is None:
        raise TraceReplayReportError(
            f"{bundle} reached a verdict on constraint {constraint_id!r}, which the block "
            f"declared for task {task_id!r} does not declare — declared must come from "
            "declared_trace_checks(outcomes) over the same batch"
        )
    return tally


def _tally_trials(
    outcomes: Sequence[TrialTraceReplayOutcome], declared: Mapping[Path, TraceChecksConfig]
) -> list[_RowTally]:
    """One row per declared constraint, fed the verdict of every trial that reached it.

    Seeded from ``declared`` and only then fed the emitted verdicts, because a
    constraint inside a route that won no trial is emitted by nothing and would
    otherwise leave the report rather than being reported unmeasured. Each verdict
    carries the live run's verdict on the *same* constraint, joined by id.
    """
    tallies: dict[tuple[str, str], _RowTally] = {}
    for outcome in outcomes:
        if outcome.result is None or outcome.task_id is None:
            continue
        task_id, bundle = outcome.task_id, outcome.bundle
        _seed_declared_rows(tallies, bundle, task_id, _declared_block(declared, bundle))
        recorded = _recorded_verdicts(outcome)
        for item in outcome.result.constraints:
            _row_for_verdict(tallies, bundle, task_id, item.id).verdicts.append(
                _TrialVerdict(item.passed, item.undecided, recorded.get(item.id))
            )
    return sorted(tallies.values(), key=lambda tally: (tally.task_id, tally.position))


def _replay_evidence(outcomes: Sequence[TrialTraceReplayOutcome]) -> TraceReplayEvidence:
    read = [outcome.evidence for outcome in outcomes if outcome.evidence is not None]
    stamps = Counter(
        "unstamped" if evidence.schema_version is None else str(evidence.schema_version)
        for evidence in read
    )
    return TraceReplayEvidence(
        bundles_read=len(read),
        bundles_with_tool_log=sum(1 for evidence in read if evidence.tool_log_present),
        bundles_skipped=sum(
            1
            for outcome in outcomes
            if outcome.status is TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
        ),
        bundles_no_task=sum(
            1 for outcome in outcomes if outcome.status is TraceReplayOutcomeStatus.SKIPPED_NO_TASK
        ),
        bundles_failed=sum(
            1 for outcome in outcomes if outcome.status is TraceReplayOutcomeStatus.FAILED
        ),
        bundles_predating_call_ids=sum(
            1 for outcome in outcomes if outcome.failure is TraceReplayFailure.PREDATES_CALL_IDS
        ),
        schema_versions=dict(stamps),
    )


def _override_notes(
    outcomes: Sequence[TrialTraceReplayOutcome],
) -> TraceReplayOverrideNotes | None:
    reports = [
        outcome.override_authoring for outcome in outcomes if outcome.override_authoring is not None
    ]
    if not reports:
        return None
    return TraceReplayOverrideNotes(
        advisories=sorted(
            {f"{item.where}: {item.message}" for report in reports for item in report.advisories}
        ),
        unchecked=sorted(
            {f"{skip.where}: {skip.reason}" for report in reports for skip in report.unchecked}
        ),
    )


def _trial_row(outcome: TrialTraceReplayOutcome, source: Path) -> TrialTraceReplay:
    """One re-checked trial's row, refusing an outcome that cannot describe itself.

    A result is written by the one path that also resolves the block's provenance and
    reads the bundle's evidence, so an outcome carrying one and not the others did not
    come from that path. Dropping it from the report instead would leave the batch's
    size unaccountable — the trial would be neither reported nor missing.
    """
    result, evidence, provenance = outcome.result, outcome.evidence, outcome.provenance
    if result is None or evidence is None or provenance is None:
        raise TraceReplayReportError(
            f"the outcome for {outcome.bundle} carries a re-checked result without the "
            "provenance of the block it was measured against or the evidence its bundle "
            "carried, so the trial cannot be reported"
        )
    return TrialTraceReplay(
        bundle=str(_bundle_rel(outcome.bundle, source)),
        provenance=provenance,
        score=result.score,
        winning_path=result.winning_path,
        gate_failed=result.gate_failed,
        tool_log_present=evidence.tool_log_present,
        recorded_binary_pass=outcome.recorded_binary_pass,
    )


def build_trace_replay_report(
    outcomes: Sequence[TrialTraceReplayOutcome],
    *,
    declared: Mapping[Path, TraceChecksConfig],
    source: Path,
    replay_id: str,
) -> TraceReplayReport | None:
    """The author-facing answer to whether each constraint was worth shipping.

    ``declared`` maps each re-checked bundle to the block it was measured against;
    :func:`declared_trace_checks` reads it off the same batch. It is not derivable
    from the verdicts: ``evaluate_trace_checks`` emits the shared constraints plus
    the winning route's only, so the constraints of a route that lost every trial
    appear in no result and would leave the report entirely.

    ``None`` where discovery found nothing, so a selector matching no bundle writes
    no report. A corpus whose bundles all skipped or failed still gets one — the
    evidence block is where that is said.
    """
    if not outcomes:
        return None
    trials = [_trial_row(outcome, source) for outcome in outcomes if outcome.result is not None]
    return TraceReplayReport(
        replay_id=replay_id,
        trials=trials,
        discrimination=[_discrimination_row(tally) for tally in _tally_trials(outcomes, declared)],
        evidence=_replay_evidence(outcomes),
        override_authoring=_override_notes(outcomes),
    )


def emit_trace_replay_report(
    outcomes: Sequence[TrialTraceReplayOutcome],
    *,
    declared: Mapping[Path, TraceChecksConfig],
    source: Path,
    replay_id: str,
) -> TraceReplayReport | None:
    """Build the discrimination report and write it beside the per-bundle results.

    Lands at ``<source>/trace_replay/<replay_id>/trace_replay_report.yaml``, and
    returns ``None`` having written nothing where the builder had nothing to report.
    """
    report = build_trace_replay_report(
        outcomes, declared=declared, source=source, replay_id=replay_id
    )
    if report is None:
        return None
    destination = trace_replay_root(source, replay_id) / TRACE_REPLAY_REPORT_FILENAME
    _dump_yaml(destination, report.model_dump(mode="json"))
    return report
