"""What a pack declares it is migrating out of its rubric, and what that is refused for.

A task directory may carry a ``migration.yaml`` beside its ``grading.yaml``, naming each
rubric criterion its trace constraints are a candidate for, have narrowed, or have
replaced. Nothing about grading reads the file: it records the author's claim so
``tolokaforge reconcile`` can check that claim against recorded judge verdicts, and so the
hazards a migration carries are refused at authoring time rather than discovered in a
report.

A sidecar rather than a ``grading.yaml`` key because :class:`GradingConfig` is
``extra="ignore"`` — a ``migration:`` key there is silently dropped — and making it a real
key needs a ``GRADING_KEYS`` manifest entry, whose every ``KeyKind`` describes score
production, which this metadata must never do.

The hazards, each measured:

* **A lost veto.** A ``required: true`` criterion is a trial-level veto carrying no score
  share, so retiring one moves the judge score not at all; only the veto goes. Such a
  criterion may therefore only be claimed by **shared** constraints carrying
  ``severity: gate`` — a route-scoped gate is escapable inside ``alternatives``, and a
  scored constraint is a fraction of a component where a veto was. That rule reads the
  author's ``was.required``, so a ``narrowed`` entry's ``was`` is cross-checked against the
  criterion the pack still holds: unchecked, declaring ``required: false`` escapes the rule
  outright.
* **A freed score share.** A *scored* criterion's weight sits in the judge component's
  denominator, so retiring one the agent failed makes the judge more generous: dropping
  ``cache_debug``'s ``explains_mechanism`` (weight 1.0 of a 1.5 denominator) raises the
  component by ``+0.667`` on a trial that scored it ``0.0``. Such an entry must therefore
  declare ``combine_weights``, the post-migration map the freed share lands in —
  unconditionally, since an author who shifts nothing declares the **identity map**, and an
  explicit identity map is read in the diff where an implied claim is invisible.
* **A corpus nobody can read.** Every entry names the committed corpus its claim is measured
  over, and the name is checked against the disk: an unchecked pointer rots silently, and a
  command handed some other corpus reports a clean verdict over it.
* **A claim no corpus can decide.** ``by`` is a conjunction over constraints recomputed per
  trial, and a trial is scored on the route it took, so ids from two different ``alternatives``
  routes are never both decided on one trial. Such an entry reaches no observation on any
  corpus, where the point of declaring one is to be measured.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from tolokaforge.core.grading.corpus_curation import (
    CORPUS_MANIFEST_FILENAME,
    is_corpus_directory,
)
from tolokaforge.core.models import (
    Criterion,
    Rubric,
    TraceChecksConfig,
    TraceConstraintSeverity,
)

MIGRATION_FILENAME = "migration.yaml"
"""The sidecar's name, beside the ``grading.yaml`` whose rubric it migrates."""


class MigrationMode(str, Enum):
    """How far a pack has taken one criterion's migration."""

    CANDIDATE = "candidate"
    NARROWED = "narrowed"
    RETIRED = "retired"


class ResidualKind(str, Enum):
    """Whether anything of the criterion still reaches the judge."""

    NONE = "none"
    TEXT = "text"


def _rejecting_blank(value: str, *, what: str, write: str) -> str:
    """``value`` unchanged, or ``ValueError`` naming what to write instead."""
    if value.strip():
        return value
    raise ValueError(f"{what} is empty, so it records nothing a reviewer can check. Write {write}")


def _text(value: str) -> str:
    """Criterion text with its wrapping normalised away.

    A YAML ``>`` block folds newlines into spaces and appends one of its own, so whether
    two authors wrote the same criterion is a question about the words, not about where
    each one wrapped or which block indicator they chose.
    """
    return " ".join(value.split())


class MigrationResidual(BaseModel):
    """What the judge still reads of a migrated criterion, or the claim that nothing does.

    A model rather than a scalar because no one string carries both. Measured with
    ``yaml.safe_load``: ``residual: none`` parses to the non-empty string ``'none'`` and
    ``residual: null`` to ``None``, so a single field answers the wrong question for one
    of the two modes whichever way an author spells it.
    """

    kind: ResidualKind
    reason: str

    model_config = {"extra": "forbid"}

    @field_validator("reason")
    @classmethod
    def _require_a_claim(cls, value: str) -> str:
        return _rejecting_blank(
            value,
            what="residual.reason",
            write=(
                "why nothing of the criterion remains (kind: none) or what the judge still "
                "reads of it (kind: text)"
            ),
        )


class MigratedCriterion(BaseModel):
    """The criterion shape the migration was declared against.

    Recorded rather than read from the pack because a ``retired`` criterion is gone from
    it: this is the only surviving record of what the evidence was gathered against, and
    both the veto rule and the freed-share rule read ``required`` off it.
    """

    description: str
    kind: Literal["binary", "graded"] = "binary"
    required: bool = False
    weight: float = 1.0

    model_config = {"extra": "forbid"}

    @field_validator("description")
    @classmethod
    def _require_the_text_measured(cls, value: str) -> str:
        return _rejecting_blank(
            value,
            what="was.description",
            write="the criterion text the evidence was gathered against",
        )


class MigrationEvidence(BaseModel):
    """What the recorded verdicts behind a decision measured.

    Which corpus they were read from is the entry's ``corpus``, in every mode: evidence is
    the measurement a *decision* rests on, and a candidate that has decided nothing is still
    measured over a corpus.

    ``kappa`` is nullable and required: Cohen's κ is undefined on a corpus with no label
    variation, so an unwritten field would read as that measurement rather than as an
    unwritten one.
    """

    observations: int
    kappa: float | None

    model_config = {"extra": "forbid"}


class MigrationAcknowledgement(BaseModel):
    """One disagreement waived by naming the trial it happened on and why."""

    trial: str
    reason: str

    model_config = {"extra": "forbid"}

    @field_validator("reason")
    @classmethod
    def _require_a_claim(cls, value: str) -> str:
        return _rejecting_blank(
            value,
            what="acknowledged.reason",
            write="why the judge's verdict on that trial is the one to discount",
        )


_RESIDUAL_BY_MODE: Mapping[MigrationMode, ResidualKind | None] = MappingProxyType(
    {
        MigrationMode.CANDIDATE: None,
        MigrationMode.NARROWED: ResidualKind.TEXT,
        MigrationMode.RETIRED: ResidualKind.NONE,
    }
)
"""Total over the modes, so a new one has to say what it claims rather than accept any."""

_RESIDUAL_ON_A_CANDIDATE = (
    "mode: candidate declares a residual. A residual is the author's judgment about a "
    "migration that happened — what the judge still reads of the criterion, or that nothing "
    "does — and a candidate has migrated nothing, so it parks a claim no evidence ever "
    "checks and blurs the one signal separating an intention from a decision. Drop the "
    "residual block, or declare the mode the measurement supports"
)

_WRONG_RESIDUAL_KIND: Mapping[MigrationMode, str] = MappingProxyType(
    {
        MigrationMode.RETIRED: (
            "mode: retired carries residual.kind: text, which claims part of the criterion "
            "survives it — a criterion nothing reads has no surviving part. Write "
            "residual.kind: none with the reason nothing remains, or declare mode: narrowed"
        ),
        MigrationMode.NARROWED: (
            "mode: narrowed carries residual.kind: none, which claims nothing of the "
            "criterion remains — then nothing was narrowed. Write residual.kind: text "
            "naming what the judge still reads, or declare mode: retired"
        ),
    }
)


class MigrationEntry(BaseModel):
    """One rubric criterion a pack's trace constraints are a candidate for, or have taken.

    ``was`` is the pre-migration criterion, ``by`` the constraint ids that claim it,
    ``corpus`` the committed corpus whose recorded verdicts the claim is measured over, and
    ``combine_weights`` the post-migration ``combine.weights`` map the freed score share
    lands in.
    """

    criterion: str
    mode: MigrationMode
    by: list[str] = Field(min_length=1)
    corpus: str
    was: MigratedCriterion
    residual: MigrationResidual | None = None
    combine_weights: dict[str, float] | None = None
    evidence: MigrationEvidence | None = None
    acknowledged: list[MigrationAcknowledgement] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("corpus")
    @classmethod
    def _require_a_path_the_base_decides(cls, value: str) -> str:
        """``corpus`` is read against a base the caller supplies, and an absolute value wins
        that join outright — resolving to itself whatever base was passed, while the refusal
        for a corpus that does not resolve still names the base it claims to have used."""
        if not Path(value).is_absolute():
            return value
        raise ValueError(
            f"corpus: {value} is an absolute path, and the value is read against a base the "
            "command supplies — the repository root for a shipped pack, the declaration's own "
            "directory for one that travels. An absolute value ignores that base, so the "
            "corpus a run reads stops being a property of the declaration. Write the path "
            "relative to the base the corpus is committed under"
        )

    @model_validator(mode="after")
    def _require_the_residual_its_mode_claims(self) -> MigrationEntry:
        """Zero disagreements satisfies ``retired``'s evidence condition and ``narrowed``'s
        alike, so which one a pack means is the author's judgment rather than the corpus's,
        and it is recorded here — in both directions, because a mode paired with the other
        mode's residual claims two contradictory things at once. A ``candidate`` carries
        none, which makes ``residual``'s presence and kind a total function of ``mode``: a
        reader can tell what an entry claims from its mode alone.
        """
        expected = _RESIDUAL_BY_MODE[self.mode]
        if expected is None:
            if self.residual is None:
                return self
            raise ValueError(_RESIDUAL_ON_A_CANDIDATE)
        if self.residual is None:
            raise ValueError(
                f"mode: {self.mode.value} declares no residual. The evidence cannot choose "
                "between narrowing a criterion and retiring it — zero disagreements "
                "satisfies both — so the choice is the author's, recorded and reviewed "
                f"here: write residual: {{ kind: {expected.value}, reason: <why> }}"
            )
        if self.residual.kind is expected:
            return self
        raise ValueError(_WRONG_RESIDUAL_KIND[self.mode])

    @model_validator(mode="after")
    def _require_the_evidence_its_mode_rests_on(self) -> MigrationEntry:
        """A candidate is measured against nothing yet; a narrow or a retirement is nothing
        but the measurement."""
        if self.mode is MigrationMode.CANDIDATE:
            if self.evidence is None:
                return self
            raise ValueError(
                "mode: candidate declares evidence. Evidence is the measurement a decision "
                "rests on, and a candidate names a criterion a constraint could replace "
                "without having decided anything — the corpus it is measured over is the "
                "entry's own corpus field. Drop the evidence block, or declare the mode the "
                "measurement supports"
            )
        if self.evidence is not None:
            return self
        raise ValueError(
            f"mode: {self.mode.value} declares no evidence, so nothing records what the "
            "migration measured over the corpus it names. Write evidence: { observations: "
            "<n>, kappa: <value or null> }, or declare mode: candidate"
        )

    @model_validator(mode="after")
    def _require_somewhere_for_a_freed_score_share_to_go(self) -> MigrationEntry:
        """A scored criterion's weight is in the judge component's denominator, so dropping
        one the agent failed makes the judge more generous — measured at ``+0.667`` on
        ``cache_debug``'s ``explains_mechanism``, whose trial scored it ``0.0``.

        The declaration is what removes the hazard and it is required unconditionally; an
        author who shifts nothing declares the identity map. It is a claim rather than a
        proof — a map absorbing nothing loads — and ``tolokaforge reconcile`` reports per
        trial what the declared map does.
        """
        if self.was.required or self.mode is MigrationMode.CANDIDATE:
            return self
        if self.combine_weights is not None:
            return self
        raise ValueError(
            f"mode: {self.mode.value} on a criterion that is not required, with no "
            "combine_weights. A scored criterion's weight is part of the judge component's "
            "denominator, so removing one the agent scored badly raises the component — "
            "measured at +0.667 on cache_debug's explains_mechanism, on a trial that "
            "scored it 0.0. Declare combine_weights, the post-migration combine.weights "
            "map the freed share lands in; an author who shifts nothing declares the "
            "identity map, which a reviewer reads in the diff where an implied claim "
            "would be invisible"
        )

    @model_validator(mode="after")
    def _require_every_acknowledgement_to_address_the_corpus(self) -> MigrationEntry:
        """A waiver names the trial it waives, and a trial outside the corpus the verdict
        was measured over waives no disagreement that verdict saw.

        Every mode, including ``candidate``: a candidate's verdict gates nothing, so a waiver
        on one changes no outcome and records the author's judgment about a disagreement the
        report prints — worth keeping checkable rather than unstatable.
        """
        if not self.acknowledged:
            return self
        corpus = Path(self.corpus)
        stray = [
            waiver.trial
            for waiver in self.acknowledged
            if not Path(waiver.trial).is_relative_to(corpus) or Path(waiver.trial) == corpus
        ]
        if not stray:
            return self
        raise ValueError(
            f"acknowledged names {stray}, which are not bundles under the corpus this entry "
            f"names ({self.corpus}). A waiver addresses a disagreement the verdict measured, "
            "so its trial is a bundle in the corpus that verdict read. Write the trial's path "
            "as corpus is written, from the same base"
        )


class MigrationDeclaration(BaseModel):
    """A task directory's ``migration.yaml``: what its rubric is losing, and to what."""

    migrations: list[MigrationEntry] = Field(min_length=1)

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class _DeclaredConstraint:
    """Where one trace constraint sits in the block, and whether it gates."""

    route: str | None
    severity: TraceConstraintSeverity

    def preserves_a_veto(self) -> bool:
        """Whether this constraint fails the trial whichever route the agent took."""
        return self.route is None and self.severity is TraceConstraintSeverity.GATE


def corpus_base_for(grading_path: Path, *, corpus_base: Path | None) -> Path:
    """The directory an entry's ``corpus`` is read against.

    The base the caller supplied, or the declaration's own directory where it supplied none,
    which is what makes a corpus travel with an external pack. One expression, because a
    reader resolving the pointer differently from the load-time check would read a corpus
    the loader never approved.
    """
    return grading_path.parent if corpus_base is None else corpus_base


def inspect_migration_declaration(
    grading_path: Path, *, corpus_base: Path | None = None
) -> MigrationDeclaration | None:
    """The migration sidecar beside ``grading_path``, checked against the pack it names.

    Reached by ``tolokaforge validate``, and deliberately not by the pre-run gate: the
    file cannot affect a grade, so a run must not abort on authoring metadata.

    Args:
        grading_path: The task's ``grading.yaml`` — the file whose rubric is migrated, and
            whose ``trace_checks`` block the declaration's ``by`` ids address.
        corpus_base: The directory each entry's ``corpus`` is read against. Defaults to the
            declaration's own directory, which is what makes a corpus travel with an
            external pack; a caller holding repository-root knowledge — the CLI — passes it
            explicitly. This module reads no ambient state, so a run from the wrong
            directory is a refusal naming the base rather than a missing corpus.

    Returns:
        The declaration, or ``None`` when the directory carries no sidecar: a pack that
        declares no migration is unchanged.

    Raises:
        ValueError: If the declaration is invalid, names a criterion or a constraint the
            pack does not have as declared, or names a corpus that does not resolve.
        RuntimeError: If either file is not a YAML mapping.
        pydantic.ValidationError: If an entry's own shape is invalid.
    """
    sidecar = grading_path.parent / MIGRATION_FILENAME
    if not sidecar.exists():
        return None

    declaration = MigrationDeclaration(
        **_mapping(
            sidecar,
            what="Migration file",
            remedy=(
                "An entry written directly under the top level makes the file a list. "
                "Write 'migrations:' then each entry indented beneath it"
            ),
        )
    )

    if not grading_path.exists():
        raise ValueError(
            f"Migration file {sidecar} declares a migration of the rubric in {grading_path}, "
            "which does not exist, so no criterion and no constraint it names resolves. "
            "Point the task's grading field at the file the rubric lives in"
        )
    grading_data = _mapping(
        grading_path,
        what="Grading file",
        remedy=(
            f"The migration declared in {sidecar} is checked against this file's rubric and "
            "trace_checks, so write both keys at its top level"
        ),
    )

    criteria = _rubric_criteria(grading_data)
    constraints = _declared_constraints(grading_data)
    base = corpus_base_for(grading_path, corpus_base=corpus_base)
    rejected = [
        message
        for entry in declaration.migrations
        for message in _entry_rejections(
            entry, criteria=criteria, constraints=constraints, corpus_base=base
        )
    ]
    if rejected:
        written = "\n".join(f"  - {message}" for message in rejected)
        raise ValueError(f"Migration file {sidecar} cannot be honoured as written:\n{written}")
    return declaration


def _mapping(path: Path, *, what: str, remedy: str) -> dict[str, Any]:
    with open(path) as handle:
        data = yaml.safe_load(handle) or {}
    if isinstance(data, dict):
        return data
    raise RuntimeError(f"{what} {path} is not a YAML mapping (got {type(data).__name__}). {remedy}")


def _rubric_criteria(grading_data: Mapping[str, Any]) -> dict[str, Criterion]:
    """The pack's rubric criteria by id — empty when it declares no judge or no rubric."""
    judge = grading_data.get("llm_judge")
    rubric = judge.get("rubric") if isinstance(judge, Mapping) else None
    if not isinstance(rubric, Mapping):
        return {}
    return {criterion.id: criterion for criterion in Rubric(**rubric).criteria}


def _declared_constraints(grading_data: Mapping[str, Any]) -> dict[str, _DeclaredConstraint]:
    """Every trace constraint the pack declares, by id, with its route and severity.

    One flat map because the block's ids are one space — a route id and a constraint id
    cannot collide — and because whether an id is route-scoped is what the veto rule asks.
    """
    block = grading_data.get("trace_checks")
    if not isinstance(block, Mapping):
        return {}
    config = TraceChecksConfig(**block)
    declared = {
        item.id: _DeclaredConstraint(route=None, severity=item.severity)
        for item in config.constraints
    }
    for path in config.alternatives or ():
        declared.update(
            {
                item.id: _DeclaredConstraint(route=path.id, severity=item.severity)
                for item in path.constraints
            }
        )
    return declared


def _entry_rejections(
    entry: MigrationEntry,
    *,
    criteria: Mapping[str, Criterion],
    constraints: Mapping[str, _DeclaredConstraint],
    corpus_base: Path,
) -> list[str]:
    """Everything one entry is refused for — what the pack contradicts about it and what the
    corpus it names does not resolve to — because an author wants the list."""
    return [
        f"{entry.criterion}: {message}"
        for message in (
            _criterion_presence_rejection(entry, criteria),
            _declared_shape_rejection(entry, criteria),
            _narrowed_conversion_rejection(entry, criteria),
            _unknown_constraint_rejection(entry, constraints),
            _route_span_rejection(entry, constraints),
            _veto_rejection(entry, constraints),
            _unresolved_corpus_rejection(entry, corpus_base),
        )
        if message is not None
    ]


def _unresolved_corpus_rejection(entry: MigrationEntry, corpus_base: Path) -> str | None:
    """The corpus an entry is measured over has to be one somebody can read.

    A pointer nothing checks is a pointer that rots: the shipped value went to a directory
    that never existed and every command reported a clean verdict over the corpus it was
    handed instead.
    """
    resolved = corpus_base / entry.corpus
    if is_corpus_directory(resolved):
        return None
    return (
        f"corpus: {entry.corpus} resolves to {resolved}, which is not a corpus directory. A "
        f"corpus holds the {CORPUS_MANIFEST_FILENAME} `tolokaforge curate` writes, or its "
        "immediate subdirectories all do — one level, because a multi-part corpus is a "
        f"directory whose subdirectories are corpora. The path is read against {corpus_base}, "
        "so a value written from the repository root needs the command run from there"
    )


def _criterion_presence_rejection(
    entry: MigrationEntry, criteria: Mapping[str, Criterion]
) -> str | None:
    present = entry.criterion in criteria
    if entry.mode is MigrationMode.RETIRED:
        if not present:
            return None
        return (
            "mode: retired, but the criterion is still declared in llm_judge.rubric."
            "criteria, so the judge still scores what the constraints are said to have "
            "replaced. Remove it from the rubric, or declare mode: narrowed with the text "
            "that remains"
        )
    if present:
        return None
    return (
        f"names no criterion in the pack's rubric (declared: {sorted(criteria) or 'none'}). "
        "A criterion the constraints have replaced is declared mode: retired"
    )


def _declared_shape_rejection(
    entry: MigrationEntry, criteria: Mapping[str, Criterion]
) -> str | None:
    """What ``was`` claims against what the rubric declares, where both exist."""
    current = criteria.get(entry.criterion)
    if current is None:
        return None
    if entry.mode is MigrationMode.CANDIDATE:
        return _stale_declared_shape(entry.was, current)
    if entry.mode is MigrationMode.NARROWED:
        return _unnarrowed_description(entry.was, current)
    return None


EVERY_DECLARED_FIELD: tuple[str, ...] = tuple(MigratedCriterion.model_fields)
"""Every field ``was`` claims, derived from the model that declares them.

Written out, this was a second list to keep in step with :class:`MigratedCriterion`, and the
failure mode of it falling behind is a field silently dropping out of both tiers that compare
``was`` — the load-time cross-check and the recorded-rubric verification — leaving the author's
claim about it unchecked. Derived, a new field is compared the moment it exists.
"""

_WHAT_A_NARROW_LEAVES_ALONE = ("kind", "required")


def _declared_shape(criterion: MigratedCriterion | Criterion) -> dict[str, Any]:
    """One criterion's shape, its description reduced to the words it is made of.

    Read off :data:`EVERY_DECLARED_FIELD` by name, so a field added to ``was`` that the
    criterion being compared does not carry raises :class:`AttributeError` here rather than
    dropping out of the comparison. That is the intent: the two shapes are comparable only
    while both sides declare the same fields, and a rubric criterion that stopped carrying
    one is a change no silent skip should absorb.
    """
    shape: dict[str, Any] = {name: getattr(criterion, name) for name in EVERY_DECLARED_FIELD}
    shape["description"] = _text(shape["description"])
    return shape


def criterion_shape_disagreement(
    was: MigratedCriterion, current: MigratedCriterion | Criterion, *, over: tuple[str, ...]
) -> str | None:
    """Where ``was`` and another reading of the criterion disagree across ``over``.

    Public because ``was`` is checked against two different sources — the criterion the pack
    still holds, here, and the rubric each corpus bundle recorded, in
    :mod:`~tolokaforge.core.grading.rubric_migration` — and a second comparison written out
    there would let the two tiers drift on what a criterion's shape is.
    """
    declared = _declared_shape(was)
    actual = _declared_shape(current)
    differing = [name for name in over if declared[name] != actual[name]]
    if not differing:
        return None
    return "; ".join(
        f"{name}: declared {declared[name]!r}, the rubric has {actual[name]!r}"
        for name in differing
    )


def _stale_declared_shape(was: MigratedCriterion, current: Criterion) -> str | None:
    written = criterion_shape_disagreement(was, current, over=EVERY_DECLARED_FIELD)
    if written is None:
        return None
    return (
        f"was does not match the criterion the rubric declares ({written}). A candidacy is "
        "against the criterion as it stands, so a stale 'was' means whatever evidence is "
        "later quoted was gathered against other text. Copy the criterion's current shape"
    )


def _narrowed_conversion_rejection(
    entry: MigrationEntry, criteria: Mapping[str, Criterion]
) -> str | None:
    """What a ``narrowed`` entry may not change about the criterion it narrows.

    A narrow leaves the criterion in the pack, so the pack is a load-time source for what
    ``was`` claims — and ``was`` is what every other rule here reads. ``weight`` is left out
    of the comparison deliberately: ``was`` describes the pre-migration state while the pack
    holds the post-migration one, and a criterion that now asks less may legitimately weigh
    less, so requiring a match would refuse a correct migration while adding nothing against
    the escape, which turns entirely on ``required``.
    """
    if entry.mode is not MigrationMode.NARROWED:
        return None
    current = criteria.get(entry.criterion)
    if current is None:
        return None
    written = criterion_shape_disagreement(entry.was, current, over=_WHAT_A_NARROW_LEAVES_ALONE)
    if written is None:
        return None
    return (
        f"mode: narrowed, but was contradicts the criterion the pack still holds ({written}). "
        "A narrow reduces the criterion's text and changes nothing else about it, while every "
        "other rule here reads 'was': the veto rule keys on was.required, so a narrow "
        "declaring a requiredness the rubric does not have escapes it altogether, and a "
        "flipped kind makes the recorded evidence incomparable with what the judge scores "
        "after the migration. Copy kind and required from the rubric and let a change to "
        "either be its own migration — was.weight is free, so a criterion that now asks less "
        "may weigh less"
    )


def _unnarrowed_description(was: MigratedCriterion, current: Criterion) -> str | None:
    if _text(was.description) != _text(current.description):
        return None
    return (
        "mode: narrowed, but was.description is the text the rubric still declares, so "
        "nothing was narrowed. Shorten the criterion in the rubric and leave the "
        "pre-migration text in was.description, or declare mode: candidate"
    )


def _unknown_constraint_rejection(
    entry: MigrationEntry, constraints: Mapping[str, _DeclaredConstraint]
) -> str | None:
    unknown = [name for name in entry.by if name not in constraints]
    if not unknown:
        return None
    return (
        f"'by' names {unknown}, which the pack's trace_checks block does not declare "
        f"(declared: {sorted(constraints) or 'none'}). A migration is by the checks that "
        "replace the criterion, so every id resolves in the same pack"
    )


def _route_span_rejection(
    entry: MigrationEntry, constraints: Mapping[str, _DeclaredConstraint]
) -> str | None:
    """The route-scoped ids one entry is ``by`` must all sit in the same route.

    A trial is scored on the route it took, so the verdicts a reconciliation recomputes carry
    the shared constraints and the winning route's alone. ``by`` is a conjunction, so an entry
    naming ids from two routes has no verdict for one of them on every trial and reaches no
    observation on any corpus. Shared ids are decided whichever route won, so they accompany
    any route's.
    """
    scoped = {
        name: constraints[name].route
        for name in entry.by
        if name in constraints and constraints[name].route is not None
    }
    if len(set(scoped.values())) < 2:
        return None
    written = ", ".join(f"{name} in route {route!r}" for name, route in scoped.items())
    return (
        f"'by' names constraints from more than one route ({written}). A trial is scored on "
        "the route it took, so a reconciliation recomputes the shared constraints and the "
        "winning route's alone — and 'by' is a conjunction, so an entry spanning two routes "
        "has no verdict for one of its ids on every trial and can never reach an observation. "
        "Name one route's constraints, with any shared ones beside them"
    )


def _veto_rejection(
    entry: MigrationEntry, constraints: Mapping[str, _DeclaredConstraint]
) -> str | None:
    """A veto may only be replaced by something that vetoes on every route.

    Scoped to the modes that replace it: a ``candidate`` entry replaces nothing and the
    criterion keeps its veto whatever the named constraints are — which is what both
    shipped candidacies are, each naming scored constraints.
    """
    if entry.mode is MigrationMode.CANDIDATE or not entry.was.required:
        return None
    weaker = {
        name: constraints[name]
        for name in entry.by
        if name in constraints and not constraints[name].preserves_a_veto()
    }
    if not weaker:
        return None
    written = "; ".join(f"{name} {_weakness(declared)}" for name, declared in weaker.items())
    return (
        f"was.required is true — a trial-level veto carrying no score share — and {written}. "
        "Retiring a veto moves the judge score not at all; only the veto goes. A constraint "
        "replacing one must therefore hold whichever route the agent took and must fail the "
        "trial: declare it in the shared 'constraints:' with severity: gate"
    )


def _weakness(declared: _DeclaredConstraint) -> str:
    if declared.route is not None:
        return (
            f"is declared inside route {declared.route!r}, which a trajectory taking another "
            "route escapes"
        )
    return "is scored, so it is a fraction of a component where a veto was"
