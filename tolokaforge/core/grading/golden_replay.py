"""Whether the golden world a pack describes can be built at all.

Substrate-neutral by construction: stdlib-only pure functions over what a pack and its
task declare — the authored action names, and the task-level facts the actions are
replayed against — so the core grading engine and the runner refuse the same authored
defect instead of each deciding for itself what an unbuildable world means. Kept beside
:mod:`state_checks`, which hashes the world once there is one.

The matcher is deliberately *not* here. Core resolves a name against the pack's
``TOOLS`` map exactly; the runner also accepts a single ``…_<name>`` suffix over the
tools it registered for the trial. A matcher living here would smuggle one substrate's
namespace into the other, and #815 owns unifying them.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class InitialStateSource(str, Enum):
    """What a task's ``initial_state.json_db`` gives a golden replay to load.

    One vocabulary for both readers of the fact: the engine, which builds a world out of
    it or refuses to, and the authoring gate, which refuses the pack before a trial pays
    for either.
    """

    JSON_FILE = "json_file"
    """A path the replay resolves under the task directory and reads."""

    INLINE = "inline"
    """A mapping written into ``task.yaml``, which is no file for the replay to load."""

    ABSENT = "absent"
    """Nothing at all."""


_UNRESOLVABLE_ACTIONS = (
    "golden actions naming no tool the replay can call: {offenders}. Skipping them would "
    "build a partial golden world and hash the trial against it, so there is no expected "
    "state and no verdict. Names resolve against {candidates}."
)

_OFFENDING_ACTION = "[{index}] {name!r}"

_UNBUILDABLE_WORLD = (
    "state_checks.hash.golden_actions has to be replayed to know the expected state, and "
    "there is no world to replay them against: {absent}. So there is no expected state, no "
    "hash verdict and no grade — rather than a state_checks score the pack's other sources "
    "earned while the hash they are weighed against went uncomputed."
)

_NO_TASK_DIR = "this grading engine was given no task directory"

# How a task withholding the initial state reads to whoever holds the engine, per shape
# it withheld it in, and ``None`` for the shape that withholds nothing. Total over
# :class:`InitialStateSource`, so a fourth shape cannot join the enum without an answer
# here. The authoring gate carries its own sentences for the same two facts, addressed to
# the pack's author rather than to the engine's caller.
_ABSENT_INITIAL_STATE: Mapping[InitialStateSource, str | None] = {
    InitialStateSource.JSON_FILE: None,
    InitialStateSource.ABSENT: "task.yaml declares no initial_state.json_db",
    InitialStateSource.INLINE: (
        "task.yaml declares initial_state.json_db inline, where the replay loads a JSON "
        "file under the task directory"
    ),
}

_NO_MCP_SERVER = "task.yaml declares no tools.agent.mcp_server"

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


class UnbuildableGoldenReplayWorld(GoldenReplayError):
    """The task facts a golden replay is executed against are not all declared.

    Distinct from a replay that ran and skipped actions (:class:`GoldenReplayRecord`):
    that one built a partial world and still hashed against it, while this one has no
    world at all, so no action is ever attempted.
    """


def classify_initial_state(json_db: str | Mapping[str, Any] | None) -> InitialStateSource:
    """Which of the three shapes a task wrote its ``initial_state.json_db`` in."""
    if not json_db:
        return InitialStateSource.ABSENT
    if isinstance(json_db, str):
        return InitialStateSource.JSON_FILE
    return InitialStateSource.INLINE


@dataclass(frozen=True)
class GoldenReplayWorld:
    """The task facts a golden-action replay is executed against.

    The two paths are relative to ``task_dir``, which is how an author writes them in
    ``task.yaml`` and how the replay resolves them.
    """

    task_dir: Path
    initial_state_path: str
    mcp_server_path: str


def require_golden_replay_world(
    *,
    task_dir: Path | None,
    initial_state_json_db: str | dict[str, Any] | None,
    mcp_server: str | None,
) -> GoldenReplayWorld:
    """The world the authored golden actions are replayed against, or raise.

    Every absent fact is named in one raise rather than only the first one found, for
    the reason :func:`resolve_golden_action_names` names every offending action: an
    author correcting a pack one exception at a time pays for a whole grading pass per
    omission. Each is named by the ``task.yaml`` key that supplies it, except the task
    directory, which is the caller's to pass and no author's to write.

    An ``initial_state.json_db`` written as an inline mapping is absent rather than
    present — the replay loads a file under ``task_dir``, so a mapping there is a world
    it cannot build.
    """
    if (
        task_dir is not None
        and isinstance(initial_state_json_db, str)
        and initial_state_json_db
        and mcp_server
    ):
        return GoldenReplayWorld(
            task_dir=task_dir,
            initial_state_path=initial_state_json_db,
            mcp_server_path=mcp_server,
        )
    absent = _absent_replay_facts(task_dir, initial_state_json_db, mcp_server)
    raise UnbuildableGoldenReplayWorld(_UNBUILDABLE_WORLD.format(absent="; ".join(absent)))


def _absent_replay_facts(
    task_dir: Path | None,
    initial_state_json_db: str | dict[str, Any] | None,
    mcp_server: str | None,
) -> Iterator[str]:
    """Each fact the replay was not given, in the order the one raise names them."""
    if task_dir is None:
        yield _NO_TASK_DIR
    withheld_state = _ABSENT_INITIAL_STATE[classify_initial_state(initial_state_json_db)]
    if withheld_state is not None:
        yield withheld_state
    if not mcp_server:
        yield _NO_MCP_SERVER


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
