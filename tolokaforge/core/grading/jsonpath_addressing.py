"""What a ``state_checks`` assertion addresses, read from the assertion itself.

Both substrates answer this question, and the two grading points that act on the
answer — the authoring gate and ``GradeTrial`` — must reach the same one, so it is
computed here once from the author's own text rather than inferred at each read.
"""

from collections.abc import Mapping
from enum import Enum
from functools import lru_cache
from typing import Any

from jsonpath_ng import Child, Fields, JSONPath, Root
from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.ext import parse

_FILESYSTEM_ROOT = "filesystem"

# The roots the core engine composes that the runner does not. Core builds ``agent``,
# ``user``, ``db`` and ``filesystem``, plus ``mock_web_url`` and ``rag_corpus_dir``
# where the task configures them (``core/env_state.py``); the runner builds ``db`` and
# ``tables`` alone. These are therefore the whole set of first segments the two
# substrates disagree about — a segment naming anything else resolves against nothing
# on both, which is a failure rather than a divergence.
_ROOTS_ONLY_THE_CORE_ENGINE_COMPOSES = frozenset(
    {"agent", "user", "mock_web_url", "rag_corpus_dir", _FILESYSTEM_ROOT}
)


class JsonPathTarget(str, Enum):
    """The state a ``state_checks.jsonpaths[*].path`` expression addresses.

    Named for what the *runner* can resolve, because that is the narrower substrate:
    the core engine composes ``agent``, ``user``, ``db`` and ``filesystem``, so a path
    the runner cannot reach is one the two substrates score differently.
    """

    TRIAL_DATABASE = "trial_database"
    """State both substrates resolve the same way, which the runner reads from the DB."""

    FILESYSTEM = "filesystem"
    """The agent-visible filesystem, which ``path_glob:`` addresses on both substrates."""

    BEYOND_THE_RUNNERS_STATE = "beyond_the_runners_state"
    """``agent``, ``user``, ``mock_web_url`` or ``rag_corpus_dir`` — composed by the core
    engine, absent from the runner's, and addressable by rooting the path at ``db``."""


def _first_segment_below_root(expression: JSONPath) -> JSONPath | None:
    """The leftmost segment ``expression`` names directly under ``$``.

    ``None`` where it names none: ``$`` alone, and a descendant selector, whose match
    set is confined to no single root.
    """
    node = expression
    parent: JSONPath | None = None
    while (left := getattr(node, "left", None)) is not None:
        parent, node = node, left
    if not isinstance(node, Root):
        return node
    return parent.right if isinstance(parent, Child) else None


@lru_cache(maxsize=512)
def jsonpath_target(path: str) -> JsonPathTarget:
    """Which state ``path`` addresses.

    The rule reads the first segment below the root against
    :data:`_ROOTS_ONLY_THE_CORE_ENGINE_COMPOSES`, the roots the two substrates disagree
    about. Everything else is read as the trial's database: a segment naming no single
    root (a bare ``$``, a descendant selector), a selector that is no field at all, and
    a name neither substrate composes, which resolves against nothing on both and so
    diverges on neither. That is the fail-safe direction — it keeps each evaluator's own
    per-assertion diagnosis.

    Raises:
        ValueError: ``path`` is not a JSONPath expression.
    """
    try:
        expression = parse(path)
    except JSONPathError as exc:
        raise ValueError(f"Not a JSONPath expression: {path!r} ({exc})") from exc
    segment = _first_segment_below_root(expression)
    if not isinstance(segment, Fields):
        return JsonPathTarget.TRIAL_DATABASE
    if _FILESYSTEM_ROOT in segment.fields:
        return JsonPathTarget.FILESYSTEM
    if _ROOTS_ONLY_THE_CORE_ENGINE_COMPOSES.intersection(segment.fields):
        return JsonPathTarget.BEYOND_THE_RUNNERS_STATE
    return JsonPathTarget.TRIAL_DATABASE


def unreachable_target(assertion: Mapping[str, Any]) -> JsonPathTarget | None:
    """Which state beyond the runner's this assertion addresses, where it addresses one.

    ``None`` for an assertion the runner can resolve, one writing no ``path`` at all,
    and one whose expression cannot be read as JSONPath — a malformed expression, or a
    ``path`` that is not text. Only an expression that parses and rooting at a segment
    the runner does not compose can be *shown* to be unreachable; the runner composes
    both the trial's database and the agent-visible filesystem (see
    :meth:`~tolokaforge.runner.service.RunnerServiceImpl._read_agent_visible_filesystem`),
    so a ``$.filesystem[…]``-rooted path grades on both substrates. The evaluators
    name an unreadable path per assertion.
    """
    path = assertion.get("path")
    if not isinstance(path, str):
        return None
    try:
        target = jsonpath_target(path)
    except ValueError:
        return None
    if target is JsonPathTarget.BEYOND_THE_RUNNERS_STATE:
        return target
    return None


def addresses_the_database(assertion: Mapping[str, Any]) -> bool:
    """Whether one ``jsonpaths`` assertion reads the trial's database.

    A path is proven to read the database only when its JSONPath expression parses
    and roots at a database segment. A ``$.filesystem[…]``-rooted path reads the
    agent-visible filesystem — not the DB — so it stays out of the DB-fetch
    population. Unparseable / non-string paths preserve the pre-existing route-to-DB
    behaviour so the evaluator's per-assertion diagnosis reaches the author instead
    of a ``DB state unavailable`` blanket message.
    """
    path = assertion.get("path")
    if path is None:
        return False
    if not isinstance(path, str):
        return True
    try:
        target = jsonpath_target(path)
    except ValueError:
        return True
    return target is JsonPathTarget.TRIAL_DATABASE


def block_addresses_the_database(state_checks: Mapping[str, Any]) -> bool:
    """Whether grading this ``state_checks`` block reads the trial's database.

    Keys are the author's, which is the vocabulary every rule shared between the two
    substrates reads.

    An enabled ``hash`` block counts whether or not it declares a source:
    ``_execute_hash_grading`` fetches the trial's stable hash before it consults
    ``expect_initial_state`` or ``golden_actions``, and a block declaring neither is a
    supported shape that compares against
    :attr:`~tolokaforge.runner.models.HashComparisonBasis.UNDECLARED_INITIAL_STATE`.

    ``db_probes`` never counts: a probe carries its own ``dsn`` and is evaluated
    against the postgres its task declares, never against the trial's DB service.

    A probe's ``expect[*].path`` is not this module's to classify, and no caller passes
    one. Those expectations address the probe's own query result — ``{rows, row_count}``
    — so they are rooted where the trial's JSONPath state carries nothing, and reading
    them here would refuse packs that grade correctly.
    """
    hash_block = state_checks.get("hash")
    if not isinstance(hash_block, Mapping):
        hash_block = {}
    if hash_block.get("enabled"):
        return True
    return any(
        addresses_the_database(assertion)
        for assertion in state_checks.get("jsonpaths") or ()
        if isinstance(assertion, Mapping)
    )
