"""Trial trajectory wire type + message model + trial-execution metrics.

Everything the runner produces as a per-trial record lives here: the
message trace (:class:`Message`, :class:`ToolCall`), status/termination
enums, the :class:`Metrics` accounting block, the rate-limit-probe
census (:class:`RateLimitProbeRoleMetrics`,
:class:`RateLimitProbeBucketMetrics`), the user-reply guard's findings
(:class:`ReplyDefect`, :class:`UserReplyGuardEvent`), and the composite
:class:`Trajectory` that carries them.
"""

import dataclasses
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Self, get_args

from pydantic import (
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from tolokaforge.core.llm.reasoning import StructuredReasoning
from tolokaforge.core.llm.usage import CostSource, ProviderRawCall, Usage
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.trial_status import TerminationReason, TrialStatus
from tolokaforge.runner.models import RecordedToolCall

__all__ = [
    "REPLY_DEFECT_EXCERPT_MAX_CHARS",
    "FirstUserMessageSource",
    "Message",
    "MessageRole",
    "Metrics",
    "ProvisionStage",
    "RateLimitProbeBucketMetrics",
    "RateLimitProbeRoleMetrics",
    "ReplyDefect",
    "SnapshotOutcome",
    "SnapshotStatus",
    "TerminationReason",
    "ToolCall",
    "ToolUsage",
    "Trajectory",
    "UserReplyGuardEvent",
    "UserReplyOutcome",
]


ProvisionStage = Literal[
    "materialise_run",
    "provision",
    "await_ready",
    "reset_recipe",
    "register_trial",
    "cycle",
]
"""The six points a :class:`~tolokaforge.core.runtime.ProvisionError` can be
raised at, as a closed vocabulary. The provisioner declares which lifecycle
step failed — plan-shape validation at run start
(``materialise_run``, raised by
:class:`~tolokaforge.core.composition_runtime.SubstrateComposer` when the
composition plan violates INV-12), compose-up (``provision``), the readiness
gate (``await_ready``), the per-trial reset hook (``reset_recipe``), the
runner-side registration that arms the trial (``register_trial``), or the
between-trials service dispatch (``cycle``) that a
:class:`~tolokaforge.core.composition_runtime.ServiceLifecycleDispatcher`
raises — and the value survives verbatim onto
:attr:`Trajectory.provision_stage` and the per-trial ``metrics.yaml``
``error_stage`` key.

Defined here because :class:`Trajectory` carries it as a field; re-exported from
:mod:`tolokaforge.core.runtime` because :class:`ProvisionError` is the raise
site and the two names read as one contract there.
"""


class MessageRole(str, Enum):
    """Message role in conversation"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FirstUserMessageSource(str, Enum):
    """Where message index 0 came from."""

    PINNED = "pinned"  # The task's initial_user_message, delivered verbatim
    SIMULATOR = "simulator"  # A user-simulator dispatch produced it


class SnapshotOutcome(str, Enum):
    """Trial-end grade-bundle produce outcome tag.

    Written to :attr:`SnapshotStatus.outcome` by the orchestrator's
    trial-end producer seam. Downstream reporting code imports the symbol
    and compares by identity (``status.outcome is SnapshotOutcome.STORED``)
    rather than by string literal — string comparisons drift when a new
    outcome is added.
    """

    STORED = "stored"
    OVERSIZE = "oversize"
    PRODUCE_FAILED = "produce_failed"
    UNGRADED = "ungraded"


class SnapshotStatus(BaseModel):
    """Grade-bundle produce outcome for a trial.

    Populated when the orchestrator's snapshot mode is enabled
    (``grader.snapshot.enabled=true``) and the trial reached the
    producer seam. ``uri`` / ``bundle_size_bytes`` / ``cap_bytes`` /
    ``reason`` are keyed by ``outcome``: :attr:`SnapshotOutcome.STORED`
    carries ``uri`` + ``bundle_size_bytes``; :attr:`SnapshotOutcome.OVERSIZE`
    carries ``bundle_size_bytes`` + ``cap_bytes`` + ``reason``;
    :attr:`SnapshotOutcome.PRODUCE_FAILED` carries ``reason``;
    :attr:`SnapshotOutcome.UNGRADED` carries no side data. Consumers gate on
    ``outcome`` before reading the optional fields.
    """

    model_config = {"extra": "forbid"}

    outcome: SnapshotOutcome
    uri: str | None = None
    bundle_size_bytes: int | None = None
    cap_bytes: int | None = None
    reason: str | None = None

    @classmethod
    def stored(cls, *, uri: str, bundle_size_bytes: int) -> Self:
        return cls(
            outcome=SnapshotOutcome.STORED,
            uri=uri,
            bundle_size_bytes=bundle_size_bytes,
        )

    @classmethod
    def oversize(cls, *, bundle_size_bytes: int, cap_bytes: int) -> Self:
        return cls(
            outcome=SnapshotOutcome.OVERSIZE,
            bundle_size_bytes=bundle_size_bytes,
            cap_bytes=cap_bytes,
            reason=(
                f"Snapshot bundle exceeded cap "
                f"({bundle_size_bytes / 1024 / 1024:.1f} MB > "
                f"{cap_bytes / 1024 / 1024:.1f} MB); fell back."
            ),
        )

    @classmethod
    def produce_failed(cls, reason: str) -> Self:
        return cls(outcome=SnapshotOutcome.PRODUCE_FAILED, reason=reason)

    @classmethod
    def ungraded(cls) -> Self:
        return cls(outcome=SnapshotOutcome.UNGRADED)


class ToolCall(BaseModel):
    """Tool call from agent"""

    id: str
    name: str
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def _require_id(self) -> Self:
        if not self.id:
            raise ValueError(
                f"tool call for {self.name!r} carries an empty id. The id is the only key "
                "that joins a call to the tool result it produced, so a call without one "
                "is not gradeable."
            )
        return self


class Message(BaseModel):
    """Conversation message"""

    role: MessageRole
    content: str = ""
    content_blocks: list[dict[str, Any]] | None = None  # Multimodal content (screenshots)
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    # Structured reasoning / thinking blocks. See tolokaforge.core.llm.reasoning.
    # Bare strings are rejected (Stage 0 migration); callers must pass
    # ``StructuredReasoning`` or a dict parsable by it.
    reasoning: StructuredReasoning | None = None
    # OpenRouter's id for the generation that produced this message, so a
    # request/response record in ``trajectory.yaml`` can be joined back to the
    # routing decision behind it. Set on assistant messages produced by an
    # OpenRouter-routed call; None on every other message and every other route.
    openrouter_generation_id: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @field_validator("reasoning", mode="before")
    @classmethod
    def _validate_reasoning(cls, value: Any) -> Any:
        if value is None or isinstance(value, StructuredReasoning):
            return value
        if isinstance(value, str):
            raise ValueError(
                "Message.reasoning must be a StructuredReasoning (or dict with "
                "'blocks'/'summary'/'budget_used'), not a bare string. "
                "Legacy string reasoning is no longer supported — see "
                "tolokaforge.core.llm.reasoning.StructuredReasoning."
            )
        if isinstance(value, dict):
            blocks = value.get("blocks", ())
            from tolokaforge.core.llm.reasoning import ReasoningBlock

            coerced_blocks = tuple(
                b if isinstance(b, ReasoningBlock) else ReasoningBlock(**b) for b in blocks
            )
            return StructuredReasoning(
                blocks=coerced_blocks,
                summary=value.get("summary"),
                budget_used=value.get("budget_used"),
            )
        raise TypeError(
            f"Message.reasoning must be StructuredReasoning | dict | None, got {type(value).__name__}"
        )


class ToolUsage(BaseModel):
    """Tool usage statistics"""

    tool_name: str
    call_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_duration_s: float = 0.0


class RateLimitProbeRoleMetrics(BaseModel):
    """One ``(role, model)`` row of a trial's rate-limit probe accounting.

    A trial's roles are different *models* in a real arena config — the agent
    is the model under test and the user simulator is a fixed, unrelated one —
    so their counters must never be shared. ``role`` + ``model`` is the key;
    the flat ``Metrics.rate_limit_*`` / ``Metrics.probe_*`` scalars are the sum
    across these rows.

    Both censuses live on the row: the FAILURE side (``retries`` / ``wait_s``)
    and the SUCCESS side (``successful_calls`` / ``success_duration_s`` and the
    token counts). The pair is the point — the 429 census is schedule-dependent
    and, for some providers, silent: a provider can throttle by slowing calls
    down rather than rejecting them, which only goodput and latency catch. See
    ``docs/OUTPUT_FORMAT.md`` § Field observations for the measurements.

    ``Metrics.usage`` cannot answer the same question: ``usage.calls`` holds
    agent calls only and carries no role field, so per-model goodput and latency
    are not computable from it. These rows are the role-attributed record.

    ``model`` is the raw provider-qualified slug the client called
    (``openrouter/anthropic/claude-sonnet-4.6``). The engine deliberately does
    *not* map it to an upstream-provider taxonomy: that grouping lives in the
    consumer, and attributing a 429 to the OpenRouter upstream that actually
    served the request needs wire data the engine does not capture.
    """

    model_config = {"extra": "forbid"}

    role: str
    model: str
    retries: int = 0
    wait_s: float = 0.0
    first_ts: datetime | None = None
    """UTC timestamp of the first 429 on this row — the 429 window, not the
    first call. Successful calls deliberately do not move it."""

    last_ts: datetime | None = None
    """UTC timestamp of the most recent 429 on this row."""

    successful_calls: int = 0
    """LLM calls that returned a result for this ``(role, model)``.

    Counts successful ``LLMClient.generate`` returns, so it is ``>=`` the number
    of ``usage.calls`` rows this role contributed: a call whose provider
    returned no usage block still succeeded but adds no usage row."""

    success_duration_s: float = 0.0
    """Summed client-observed duration of those successful calls.

    ``success_duration_s / wall_seconds`` is the Little's-law in-flight
    concurrency the provider actually served. Computed on successes only, which
    is what makes it schedule-independent — unlike the 429 census, it does not
    depend on how often a blocked client chose to poll."""

    prompt_tokens: int = 0
    """Prompt tokens the provider reported for those successful calls.

    Tokens, not calls, are what sums across run legs: measured token profiles
    differ several-fold between domains, so a call in one leg is not the same
    unit of work as a call in another (see ``docs/OUTPUT_FORMAT.md`` § Field
    observations)."""

    completion_tokens: int = 0
    """Completion tokens the provider reported for those successful calls."""


class RateLimitProbeBucketMetrics(BaseModel):
    """One fixed-width absolute-time window of one ``(role, model)``'s throughput.

    **The window boundary is anchored on the Unix epoch, not on run start.**
    That is the whole reason this row exists. The intended measurement runs all
    seven domain legs simultaneously, each in its own runner process, and sums
    per-leg throughput into a global number; the sum is only valid if the legs'
    windows can be aligned in absolute time. ``bucket_start_ts`` is
    ``floor(epoch_seconds / bucket_width_s) * bucket_width_s`` rendered as UTC,
    so two processes on two machines with synchronised clocks emit the *same*
    value for the same instant and a consumer joins on it directly.

    Cumulative totals cannot replace these rows: measured goodput is
    non-stationary at CONSTANT offered concurrency, and one blended average
    reports neither end of the decay (``docs/OUTPUT_FORMAT.md`` § Field
    observations).

    A call is counted in the window it *finished* in — goodput is completions
    per window. ``retries`` / ``wait_s`` are the 429s scheduled inside the same
    window, so the served and rejected sides of one interval sit side by side.

    No ``first_ts`` / ``last_ts``, unlike :class:`RateLimitProbeRoleMetrics`:
    ``bucket_start_ts`` plus ``Metrics.probe_bucket_width_s`` already bound every
    event in the row, and a sub-window position is not a quantity a cross-leg sum
    can use.
    """

    model_config = {"extra": "forbid"}

    bucket_start_ts: datetime
    role: str
    model: str
    successful_calls: int = 0
    success_duration_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    wait_s: float = 0.0


_VALID_COST_SOURCES: frozenset[str] = frozenset(get_args(CostSource))


def _coerce_calls(value: Any) -> tuple[ProviderRawCall, ...]:
    """Coerce the round-tripped ``calls`` payload back into ``ProviderRawCall``.

    The YAML representation is ``list[dict]``; in-process construction
    passes ``ProviderRawCall`` instances directly. ``cost_source`` is
    rejected here when it isn't one of :data:`CostSource`'s literals,
    so a corrupt YAML surfaces as a Pydantic ``ValidationError`` rather
    than a silently downgraded record.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Usage.calls must be a list/tuple, got {type(value).__name__}")
    out: list[ProviderRawCall] = []
    for item in value:
        if isinstance(item, ProviderRawCall):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise TypeError(
                f"Usage.calls entries must be ProviderRawCall | dict, got {type(item).__name__}"
            )
        source = item.get("cost_source", "unknown")
        if source not in _VALID_COST_SOURCES:
            raise ValueError(
                f"ProviderRawCall.cost_source must be one of {sorted(_VALID_COST_SOURCES)}, "
                f"got {source!r}"
            )
        out.append(ProviderRawCall(**item))
    return tuple(out)


class Metrics(BaseModel):
    """Trial execution metrics.

    ``usage`` carries the full :class:`Usage` accounting (prompt / completion /
    reasoning / cache-creation / cache-read / cached / provider_raw / calls).
    Per-call cost / latency live on ``usage.calls[*]`` — the previously
    redundant flat ``api_call_latencies_s`` list is gone.

    ``cost_usd`` is the trial-level sum of every API call's ``cost_usd``;
    walk ``usage.calls`` to find which calls were litellm- vs locally-priced
    (each :class:`ProviderRawCall` carries its own ``cost_source``). The
    earlier ``cost_usd_est`` / ``cost_usd_provider`` split is gone.

    The ``rate_limit_*`` and ``probe_*`` counters are populated only while
    :class:`RateLimitProbeConfig` is enabled; on every other run they stay at
    their zero / ``None`` / empty defaults. Two prefixes, two censuses of the
    same mode: ``rate_limit_*`` is the FAILURE side (429 retries and the sleep
    they cost) and ``probe_*`` is the SUCCESS side (goodput, served
    concurrency, tokens). Both are needed — the 429 census is
    schedule-dependent and, for some providers, silent, while goodput and
    latency are not.

    ``latency_total_s`` is trial wall time and therefore *includes* the probe's
    429 sleep — a non-zero ``rate_limit_wait_s`` is the mechanical marker that
    this trial's latency figures are not comparable with a normal run's.

    The flat counters of both censuses are the sum across
    ``rate_limit_by_role_model``, whose rows keep the agent's and the
    simulator's numbers apart (they are different models). ``probe_buckets``
    resolves the same rows into fixed-width absolute-time windows, because a
    cumulative total hides non-stationarity — measured goodput decays at CONSTANT
    offered concurrency (``docs/OUTPUT_FORMAT.md`` § Field observations).

    A single trial's goodput *ratio* is meaningless — goodput is a run-level
    quantity. Every field here is therefore an additive count over an
    absolute-time window, never a rate: a consumer sums the counts across
    trials and legs first, and forms the ratio last. See
    ``docs/OUTPUT_FORMAT.md`` § ``probe_*`` for the arithmetic.
    """

    model_config = {"extra": "forbid"}

    latency_total_s: float = 0.0
    turns: int = 0
    api_calls: int = 0
    usage: Usage = Field(default_factory=Usage)
    openrouter_generation_ids: list[str] = Field(default_factory=list)
    """Every OpenRouter generation id the trial's agent calls returned, in call order.

    Each id resolves at ``https://openrouter.ai/api/v1/generation?id=<id>`` to
    the upstream provider that actually served that call, so a trial whose
    result is suspected of being a routing artefact can be checked after the
    fact instead of re-run. Empty on a trial that never reached OpenRouter.

    A list, not a scalar: OpenRouter routes each request independently, so one
    trial's turns can be served by different upstreams. Shorter than
    ``api_calls`` whenever a call was served off an unrouted provider. The
    per-call view — which id belongs to which turn — is
    ``usage.calls[*].openrouter_generation_id``. The two are **not** positionally
    aligned and this list is not derivable from ``usage.calls``: a response
    that carried an ``x-generation-id`` header but no usage block contributes
    an id here and no ``ProviderRawCall`` there. Consumers that need per-call
    attribution read ``usage.calls``; consumers that need "did this trial
    reach OpenRouter at all" read this list."""

    cost_usd: float | None = None
    tool_calls: int = 0
    tool_success_rate: float = 0.0
    stuck_detected: bool = False
    tool_usage: list[ToolUsage] = Field(default_factory=list)
    rate_limit_retries: int = 0
    rate_limit_wait_s: float = 0.0
    rate_limit_first_ts: datetime | None = None
    rate_limit_last_ts: datetime | None = None
    rate_limit_by_role_model: list[RateLimitProbeRoleMetrics] = Field(default_factory=list)
    probe_successful_calls: int = 0
    """Successful LLM calls across every role in the trial (probe mode only)."""

    probe_success_duration_s: float = 0.0
    """Summed client-observed duration of those successful calls."""

    probe_prompt_tokens: int = 0
    """Prompt tokens the provider reported for those successful calls."""

    probe_completion_tokens: int = 0
    """Completion tokens the provider reported for those successful calls."""

    probe_bucket_width_s: int = 0
    """Width of a ``probe_buckets`` window, seconds. ``0`` when not probing.

    Emitted so a consumer never has to assume the width the run was configured
    with; it is the denominator of every per-window rate."""

    probe_dropped_buckets: int = 0
    """``(role, model, window)`` rows refused by ``RateLimitProbeConfig.max_buckets``.

    Rows, not windows — the same unit as the cap, which bounds the number of
    ``probe_buckets`` rows. One window lost on a two-role trial counts **2**,
    because two series each lost a row.

    Non-zero means ``probe_buckets`` is a truncated *prefix* of the trial and
    only the flat / per-``(role, model)`` totals are complete. Never silent."""

    probe_buckets: list[RateLimitProbeBucketMetrics] = Field(default_factory=list)
    """Per-``(role, model, absolute window)`` throughput, sorted by window then
    role then model. See :class:`RateLimitProbeBucketMetrics`."""

    @field_validator("usage", mode="before")
    @classmethod
    def _validate_usage(cls, value: Any) -> Any:
        """Accept ``Usage`` instances or their dict round-trip form.

        YAML deserialisation lands here with a plain ``dict``; the runtime
        accumulation path lands here with a :class:`Usage` instance
        (``default_factory=Usage`` / ``metrics.usage + result.usage``).
        Anything else is rejected to surface serialisation bugs instead of
        masking them. ``calls`` entries are coerced from dicts back into
        :class:`ProviderRawCall`, with ``cost_source`` validated against
        the :data:`CostSource` literal.
        """
        if value is None:
            return Usage()
        if isinstance(value, Usage):
            return value
        if isinstance(value, dict):
            # Drop unknown keys defensively so we can evolve Usage without
            # crashing on historical YAML — but keep ``provider_raw`` as-is.
            known_fields = {
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "provider_raw",
                "calls",
            }
            filtered = {k: v for k, v in value.items() if k in known_fields}
            if "calls" in filtered:
                filtered["calls"] = _coerce_calls(filtered["calls"])
            return Usage(**filtered)
        raise TypeError(f"Metrics.usage must be Usage | dict | None, got {type(value).__name__}")

    @field_serializer("usage")
    def _serialize_usage(self, value: Usage) -> dict[str, Any]:
        """Emit ``usage`` as a plain dict so ``metrics.yaml`` is readable.

        Pydantic's native dataclass serialisation works, but wrapping it here
        keeps the output-writer contract explicit and guarantees
        ``model_dump(mode="json")`` always yields a ``dict``, which the
        trajectory writer requires. Per-call records are emitted via
        :func:`dataclasses.asdict` so future :class:`ProviderRawCall` fields
        are surfaced automatically.
        """
        return {
            "prompt_tokens": value.prompt_tokens,
            "completion_tokens": value.completion_tokens,
            "reasoning_tokens": value.reasoning_tokens,
            "cached_tokens": value.cached_tokens,
            "cache_creation_input_tokens": value.cache_creation_input_tokens,
            "cache_read_input_tokens": value.cache_read_input_tokens,
            "provider_raw": dict(value.provider_raw),
            "calls": [dataclasses.asdict(call) for call in value.calls],
        }


REPLY_DEFECT_EXCERPT_MAX_CHARS = 200
"""Longest matched span a :class:`ReplyDefect` may carry, in characters.

The excerpt is model-authored text persisted into the trial bundle as the
evidence for a discarded reply, so it is bounded rather than free-length."""


class ReplyDefect(BaseModel):
    """One detector's finding on one generated user reply.

    Produced by the user-reply guard
    (:mod:`tolokaforge.core.actors.reply_guard`) and carried on the
    :class:`~tolokaforge.core.llm.client.GenerationResult` of the turn whose
    earlier attempts it describes.
    """

    model_config = {"extra": "forbid"}

    detector: str
    """``name`` of the detector that produced this finding."""

    reason: str
    """Stable machine code for the shape that was matched, e.g.
    ``self_identified_as_model``. Consumers group on this, not on
    :attr:`excerpt`."""

    excerpt: str = Field(max_length=REPLY_DEFECT_EXCERPT_MAX_CHARS)
    """The matched span of the discarded reply, truncated to the bound.

    ``max_length`` documents the bound in the schema; the before-validator
    below applies it, so no value ever reaches the constraint too long."""

    @field_validator("excerpt", mode="before")
    @classmethod
    def _bound_excerpt(cls, value: Any) -> Any:
        """Truncate rather than refuse: an overlong span is still evidence, and
        refusing it would take the reply out of the guard as a crash."""
        return value[:REPLY_DEFECT_EXCERPT_MAX_CHARS] if isinstance(value, str) else value


class UserReplyOutcome(str, Enum):
    """The reply guard's verdict on one dispatched user turn."""

    DELIVERED = "delivered"  # A later attempt passed the guard
    REFUSED = "refused"  # The attempt budget was spent; the trial fails


class UserReplyGuardEvent(BaseModel):
    """What one user turn cost when it cost more than one generation.

    A turn whose first generation passed the guard records no event, so a trial
    that never broke frame carries an empty list rather than a row per turn.
    """

    model_config = {"extra": "forbid"}

    message_index: int
    """Position in :attr:`Trajectory.messages` this turn was dispatched at.

    The index the turn's USER message occupies, *or would have occupied had one
    been appended*. A reply the guard accepts can still be a bare ``###STOP###``,
    which terminates the dialogue and puts the loop's SYSTEM termination message
    at this index instead; a refused turn does the same. Nothing may assume the
    message here is user-role."""

    outcome: UserReplyOutcome
    """The guard's verdict, not the runner's subsequent disposition of the reply."""

    rejected: list[ReplyDefect] = Field(min_length=1)
    """One entry per discarded attempt, in the order they were generated.

    Never empty: a turn that discarded nothing records no event at all."""


class Trajectory(BaseModel):
    """Complete trial trajectory.

    Carries only the message trace + status + metrics. The agent's system
    prompt and the user simulator's system prompt live in a sibling
    ``prompts.yaml`` artifact (written by
    :class:`~tolokaforge.core.output.artifacts.FileArtifactWriter.write_prompts`)
    so this file stays small and easy to scan during analysis. Tool
    schemas similarly live in ``tools_schemas.yaml``. Every trial bundle
    is self-contained — no cross-trial sidecars.
    """

    task_id: str
    trial_index: int
    start_ts: datetime
    end_ts: datetime
    status: TrialStatus = TrialStatus.COMPLETED
    termination_reason: TerminationReason | None = None
    # How message index 0 was delivered, so an analyst can partition trials
    # into authored-opener and generated-opener without re-reading the task
    # pack. ``None`` is the honest state for a trial whose turn loop never
    # bootstrapped, and for a bundle written before the key existed.
    first_user_message_source: FirstUserMessageSource | None = None
    messages: list[Message]
    # One entry per user turn that cost more than one generation, so a run's
    # flagged replies — and the trials refused because none was clean —
    # are diagnosable from the bundle. Empty on a trial where every user turn
    # passed the guard on its first attempt.
    user_reply_guard_events: list[UserReplyGuardEvent] = Field(default_factory=list)
    final_env_state: dict[str, Any] = Field(default_factory=dict)
    metrics: Metrics = Field(default_factory=Metrics)
    # The trial's ordered tool-call record, one entry per call across every
    # executor. Persisted as the ``tool_log.yaml`` sidecar, not as a key on
    # ``trajectory.yaml`` — see docs/OUTPUT_FORMAT.md.
    tool_log: list[RecordedToolCall] = Field(default_factory=list)
    grade: Grade | None = None
    # Grading ran for this trial and could not produce a verdict; this is the
    # reason it gave. ``None`` means grading either succeeded or was correctly
    # not attempted — it does not distinguish those two, ``grade`` does.
    grading_error: str | None = None
    # Which point of the provisioning lifecycle raised ``ProvisionError``.
    # Non-``None`` iff ``termination_reason == PROVISION_ERROR``; ``None`` on
    # every other trial including bundles the executor writes for a failure
    # whose stage the raise site did not name (the closed set at
    # ``ProvisionStage`` is exhaustive over the raise sites, so this is a
    # defensive default, not a documented gap).
    provision_stage: ProvisionStage | None = None
    # Grade-bundle produce outcome for this trial. ``None`` iff snapshot
    # mode is disabled for the run OR the trial ended before grading.
    # Populated by the orchestrator's trial-end producer seam after
    # ``_grade`` completes when ``grader.snapshot.enabled=true``.
    snapshot_status: SnapshotStatus | None = None
    # Monotonic integer stamped on every trajectory; bumped whenever the
    # simulator prompt shape or the conversation context the simulator sees
    # is revised so that downstream analytics can gate comparisons across
    # runs. Stays on Trajectory because it's metadata about the
    # message-trace shape, not the prompt itself.
    simulator_schema_version: int = 4

    @model_validator(mode="after")
    def _reject_graded_and_ungradeable(self) -> Self:
        """Refuse a value claiming both a verdict and a reason there is none.

        ``Trajectory`` does not enable ``validate_assignment``, so this fires on
        construction and ``model_validate`` but stays silent on attribute
        assignment — which is how the conductor sets both fields. It is a
        deserialisation-boundary guard: a contradictory trajectory cannot be
        read back off disk or built from a dict.
        """
        if self.grading_error is not None and self.grade is not None:
            raise ValueError(
                f"trajectory {self.task_id!r}:{self.trial_index} carries both a grade "
                f"(score {self.grade.score}) and grading_error {self.grading_error!r}. "
                "grading_error records that no verdict could be computed, so a grade "
                "alongside it describes a trial two different ways."
            )
        return self
