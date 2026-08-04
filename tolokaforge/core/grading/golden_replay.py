"""Whether the golden world a pack describes can be built at all.

Substrate-neutral by construction: stdlib-only pure functions over authored action
names, so the core grading engine and the runner refuse the same authored defect
instead of each deciding for itself what a name resolving to nothing means. Kept
beside :mod:`state_checks`, which hashes the world once there is one.

The matcher is deliberately *not* here. Core resolves a name against the pack's
``TOOLS`` map exactly; the runner also accepts a single ``…_<name>`` suffix over the
tools it registered for the trial. A matcher living here would smuggle one substrate's
namespace into the other, and #815 owns unifying them.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

_UNRESOLVABLE_ACTIONS = (
    "golden actions naming no tool the replay can call: {offenders}. Skipping them would "
    "build a partial golden world and hash the trial against it, so there is no expected "
    "state and no verdict. Names resolve against {candidates}."
)

_OFFENDING_ACTION = "[{index}] {name!r}"

_INCOMPLETE_REPLAY = (
    "GOLDEN REPLAY ERRORS: {failed} of {authored} golden actions did not run, so the state "
    "the trial was hashed against is a partial golden world: {failures}"
)

_FAILED_ACTION = "[{index}] {name} raised {error}"


class GoldenReplayError(Exception):
    """A golden-action replay could not be executed.

    There is no expected state to compare against, so the trial has no
    state-hash verdict — not a failing one.
    """


class UnresolvableGoldenAction(GoldenReplayError):
    """A golden action naming a tool the replay cannot call, or naming nothing at all."""


@dataclass(frozen=True)
class FailedGoldenAction:
    """A golden action that resolved to a tool, ran, and raised."""

    index: int
    name: str
    error: str

    @classmethod
    def from_exception(cls, index: int, name: str, error: BaseException) -> FailedGoldenAction:
        return cls(index=index, name=name, error=f"{type(error).__name__}: {error}")


@dataclass(frozen=True)
class GoldenReplayRecord:
    """How much of the authored golden path ran, and what stopped the rest.

    Only an action that *raised* is a failure here. One reporting its failure by
    returning ``{"error": …}`` — what every tool built through ``create_server`` does —
    returns normally and is indistinguishable from success to either substrate, so it
    leaves no failure and no reason (#831).
    """

    authored: int
    failures: tuple[FailedGoldenAction, ...] = ()


def incomplete_replay_reason(record: GoldenReplayRecord) -> str | None:
    """The sentence both substrates put on a grade whose replay skipped actions.

    ``None`` for a replay that ran whole: the expected state is then the world the pack
    describes and there is nothing to say about it. Otherwise the verdict beside this
    sentence was computed against a state no author asked for, which is why the count
    and every offending action are named rather than "the replay had errors".

    The ``GOLDEN REPLAY ERRORS:`` prefix is matched by a downstream consumer that
    classifies trials by it (#599), so it is a contract rather than a phrasing choice.
    """
    if not record.failures:
        return None
    return _INCOMPLETE_REPLAY.format(
        failed=len(record.failures),
        authored=record.authored,
        failures="; ".join(
            _FAILED_ACTION.format(index=failure.index, name=failure.name, error=failure.error)
            for failure in record.failures
        ),
    )


def resolve_golden_action_names(
    names: Sequence[str | None],
    *,
    candidates: Collection[str],
    match: Callable[[str, Collection[str]], str | None],
) -> list[str]:
    """The candidate each authored golden-action name resolves to, in order, or raise.

    Every offending action is named in one raise rather than only the first one found,
    because an author correcting a golden path one exception at a time pays for a whole
    replay per typo.

    A falsy name resolves to nothing without consulting ``match``, so an action with no
    ``name`` key, one written ``name: ""``, and one written ``name: null`` all draw the
    same error as a name that simply does not exist — the four shapes are equally
    unreplayable and the author's fix is the same.
    """
    resolved: list[str] = []
    offenders: list[str] = []
    for index, name in enumerate(names):
        matched = match(name, candidates) if name else None
        if matched is None:
            offenders.append(_OFFENDING_ACTION.format(index=index, name=name))
            continue
        resolved.append(matched)

    if offenders:
        raise UnresolvableGoldenAction(
            _UNRESOLVABLE_ACTIONS.format(
                offenders="; ".join(offenders), candidates=sorted(candidates)
            )
        )
    return resolved
