"""The author-facing ``state_checks.hash`` block, how its verdict folds with a
JSONPath score into one ``state_checks`` score, and which source combinations have
no fold at all.

Substrate-neutral by construction: the block is declared here once and the fold is
arithmetic over its members, so the core grading engine and the runner service
compose the component by the same rule instead of each carrying its own. The runner
declares the same members flattened onto its wire config and translates *into* this
block, which is what lets every shared rule be written against the author's own key
names. Kept out of ``state_checks`` because that module pulls ``importlib.util``,
``jsonpath_ng`` and the diff formatter, none of which the runner has any reason to
import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

MISSING_HASH_WEIGHT_MESSAGE = (
    "state_checks.hash.weight is required when a hash source and a non-empty "
    "state_checks.jsonpaths are both configured — there is no defensible default. "
    "Choose one: weight: 1.0 lets the hash decide, weight: 0.0 lets the jsonpaths "
    "decide, weight: 0.5 gives them equal shares."
)

CONFLICTING_STATE_SOURCES_MESSAGE = (
    "state_checks.db_probes is the sole state source for a task that declares it, and a "
    "non-empty state_checks.jsonpaths or a state_checks.hash block that is enabled with a "
    "source scores the same component — one of the two verdicts would be discarded and "
    "nothing says which. Choose one: keep db_probes and drop the other source, or drop "
    "db_probes and let the hash and jsonpaths grade the state."
)

INERT_HASH_WEIGHT_REASON = (
    "state_checks.hash.weight was declared but not consulted: the weight applies "
    "only when a hash verdict and a non-empty state_checks.jsonpaths are both scored"
)

CONFLICTING_HASH_SOURCES_MESSAGE = (
    "state_checks.hash.{first} and state_checks.hash.{second} each name an expected final "
    "state: the comparison has two candidates and nothing says which it runs against. "
    "Declare one — expect_initial_state for a refusal task whose expected final state is "
    "the initial state, golden_actions for a task that changes state."
)

WEIGHT_DOMAIN_MESSAGE = "state_checks.hash.weight must be a real number within [0.0, 1.0]"

AUTHORED_HASH_WEIGHT_CONTEXT = "grading.yaml state_checks.hash.weight"
"""Where an author reads a rejected weight from, named once for every rule that reports one."""

EXPECT_INITIAL_STATE_KEY = "expect_initial_state"
"""The source naming the state the trial started in, rather than one the trial reaches."""

RETIRED_HASH_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "expected_state_hash": (
            "state_checks.hash.expected_state_hash has been retired — a stored hash is "
            "written in one substrate's hash algebra and the other cannot compare against "
            "it, so a literal that grades 1.0 on the core engine grades 0.0 through the "
            "runner (#915). Declare the source instead: `golden_actions: [...]` for a task "
            "that changes state, or `expect_initial_state: true` for a refusal task whose "
            "expected final state is the initial state."
        )
    }
)
"""The ``state_checks.hash`` keys that are neither declared fields nor unknown keys, and
what each tells an author to write instead.

Read by :class:`StateHashConfig`'s own before-validator, by the authoring gate's
unknown-key refusal and by :func:`refuse_retired_hash_keys`, so what a block may carry
and what a pack read refuses it for cannot disagree.
"""

HASH_SOURCE_KEYS: tuple[str, ...] = (
    "golden_actions",
    EXPECT_INITIAL_STATE_KEY,
)
"""The ``state_checks.hash`` members that give the hash something to compare against.

Author-facing key names, so a substrate flattening the ``hash`` block onto its own
fields translates *into* these rather than restating which keys count as a source.
"""


def refuse_retired_hash_keys(hash_block: Any, *, context: str) -> None:
    """Raise the migration a populated retired ``state_checks.hash`` key draws.

    Every read a pack passes through calls this — the authoring gate, and both of
    :class:`~tolokaforge.adapters.native.NativeAdapter`'s grading reads — because they
    share a file and not an object: ``tolokaforge run-trial`` runs no grading pre-flight,
    so the description build is the only read a trial there reaches.

    A block :data:`RETIRED_HASH_KEYS` names *inertly* is left to
    :class:`StateHashConfig`, which drops it: an author who can act meets the raise, and a
    recorded bundle serialized against the old schema still loads.

    Args:
        hash_block: The authored ``state_checks.hash`` block. Anything that is not a
            mapping declares no key to refuse and is the shape validation's business.
        context: Where the author reads the offending block from, prefixed to the
            migration.

    Raises:
        ValueError: If *hash_block* declares a retired key with a value.
    """
    if not isinstance(hash_block, Mapping):
        return
    for key, migration in RETIRED_HASH_KEYS.items():
        if hash_block.get(key):
            raise ValueError(f"{context}: {migration}")


def validate_hash_weight(value: object, *, context: str) -> float:
    """Return ``value`` as a weight, or raise ``ValueError`` naming ``context``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{context}: {WEIGHT_DOMAIN_MESSAGE}, got {value!r} of type {type(value).__name__}"
        )
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{context}: {WEIGHT_DOMAIN_MESSAGE}, got {value!r}")
    return float(value)


class StateHashConfig(BaseModel):
    """The ``state_checks.hash`` block an author writes, and the runner translates into.

    ``extra="forbid"`` because a key this block does not declare requests *nothing*:
    a misspelled ``enabled`` or ``expect_initial_state`` leaves the hash unscored while
    the trial grades on whatever else the pack declared, which is a better grade than
    the same block spelled correctly. The keys in :data:`RETIRED_HASH_KEYS` are the one
    exception — :meth:`_drop_retired_hash_keys` removes them, so the extra check never
    sees them.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    expect_initial_state: bool = False
    # Unclaimed on both axes, each for its own reason. The element shape, because two
    # live here — the author's ``{name, kwargs}`` mappings and the ``GoldenAction``
    # instances the runner hands the same field — and unifying them is #907. The
    # container shape, because ``unreplayable_golden_source`` owns it: a truthy
    # non-list draws a refusal naming the key, the received type and the fix, and the
    # falsy spellings read as no source at all. A ``list`` annotation would answer
    # both first, in one line that names none of it.
    golden_actions: Any = Field(default_factory=list)
    weight: float | None = None
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_hash_keys(cls, data: Any) -> Any:
        """Remove every :data:`RETIRED_HASH_KEYS` member, populated or not.

        A populated one is dropped as well as an inert one, which is where this parts
        company with the ``state_checks`` retirement beside it: the substrate that graded
        every recorded bundle carrying a hash literal never read it, so dropping it
        changes nothing those bundles replay, and refusing it here would strand them.
        The author who can act meets :func:`refuse_retired_hash_keys` at every read a
        pack passes through.

        Returning the filtered mapping rather than the original is what lets a bundle
        past ``extra="forbid"``, which reads whatever this hands back.
        """
        if not isinstance(data, dict):
            return data
        return {key: value for key, value in data.items() if key not in RETIRED_HASH_KEYS}

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight_domain(cls, value: object) -> float | None:
        """Reject what the fold rejects, before Pydantic coerces it into range.

        ``mode="before"`` is load-bearing: lax coercion turns ``true`` into ``1.0``
        and ``"0.6"`` into ``0.6``, so any validator running after it — or a
        declarative ``ge``/``le`` bound — sees a clean float and can no longer tell
        that the author wrote a bool or a string. ``weight: true`` would then
        silently mean "the hash decides outright".

        ``None`` passes through rather than being validated: it is the shape of a
        block declaring no weight, and Pydantic runs before-validators for an
        explicitly written ``weight: null``.
        """
        if value is None:
            return None
        return validate_hash_weight(value, context=AUTHORED_HASH_WEIGHT_CONTEXT)

    @model_validator(mode="after")
    def _refuse_a_second_expected_state(self) -> StateHashConfig:
        """Reject a block declaring more than one hash source.

        The sources name different expected states and the block declares no
        precedence between them, so the trial would be compared against whichever one
        the substrate reading it happens to consult first. Refused here rather than at
        each reader, so every path that constructs the block — the core config, the
        authoring gate, both adapter reads, and the runner's translation of its own
        flattened fields — refuses the same pack.

        Pairwise over :data:`HASH_SOURCE_KEYS` rather than anchored on one member of it,
        so a source added to that tuple is excluded against every other one by this rule
        instead of by a second one nobody remembers to write.
        """
        declared = [key for key in HASH_SOURCE_KEYS if getattr(self, key)]
        if len(declared) < 2:
            return self
        raise ValueError(
            CONFLICTING_HASH_SOURCES_MESSAGE.format(first=declared[0], second=declared[1])
        )


def hash_block_is_a_state_source(hash_config: StateHashConfig | None) -> bool:
    """Whether the block gives the ``state_checks`` component a hash verdict to score.

    Hash grading on *and* one of :data:`HASH_SOURCE_KEYS` declared to compare against.
    Every member is read for truth rather than presence, as the neighbouring rules read
    theirs: an empty ``golden_actions`` replays nothing, and an enabled block with
    nothing to compare against is the authoring gate's shape at the hash key.
    """
    if hash_config is None:
        return False
    return bool(hash_config.enabled) and any(getattr(hash_config, key) for key in HASH_SOURCE_KEYS)


def resolve_hash_weight(
    hash_config: StateHashConfig | None,
    *,
    jsonpaths: Sequence[Any],
    context: str,
) -> float | None:
    """Return the author's ``state_checks.hash.weight``, or ``None`` if none is declared.

    Raises ``ValueError`` carrying :data:`MISSING_HASH_WEIGHT_MESSAGE` for the one
    shape whose component score is undecidable: hash grading on, a hash source to
    grade against, and assertions to weigh the verdict against, with no weight.
    Every other shape yields at most one score, so a weight there divides nothing —
    it is range-checked and returned for reporting rather than rejected, which is
    what keeps a recorded bundle's weight beside an empty assertion list loadable.

    Range-checked here as well as by the block's own validator because the block is
    mutable: a caller that reached past load and wrote a weight the author could not
    have declared is refused at the read that would have folded by it.
    """
    declared = None if hash_config is None else hash_config.weight
    weight = None if declared is None else validate_hash_weight(declared, context=context)
    undecidable = weight is None and hash_block_is_a_state_source(hash_config) and bool(jsonpaths)
    if undecidable:
        raise ValueError(MISSING_HASH_WEIGHT_MESSAGE)
    return weight


def probes_conflict_with_another_state_source(
    *,
    db_probes: Sequence[Any],
    jsonpaths: Sequence[Any],
    hash_is_a_state_source: bool,
) -> bool:
    """Whether ``db_probes`` is declared beside a source that scores the same component.

    "A source that also scores" is a non-empty ``jsonpaths``, or a hash block that is a
    source by :func:`hash_block_is_a_state_source`. An empty ``jsonpaths`` asserts
    nothing, so it discards nothing here.

    Takes the hash *fact* rather than the block because its two callers hold two shapes
    of it: :func:`refuse_probes_beside_another_state_source` a constructed
    :class:`StateHashConfig`, and the authoring gate a fragment it never constructed one
    from. Which shape carries two verdicts is stated here either way, and the authoring
    gate reports it where the two config models raise on it.
    """
    if not db_probes:
        return False
    return bool(jsonpaths or hash_is_a_state_source)


def refuse_probes_beside_another_state_source(
    *,
    db_probes: Sequence[Any],
    jsonpaths: Sequence[Any],
    hash_config: StateHashConfig | None,
    context: str,
) -> None:
    """Raise ``ValueError`` for ``db_probes`` declared beside a source that also scores.

    Only the runner evaluates a probe, so the two substrates would not even discard the
    same verdict: the runner keeps the probe's and hides the rest, core folds the hash
    with the assertions and never sees the probe. One trial, two ``state_checks``
    components, and no declared share to fold them by — so the pair is refused instead.

    Which shape that is belongs to :func:`probes_conflict_with_another_state_source`.
    """
    if probes_conflict_with_another_state_source(
        db_probes=db_probes,
        jsonpaths=jsonpaths,
        hash_is_a_state_source=hash_block_is_a_state_source(hash_config),
    ):
        raise ValueError(f"{context}: {CONFLICTING_STATE_SOURCES_MESSAGE}")


def compose_state_checks_score(
    *,
    hash_score: float | None,
    jsonpath_score: float | None,
    hash_weight: float | None,
) -> float | None:
    """Fold the two state-check sources into one component score.

    ``None`` means *not evaluated*, on input and on output: a source that was
    not configured contributes nothing rather than a vacuous ``1.0`` or a
    failing-looking ``0.0``. A single evaluated source is therefore passed
    through untouched and ``hash_weight`` is never consulted — which is what
    keeps a tau-style pack (hash on, no jsonpaths) scoring its hash verdict at
    every weight. The weight is consulted only when both sources are real, and
    then it is mandatory: every candidate default silently discards one of them.
    """
    if hash_score is None and jsonpath_score is None:
        return None
    if hash_score is None:
        return jsonpath_score
    if jsonpath_score is None:
        return hash_score
    if hash_weight is None:
        raise ValueError(MISSING_HASH_WEIGHT_MESSAGE)
    return jsonpath_score * (1.0 - hash_weight) + hash_score * hash_weight


def inert_hash_weight_reason(
    *,
    hash_score: float | None,
    jsonpath_score: float | None,
    hash_weight: float | None,
) -> str | None:
    """Report a declared weight that :func:`compose_state_checks_score` never read.

    Takes the composer's own arguments so the condition reported to the author
    cannot drift from the short circuit that actually skipped the weight.
    """
    if hash_weight is None:
        return None
    if hash_score is not None and jsonpath_score is not None:
        return None
    return INERT_HASH_WEIGHT_REASON
