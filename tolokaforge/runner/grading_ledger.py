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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.grading.key_manifest import (
    GRADING_KEYS,
    GradingKey,
    KeyKind,
    SubstrateCoverage,
    entry,
    scored_keys_claiming_runner,
)
from tolokaforge.runner.models import GradingConfig, StateChecksConfig, TranscriptRulesConfig

EVALUATED = "evaluated"

HASH_DISABLED_SKIP = "skipped: hash grading not enabled"
NO_TRANSCRIPT_INPUT_SKIP = "skipped: no transcript messages or tool history"
NO_JUDGE_MESSAGES_SKIP = "skipped: no transcript messages"


def _manifest_key(author_key: str) -> str:
    """``author_key`` itself, raising at import when the manifest no longer has it."""
    return entry(author_key).author_key


MUST_CONTAIN_KEY = _manifest_key("transcript_rules.must_contain")
DISALLOW_REGEX_KEY = _manifest_key("transcript_rules.disallow_regex")
MAX_TURNS_KEY = _manifest_key("transcript_rules.max_turns")
TOOL_EXPECTATIONS_KEY = _manifest_key("transcript_rules.tool_expectations")
REQUIRED_ACTIONS_KEY = _manifest_key("transcript_rules.required_actions")
COMMUNICATE_INFO_KEY = _manifest_key("transcript_rules.communicate_info")
JSONPATHS_KEY = _manifest_key("state_checks.jsonpaths")
DB_PROBES_KEY = _manifest_key("state_checks.db_probes")
LLM_JUDGE_KEY = _manifest_key("llm_judge")

_HASH_FAMILY_ROOT = "state_checks.hash"

# Every model the runner's GradingConfig reaches, with where its fields sit in
# ``GradingConfig.model_dump()``.
_RUNNER_CONFIG_MODELS: dict[str, tuple[type[BaseModel], tuple[str, ...]]] = {
    "GradingConfig": (GradingConfig, ()),
    "StateChecksConfig": (StateChecksConfig, ("state_checks",)),
    "TranscriptRulesConfig": (TranscriptRulesConfig, ("transcript_rules",)),
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


def hash_family_author_keys() -> tuple[str, ...]:
    """Every ledger key under ``state_checks.hash``.

    The adapter populates ``expected_hash`` and ``golden_actions`` regardless of
    ``hash.enabled``, so the family shares one accounting outcome; accounting it
    leaf by leaf would leave a populated leaf unaccounted whenever hash grading is
    off.
    """
    return tuple(
        item.author_key
        for item in LEDGER_KEYS
        if item.author_key == _HASH_FAMILY_ROOT
        or item.author_key.startswith(f"{_HASH_FAMILY_ROOT}.")
    )


def transcript_rules_author_keys() -> tuple[str, ...]:
    """Every ledger key under ``transcript_rules``."""
    return tuple(
        item.author_key for item in LEDGER_KEYS if item.author_key.startswith("transcript_rules.")
    )


def runner_dump_path(item: GradingKey) -> tuple[str, ...]:
    """Where ``item``'s value sits in the runner ``GradingConfig.model_dump()``.

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
    grading_config: GradingConfig, accounted_keys: Mapping[str, str]
) -> LedgerAudit:
    """Subtract ``accounted_keys`` from the scored keys ``grading_config`` populates.

    A key counts as populated only when it is truthy in
    ``model_dump(exclude_defaults=True)``: an explicitly written
    ``disallowed_tools: []`` is indistinguishable from unset, and an empty check
    has nothing to evaluate either way.
    """
    dumped = grading_config.model_dump(exclude_defaults=True)
    unaccounted: list[str] = []
    skip_notes: list[str] = []
    for item in LEDGER_KEYS:
        if item.runner_field is None or not _dumped_value(dumped, runner_dump_path(item)):
            continue
        record = accounted_keys.get(item.author_key)
        if record is None:
            unaccounted.append(_unaccounted_detail(item))
        elif record != EVALUATED:
            skip_notes.append(f"{item.author_key} {record}")
    error = None
    if unaccounted:
        error = (
            "Grading config populates scored keys the runner neither evaluated nor "
            f"recorded a skip for: {'; '.join(unaccounted)}"
        )
    return LedgerAudit(error=error, skip_notes=tuple(skip_notes))


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
