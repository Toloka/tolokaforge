"""Scoring a pack's transcript rules against the trial's event timeline.

Substrate-neutral and pure — no services, no I/O — over the timeline both
substrates build and the one ``TranscriptRulesConfig`` both substrates validate,
so a rule reaches the same verdict whichever substrate grades the trial. The
config is taken as the model, never as a dump: a dict seam admits a field
spelling or a rule shape neither substrate agreed to, and admits it silently.

:func:`evaluate_transcript_rules` serves two integration points — the runner's
``GradeTrial`` and the core :class:`~tolokaforge.core.grading.combine.GradingEngine`
— the way :func:`~tolokaforge.core.grading.trace_checks.evaluate_trace_checks`
already does.

The authored vocabulary is documented in ``docs/GRADING.md`` § "Transcript Rules".
"""

import re
from collections.abc import Sequence
from typing import Literal

from tolokaforge.core.grading.key_manifest import (
    COMMUNICATE_INFO_KEY,
    DISALLOW_REGEX_KEY,
    EVALUATED,
    MAX_TURNS_KEY,
    MIN_ASSISTANT_TURNS_KEY,
    MUST_CONTAIN_KEY,
    REQUIRED_ACTIONS_KEY,
    TOOL_EXPECTATIONS_KEY,
)
from tolokaforge.core.grading.trace_timeline import (
    AttemptedCall,
    TrialTimeline,
    assistant_texts,
    attempted_calls,
)
from tolokaforge.runner.models import (
    CommunicateInfo,
    KeyAccountingRecord,
    RequiredAction,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    TranscriptEvaluationResult,
    TranscriptRuleResult,
    TranscriptRulesConfig,
)


def evaluate_transcript_rules(
    timeline: TrialTimeline,
    rules: TranscriptRulesConfig,
) -> TranscriptEvaluationResult:
    """
    Evaluate the author-facing ``TranscriptRulesConfig`` against the trial's
    event timeline.

    ``rules`` is the validated model task authors write in ``grading.yaml``,
    decomposed into one independent sub-check per field entry, each of which
    produces a visible ``TranscriptRuleResult`` in ``details``:

    - ``must_contain`` (list[str]): each string must appear (case-insensitive
      substring) in some assistant message → one sub-check per string.
    - ``disallow_regex`` (list[str]): none of the regexes may match any
      assistant message → one sub-check per regex.
    - ``max_turns`` (int | None): the number of assistant turns must be within
      the limit → one sub-check when set.
    - ``min_assistant_turns`` (int | None): the trial must have produced at least
      this many assistant turns. A **gate on the whole component**, not a
      sub-check: an unmet floor appends one failing row and forces ``score`` to
      ``0.0``, and a met floor appends no row so it never dilutes the fraction.
    - ``tool_expectations`` ({required_tools, disallowed_tools}): one sub-check
      per declared tool, over the **agent's** calls alone. A required tool must
      have been called *successfully*; a disallowed tool must not have run at
      **any** status, because an attempted forbidden call is itself the violation.
      A user-simulator call satisfies neither list and violates neither. Both fail
      on a timeline that carries no records for a call the message view declared —
      whether it ran, and who ran it, is then unknown, and a "did not run" reading
      would pass every forbidden call.
    - ``required_actions`` (list[RequiredAction]): each declared tool call must
      appear on the timeline, matched by ``name`` + ``requestor`` and
      by the argument subset named in ``compare_args`` (``None`` = compare all
      declared args, ``[]`` = compare none) → one sub-check per action.
    - ``communicate_info`` (list[CommunicateInfo]): each ``required`` info
      string must appear in an assistant message → one sub-check per required
      entry (non-required entries are advisory and not scored).

    Scoring: ``score`` is the fraction of sub-checks that passed — unless a
    declared ``min_assistant_turns`` floor is unmet, in which case it is ``0.0``
    whatever the other sub-checks did. ``passed`` is True iff every sub-check
    passed. The component score feeds
    ``combine_grade_components`` where ``pass_threshold`` is applied, so a
    fraction (rather than all-or-nothing) lets authors set partial-credit
    thresholds (e.g. ``pass_threshold: 0.75``). A config that produces no sub-check
    rows is a no-op pass with ``score=1.0`` — no fields set, or a met floor as the
    only declared rule, leaves nothing that could have been violated.

    Unknown / missing data is surfaced as a FAILING sub-check, never silently
    passed.

    ``accounted_keys`` names the author keys this call decomposed, for the
    runner's runtime ledger. The core engine has no ledger and discards it: the
    accounting is a record of what one ``GradeTrial`` RPC did, not a property of
    the score.

    Args:
        timeline: The trial's event timeline, built by
            :func:`~tolokaforge.core.grading.trace_timeline.build_trial_timeline`
        rules: The validated ``TranscriptRulesConfig``

    Returns:
        TranscriptEvaluationResult with passed, score, per-sub-check details, and
        the author keys this call decomposed

    Raises:
        TypeError: ``rules`` is not a ``TranscriptRulesConfig``. The evaluator is
            the one place the two substrates meet, and a dump reopens the untyped
            seam this signature exists to close — a caller handing one over is
            refused rather than silently reading a shape it cannot validate.
    """
    if not isinstance(rules, TranscriptRulesConfig):
        raise TypeError(
            "evaluate_transcript_rules takes a validated TranscriptRulesConfig, not "
            f"{type(rules).__name__}. Pass the model both substrates already hold"
        )

    details: list[TranscriptRuleResult] = []
    accounted_keys: dict[str, KeyAccountingRecord] = {}

    tool_expectations = rules.tool_expectations
    required_tools = tool_expectations.required_tools if tool_expectations else []
    disallowed_tools = tool_expectations.disallowed_tools if tool_expectations else []

    assistant_messages = assistant_texts(timeline)
    calls = attempted_calls(timeline)
    records_present = timeline.records_present

    unmet_floor: TranscriptRuleResult | None = None
    if rules.min_assistant_turns is not None:
        accounted_keys[MIN_ASSISTANT_TURNS_KEY] = EVALUATED
        unmet_floor = _unmet_activity_floor(rules.min_assistant_turns, len(assistant_messages))
        if unmet_floor is not None:
            details.append(unmet_floor)

    if rules.must_contain:
        accounted_keys[MUST_CONTAIN_KEY] = EVALUATED
        for text in rules.must_contain:
            details.append(_check_must_contain(text, assistant_messages))

    if rules.disallow_regex:
        accounted_keys[DISALLOW_REGEX_KEY] = EVALUATED
        for pattern in rules.disallow_regex:
            details.append(_check_disallow_regex(pattern, assistant_messages))

    if rules.max_turns is not None:
        accounted_keys[MAX_TURNS_KEY] = EVALUATED
        details.append(_check_max_turns(rules.max_turns, len(assistant_messages)))

    if required_tools or disallowed_tools:
        accounted_keys[TOOL_EXPECTATIONS_KEY] = EVALUATED
        for tool_name in required_tools:
            details.append(_check_required_tool(tool_name, calls, records_present=records_present))
        for tool_name in disallowed_tools:
            details.append(
                _check_disallowed_tool(tool_name, calls, records_present=records_present)
            )

    if rules.required_actions:
        accounted_keys[REQUIRED_ACTIONS_KEY] = EVALUATED
        for action in rules.required_actions:
            details.append(_check_required_action(action, calls, records_present=records_present))

    if rules.communicate_info:
        accounted_keys[COMMUNICATE_INFO_KEY] = EVALUATED
        for info in rules.communicate_info:
            check = _check_communicate_info(info, assistant_messages)
            if check is not None:
                details.append(check)

    if not details:
        # No sub-check rows — nothing can be violated. Either nothing was declared,
        # or a met activity floor was the only declaration and emits no row.
        return TranscriptEvaluationResult(
            passed=True, score=1.0, details=[], accounted_keys=accounted_keys
        )

    passed_count = sum(1 for d in details if d.passed)
    total_count = len(details)
    # An unmet activity floor vetoes the whole component: a trial that did not act
    # makes the other sub-checks' verdicts evidence of nothing, and a fraction
    # would let any pass_threshold at or below it swallow the violation.
    score = 0.0 if unmet_floor is not None else passed_count / total_count
    all_passed = passed_count == total_count

    return TranscriptEvaluationResult(
        passed=all_passed, score=score, details=details, accounted_keys=accounted_keys
    )


def scored_transcript_rules(
    timeline: TrialTimeline, rules: TranscriptRulesConfig
) -> TranscriptRulesConfig | None:
    """The part of ``rules`` the trial's timeline carries evidence for.

    ``None`` is the trial that left no trace of itself — a timeline carrying
    neither a conversational turn nor a tool call — under a pack declaring no
    activity floor: nothing is evaluable, so the component is left out of the
    combine rather than scored against evidence the trial does not have.

    A declared floor *is* evaluated on that timeline, because no events is
    precisely the answer it asks for, so the floor comes back alone while every
    other rule it was declared beside does not.

    Both substrates fold ``transcript_rules`` through this decision, so whether an
    events-less trial scores the component does not depend on which one graded it.
    """
    if timeline.events:
        return rules
    if rules.min_assistant_turns is None:
        return None
    return TranscriptRulesConfig(min_assistant_turns=rules.min_assistant_turns)


# Map the RequiredAction.requestor vocabulary ("assistant"/"user", the
# author-facing role names) onto the recorded executor identity. They name the
# same actor.
_REQUESTOR_TO_EXECUTOR: dict[Literal["assistant", "user"], ToolExecutorIdentity] = {
    "assistant": ToolExecutorIdentity.AGENT,
    "user": ToolExecutorIdentity.USER,
}


_UNRECORDED = (
    "the trial carries no tool-call record, so nothing knows whether the calls it "
    "declared ran. A timeline rebuilt from a bundle's trajectory.yaml alone is in "
    "that state — the record is the bundle's tool_log.yaml sidecar"
)


def _declared(calls: Sequence[AttemptedCall], tool_name: str) -> list[AttemptedCall]:
    """Every call to ``tool_name`` the agent asked for, whether or not it ran."""
    return [call for call in calls if call.tool_name == tool_name]


def _executed_by_agent(calls: Sequence[AttemptedCall], tool_name: str) -> list[AttemptedCall]:
    """The agent's own calls to ``tool_name`` that ran, so carry an outcome.

    ``tool_expectations`` grade the agent's tool use, so a user-simulator call is
    another actor's and counts for neither list. Executor identity comes from the
    record, which is also what separates ran from did-not-run: without records
    every call reads as un-run and as executed by nobody, so callers ask
    :func:`_outcome_unknown` first instead of reading absent evidence as a fact.
    """
    return [
        call
        for call in _declared(calls, tool_name)
        if call.status is not None and call.executor is ToolExecutorIdentity.AGENT
    ]


def _outcome_unknown(
    calls: Sequence[AttemptedCall], tool_name: str, *, records_present: bool
) -> bool:
    """Whether the timeline can say if the declared calls to ``tool_name`` ran.

    A record can only name a call the message view declared, so a tool the agent
    never asked for never ran — knowable from the message view alone. Whatever it
    did ask for is unknown the moment the record view is missing.
    """
    return not records_present and bool(_declared(calls, tool_name))


def _check_must_contain(text: str, assistant_messages: Sequence[str]) -> TranscriptRuleResult:
    """Each required string must appear (case-insensitive) in some assistant message."""
    needle = text.lower()
    found = any(needle in content.lower() for content in assistant_messages)
    return TranscriptRuleResult(
        rule_type="must_contain",
        rule={"must_contain": text},
        passed=found,
        message=(
            f"Found required text {text!r} in an assistant message"
            if found
            else f"Required text {text!r} not found in any assistant message"
        ),
    )


def _check_disallow_regex(pattern: str, assistant_messages: Sequence[str]) -> TranscriptRuleResult:
    """No assistant message may match the disallowed regex.

    An invalid regex is an author error — surface it as a FAIL rather than
    silently treating it as 'no match'.
    """
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return TranscriptRuleResult(
            rule_type="disallow_regex",
            rule={"disallow_regex": pattern},
            passed=False,
            message=f"Invalid disallow_regex {pattern!r}: {exc}",
        )

    for content in assistant_messages:
        if compiled.search(content):
            return TranscriptRuleResult(
                rule_type="disallow_regex",
                rule={"disallow_regex": pattern},
                passed=False,
                message=f"Disallowed pattern {pattern!r} matched an assistant message",
            )
    return TranscriptRuleResult(
        rule_type="disallow_regex",
        rule={"disallow_regex": pattern},
        passed=True,
        message=f"Disallowed pattern {pattern!r} did not match any assistant message",
    )


def _check_max_turns(max_turns: int, turn_count: int) -> TranscriptRuleResult:
    """Assistant turn count must be within the limit.

    A "turn" is one assistant generation. This counts the agent's responses
    rather than user messages so the limit caps the agent's activity, which is
    what authors intend to bound.
    """
    within = turn_count <= max_turns
    return TranscriptRuleResult(
        rule_type="max_turns",
        rule={"max_turns": max_turns},
        passed=within,
        message=(
            f"Assistant turn count {turn_count} within limit of {max_turns}"
            if within
            else f"Assistant turn count {turn_count} exceeds limit of {max_turns}"
        ),
    )


def _unmet_activity_floor(min_assistant_turns: int, turn_count: int) -> TranscriptRuleResult | None:
    """The failing row for an unmet activity floor, or ``None`` when it is met.

    A met floor emits no row at all. The floor is a gate on the whole component,
    so a passing row would dilute the fraction the other sub-checks produce.
    """
    if turn_count >= min_assistant_turns:
        return None
    return TranscriptRuleResult(
        rule_type="min_assistant_turns",
        rule={"min_assistant_turns": min_assistant_turns},
        passed=False,
        message=(
            f"Assistant turn count {turn_count} below min_assistant_turns of {min_assistant_turns}"
        ),
    )


def _check_required_tool(
    tool_name: str, calls: Sequence[AttemptedCall], *, records_present: bool
) -> TranscriptRuleResult:
    """The agent must have called a required tool successfully at least once.

    Same "a failed call did not happen" rule ``_check_required_action`` applies:
    an errored call did not accomplish the work the author required. A
    user-simulator call to the same tool is not the agent having used it, so it
    satisfies nothing whatever its status.
    """
    if _outcome_unknown(calls, tool_name, records_present=records_present):
        return TranscriptRuleResult(
            rule_type="required_tool",
            rule={"required_tool": tool_name},
            passed=False,
            message=f"Required tool {tool_name!r}: {_UNRECORDED}",
        )
    called = any(
        call.status is ToolExecutionStatus.SUCCESS for call in _executed_by_agent(calls, tool_name)
    )
    return TranscriptRuleResult(
        rule_type="required_tool",
        rule={"required_tool": tool_name},
        passed=called,
        message=(
            f"Required tool {tool_name!r} was called successfully"
            if called
            else f"Required tool {tool_name!r} was never called successfully"
        ),
    )


def _check_disallowed_tool(
    tool_name: str, calls: Sequence[AttemptedCall], *, records_present: bool
) -> TranscriptRuleResult:
    """The agent must not have run a disallowed tool, at any status.

    Status-insensitive on purpose: attempting a forbidden call is the violation,
    so an errored attempt fails the check just like a successful one. A call the
    agent declared on a terminating turn never reached the substrate and is not
    counted, and neither is a user-simulator call — the prohibition is on the
    agent. A ``trace_checks`` matcher over the call event is where either intent is
    nameable. Both exclusions need the record view, so a declared call the timeline holds no
    records for fails the check instead of reading as one that never ran.
    """
    if _outcome_unknown(calls, tool_name, records_present=records_present):
        return TranscriptRuleResult(
            rule_type="disallowed_tool",
            rule={"disallowed_tool": tool_name},
            passed=False,
            message=f"Disallowed tool {tool_name!r}: {_UNRECORDED}",
        )
    offending = _executed_by_agent(calls, tool_name)
    if not offending:
        message = f"Disallowed tool {tool_name!r} was never called"
    else:
        statuses = ", ".join(sorted({call.status.value for call in offending if call.status}))
        message = (
            f"Disallowed tool {tool_name!r} was called {len(offending)} "
            f"time(s) (statuses: {statuses})"
        )
    return TranscriptRuleResult(
        rule_type="disallowed_tool",
        rule={"disallowed_tool": tool_name},
        passed=not offending,
        message=message,
    )


def _check_required_action(
    action: RequiredAction, calls: Sequence[AttemptedCall], *, records_present: bool
) -> TranscriptRuleResult:
    """A declared tool call must appear on the timeline.

    Matching:
    - ``name`` must match the called tool exactly;
    - ``requestor`` ("assistant"/"user") must match the call's ``executor``
      ("agent"/"user");
    - the call must have ``status == "success"`` (a failed call did not happen);
    - arguments named by ``compare_args`` must match the declared values.
      ``compare_args is None`` compares every declared argument; ``[]`` compares
      none (presence of the tool call is enough).

    An empty ``name`` is authorable — the field is required but not bounded — and
    names no tool, so it is refused rather than matched against every call.
    """
    declared = action.model_dump()
    expected_executor = _REQUESTOR_TO_EXECUTOR[action.requestor]

    if action.compare_args is None:
        keys_to_compare = list(action.arguments.keys())
    else:
        keys_to_compare = list(action.compare_args)

    label = (
        f"required_action {action.action_id!r} "
        f"(tool={action.name!r}, requestor={action.requestor!r})"
    )

    if not action.name:
        return TranscriptRuleResult(
            rule_type="required_action",
            rule=declared,
            passed=False,
            message=f"{label}: no name declared",
        )

    if _outcome_unknown(calls, action.name, records_present=records_present):
        return TranscriptRuleResult(
            rule_type="required_action",
            rule=declared,
            passed=False,
            message=f"{label}: {_UNRECORDED}",
        )

    for call in calls:
        if call.tool_name != action.name:
            continue
        if call.executor is not expected_executor:
            continue
        if call.status is not ToolExecutionStatus.SUCCESS:
            continue
        if all(call.arguments.get(k) == action.arguments.get(k) for k in keys_to_compare):
            return TranscriptRuleResult(
                rule_type="required_action",
                rule=declared,
                passed=True,
                message=f"{label}: matched a successful tool call",
            )

    return TranscriptRuleResult(
        rule_type="required_action",
        rule=declared,
        passed=False,
        message=(
            f"{label}: no matching successful tool call found"
            + (f" with args {keys_to_compare}" if keys_to_compare else "")
        ),
    )


def _check_communicate_info(
    info: CommunicateInfo, assistant_messages: Sequence[str]
) -> TranscriptRuleResult | None:
    """A required info string must appear in an assistant message.

    Returns ``None`` (no sub-check, not scored) for non-required info — those
    are advisory hints, not gating requirements.
    """
    if not info.required:
        return None

    needle = info.info.lower()
    found = any(needle in content.lower() for content in assistant_messages)
    return TranscriptRuleResult(
        rule_type="communicate_info",
        rule=info.model_dump(),
        passed=found,
        message=(
            f"Communicated required info {info.info!r}"
            if found
            else f"Required info {info.info!r} was not communicated to the user"
        ),
    )
