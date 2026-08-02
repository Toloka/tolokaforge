"""Trace-check replay — re-evaluate a pack's constraints over recorded bundles.

Replay is a *caller* of the one production evaluator
(:func:`~tolokaforge.core.grading.trace_checks.evaluate_trace_checks`), never a
second implementation. It reads a trial bundle written by the eval flow, rebuilds
the timeline from what the bundle persisted — ``trajectory.yaml`` for the message
view, ``tool_log.yaml`` for the tool-call record — and scores the pack's
``trace_checks`` block against it again.

Nothing here runs an agent, an environment or a judge, so a replay costs no
tokens and starts no container. That is a structural property of the imports, not
a promise: this module reaches the evaluator and the bundle reader and stops
there.

The source run is left as it was found. Artifacts land under
``<source>/trace_replay/<replay_id>/…``, a sibling of judge replay's ``replays/``
so neither command's discovery walks the other's output.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_core import ErrorDetails

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
    "TRACE_CHECKS_RESULT_FILENAME",
    "TRACE_REPLAY_DIRNAME",
    "BundleEvidence",
    "ConstraintProvenance",
    "MissingTraceReplayInputError",
    "TraceReplayEligibility",
    "TraceReplayInputs",
    "TraceReplayOutcomeStatus",
    "TrialTraceReplayOutcome",
    "classify_trace_trial",
    "discover_trace_bundles",
    "read_trace_replay_inputs",
    "replay_trace_checks",
    "run_trace_replay_batch",
]

#: Subdirectory replay artifacts are written under; excluded from discovery so a
#: source pointed at a previous run's output never re-checks bundles nested there.
TRACE_REPLAY_DIRNAME = "trace_replay"
#: Per-bundle artifact name. Deliberately one no trial bundle already holds, so a
#: write that escaped the output subtree creates a file rather than clobbering one.
TRACE_CHECKS_RESULT_FILENAME = "trace_checks_result.yaml"

_BUNDLE_MARKERS = ("task.yaml", "trajectory.yaml")


class TraceReplayEligibility(str, Enum):
    """Whether a recorded trial has trace constraints to re-check."""

    ELIGIBLE = "eligible"
    NOT_APPLICABLE = "not_applicable"


class TraceReplayOutcomeStatus(str, Enum):
    """Per-trial disposition in a batch replay.

    ``REPLAYED`` — recomputed and the artifact written. ``WOULD_REPLAY`` — a
    ``dry_run`` trial that is eligible and reconstructable. ``SKIPPED_NOT_APPLICABLE``
    — the bundle declares no ``trace_checks`` and no override was supplied, which is
    a declared skip, never a silent one. ``FAILED`` — the bundle could not be
    classified or reconstructed; ``reason`` names the file and the defect, and the
    batch continues.
    """

    REPLAYED = "replayed"
    WOULD_REPLAY = "would_replay"
    SKIPPED_NOT_APPLICABLE = "skipped_not_applicable"
    FAILED = "failed"


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
    declares no ``trace_checks`` — that is a declared skip.
    """


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
    """

    config: TraceChecksConfig
    provenance: ConstraintProvenance
    timeline: TrialTimeline
    evidence: BundleEvidence
    recorded_constraints: tuple[TraceConstraintResult, ...] | None
    recorded_summary: TraceChecksSummary | None


@dataclass(frozen=True)
class TrialTraceReplayOutcome:
    """Outcome of one trial in a batch replay.

    ``evidence`` is ``None`` exactly where the bundle was never read — a declared
    skip or a failure — which is a different statement from an unstamped,
    record-less bundle.
    """

    bundle: Path
    status: TraceReplayOutcomeStatus
    reason: str | None = None
    provenance: ConstraintProvenance | None = None
    evidence: BundleEvidence | None = None
    result: TraceChecksResult | None = None
    artifacts_dir: Path | None = None


def _load_yaml_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MissingTraceReplayInputError(f"unreadable YAML at {path}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else None


def _is_bundle(path: Path) -> bool:
    return path.is_dir() and all((path / marker).exists() for marker in _BUNDLE_MARKERS)


def discover_trace_bundles(source: Path) -> list[Path]:
    """Discover re-checkable trial bundles under ``source``, layout-agnostic.

    A directory is a bundle iff it directly contains ``task.yaml`` +
    ``trajectory.yaml``. Not ``grade.yaml``: a trial is worth re-checking whether
    or not it was ever graded. Handles the three recorded layouts uniformly — a run
    dir with a ``trials/<task>/<idx>/`` subtree, a flat collection of bundle dirs,
    or a single bundle dir. Returned sorted for stable batches.
    """
    source = Path(source)
    if _is_bundle(source):
        return [source]
    bundles = {
        marker.parent
        for marker in source.rglob("trajectory.yaml")
        if TRACE_REPLAY_DIRNAME not in marker.relative_to(source).parts
        and _is_bundle(marker.parent)
    }
    return sorted(bundles)


def classify_trace_trial(
    bundle: Path, *, override: TraceChecksConfig | None = None
) -> TraceReplayEligibility:
    """Classify a recorded trial as having constraints to re-check, or not.

    Eligible iff the bundle's ``task.yaml`` declares a ``grading_config.trace_checks``
    block, or an override supplies one — an override replaces the block wholesale,
    so it makes a trial that declared none re-checkable. Raises
    :class:`MissingTraceReplayInputError` when ``task.yaml`` is missing or is not a
    mapping: that is not a constraint-less trial, it is not a trial bundle.
    """
    task = _load_yaml_mapping(Path(bundle) / "task.yaml")
    if task is None:
        raise MissingTraceReplayInputError(
            f"not a trial bundle: {Path(bundle) / 'task.yaml'} is missing or not a mapping"
        )
    if override is not None or _declared_trace_checks(task) is not None:
        return TraceReplayEligibility.ELIGIBLE
    return TraceReplayEligibility.NOT_APPLICABLE


def _declared_trace_checks(task: dict[str, Any] | None) -> Any:
    return ((task or {}).get("grading_config") or {}).get("trace_checks")


def _resolve_trace_checks(
    bundle: Path, task: dict[str, Any] | None, override: TraceChecksConfig | None
) -> tuple[TraceChecksConfig, ConstraintProvenance]:
    if override is not None:
        return override, ConstraintProvenance.OVERRIDE
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
        f"produced, so the trial cannot be re-checked at all. {detail}"
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


def _recorded_verdicts(
    bundle: Path,
) -> tuple[tuple[TraceConstraintResult, ...] | None, TraceChecksSummary | None]:
    grade = _load_yaml_mapping(bundle / "grade.yaml")
    if grade is None:
        return None, None
    recorded = grade.get("trace_check_results")
    summary = grade.get("trace_checks_summary")
    try:
        constraints = (
            None
            if recorded is None
            else tuple(TraceConstraintResult.model_validate(item) for item in recorded)
        )
        return constraints, (
            None if summary is None else TraceChecksSummary.model_validate(summary)
        )
    except ValidationError as exc:
        raise MissingTraceReplayInputError(
            f"{bundle / 'grade.yaml'} records trace-check verdicts that do not validate: {exc}"
        ) from exc


def _schema_version(bundle: Path) -> int | None:
    """The bundle's schema stamp, ``None`` where the bundle predates the stamp."""
    stamped = (_load_yaml_mapping(bundle / "metrics.yaml") or {}).get("schema_version")
    if stamped is None or (isinstance(stamped, int) and not isinstance(stamped, bool)):
        return stamped
    raise MissingTraceReplayInputError(
        f"{bundle / 'metrics.yaml'} stamps schema_version {stamped!r}, which is not a "
        "version number, so the bundle cannot say which artifacts it carries"
    )


def read_trace_replay_inputs(
    bundle: Path, *, override: TraceChecksConfig | None = None
) -> TraceReplayInputs:
    """Reconstruct one bundle's re-check inputs from what it persisted.

    Raises :class:`MissingTraceReplayInputError`, naming the file and the defect,
    for every unreadable or invalid input — including a bundle written before tool
    calls carried ids, which fails validation rather than producing a timeline
    nothing can join. Callers must classify the trial first; this is only valid for
    an eligible one.
    """
    bundle = Path(bundle)
    task = _load_yaml_mapping(bundle / "task.yaml")
    config, provenance = _resolve_trace_checks(bundle, task, override)
    trajectory, tool_log_present = _load_trajectory(bundle)
    recorded_constraints, recorded_summary = _recorded_verdicts(bundle)
    return TraceReplayInputs(
        config=config,
        provenance=provenance,
        timeline=build_trial_timeline(
            trajectory.messages, trajectory.tool_log, trajectory.termination_reason
        ),
        evidence=BundleEvidence(
            tool_log_present=tool_log_present, schema_version=_schema_version(bundle)
        ),
        recorded_constraints=recorded_constraints,
        recorded_summary=recorded_summary,
    )


def replay_trace_checks(inputs: TraceReplayInputs) -> TraceChecksResult:
    """Score the resolved constraint block against the reconstructed timeline.

    Drives the one production evaluator, so a re-check and the live grading it is
    compared against are the same computation over two readings of one trial.
    """
    return evaluate_trace_checks(inputs.timeline, inputs.config)


def _trace_replay_destination(source: Path, bundle: Path, replay_id: str) -> Path:
    """Where a bundle's artifacts go, preserving its discovered path structure.

    A single-bundle ``source`` has no structure to preserve — the bundle *is* the
    source — so it lands under its own directory name, as judge replay's mapping
    does for the same layout.
    """
    try:
        rel = bundle.relative_to(source)
    except ValueError:
        rel = Path(bundle.name)
    if rel == Path("."):
        rel = Path(bundle.name)
    return source / TRACE_REPLAY_DIRNAME / replay_id / rel


def _write_trace_checks_result(destination: Path, result: TraceChecksResult) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with open(destination / TRACE_CHECKS_RESULT_FILENAME, "w", encoding="utf-8") as f:
        yaml.dump(
            result.model_dump(mode="json"),
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


def _replay_one_bundle(
    source: Path,
    bundle: Path,
    *,
    replay_id: str,
    override: TraceChecksConfig | None,
    dry_run: bool,
) -> TrialTraceReplayOutcome:
    try:
        if classify_trace_trial(bundle, override=override) is TraceReplayEligibility.NOT_APPLICABLE:
            return TrialTraceReplayOutcome(
                bundle=bundle, status=TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
            )
        inputs = read_trace_replay_inputs(bundle, override=override)
    except (MissingTraceReplayInputError, TimelineInconsistencyError) as exc:
        return TrialTraceReplayOutcome(
            bundle=bundle, status=TraceReplayOutcomeStatus.FAILED, reason=str(exc)
        )

    if dry_run:
        return TrialTraceReplayOutcome(
            bundle=bundle,
            status=TraceReplayOutcomeStatus.WOULD_REPLAY,
            provenance=inputs.provenance,
            evidence=inputs.evidence,
        )

    result = replay_trace_checks(inputs)
    destination = _trace_replay_destination(source, bundle, replay_id)
    _write_trace_checks_result(destination, result)
    return TrialTraceReplayOutcome(
        bundle=bundle,
        status=TraceReplayOutcomeStatus.REPLAYED,
        provenance=inputs.provenance,
        evidence=inputs.evidence,
        result=result,
        artifacts_dir=destination,
    )


def run_trace_replay_batch(
    source: Path,
    *,
    replay_id: str,
    trial: Path | None = None,
    override: TraceChecksConfig | None = None,
    dry_run: bool = False,
) -> list[TrialTraceReplayOutcome]:
    """Re-check every eligible trial under ``source`` sequentially.

    A bundle declaring no ``trace_checks`` and given no override is reported
    skipped; one that cannot be read or reconstructed is a named per-trial failure
    and the batch continues. With ``dry_run`` the inputs are still resolved — the
    reconstruction is the thing worth checking for free — and nothing is written.
    Otherwise each bundle's recomputed result is written to
    ``<source>/trace_replay/<replay_id>/…``; no file the source already held is
    opened for write.
    """
    source = Path(source)
    bundles = [Path(trial)] if trial is not None else discover_trace_bundles(source)
    return [
        _replay_one_bundle(source, bundle, replay_id=replay_id, override=override, dry_run=dry_run)
        for bundle in bundles
    ]
