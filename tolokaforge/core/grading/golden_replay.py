"""Whether the expected state a pack's hash compares against can be built at all.

Two shapes of it: the golden world a pack's actions describe, and the state its task
starts in, which a refusal task compares against directly.

Substrate-neutral by construction: stdlib-only pure functions over what a pack and its
task declare — the authored action names, the task-level facts the actions are replayed
against, and what an action's tool answered — so the core grading engine and the runner
refuse the same authored defect instead of each deciding for itself what an unbuildable
world means. Kept beside :mod:`state_checks`, which hashes the world once there is one.

The matcher is deliberately *not* here. Core resolves a name against the pack's
``TOOLS`` map exactly; the runner also accepts a single ``…_<name>`` suffix over the
tools it registered for the trial. A matcher living here would smuggle one substrate's
namespace into the other, and #815 owns unifying them.

:func:`declared_failure` is not that shape and belongs here: a name's namespace differs
*per substrate*, while the convention a tool signals failure in differs *per pack* and
reaches both substrates identically — a mapping for a pack whose tools return one, a JSON
string for a tau-style pack whose tools ``json.dumps`` their answer.
"""

from __future__ import annotations

import json
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

_A_SOURCE_NO_REPLAY_CAN_ITERATE = (
    "state_checks.hash.golden_actions is the list of actions a golden replay executes, got "
    "{kind} ({value!r}): neither substrate can replay that, so the trial is paid for and "
    "takes no state-hash verdict at all. Write each action as a '- name: <tool>' list "
    "entry, or drop the source"
)

_UNNAMEABLE_ACTIONS = (
    "golden actions declaring no tool for the replay to call: {offenders}. An action is a "
    "mapping carrying the name of the tool to call, so there is nothing here to resolve "
    "and the trial would be paid for and take no state-hash verdict at all."
)

_UNNAMEABLE_ACTION = "[{index}] {kind} ({value!r})"

_NO_TASK_DIR = "this grading engine was given no task directory"

_NO_INITIAL_STATE_DECLARED = "task.yaml declares no initial_state.json_db"

# How a task withholding the initial state reads to whoever holds the engine, per shape
# it withheld it in, and ``None`` for the shape that withholds nothing. Total over
# :class:`InitialStateSource`, so a fourth shape cannot join the enum without an answer
# here. The authoring gate carries its own sentences for the same two facts, addressed to
# the pack's author rather than to the engine's caller.
_ABSENT_INITIAL_STATE: Mapping[InitialStateSource, str | None] = {
    InitialStateSource.JSON_FILE: None,
    InitialStateSource.ABSENT: _NO_INITIAL_STATE_DECLARED,
    InitialStateSource.INLINE: (
        "task.yaml declares initial_state.json_db inline, where the replay loads a JSON "
        "file under the task directory"
    ),
}

_NO_MCP_SERVER = "task.yaml declares no tools.agent.mcp_server"

_NO_INITIAL_STATE_TO_COMPARE = (
    "state_checks.hash.expect_initial_state compares the trial against the state its task "
    "starts in, and {because}. So there is no expected state, no hash verdict and no grade — "
    "rather than a state_checks score the pack's other sources earned beside a hash nothing "
    "computed. Write initial_state.json_db in task.yaml, or drop the source"
)

_NO_TASK_DIR_TO_RESOLVE_IT_UNDER = (
    "task.yaml declares initial_state.json_db as the file {path!r}, which is resolved under "
    "the task directory, and " + _NO_TASK_DIR
)

_UNREADABLE_INITIAL_STATE = (
    "task.yaml declares initial_state.json_db as the file {path!r}, which holds no JSON "
    "object: {problem}"
)

_AN_EMPTY_JSON_OBJECT = "it holds an empty JSON object"

_INCOMPLETE_REPLAY = (
    "GOLDEN REPLAY ERRORS: {failed} of {authored} golden actions did not take effect, so the "
    "state the trial was hashed against is a partial golden world: {failures}"
)

_FAILED_ACTION = "[{index}] {name} {kind} {error}"


class GoldenReplayError(Exception):
    """The expected state a hash comparison needs could not be produced.

    A replay that could not be executed, or an initial state that could not be
    resolved: either way there is no expected state to compare against, so the trial
    has no state-hash verdict — not a failing one.
    """


class UnresolvableGoldenAction(GoldenReplayError):
    """A golden action naming a tool the replay cannot call, or naming nothing at all."""


class UnreplayableGoldenSource(GoldenReplayError):
    """The authored ``golden_actions`` is not the list of actions a replay executes.

    Distinct from an action the replay cannot resolve
    (:class:`UnresolvableGoldenAction`): that one is an action, while this is a source
    holding no actions at all, so there is no index to address.
    """


class UnresolvableInitialState(GoldenReplayError):
    """The state a task starts in is not there to be compared against.

    Distinct from a world a replay cannot be built in
    (:class:`UnbuildableGoldenReplayWorld`): that one needs a JSON *file* under the task
    directory and the tools to replay actions against it, while this one needs the state
    itself and admits either shape a task writes it in.
    """


class UnbuildableGoldenReplayWorld(GoldenReplayError):
    """The task facts a golden replay is executed against are not all declared.

    Distinct from a replay whose actions did not all take effect
    (:class:`GoldenReplayRecord`): that one built a partial world and still hashed against
    it, while this one has no world at all, so no action is ever attempted.
    """


def classify_initial_state(json_db: str | Mapping[str, Any] | None) -> InitialStateSource:
    """Which of the three shapes a task wrote its ``initial_state.json_db`` in."""
    if not json_db:
        return InitialStateSource.ABSENT
    if isinstance(json_db, str):
        return InitialStateSource.JSON_FILE
    return InitialStateSource.INLINE


def read_declared_initial_state(
    *, task_dir: Path | None, initial_state_json_db: str | Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The state a task declared it starts in, out of whichever shape it declared it in.

    Both shapes are admitted, unlike :func:`require_golden_replay_world` beside it: a
    replay needs a JSON *file* to load into the pack's own tools, while whoever reads
    the state needs the state itself, which an inline mapping supplies as well as a
    file does. A path is resolved under ``task_dir``, which is how an author writes one
    in ``task.yaml``.

    Neutral about what a missing state means, because that differs per reader: a task
    declaring none answers ``None`` rather than raising, and every refusal names
    ``initial_state.json_db``, the declared path and the problem without naming which
    source wanted the state. The empty state is a state here — the reading that refuses
    it belongs to whoever cannot use one, which is :func:`resolve_initial_state` above.

    Raises:
        UnresolvableInitialState: the task declares a file and there is no ``task_dir``
            to resolve it under, or the file cannot be read or holds no JSON object.
    """
    source = classify_initial_state(initial_state_json_db)
    if source is InitialStateSource.ABSENT:
        return None
    if source is InitialStateSource.INLINE:
        return dict(initial_state_json_db)  # type: ignore[arg-type]  # INLINE is a Mapping
    if task_dir is None:
        raise UnresolvableInitialState(
            _NO_TASK_DIR_TO_RESOLVE_IT_UNDER.format(path=initial_state_json_db)
        )
    return _loaded_initial_state(task_dir / initial_state_json_db, initial_state_json_db)


def resolve_initial_state(
    *, task_dir: Path | None, initial_state_json_db: str | Mapping[str, Any] | None
) -> dict[str, Any]:
    """The state a ``state_checks.hash`` comparison is computed against.

    :func:`read_declared_initial_state` under the sentence a hash source is refused
    with: this caller hashes what it gets back, so it admits neither an absent state nor
    an empty one, and every refusal is addressed to the author of the source that wanted
    it rather than to whoever else may hold the same task.

    Raises:
        UnresolvableInitialState: the task declares no initial state, or declares a file
            no reader can resolve or parse as a JSON object. Never the empty state,
            which hashes to a digest no trial can match and would read as an agent
            failure.
    """
    try:
        state = read_declared_initial_state(
            task_dir=task_dir, initial_state_json_db=initial_state_json_db
        )
    except UnresolvableInitialState as error:
        raise UnresolvableInitialState(_no_initial_state(str(error))) from error
    if state is None:
        raise UnresolvableInitialState(_no_initial_state(_NO_INITIAL_STATE_DECLARED))
    if not state:
        raise UnresolvableInitialState(
            _no_initial_state(
                _UNREADABLE_INITIAL_STATE.format(
                    path=initial_state_json_db, problem=_AN_EMPTY_JSON_OBJECT
                )
            )
        )
    return state


def _loaded_initial_state(path: Path, declared: Any) -> dict[str, Any]:
    """The JSON object at ``path``, or raise naming what ``declared`` resolved to."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise UnresolvableInitialState(
            _UNREADABLE_INITIAL_STATE.format(
                path=declared, problem=f"{type(error).__name__}: {error}"
            )
        ) from error
    if not isinstance(loaded, dict):
        raise UnresolvableInitialState(
            _UNREADABLE_INITIAL_STATE.format(
                path=declared, problem=f"it holds a {type(loaded).__name__}"
            )
        )
    return loaded


def _no_initial_state(because: str) -> str:
    """The one sentence every unresolvable initial state is refused with."""
    return _NO_INITIAL_STATE_TO_COMPARE.format(because=because)


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


class GoldenActionFailure(str, Enum):
    """How a golden action said it did not take effect, and the verb the sentence reads.

    The two are one fact — an authored action whose effect is missing from the world the
    trial is hashed against — differing only in how the tool's author chose to signal it,
    which is why both join one record. Naming the mechanism anyway is what tells the author
    where to look: a raise is a call the pack got wrong, a report is a call the pack got
    right about a state that refused it.
    """

    RAISED = "raised"
    """The call itself failed, so the tool's body never decided anything."""

    REPORTED = "reported"
    """The tool ran, declined the request, and said so in what it returned."""


def declared_failure(payload: object) -> str | None:
    """The failure a tool reported by its return value, or ``None``.

    A ``Mapping`` whose top-level ``"error"`` is truthy, or a ``str`` that JSON-decodes to
    one. Everything else is a success as far as this reads it: a non-``Mapping``, a ``str``
    that is no JSON, a JSON ``str`` decoding to something other than a ``Mapping``, and a
    ``Mapping`` carrying no ``"error"`` or a falsy one. Both payload shapes are reached in
    this repository — a pack whose tools return mappings hands over the mapping, a
    tau-style pack hands over the ``json.dumps`` of one — on either substrate.

    **Truthiness, not key presence.** ``{"error": null}`` — a plausible "no error" idiom
    — stays the success it reads as. The message a failure carries is its tool author's,
    so an empty one is read as no failure too: ``_tool_error_to_dict`` writes ``str(exc)``
    verbatim, and ``ToolError("")`` therefore arrives here as ``{"error": ""}``. That is a
    deliberate reading rather than an impossibility — a failure signalled with nothing said
    about it is indistinguishable here from one never signalled.

    The message is ``str()`` of the value, because a truthy non-string one
    (``{"error": {"code": 5}}``) is reachable from a pack out of this tree. A ``"details"``
    list beside it — the second key ``_tool_error_to_dict`` writes — is appended, so a
    field-level validation failure names its fields rather than only its summary.
    """
    decoded = _decoded_json(payload) if isinstance(payload, str) else payload
    if not isinstance(decoded, Mapping):
        return None
    error = decoded.get("error")
    if not error:
        return None
    details = decoded.get("details")
    if not isinstance(details, list) or not details:
        return str(error)
    return f"{error} ({'; '.join(str(detail) for detail in details)})"


def _decoded_json(payload: str) -> object:
    """What the string carries, or the string itself when it carries no JSON."""
    try:
        return json.loads(payload)
    except ValueError:
        return payload


@dataclass(frozen=True)
class FailedGoldenAction:
    """A golden action that resolved to a tool, ran, and did not take effect."""

    index: int
    name: str
    kind: GoldenActionFailure
    error: str

    @classmethod
    def from_exception(cls, index: int, name: str, error: BaseException) -> FailedGoldenAction:
        return cls(
            index=index,
            name=name,
            kind=GoldenActionFailure.RAISED,
            error=f"{type(error).__name__}: {error}",
        )

    @classmethod
    def from_reported_failure(cls, index: int, name: str, message: str) -> FailedGoldenAction:
        return cls(index=index, name=name, kind=GoldenActionFailure.REPORTED, error=message)

    @classmethod
    def from_substrate_failure(cls, index: int, name: str, message: str) -> FailedGoldenAction:
        """A call a substrate declared failed beside the prose it flattened the raise into.

        The raised kind, and the same one :meth:`from_exception` records: the flag is the
        substrate's rather than the tool's, so the tool's body never decided anything — a
        substrate that answers a call whose exception escaped with a failure flag beside
        text says exactly what an exception reaching the replay loop says. ``message`` is
        the substrate's own words, kept whole, so an author reads the layer that failed
        rather than a summary of it.
        """
        return cls(index=index, name=name, kind=GoldenActionFailure.RAISED, error=message)


@dataclass(frozen=True)
class GoldenReplayRecord:
    """How much of the authored golden path took effect, and what stopped the rest.

    Both kinds of failure share the one tuple, so the count covers an action that raised
    and one that ran and reported its failure by what it returned — what every tool built
    through ``create_server`` does with a ``ToolError``, since the registry converts the
    raise into a return payload.
    """

    authored: int
    failures: tuple[FailedGoldenAction, ...] = ()


def incomplete_replay_reason(record: GoldenReplayRecord) -> str | None:
    """The sentence both substrates put on a grade whose replay did not take full effect.

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
            _FAILED_ACTION.format(
                index=failure.index,
                name=failure.name,
                kind=failure.kind.value,
                error=failure.error,
            )
            for failure in record.failures
        ),
    )


def unreplayable_golden_source(golden_actions: object) -> str | None:
    """Why no substrate can replay this authored ``golden_actions``, or ``None``.

    Truth before shape, which is the reading every rule over this source already uses: a
    falsy value — ``[]``, ``{}``, ``""``, ``0``, ``false``, or the key written bare — is
    no replay rather than a malformed source, so every read site loads it as no actions
    to replay and the block is refused only where it declares no other source. What each
    substrate then *grades* for a replay of no actions is its own answer and still
    differs; no read of this source decides it.

    A truthy value that is not a list has no reading at all: core hands it to the replay
    loop and the runner iterates it onto the wire, so each fails on the authored value
    once the trial is paid for. One sentence for both, and for the authoring gate that
    reports the shape rather than raising on it, so an author meets the same fix wherever
    they first hear about it.
    """
    if not golden_actions or isinstance(golden_actions, list):
        return None
    return _A_SOURCE_NO_REPLAY_CAN_ITERATE.format(
        kind=type(golden_actions).__name__, value=golden_actions
    )


def refuse_unreplayable_golden_source(golden_actions: object, *, context: str) -> None:
    """Raise ``UnreplayableGoldenSource`` for a source no replay can iterate.

    ``context`` is the locus its caller can name: the grading file for a reader holding
    that path, the file's name for one holding nothing but the parsed block.

    Which shape that is belongs to :func:`unreplayable_golden_source`.
    """
    reason = unreplayable_golden_source(golden_actions)
    if reason is not None:
        raise UnreplayableGoldenSource(f"{context}: {reason}")


def require_replayable_golden_actions(
    golden_actions: object, *, context: str
) -> list[Mapping[str, Any]]:
    """Every authored action as the mapping a replay reads it as, or raise.

    The read for a caller with no resolution step between the authored value and a paid
    trial: an element that is no mapping is refused here, naming its index, where core
    tolerates it and lets :func:`resolve_golden_action_names` refuse it by the same class
    one step later. Tolerating it here instead — returning ``None`` for its name, as core
    does — would lower an action named ``""`` onto the wire, and that constructs cleanly.

    Only the element's *shape* is answered. A mapping declaring no usable name reaches the
    caller and is lowered the same way, which is #886 rather than this precondition.

    A falsy source is no actions to replay and loads as the empty list, the refusal above
    having answered every other non-list.

    Raises:
        UnreplayableGoldenSource: the source is not the list of actions to replay.
        UnresolvableGoldenAction: an element of it is no mapping at all, every offending
            index named in one raise.
    """
    refuse_unreplayable_golden_source(golden_actions, context=context)
    if not isinstance(golden_actions, list):
        return []
    offenders = [
        _UNNAMEABLE_ACTION.format(index=index, kind=type(action).__name__, value=action)
        for index, action in enumerate(golden_actions)
        if not isinstance(action, Mapping)
    ]
    if offenders:
        raise UnresolvableGoldenAction(
            f"{context}: {_UNNAMEABLE_ACTIONS.format(offenders='; '.join(offenders))}"
        )
    return golden_actions


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
