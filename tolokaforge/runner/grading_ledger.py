"""Runtime accounting for the scored grading keys a runner grading config carries.

``GradeTrial`` records, at every point an evaluator is invoked or deliberately
skipped, which author-facing ``grading.yaml`` key that call accounts for.
:func:`audit_accounted_keys` then subtracts those records from the scored keys the
request's config actually populated. A non-empty remainder is a key that would
silently score nothing in production, so the RPC fails naming it instead of
returning a grade computed without it.

Scope is :attr:`~tolokaforge.core.grading.key_manifest.KeyKind.SCORED_CHECK` by
construction. ``CONFIG_INPUT`` keys (``id_fields``, ``relaxed_validation``,
``numeric_string_fields``) shape how another check behaves and ``AGGREGATION``
keys are the combine itself, so neither is ever evaluated in the component phase
and neither belongs in the ledger.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.grading.key_manifest import EVALUATED as EVALUATED
from tolokaforge.core.grading.key_manifest import (
    GRADING_KEYS,
    RUNNER_HASH_EVALUATOR,
    TRACE_ALTERNATIVES_KEY,
    TRACE_CONSTRAINT_KEY_BY_KIND,
    TRACE_CONSTRAINTS_KEY,
    GradingKey,
    KeyKind,
    SubstrateCoverage,
    checked_author_key,
    family_author_keys,
    scored_keys_claiming_runner,
)
from tolokaforge.core.grading.key_manifest import (
    NO_TIMELINE_EVENTS_SKIP as NO_TIMELINE_EVENTS_SKIP,
)
from tolokaforge.core.grading.key_manifest import (
    UNBOUND_BINDING_SKIP as UNBOUND_BINDING_SKIP,
)
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    KeyAccounting,
    KeyAccountingRecord,
    RunnerGradingConfig,
    RunnerStateChecksConfig,
    TraceChecksConfig,
    TranscriptRulesConfig,
)

HASH_DISABLED_SKIP = KeyAccountingRecord(
    outcome=KeyAccounting.SKIPPED, detail="hash grading not enabled"
)
NO_JUDGE_MESSAGES_SKIP = KeyAccountingRecord(
    outcome=KeyAccounting.SKIPPED, detail="no transcript messages"
)
CORE_ONLY_HASH_SKIP = KeyAccountingRecord(
    outcome=KeyAccounting.SKIPPED, detail="core-only — no runner path reads it (#693)"
)
CUSTOM_CHECKS_DISABLED_SKIP = KeyAccountingRecord(
    outcome=KeyAccounting.SKIPPED, detail="custom checks not enabled"
)


MUST_CONTAIN_KEY = checked_author_key("transcript_rules.must_contain")
DISALLOW_REGEX_KEY = checked_author_key("transcript_rules.disallow_regex")
MAX_TURNS_KEY = checked_author_key("transcript_rules.max_turns")
MIN_ASSISTANT_TURNS_KEY = checked_author_key("transcript_rules.min_assistant_turns")
TOOL_EXPECTATIONS_KEY = checked_author_key("transcript_rules.tool_expectations")
REQUIRED_ACTIONS_KEY = checked_author_key("transcript_rules.required_actions")
COMMUNICATE_INFO_KEY = checked_author_key("transcript_rules.communicate_info")
JSONPATHS_KEY = checked_author_key("state_checks.jsonpaths")
DB_PROBES_KEY = checked_author_key("state_checks.db_probes")
LLM_JUDGE_KEY = checked_author_key("llm_judge")
CUSTOM_CHECKS_KEY = checked_author_key("custom_checks")

# Every model the runner's RunnerGradingConfig reaches, with where its fields sit
# in ``RunnerGradingConfig.model_dump()``. Keys match the Python class names so a
# manifest ``runner_field="ModelName.field"`` resolves via a single lookup.
_RUNNER_CONFIG_MODELS: dict[str, tuple[type[BaseModel], tuple[str, ...]]] = {
    "RunnerGradingConfig": (RunnerGradingConfig, ()),
    "RunnerStateChecksConfig": (RunnerStateChecksConfig, ("state_checks",)),
    "TranscriptRulesConfig": (TranscriptRulesConfig, ("transcript_rules",)),
    "TraceChecksConfig": (TraceChecksConfig, ("trace_checks",)),
}

# ``scored_keys_claiming_runner()`` widened by the scored keys the manifest
# declares CORE_ONLY yet the adapter still translates onto a runner field: those
# arrive populated on a real request and must be accounted for too.
LEDGER_KEYS: tuple[GradingKey, ...] = (
    *scored_keys_claiming_runner(),
    *(
        item
        for item in GRADING_KEYS
        if item.kind is KeyKind.SCORED_CHECK
        and item.coverage is SubstrateCoverage.CORE_ONLY
        and item.runner_field is not None
    ),
)


_HASH_FAMILY_KEYS = frozenset(family_author_keys("state_checks.hash"))
_HASH_FAMILY = tuple(item for item in LEDGER_KEYS if item.author_key in _HASH_FAMILY_KEYS)
# The hash family splits by who consumes each member: the runner's hash evaluator
# shares one outcome across the members it reads, while a member the manifest
# gives no runner evaluator is dead on this substrate however hash grading went.
_RUNNER_HASH_FAMILY = tuple(
    item.author_key for item in _HASH_FAMILY if item.runner_evaluator is not None
)
_CORE_ONLY_HASH_FAMILY = tuple(
    item.author_key for item in _HASH_FAMILY if item.runner_evaluator is None
)


def reject_hash_members_read_by_another_evaluator(items: Iterable[GradingKey]) -> None:
    """Raise unless every hash-family member is read by the one hash evaluator.

    :func:`hash_family_accounting` splits the family on ``runner_evaluator is not
    None``, but the outcome its caller hands in is specifically the hash evaluator's.
    A member some other runner evaluator reads would silently be reported with the
    hash evaluator's verdict, and the accounting-site lock would not notice because
    the key is accountable either way.
    """
    for item in items:
        if item.runner_evaluator not in (None, RUNNER_HASH_EVALUATOR):
            raise ValueError(
                f"{item.author_key}: hash-family member declares runner evaluator "
                f"{item.runner_evaluator!r}, but hash_family_accounting() hands every "
                f"member the outcome of {RUNNER_HASH_EVALUATOR}. A member another "
                "evaluator reads needs its own recording site, not this family's outcome"
            )


reject_hash_members_read_by_another_evaluator(_HASH_FAMILY)


@dataclass(frozen=True)
class LedgerAudit:
    """The ledger's verdict on one component phase.

    ``error`` is set when a populated scored key was neither evaluated nor
    skipped. ``skip_notes`` describe the populated keys that recorded a skip, for
    surfacing in ``grade.reasons`` — a skip a task author cannot see is as silent
    as no accounting at all.
    """

    error: str | None
    skip_notes: tuple[str, ...]


def skip_note_prefix(author_key: str) -> str:
    """How every skip note for ``author_key`` starts, whatever the detail.

    A caller asserting a key was *not* skipped has no hypothetical detail to
    match on, so it matches this instead: the prefix is the whole format the
    audit renders, and single-sourcing it is what stops a test from spelling a
    sentence production never emits.
    """
    return f"{author_key} skipped: "


def skip_note(author_key: str, record: KeyAccountingRecord) -> str:
    """The sentence ``grade.reasons`` carries for ``author_key``'s recorded skip.

    Raises ``ValueError`` on an ``EVALUATED`` record: an evaluated key produces
    no note at all, and rendering one would let a caller assert against text the
    audit never emits.
    """
    if record.outcome is KeyAccounting.EVALUATED:
        raise ValueError(
            f"{author_key}: an EVALUATED record has no skip note — the audit renders "
            "a note only for a populated key that was skipped"
        )
    return f"{skip_note_prefix(author_key)}{record.detail}"


def hash_family_accounting(runner_outcome: KeyAccountingRecord) -> dict[str, KeyAccountingRecord]:
    """The whole ``state_checks.hash`` family's accounting for one component phase.

    ``runner_outcome`` covers the members the runner's hash evaluator reads: the
    adapter populates ``expected_hash`` and ``golden_actions`` regardless of
    ``hash.enabled``, so those members share one outcome rather than being
    accounted leaf by leaf, which would leave a populated leaf out whenever hash
    grading is off. A member the manifest declares core-only is a skip either way
    — recording it as evaluated would report a silently dead key as scored.
    """
    return {
        **dict.fromkeys(_RUNNER_HASH_FAMILY, runner_outcome),
        **dict.fromkeys(_CORE_ONLY_HASH_FAMILY, CORE_ONLY_HASH_SKIP),
    }


def transcript_rules_author_keys() -> tuple[str, ...]:
    """Every ledger key under ``transcript_rules``, for the degenerate skip.

    A trial whose timeline carries no events is evidence for exactly one
    transcript rule — the activity floor, whose answer absence supplies — so the
    sole caller evaluates a declared ``min_assistant_turns`` and records the skip
    over the rest. The whole subtree is returned regardless: a helper that
    silently omitted a key would leave a future caller's skip incomplete.
    """
    return tuple(
        item.author_key for item in LEDGER_KEYS if item.author_key.startswith("transcript_rules.")
    )


def accountable_author_keys() -> frozenset[str]:
    """The per-key human declaration that a recording site files each author key.

    A declaration, not evidence. Lock 15 of
    ``tests/canonical/test_grading_substrate_parity.py`` drives every ledger key's
    site through a real ``GradeTrial`` and reads the outcome it filed; what this
    set adds is the claim a person made, key by key, independent of whether anyone
    could build a driver for it. If a driver is ever narrowed or removed, the
    declaration is what survives. Lock 5 reads it, and a key missing from here
    fails the canonical suite rather than failing ``GradeTrial`` in production for
    every task that populates it.

    No runtime path calls it: its only callers are that canonical lock and the
    ledger's own unit tests. Every member is named per site, or derived from a
    hand-written mapping that same site is handed — the two hash-family tuples,
    and the per-kind trace-constraint keys. Comprehending any member from
    :data:`LEDGER_KEYS` would make lock 5 compare that set against itself: a
    tautological assertion is worse than a missing one, because the next reader
    trusts its message and stops looking.
    """
    return frozenset(
        {
            *_RUNNER_HASH_FAMILY,
            *_CORE_ONLY_HASH_FAMILY,
            *TRACE_CONSTRAINT_KEY_BY_KIND.values(),
            TRACE_CONSTRAINTS_KEY,
            TRACE_ALTERNATIVES_KEY,
            MUST_CONTAIN_KEY,
            DISALLOW_REGEX_KEY,
            MAX_TURNS_KEY,
            MIN_ASSISTANT_TURNS_KEY,
            REQUIRED_ACTIONS_KEY,
            TOOL_EXPECTATIONS_KEY,
            COMMUNICATE_INFO_KEY,
            JSONPATHS_KEY,
            DB_PROBES_KEY,
            LLM_JUDGE_KEY,
            CUSTOM_CHECKS_KEY,
        }
    )


def runner_dump_path(item: GradingKey) -> tuple[str, ...]:
    """Where ``item``'s value sits in the runner ``RunnerGradingConfig.model_dump()``.

    Raises ``ValueError`` when ``runner_field`` names a model or a field the runner
    grading config does not declare, so a malformed manifest entry fails the
    canonical parity suite rather than at grade time in production.
    """
    if item.runner_field is None:
        raise ValueError(f"{item.author_key}: has no runner_field to resolve")
    if item.runner_dict_key is not None:
        raise ValueError(
            f"{item.author_key}: runner_dict_key {item.runner_dict_key!r} is not "
            "resolvable — the ledger reads typed runner fields only"
        )
    model_name, _, field_name = item.runner_field.partition(".")
    declared = _RUNNER_CONFIG_MODELS.get(model_name)
    if declared is None:
        raise ValueError(
            f"{item.author_key}: runner_field {item.runner_field!r} names {model_name!r}, "
            "which is not part of the runner grading config"
        )
    model, prefix = declared
    if field_name not in model.model_fields:
        raise ValueError(
            f"{item.author_key}: runner_field {item.runner_field!r} does not resolve — "
            f"{model_name} has no field {field_name!r}"
        )
    return (*prefix, field_name)


def audit_accounted_keys(
    grading_config: RunnerGradingConfig, accounted_keys: Mapping[str, KeyAccountingRecord]
) -> LedgerAudit:
    """Subtract ``accounted_keys`` from the scored keys ``grading_config`` populates.

    A key counts as populated only when it is truthy in
    ``model_dump(exclude_defaults=True)``: an explicitly written
    ``disallowed_tools: []`` is indistinguishable from unset, and an empty check
    has nothing to evaluate either way.
    """
    unaccounted: list[str] = []
    skip_notes: list[str] = []
    for item in _populated_items(grading_config):
        record = accounted_keys.get(item.author_key)
        if record is None:
            unaccounted.append(_unaccounted_detail(item))
        elif record.outcome is not KeyAccounting.EVALUATED:
            skip_notes.append(skip_note(item.author_key, record))
    error = None
    if unaccounted:
        error = (
            "Grading config populates scored keys the runner neither evaluated nor "
            f"recorded a skip for: {'; '.join(unaccounted)}"
        )
    return LedgerAudit(error=error, skip_notes=tuple(skip_notes))


def populated_ledger_keys(grading_config: RunnerGradingConfig) -> frozenset[str]:
    """The ledger keys ``grading_config`` populates, by the audit's own predicate.

    Restricted to keys naming a ``runner_field``, as the audit's loop is: the
    ``state_checks.hash`` family root names none, and resolving it raises. So the
    domain is the ledger keys a recording site can be driven for, and a caller
    reading this set is reading the same fact the audit acted on.

    No runtime path calls it: its only callers are lock 15 of
    ``tests/canonical/test_grading_substrate_parity.py`` and the ledger's own unit
    tests.
    """
    return frozenset(item.author_key for item in _populated_items(grading_config))


def _populated_items(grading_config: RunnerGradingConfig) -> tuple[GradingKey, ...]:
    """Every ledger key the config populates — the one predicate both readers visit."""
    dumped = grading_config.model_dump(exclude_defaults=True)
    return tuple(
        item for item in LEDGER_KEYS if item.runner_field is not None and _populated(dumped, item)
    )


def _populated(dumped: dict[str, Any], item: GradingKey) -> bool:
    """Whether the request's config carries a value for ``item``.

    Several entries name one ``list[BaseModel]`` field and are told apart by their
    element path, so for those the list being non-empty is not the question — a
    block declaring one ``before`` constraint populates the ``before`` key and none
    of the other nine, and reading the field alone would demand a recording site
    for every kind of every task.
    """
    value = _dumped_value(dumped, runner_dump_path(item))
    if item.runner_element_path is None:
        return bool(value)
    return _element_declares(value, item.runner_element_path)


def _element_declares(elements: Any, element_path: str) -> bool:
    """Whether some element of a dumped list field carries ``element_path``.

    A path a composite constraint nests counts, because the evaluator reaches it
    and accounts for it: an ``all_of`` holding a ``before`` populates the ``before``
    key. Descent therefore follows the values held under a constraint kind, and
    stops anywhere else — a matcher's ``args`` keys are the author's own argument
    names, so ``args: {before: ...}`` is a predicate on an argument called
    ``before``, not a nested ordering constraint.
    """
    if not isinstance(elements, list):
        return False
    segments = tuple(element_path.split("."))
    return any(_element_segment_declared(element, segments) for element in elements)


def _element_segment_declared(node: Any, segments: tuple[str, ...]) -> bool:
    if not segments:
        return bool(node)
    if not isinstance(node, dict):
        return False
    if segments[0] in node:
        return _element_segment_declared(node[segments[0]], segments[1:])
    return any(
        _element_segment_declared(nested, segments)
        for kind in TRACE_CONSTRAINT_KINDS
        if kind in node
        for nested in _nested_expressions(node[kind])
    )


def _nested_expressions(payload: Any) -> tuple[Any, ...]:
    """A constraint payload's own sub-expressions, list-valued or single."""
    return tuple(payload) if isinstance(payload, list) else (payload,)


def _dumped_value(dumped: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = dumped
    for segment in path:
        if not isinstance(node, dict):
            return None
        node = node.get(segment)
    return node


def _unaccounted_detail(item: GradingKey) -> str:
    if item.coverage is SubstrateCoverage.CORE_ONLY:
        return f"{item.author_key} (manifest declares CORE_ONLY: {item.reason})"
    evaluator = item.runner_evaluator or "none declared"
    return f"{item.author_key} (expected runner evaluator: {evaluator})"
