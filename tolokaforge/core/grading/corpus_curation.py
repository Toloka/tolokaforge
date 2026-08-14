"""A run directory becomes a judge-labelled corpus, by a rule that runs.

A migration's evidence is a set of recorded trials somebody chose, and the choosing is
where the judgment sits. This module makes the choice mechanical and then writes it
down: every discovered bundle is either admitted with the verdict its judge recorded or
named with the reason it was not, and both halves land in the corpus's own
``corpus.yaml``. The composition therefore survives the run directory it came from,
which is gitignored and, for the corpus already committed here, gone from most
checkouts.

**Admission.** A bundle is admitted iff it carries ``task.yaml``, ``trajectory.yaml``
and ``grade.yaml``; its judge ran to completion; its ``criterion_results`` holds a
verdict for the curated criterion; and it is not environment-dead.

**Environment-dead is defined on the record, not on rendered text.** A bundle is
environment-dead iff it carries a ``tool_log.yaml``, that record holds at least one
call, and **no** call has ``status: success`` — a trial whose tools never worked was
still judged, and the judge scored a transcript of failures. The status comes from the
record alone (:func:`~tolokaforge.core.output.artifacts.read_recorded_tool_log`), while
the ``role: tool`` message's ``Error: `` prefix is harness-rendered rather than a field,
so a **record-less** bundle is not rejected by this rule: the claim would need evidence
the bundle does not carry. Each admitted bundle's manifest entry records whether it
carried a record, so the rule's reach is written down rather than assumed.

**The write set is what a differential reads** — ``task.yaml``, ``trajectory.yaml``,
``grade.yaml``, ``tools_schemas.yaml``, ``metrics.yaml`` — plus ``tool_log.yaml``
wherever the source bundle had one. A corpus is record-carrying exactly when its sources
were, which is the property a constraint reading ``status``, ``executor`` or
``latency_seconds`` needs (``docs/RUBRIC_MIGRATION.md``).

**A multi-part corpus is a directory whose subdirectories are corpora**, each written by
its own invocation. Nothing here knows about halves: two invocations with two ``--into``
targets is what makes one, and each half stays a corpus a reader can point a command at
on its own.

Nothing here spends anything: it reaches the one bundle-discovery rule, the bundle
readers and the tool-call record, and stops there.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import chain
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, model_validator

from tolokaforge.core.grading.replay_layout import discover_trial_bundles
from tolokaforge.core.grading.trace_replay import (
    MissingTraceReplayInputError,
    recorded_grade,
    recorded_task,
    recorded_task_id,
)
from tolokaforge.core.models import JudgeStatus, RecordedToolCall, ToolExecutionStatus
from tolokaforge.core.output.artifacts import RedactedBundleError, read_recorded_tool_log
from tolokaforge.core.output_writer import (
    GRADE_FILENAME,
    METRICS_FILENAME,
    TASK_FILENAME,
    TOOL_LOG_FILENAME,
    TRAJECTORY_FILENAME,
)

__all__ = [
    "CORPUS_CARRIED_FILES",
    "CORPUS_MANIFEST_FILENAME",
    "CORPUS_REQUIRED_FILES",
    "CorpusManifest",
    "CuratedBundle",
    "CurationError",
    "CurationOutcome",
    "CurationRejection",
    "RejectedTrial",
    "RejectionAuthority",
    "curate_corpus",
    "is_corpus_directory",
]

#: The manifest a corpus directory carries, and what makes a directory one.
CORPUS_MANIFEST_FILENAME = "corpus.yaml"
#: Written for every admitted bundle — the admission rule guarantees all three.
CORPUS_REQUIRED_FILES = (TASK_FILENAME, TRAJECTORY_FILENAME, GRADE_FILENAME)
#: Written where the source bundle carries them. ``tool_log.yaml``'s absence changes
#: what a constraint can read, so the manifest records it per bundle as well.
CORPUS_CARRIED_FILES = ("tools_schemas.yaml", METRICS_FILENAME, TOOL_LOG_FILENAME)

#: The subdirectory a run keeps its trials under: ``<run-id>/trials/<task-id>/<index>``.
_RUN_TRIALS_DIRNAME = "trials"


class CurationError(Exception):
    """A curating run cannot proceed — bad destination, unreadable bundle, or a
    naming collision the corpus layout cannot express."""


class CurationRejection(str, Enum):
    """Why a discovered trial did not enter the corpus."""

    MISSING_TASK_SNAPSHOT = "missing_task_snapshot"
    MISSING_GRADE = "missing_grade"
    JUDGE_INCOMPLETE = "judge_incomplete"
    CRITERION_NOT_JUDGED = "criterion_not_judged"
    ENVIRONMENT_DEAD = "environment_dead"
    AUTHOR_EXCLUDED = "author_excluded"


class RejectionAuthority(str, Enum):
    """Whose judgment the rejection was."""

    RULE = "rule"
    AUTHOR = "author"


class CuratedBundle(BaseModel):
    """One admitted trial, as the corpus records it."""

    directory: str
    source_run: str
    task_id: str
    agent_model: str
    # ``None`` where the trial's own snapshot names no judge: the harness records
    # ``model_config.judge: null`` for a run that configures no run-level judge, and the
    # label still came from the judge the pack's ``grading_config`` named.
    judge_model: str | None
    # The judge's recorded verdict for the curated criterion — the label the corpus
    # exists to carry.
    met: bool
    # Whether the source bundle carried ``tool_log.yaml``. Where false, the
    # environment-dead rule had no record to read and did not classify this bundle,
    # and a constraint reading ``status`` cannot be recomputed over it.
    record_carried: bool

    model_config = {"extra": "forbid"}


class RejectedTrial(BaseModel):
    """One discovered trial the corpus does not hold, and why."""

    # The path the curating run read it from, as the sources were named on the
    # command line: the run directories are gitignored, so this outlives them only
    # as a statement of where the trial was.
    source: str
    reason: CurationRejection
    by: RejectionAuthority
    # For a rule rejection, the measurement behind it (``tool_calls: 6, succeeded: 0``);
    # for an author rejection, the reason the author gave.
    observation: str

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _require_the_authority_the_reason_carries(self) -> RejectedTrial:
        """``author_excluded`` is the author's judgment and every other reason is a rule's,
        in both directions.

        The manifest is what outlives the gitignored runs behind a corpus, so it is read as
        the account of why a trial is missing. Unchecked, a hand-edited entry could claim a
        rule found a bundle environment-dead when an author dropped it, or credit the author
        with a measurement they never made — and nothing on disk would contradict either.
        """
        by_the_author = self.reason is CurationRejection.AUTHOR_EXCLUDED
        if by_the_author is (self.by is RejectionAuthority.AUTHOR):
            return self
        raise ValueError(
            f"reason: {self.reason.value} is recorded by: {self.by.value}. "
            f"{CurationRejection.AUTHOR_EXCLUDED.value} is the one rejection an author makes "
            "and every other is a rule's, so a corpus recording the pair the other way round "
            "attributes a judgment to whoever did not make it"
        )


class CorpusManifest(BaseModel):
    """A corpus's own account of its composition."""

    criterion: str
    task_ids: list[str]
    curated_from: list[str]
    curated_at: datetime
    bundles: list[CuratedBundle]
    excluded: list[RejectedTrial]

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class CurationOutcome:
    """What one curating run admitted, rejected and wrote.

    ``written`` is false for a dry run and for a run that admitted nothing — a corpus
    holding no bundle is not written, and the caller reports that rather than leaving
    an empty directory behind.
    """

    into: Path
    sources: tuple[Path, ...]
    manifest: CorpusManifest
    written: bool


def is_corpus_directory(path: Path) -> bool:
    """Whether ``path`` is a corpus this command writes.

    One directory carrying a manifest, or one whose immediate subdirectories all do — the
    multi-part shape above, and exactly one level of it. "A manifest anywhere beneath"
    would call ``tests/data/migration_corpora`` a corpus while it is every corpus in the
    tree at once.
    """
    if not path.is_dir():
        return False
    if (path / CORPUS_MANIFEST_FILENAME).exists():
        return True
    parts = [child for child in path.iterdir() if child.is_dir()]
    return bool(parts) and all((part / CORPUS_MANIFEST_FILENAME).exists() for part in parts)


def curate_corpus(
    *,
    sources: Sequence[Path],
    into: Path,
    criterion: str,
    exclusions: Mapping[Path, str] | None = None,
    replace: bool = False,
    dry_run: bool = False,
) -> CurationOutcome:
    """Write the corpus of *criterion* the bundles under *sources* support.

    Raises:
        CurationError: the destination is not one this command may write, an
            ``exclusions`` key names no discovered bundle or one the rule already
            rejected, a bundle cannot be read as a recorded trial, or two admitted
            bundles claim one directory name.
    """
    sources = tuple(Path(source) for source in sources)
    into = Path(into)
    named_exclusions = {Path(path).resolve(): reason for path, reason in (exclusions or {}).items()}
    _refuse_a_destination_this_command_may_not_write(into, sources, replace=replace)

    discovered = _discovered_bundles(sources)
    _refuse_an_exclusion_naming_no_discovered_bundle(named_exclusions, discovered)

    admitted: list[tuple[Path, CuratedBundle]] = []
    rejected: list[RejectedTrial] = []
    for bundle in discovered:
        disposition = _classified(bundle, criterion=criterion)
        excluded_by = named_exclusions.get(bundle.resolve())
        if isinstance(disposition, RejectedTrial):
            _refuse_an_exclusion_the_rule_already_covers(bundle, disposition, excluded_by)
            rejected.append(disposition)
        elif excluded_by is not None:
            rejected.append(_author_rejection(bundle, excluded_by))
        else:
            admitted.append((bundle, disposition))
    _refuse_two_bundles_claiming_one_directory(admitted)

    manifest = CorpusManifest(
        criterion=criterion,
        task_ids=sorted({entry.task_id for _, entry in admitted}),
        curated_from=sorted({entry.source_run for _, entry in admitted}),
        curated_at=datetime.now(timezone.utc),
        bundles=[entry for _, entry in admitted],
        excluded=rejected,
    )
    written = bool(admitted) and not dry_run
    if written:
        _write_corpus(into, manifest, admitted, replace=replace)
    return CurationOutcome(into=into, sources=sources, manifest=manifest, written=written)


def _discovered_bundles(sources: Sequence[Path]) -> list[Path]:
    """Every recorded trial under *sources*, in source order, each one once.

    Two sources may overlap — a run directory and one bundle inside it — and a
    bundle curated twice would be copied twice under one name.
    """
    discovered: list[Path] = []
    seen: set[Path] = set()
    for bundle in chain.from_iterable(discover_trial_bundles(source) for source in sources):
        if bundle.resolve() in seen:
            continue
        seen.add(bundle.resolve())
        discovered.append(bundle)
    return discovered


def _classified(bundle: Path, *, criterion: str) -> CuratedBundle | RejectedTrial:
    """Admit *bundle* into the corpus of *criterion*, or name why it does not enter."""
    if not (bundle / TASK_FILENAME).exists():
        return _rule_rejection(bundle, CurationRejection.MISSING_TASK_SNAPSHOT, "no task.yaml")
    grade = _read(bundle, recorded_grade)
    if grade is None:
        return _rule_rejection(bundle, CurationRejection.MISSING_GRADE, "no grade.yaml")

    judge_status = grade.get("judge_status")
    if judge_status != JudgeStatus.COMPLETED.value:
        return _rule_rejection(
            bundle, CurationRejection.JUDGE_INCOMPLETE, f"judge_status: {judge_status!r}"
        )
    row = _recorded_row(grade, criterion)
    met = row.get("met") if row is not None else None
    if not isinstance(met, bool):
        return _rule_rejection(
            bundle,
            CurationRejection.CRITERION_NOT_JUDGED,
            _why_the_criterion_carries_no_verdict(grade, criterion, row),
        )

    calls, record_carried = _recorded_tool_calls(bundle)
    if record_carried and calls and not any(_succeeded(call) for call in calls):
        return _rule_rejection(
            bundle,
            CurationRejection.ENVIRONMENT_DEAD,
            f"tool_calls: {len(calls)}, succeeded: 0",
        )

    task = _read(bundle, recorded_task)
    run_id, trial_index = _recorded_run(bundle)
    return CuratedBundle(
        directory=f"{run_id}_trial{trial_index}",
        source_run=run_id,
        task_id=recorded_task_id(bundle, task),
        agent_model=_recorded_agent_model(bundle, task),
        judge_model=_named_model(task, "judge"),
        met=met,
        record_carried=record_carried,
    )


def _succeeded(call: RecordedToolCall) -> bool:
    return call.status is ToolExecutionStatus.SUCCESS


def _read(bundle: Path, reader: Callable[[Path], Any]) -> Any:
    """Read a bundle artifact, reporting an unreadable one as a curation failure."""
    try:
        return reader(bundle)
    except MissingTraceReplayInputError as exc:
        raise CurationError(f"{bundle} cannot be read as a recorded trial: {exc}") from exc


def _recorded_tool_calls(bundle: Path) -> tuple[list[RecordedToolCall], bool]:
    try:
        return read_recorded_tool_log(bundle)
    except RedactedBundleError as exc:
        raise CurationError(
            f"{bundle} was redacted before it was written, so it is not evidence of what "
            f"the agent did: {exc}"
        ) from exc
    except ValueError as exc:
        raise CurationError(f"{bundle} carries an unreadable tool-call record: {exc}") from exc


def _recorded_row(grade: Mapping[str, Any], criterion: str) -> Mapping[str, Any] | None:
    """The judge's row for *criterion*, ``None`` where it judged no such criterion."""
    for result in grade.get("criterion_results") or []:
        if isinstance(result, Mapping) and result.get("id") == criterion:
            return result
    return None


def _why_the_criterion_carries_no_verdict(
    grade: Mapping[str, Any], criterion: str, row: Mapping[str, Any] | None
) -> str:
    """Two states share the rejection and not the observation.

    A row that is *there* and holds a non-boolean ``met`` would otherwise be reported by
    listing the criteria judged — which includes the very criterion the reason says was not.
    """
    if row is None:
        return f"criteria judged: {_judged_criteria(grade)}"
    return f"{criterion} recorded met: {row.get('met')!r}, which is not a verdict"


def _judged_criteria(grade: Mapping[str, Any]) -> str:
    judged = [
        str(result.get("id"))
        for result in grade.get("criterion_results") or []
        if isinstance(result, Mapping)
    ]
    return ", ".join(judged) if judged else "none"


def _named_model(task: Mapping[str, Any], role: str) -> str | None:
    """The model a recorded trial ran *role* on, off its own ``task.yaml``."""
    configured = task.get("model_config") or {}
    named = (configured.get(role) or {}).get("name") if isinstance(configured, Mapping) else None
    return named if isinstance(named, str) and named else None


def _recorded_agent_model(bundle: Path, task: Mapping[str, Any]) -> str:
    """The model that produced the transcript — a trial the corpus admits has one."""
    named = _named_model(task, "agent")
    if named is None:
        raise CurationError(
            f"{bundle / TASK_FILENAME} names no model_config.agent.name, so the corpus cannot "
            f"say which model produced the trial it would admit"
        )
    return named


def _recorded_run(bundle: Path) -> tuple[str, str]:
    """The run a bundle belongs to and its index in it, off its recorded location."""
    trial = bundle.resolve()
    if trial.parent.parent.name != _RUN_TRIALS_DIRNAME:
        raise CurationError(
            f"{bundle} does not sit at <run-id>/{_RUN_TRIALS_DIRNAME}/<task-id>/<index>, so "
            "nothing names the run it came from and the corpus cannot name its directory "
            "<run-id>_trial<index>"
        )
    return trial.parent.parent.parent.name, trial.name


def _rule_rejection(bundle: Path, reason: CurationRejection, observation: str) -> RejectedTrial:
    return RejectedTrial(
        source=str(bundle), reason=reason, by=RejectionAuthority.RULE, observation=observation
    )


def _author_rejection(bundle: Path, reason: str) -> RejectedTrial:
    return RejectedTrial(
        source=str(bundle),
        reason=CurationRejection.AUTHOR_EXCLUDED,
        by=RejectionAuthority.AUTHOR,
        observation=reason,
    )


def _refuse_an_exclusion_naming_no_discovered_bundle(
    exclusions: Mapping[Path, str], discovered: Sequence[Path]
) -> None:
    unmatched = sorted(str(path) for path in set(exclusions) - {b.resolve() for b in discovered})
    if unmatched:
        raise CurationError(
            f"excluded {', '.join(unmatched)}, which no --source discovered — an exclusion "
            "the run never reached excludes nothing, and a corpus written past it would "
            "hold whatever the author meant to keep out"
        )


def _refuse_an_exclusion_the_rule_already_covers(
    bundle: Path, rejection: RejectedTrial, excluded_by: str | None
) -> None:
    """An author excluding what a rule already rejects would overwrite the measurement.

    The manifest records one reason per trial, and the rule's is the one that survives
    the run directory: it names what was observed rather than what was decided.
    """
    if excluded_by is None:
        return
    raise CurationError(
        f"excluded {bundle} as {excluded_by!r}, but the rule already rejects it as "
        f"{rejection.reason.value} ({rejection.observation}). --exclude is for a bundle "
        "no rule covers"
    )


def _refuse_two_bundles_claiming_one_directory(
    admitted: Sequence[tuple[Path, CuratedBundle]],
) -> None:
    """One run's two tasks share a trial index, and the naming rule cannot tell them apart."""
    claimed: dict[str, Path] = {}
    for source, entry in admitted:
        clash = claimed.get(entry.directory)
        if clash is not None:
            raise CurationError(
                f"{source} and {clash} both curate to {entry.directory}: one run's trials "
                "are indexed per task, and a corpus naming bundles <run-id>_trial<index> "
                "cannot hold two tasks' trials from one run. Curate them into corpora of "
                "their own"
            )
        claimed[entry.directory] = source


def _refuse_a_destination_this_command_may_not_write(
    into: Path, sources: Sequence[Path], *, replace: bool
) -> None:
    for source in sources:
        if _nests(into, source):
            raise CurationError(
                f"--into {into} and --source {source} nest, and --replace rewrites the whole "
                "destination directory — curate into a directory outside every source"
            )
    if not into.exists():
        return
    if not into.is_dir():
        raise CurationError(f"--into {into} is not a directory")
    if not (into / CORPUS_MANIFEST_FILENAME).exists():
        if any(into.iterdir()):
            raise CurationError(
                f"{into} is not empty and holds no {CORPUS_MANIFEST_FILENAME}, so it is not a "
                "corpus this command wrote; curate into a directory of the corpus's own"
            )
        return
    if not replace:
        raise CurationError(
            f"{into} already holds {CORPUS_MANIFEST_FILENAME}. Pass --replace to rewrite the "
            "whole corpus — a corpus is never a half-refresh"
        )


def _nests(one: Path, other: Path) -> bool:
    left, right = one.resolve(), other.resolve()
    return left == right or left in right.parents or right in left.parents


def _write_corpus(
    into: Path,
    manifest: CorpusManifest,
    admitted: Sequence[tuple[Path, CuratedBundle]],
    *,
    replace: bool,
) -> None:
    if replace and into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True, exist_ok=True)
    for source, entry in admitted:
        _copy_bundle(source, into / entry.directory)
    (into / CORPUS_MANIFEST_FILENAME).write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )


def _copy_bundle(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in CORPUS_REQUIRED_FILES:
        shutil.copy2(source / name, destination / name)
    for name in CORPUS_CARRIED_FILES:
        if (source / name).exists():
            shutil.copy2(source / name, destination / name)
