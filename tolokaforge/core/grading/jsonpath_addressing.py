"""What a ``state_checks`` assertion addresses, read from the assertion itself.

Both substrates answer this question, and the two grading points that act on the
answer — the authoring gate and ``GradeTrial`` — must reach the same one, so it is
computed here once from the author's own text rather than inferred at each read.
"""

from collections.abc import Mapping
from enum import Enum
from typing import Any

from jsonpath_ng import Child, Fields, JSONPath, Root
from jsonpath_ng.exceptions import JSONPathError
from jsonpath_ng.ext import parse

_FILESYSTEM_ROOT = "filesystem"


class JsonPathTarget(str, Enum):
    """The state a ``state_checks.jsonpaths[*].path`` expression addresses."""

    DATABASE = "database"
    """The trial's database — the ``db`` and ``tables`` roots the runner composes."""

    FILESYSTEM = "filesystem"
    """The agent-visible filesystem, which ``path_glob:`` addresses on both substrates."""


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


def jsonpath_target(path: str) -> JsonPathTarget:
    """Which state ``path`` addresses.

    ``filesystem`` is the only root the runner's JSONPath state cannot carry — that
    state is composed from the trial's database alone — so the rule reads the first
    segment below the root and every other segment, including a wildcard or a
    descendant selector naming no root at all, addresses the database.

    Raises:
        ValueError: ``path`` is not a JSONPath expression.
    """
    try:
        expression = parse(path)
    except JSONPathError as exc:
        raise ValueError(f"Not a JSONPath expression: {path!r} ({exc})") from exc
    segment = _first_segment_below_root(expression)
    if isinstance(segment, Fields) and _FILESYSTEM_ROOT in segment.fields:
        return JsonPathTarget.FILESYSTEM
    return JsonPathTarget.DATABASE


def addresses_the_filesystem(assertion: Mapping[str, Any]) -> bool:
    """Whether one ``jsonpaths`` assertion provably reads the agent-visible filesystem.

    Only an expression that parses can be shown to address the filesystem, so anything
    unreadable as JSONPath — a malformed expression, a ``path`` that is not text at all
    — answers ``False`` and stays in the database-reading population.
    """
    path = assertion.get("path")
    if not isinstance(path, str):
        return False
    try:
        return jsonpath_target(path) is JsonPathTarget.FILESYSTEM
    except ValueError:
        return False


def addresses_the_database(assertion: Mapping[str, Any]) -> bool:
    """Whether one ``jsonpaths`` assertion reads the trial's database.

    Every assertion writing a ``path`` at all does, unless that path is provably rooted
    at the filesystem. Reading it the other way round — classifying whatever cannot be
    parsed as addressing nothing — would drop those assertions out of the state fetch
    and grade them against a state never read, replacing the evaluators' own
    per-assertion diagnosis with ``DB state unavailable``.
    """
    return assertion.get("path") is not None and not addresses_the_filesystem(assertion)


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
