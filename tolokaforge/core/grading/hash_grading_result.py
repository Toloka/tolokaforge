"""What one state-hash comparison found: the result both grading substrates return.

The runner's ``RunnerServiceImpl._execute_hash_grading`` and core's
``StateChecker.check_hash`` / ``StateChecker.check_hash_against_golden_replay`` compare a
trial's state with the state it is graded against, and each returns a
:class:`HashGradingResult`. The verdict is the one bit ``hash_match``, and
``hash_score`` is derived from it: neither substrate can hand the fold a partial or a
contradictory hash score, and a result has no score to set. What a later change has a
comparison report is a field here, not one more element of a returned tuple.

The module imports nothing outside the standard library at runtime, so core's hash
checks return the type without loading the runner models or gRPC; the runner models'
types appear in its annotations only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tolokaforge.core.grading.golden_replay import GoldenReplayRecord
    from tolokaforge.runner.models import ComparisonViewGradeRecord

__all__ = ["HashComparisonBasis", "HashGradingResult"]


class HashComparisonBasis(str, Enum):
    """The state a hash comparison was run against, and what selected it.

    The two initial-state members grade identically by construction — the evaluator
    resets the trial's database and hashes it either way — and are separate members
    because the ledger accounts for a *declared* source and has nothing to file for a
    block that declared none. Collapsing them would leave ``expect_initial_state``
    accounted for without being read.
    """

    DECLARED_INITIAL_STATE = "declared_initial_state"
    """``expect_initial_state``: the author asked for the state the task starts in."""

    GOLDEN_REPLAY = "golden_replay"
    """``golden_actions``: the state replaying them from the initial state produces."""

    UNDECLARED_INITIAL_STATE = "undeclared_initial_state"
    """No source at all: the same initial state, reached by falling through."""


@dataclass(frozen=True, kw_only=True)
class HashGradingResult:
    """What a state-hash comparison found, as either grading substrate returns it.

    Frozen, and built by keyword only. ``hash_match`` must be a ``bool``: the type is
    the whole of what holds a hash verdict to ``0.0`` or ``1.0``, on both substrates.
    """

    hash_match: bool
    """Whether the trial's state hashes equal to the state it is graded against."""

    reason: str | None = None
    """Core's sentence for the verdict, the one ``GradingEngine`` reports.

    The runner composes its sentence from the grade's components at the fold
    (:func:`~tolokaforge.core.grading.composite_fold.build_grade_reasons`), so its
    results carry ``None``.
    """

    basis: HashComparisonBasis | None = None
    """Which state the verdict was reached against, and which declaration selected it.

    Carried out of the evaluator rather than re-derived from the config by whoever needs
    it: the runner's runtime ledger accounts for the source key this names, so a config
    read a second time at the accounting site would report a key as evaluated whether or
    not the evaluator ever looked at it. ``None`` for a comparison against a state or a
    digest the caller supplied without naming a source.
    """

    golden_replay: GoldenReplayRecord | None = None
    """How much of the golden path ran; ``None`` where the comparison replayed nothing.

    An unresolvable name never reaches the replay — it fails the whole grade — so every
    failure here describes an action that ran against a world it did not fit.
    """

    state_diff: dict[str, Any] | None = None
    """The diff reported beside a mismatch, as the JSON object the grade carries.

    The runner's is a dumped ``StateDiff`` of the stable states; core's golden replay
    reports the diff ``calculate_state_diff`` computes. ``None`` on a match, and where
    the comparison computes none.
    """

    comparison_view: ComparisonViewGradeRecord | None = None
    """Both views' records and, on a mismatch, the view diff; ``None`` without a view.

    With a view declared, ``hash_match`` compares the two views, and ``state_diff`` is
    the raw diff, kept for the author beside the view diff.
    """

    def __post_init__(self) -> None:
        if type(self.hash_match) is not bool:
            raise TypeError(
                f"hash_match is the hash verdict, a bool, not {self.hash_match!r}: the score "
                f"is derived from it and cannot be set"
            )

    @property
    def hash_score(self) -> float:
        """``1.0`` on a match and ``0.0`` otherwise: the score cannot disagree with the bit.

        Meaningful only when :attr:`hash_unscorable` is ``False``: a broken replay hashed
        the trial against a state no author asked for, so the runner reads
        :attr:`hash_unscorable` before writing this into its components — the write
        skipped, the ``hash_score`` component stays at the ``-1.0`` not-evaluated
        sentinel, and the fold refuses the trial rather than composing a fabricated
        verdict.
        """
        return 1.0 if self.hash_match else 0.0

    @property
    def hash_unscorable(self) -> bool:
        """Whether a golden replay left behind a world no author asked for.

        ``True`` when :attr:`golden_replay` recorded a per-action failure: one or more
        actions failed during the replay and left partial state behind, so a hash
        against it would grade the trial against a world no author asked for. The
        runner reads it before writing :attr:`hash_score` into its components; core's
        engine scores the verdict and names the incomplete replay in its reasons.
        """
        return self.golden_replay is not None and bool(self.golden_replay.failures)
