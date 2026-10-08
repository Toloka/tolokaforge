"""Post-run invariants over a finished run bundle.

Every rule here answers one question: *does this bundle report a number it
did not measure?* A benchmark that scores a trial it never ran, or counts a
trial that is not on disk, is not noisy — it is wrong in a direction no
downstream consumer can detect, because a fabricated zero and a measured zero
are the same bytes.

The rules read the bundle and nothing else: no model, no network, no
grading internals beyond the shapes :mod:`tolokaforge.core.output.aggregate_models`
and :class:`tolokaforge.core.models.Grade` already publish. They run in
milliseconds over a finished run directory, which is what lets them be a gate
rather than a report.

* :class:`FidelityRule` — the rule ids, and what each one asserts.
* :class:`FidelityViolation` — one failed assertion, with where and why.
* :func:`check_run_bundle` — every rule over one run directory.

``infrastructure_aborts`` keys are derived from
:data:`tolokaforge.core.failure_attribution.EXCLUDED_TYPED_REASONS` rather
than listed here, so a termination reason added there cannot become silently
uncountable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.failure_attribution import EXCLUDED_TYPED_REASONS
from tolokaforge.core.models.grade_components import GradeComponents
from tolokaforge.core.output_writer import (
    GRADE_FILENAME,
    METRICS_FILENAME,
    TASK_FILENAME,
)

__all__ = [
    "AGGREGATE_FILENAME",
    "FidelityRule",
    "FidelityViolation",
    "PER_TASK_METRICS_FILENAME",
    "check_run_bundle",
    "expected_infrastructure_abort_keys",
]

AGGREGATE_FILENAME = "aggregate.json"
PER_TASK_METRICS_FILENAME = "per_task_metrics.json"
TRIALS_DIRNAME = "trials"

_COMPONENT_FIELDS: tuple[str, ...] = tuple(GradeComponents.model_fields)
"""The grading tiers, read off the model so a new tier is covered on arrival."""

_ORACLE_HARNESS = "oracle"
"""The declared harness that solves a task without calling a model.

An oracle trial spends nothing by design and still carries a real verifier
verdict, so it is the one honest way to hold ``cost_usd: null`` beside a
score. Read from the trial's own ``task.yaml``; a run cannot opt out of R5 by
configuration.
"""


class FidelityRule(str, Enum):
    """What each rule asserts about a finished bundle."""

    UNMEASURED_SCORE = "R1"
    """A grade that measured nothing, and does not declare itself
    harness-synthesised, still reported a score."""

    DENOMINATOR_CLOSURE = "R2"
    """``total_trials`` does not decompose into the measured trials plus the
    infrastructure aborts, or the abort keys are not the excluded reasons."""

    DISK_RECONCILIATION = "R3"
    """A per-task row counts trials that are not on disk."""

    MARKER_COHERENCE = "R4"
    """``synthesized_by_termination_reason`` and all-``None`` components
    disagree about whether anything was measured."""

    SPEND_SANITY = "R5"
    """A trial that recorded no spend carries a numeric score."""


@dataclass(frozen=True)
class FidelityViolation:
    """One failed assertion."""

    rule: FidelityRule
    where: str
    """Bundle-relative location — a filename, or ``trials/<task>/<trial>``."""
    detail: str

    def __str__(self) -> str:
        return f"{self.rule.value} {self.where}: {self.detail}"


def expected_infrastructure_abort_keys() -> frozenset[str]:
    """The ``infrastructure_aborts`` keys a current bundle must carry."""
    return frozenset(reason.value for reason in EXCLUDED_TYPED_REASONS)


def carries_measured_denominator(aggregate: Any) -> bool:
    """Whether *aggregate* is a bundle the denominator rules can read.

    ``measured_trials`` arrived with the measured/aborted split, so a bundle
    without it predates the contract R2 and R3 check and has no denominator to
    close. Every run the current orchestrator writes carries it, so scoping on
    it skips archived bundles without ever skipping a live run.
    """
    return isinstance(aggregate, dict) and "measured_trials" in aggregate


def check_run_bundle(run_dir: Path) -> list[FidelityViolation]:
    """Return every invariant *run_dir* violates, in rule order.

    An empty list means the bundle's numbers are all backed by something that
    happened. An absent ``aggregate.json`` is itself a violation — a gate that
    passes on a run that wrote no aggregate is a gate that passes on anything.
    The per-trial rules read grades directly, so they apply to every bundle,
    including ones too old for the denominator rules.
    """
    violations: list[FidelityViolation] = []
    aggregate = _read_json(run_dir / AGGREGATE_FILENAME)
    if aggregate is None:
        violations.append(
            FidelityViolation(
                FidelityRule.DENOMINATOR_CLOSURE,
                AGGREGATE_FILENAME,
                "absent or unreadable, so the run reports no denominator at all",
            )
        )
    elif carries_measured_denominator(aggregate):
        rows = _read_json(run_dir / PER_TASK_METRICS_FILENAME)
        violations.extend(_check_denominators(aggregate, rows))
        violations.extend(_check_disk_reconciliation(run_dir, rows))
    violations.extend(_check_trials(run_dir))
    return sorted(violations, key=lambda v: (v.rule.value, v.where))


# ---------------------------------------------------------------------------
# R2 — denominator closure
# ---------------------------------------------------------------------------


def _check_denominators(
    aggregate: dict[str, Any],
    rows: list[dict[str, Any]] | None,
) -> list[FidelityViolation]:
    """Every denominator closes, over the run and over each task.

    ``harness_errors`` and ``ungradeable`` overlap ``measured_trials`` by
    design — our own defects stay in the denominator — so the closure is
    ``total_trials == measured_trials + sum(infrastructure_aborts)``, the
    aborts being the only trials that sit outside it.
    """
    found: list[FidelityViolation] = []
    scopes: list[tuple[str, dict[str, Any]]] = [(AGGREGATE_FILENAME, aggregate)]
    for row in rows or []:
        task_id = row.get("task_id", "<unnamed>")
        scopes.append((f"{PER_TASK_METRICS_FILENAME}[{task_id}]", row))

    expected_keys = expected_infrastructure_abort_keys()
    for where, scope in scopes:
        aborts = scope.get("infrastructure_aborts")
        if not isinstance(aborts, dict):
            found.append(
                FidelityViolation(
                    FidelityRule.DENOMINATOR_CLOSURE,
                    where,
                    "infrastructure_aborts is absent, so an aborted trial has "
                    "nowhere to be counted",
                )
            )
            continue
        missing = expected_keys - set(aborts)
        if missing:
            found.append(
                FidelityViolation(
                    FidelityRule.DENOMINATOR_CLOSURE,
                    where,
                    "infrastructure_aborts cannot report "
                    f"{', '.join(sorted(missing))} — a trial excluded for that "
                    "reason is uncountable",
                )
            )
        total = scope.get("total_trials")
        measured = scope.get("measured_trials")
        if not isinstance(total, int) or not isinstance(measured, int):
            found.append(
                FidelityViolation(
                    FidelityRule.DENOMINATOR_CLOSURE,
                    where,
                    "total_trials / measured_trials are absent, so the "
                    "denominator cannot be checked",
                )
            )
            continue
        aborted = sum(v for v in aborts.values() if isinstance(v, int))
        if total != measured + aborted:
            found.append(
                FidelityViolation(
                    FidelityRule.DENOMINATOR_CLOSURE,
                    where,
                    f"total_trials {total} != measured_trials {measured} + "
                    f"infrastructure_aborts {aborted}",
                )
            )
        scored = scope.get("scored_trials")
        if isinstance(scored, int) and scored > measured:
            found.append(
                FidelityViolation(
                    FidelityRule.DENOMINATOR_CLOSURE,
                    where,
                    f"scored_trials {scored} exceeds measured_trials {measured}",
                )
            )
    return found


# ---------------------------------------------------------------------------
# R3 — disk reconciliation
# ---------------------------------------------------------------------------


def _check_disk_reconciliation(
    run_dir: Path,
    rows: list[dict[str, Any]] | None,
) -> list[FidelityViolation]:
    """Each per-task row counts exactly the trial directories that exist.

    A row claiming more trials than are on disk is counting an attempt that
    left no evidence — the shape an abandoned retry takes.
    """
    if rows is None:
        return [
            FidelityViolation(
                FidelityRule.DISK_RECONCILIATION,
                PER_TASK_METRICS_FILENAME,
                "absent or unreadable, so no per-task count can be reconciled",
            )
        ]
    found: list[FidelityViolation] = []
    for row in rows:
        task_id = row.get("task_id")
        claimed = row.get("total_trials")
        if not isinstance(task_id, str) or not isinstance(claimed, int):
            continue
        task_dir = run_dir / TRIALS_DIRNAME / task_id
        on_disk = (
            sum(1 for child in task_dir.iterdir() if child.is_dir()) if task_dir.is_dir() else 0
        )
        if claimed != on_disk:
            found.append(
                FidelityViolation(
                    FidelityRule.DISK_RECONCILIATION,
                    f"{PER_TASK_METRICS_FILENAME}[{task_id}]",
                    f"total_trials {claimed} but {on_disk} trial "
                    f"{'directory' if on_disk == 1 else 'directories'} on disk",
                )
            )
    return found


# ---------------------------------------------------------------------------
# R1, R4, R5 — per-trial rules
# ---------------------------------------------------------------------------


def _check_trials(run_dir: Path) -> list[FidelityViolation]:
    """Walk every trial directory once, applying the three per-trial rules."""
    trials_root = run_dir / TRIALS_DIRNAME
    if not trials_root.is_dir():
        return []
    found: list[FidelityViolation] = []
    for task_dir in sorted(p for p in trials_root.iterdir() if p.is_dir()):
        for trial_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            where = f"{TRIALS_DIRNAME}/{task_dir.name}/{trial_dir.name}"
            grade = _read_yaml(trial_dir / GRADE_FILENAME)
            if not isinstance(grade, dict):
                continue
            found.extend(_check_grade_shape(where, grade))
            found.extend(_check_spend(where, trial_dir, grade))
    return found


def _check_grade_shape(where: str, grade: dict[str, Any]) -> list[FidelityViolation]:
    """R1 and R4 — the two halves of "nothing was measured" must agree.

    ``components`` all ``None`` and ``synthesized_by_termination_reason`` are
    the codebase's two statements that no evaluator ran. R4 holds them to
    being the same statement. R1 then catches the grade that makes the first
    statement, omits the second, and reports a score anyway: it measured
    nothing, says so in its components, and still contributes a number.
    """
    components = grade.get("components")
    components = components if isinstance(components, dict) else {}
    nothing_measured = all(components.get(name) is None for name in _COMPONENT_FIELDS)
    synthesized = grade.get("synthesized_by_termination_reason")
    score = grade.get("score")

    found: list[FidelityViolation] = []
    if (synthesized is not None) != nothing_measured:
        found.append(
            FidelityViolation(
                FidelityRule.MARKER_COHERENCE,
                where,
                f"synthesized_by_termination_reason is {synthesized!r} but "
                f"{'no' if nothing_measured else 'a'} component carries a score",
            )
        )
    if nothing_measured and synthesized is None and isinstance(score, int | float):
        found.append(
            FidelityViolation(
                FidelityRule.UNMEASURED_SCORE,
                where,
                f"score {score} with every component None and no "
                "synthesized_by_termination_reason — the grade reports a number "
                "nothing produced",
            )
        )
    return found


def _check_spend(
    where: str,
    trial_dir: Path,
    grade: dict[str, Any],
) -> list[FidelityViolation]:
    """R5 — a trial that never reached the model has no score to report.

    "Never reached" is read as no spend *and* no tokens in either direction.
    Both halves are needed: a trial whose provider went unpriced still ran and
    still earns its score (``unpriced_trials`` counts those), and a trial that
    spent nothing on zero tokens did not. Neither half mentions grading, which
    is what makes this the widest net in the set — it holds whatever the
    adapter wrote into the grade.

    The one honest ``cost_usd: null`` beside a score is a declared oracle: it
    solves the task without a model and still earns a real verifier verdict.
    """
    score = grade.get("score")
    if not isinstance(score, int | float):
        return []
    metrics = _read_yaml(trial_dir / METRICS_FILENAME)
    if not isinstance(metrics, dict):
        return [
            FidelityViolation(
                FidelityRule.SPEND_SANITY,
                where,
                f"score {score} with no {METRICS_FILENAME}, so nothing "
                "corroborates that the trial ran",
            )
        ]
    if metrics.get("cost_usd") is not None or _billed_tokens(metrics):
        return []
    if _declared_harness(trial_dir) == _ORACLE_HARNESS:
        return []
    return [
        FidelityViolation(
            FidelityRule.SPEND_SANITY,
            where,
            f"score {score} with cost_usd null and no tokens in either "
            "direction — the trial reports a verdict on a model it never reached",
        )
    ]


def _billed_tokens(metrics: dict[str, Any]) -> bool:
    """Whether the trial's usage shows the model was reached at all."""
    usage = metrics.get("usage")
    if not isinstance(usage, dict):
        return False
    return any(
        isinstance(usage.get(field), int | float) and usage[field] > 0
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens")
    )


def _declared_harness(trial_dir: Path) -> str | None:
    """The harness the trial's own ``task.yaml`` says ran it, if it says."""
    task = _read_yaml(trial_dir / TASK_FILENAME)
    if not isinstance(task, dict):
        return None
    agent = ((task.get("model_config") or {}).get("agent")) or {}
    resolved = agent.get("resolved") or {}
    harness = resolved.get("harness") or agent.get("name")
    return harness if isinstance(harness, str) else None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
