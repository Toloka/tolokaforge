"""Capability enum for the per-model capability-driven integration suite.

Every LLM capability probe parametrises over
:data:`tolokaforge.testing.certify.ALL_MODELS`. Each probe
asserts ONE :class:`Capability` — when the certificate's ``required``
set includes the capability, the probe must pass against the live
provider; when ``known_unsupported`` includes it, the probe auto-skips
with an explanatory message; when neither mentions it, the probe ALSO
auto-skips with ``capability not declared`` — forcing honest
certificates.

Design goals:

1. **Single source of truth.** Capabilities enumerated once; tests never
   hard-code provider / model strings.
2. **Honest declarations.** Overlap between ``required`` and
   ``known_unsupported`` is a config bug — caught at dataclass
   construction time (see :meth:`~tolokaforge.testing.certify.certificate.ModelCertificate.__post_init__`).
3. **No leaked abstractions.** Tests built on top of this module speak
   :class:`~tolokaforge.core.llm.client.LLMClient` +
   :class:`~tolokaforge.core.llm.reasoning.ReasoningConfig` +
   :class:`~tolokaforge.core.llm.usage.Usage` only — never raw provider
   payloads.

See also :mod:`tolokaforge_models.certificates.registry` for the concrete
``ALL_MODELS`` tuple, exposed at :data:`tolokaforge.testing.certify.ALL_MODELS`.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["Capability"]


class Capability(str, Enum):
    """Every behavioural contract a :class:`~tolokaforge.core.llm.client.LLMClient`
    may or may not support against a given live provider.

    Each value maps 1:1 to a ``test_<value>.py`` file under
    :mod:`tolokaforge.testing.certify.suite`. Adding a new capability
    requires:

    * Adding the enum member here.
    * Shipping a new ``test_<value>.py`` under
      ``tolokaforge/testing/certify/suite/`` that uses
      ``skip_unless_capability_declared`` to gate the body.
    * Declaring the capability on every
      :class:`ModelCertificate` (in either ``required`` or
      ``known_unsupported``) — the canonical capability-registry test
      rejects orphan capabilities.
    """

    BASIC_COMPLETION = "basic_completion"
    """Model returns non-empty text for a simple user turn."""

    SIMPLE_TOOL_CALL = "simple_tool_call"
    """Model emits a structured tool call when exactly one tool is offered."""

    MULTI_TURN_TOOL_USE = "multi_turn_tool_use"
    """Two-turn flow: model calls a tool, observes the tool result, and
    either calls another tool or produces a final answer."""

    MULTI_TURN_ERROR_RECOVERY = "multi_turn_error_recovery"
    """After a tool call fails with an explicit error message naming the
    missing field, the model corrects the call on the next turn rather
    than re-emitting the identical broken call.

    Distinct from :attr:`MULTI_TURN_TOOL_USE`, which only asserts that
    the model continues the dialogue after a successful tool result.
    This capability stresses the **error-feedback channel**: the runtime
    validation message (eg ``missing_required_field: either contact_id
    or contact_email must be provided``) appears in the tool message,
    and a passing model treats that text as a signal — looking up the
    missing field in the user's original message and including it on
    the retry.

    Surfaced as a real eval failure mode: grok-4.3 retried the same broken
    ``salesforce_create_case`` call 5+ times in a row after the tool
    explicitly said which field was needed, even though the missing
    value (an email) was sitting in the user's original message. The
    earlier integration tests passed for grok-4.3 because none of them
    probe the error-feedback surface — they all run happy-path or
    schema-shape checks. This capability closes that gap."""

    DICT_MAP_TOOL_CALL = "dict_map_tool_call"
    """Typed ``Dict[str, T]`` tool parameters survive round-trip — the
    model doesn't stringify, flatten, or silently drop the dict."""

    ENUM_SLASH_TOLERANCE = "enum_slash_tolerance"
    """Provider accepts a tool schema whose enum values contain ``/``
    (forward slash) — eg ``{"enum": ["income/salary verification letter",
    "pay stubs"]}``.

    Verified 2026-05-14: xAI's grok-4.3 endpoint rejects such schemas
    with the opaque ``OpenrouterException - Invalid arguments passed to
    the model``. Bisected to a single enum value in
    a bank/HR evaluation domain — replacing the ``/`` with a space
    makes grok-4.3 accept the same schema; passing only the ``/``-
    containing value (and nothing else) makes it reject. Grok-4 (and
    every other registered model — OpenAI GPT-5.x, Anthropic Opus
    4.6/4.7, Qwen 3.6, Kimi K2.6, DeepSeek V4, MiMo V2.5, Gemini Flash
    + Gemini 3.1 Pro) accepts the identical payload.

    Pinned as a capability because this is a model-route quirk whose
    fix is presumed but not announced. The paired ratchet test
    (:mod:`test_enum_slash_tolerance_unsupported_ratchet`) probes the
    ``known_unsupported`` cert; when xAI relaxes the validator and the
    ratchet trips, that's the signal to flip grok-4.3 to ``required``.

    Search for prior art at: https://github.com/anomalyco/opencode/issues/23704
    (similar Grok schema-strictness pattern on ``additionalProperties``)
    and https://github.com/agno-agi/agno/issues/7455."""

    RE2_PATTERN_TOLERANCE = "re2_pattern_tolerance"
    """Provider accepts a tool schema whose ``pattern`` field contains
    a RE2-incompatible regex (lookarounds ``(?!``/``(?=``/``(?<!``/
    ``(?<=`` or backreferences ``\\1``..``\\9``) — eg the Pydantic-emitted
    ``Decimal``-string idiom
    ``"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$"``.

    Verified live 2026-05-20 via direct OpenRouter REST probe with a
    handcrafted ``Optional[str]`` schema carrying the lookahead-bearing
    pattern:

    * **xAI grok-4.3** rejects with the opaque
      ``OpenrouterException - Invalid arguments passed to the model``.
    * **openai/gpt-5.4 + gpt-5.5**, **anthropic/claude-opus-4.6/4.7**,
      **qwen/qwen3.6-plus**, **moonshotai/kimi-k2.6**,
      **deepseek/deepseek-v4-pro**, **xiaomi/mimo-v2.5-pro**, and the
      whole Gemini family all accept the identical payload.

    Companion to :attr:`ENUM_SLASH_TOLERANCE`: same xAI validator
    strictness, different surface. The
    :class:`~tolokaforge.core.llm.schema_sanitizer.StrictSchema`
    ``strip_re2_incompatible_patterns`` flag is the workaround — it
    strips RE2-incompat ``pattern`` values before the schema reaches
    grok-4.3. The day xAI relaxes the validator, the paired ratchet
    test (:mod:`test_re2_pattern_tolerance_unsupported_ratchet`)
    trips, and grok-4.3 flips from ``known_unsupported`` to
    ``required`` so we can drop the strip-trigger.

    NB: every provider EXCEPT grok-4.3 silently accepts the lookaround
    pattern even though most function-calling APIs document only a RE2
    subset. The strip used to be unconditionally applied "for safety"
    before this capability split landed; the probe above shows it is
    in fact xAI-specific."""

    DISCRIMINATED_UNION_TOOL_CALL = "discriminated_union_tool_call"
    """A multi-turn tool exercising a Pydantic discriminated union as the
    nested argument (``item: TicketCreate | UserCreate | …``) round-trips
    on **both** turns. Mirrors the OTS evaluation failure surface where
    open-weights models (mimo, deepseek-v4, Kimi) emit the union member
    as a JSON-encoded string instead of a native dict — the same
    stringification quirk that :attr:`DICT_MAP_TOOL_CALL` exercises on
    typed-dict-map parameters, but on a richer shape and across two
    turns so a single-turn stringification fluke can't pass while a
    real recovery contract is broken.

    Empirical motivation: mimo + deepseek were hitting 20–25 ``zendesk_create_item`` retry
    failures per trial because the wire payload was
    ``{"item": "{\\"subject\\": ...}"}``. The
    :class:`~tolokaforge.core.llm.response_policy.JsonCoerceResponse`
    policy recovers this; this capability gates the contract."""

    DECIMAL_FIELD_TOOL_CALL = "decimal_field_tool_call"
    """Pydantic-generated ``Decimal`` schemas don't trip the provider's
    regex validator (P1 — see
    [`AGENTS.md`](../../../AGENTS.md) gotcha #13)."""

    THINKING_EMITS_BLOCKS = "thinking_emits_blocks"
    """Provider surfaces structured thinking blocks, not just a
    concatenated reasoning summary (P4a/P4c)."""

    THINKING_REPLAY_ROUNDTRIP = "thinking_replay_roundtrip"
    """Signed thinking blocks from turn 1 are echoed back verbatim on
    turn 2 so interleaved thinking survives (P4b)."""

    PROMPT_CACHING = "prompt_caching"
    """A second identical call hits the provider-side prompt cache —
    observable via ``Usage.cache_read_input_tokens`` (P8).

    This is the **explicit-marker** contract: the harness attaches
    Anthropic ``cache_control`` markers via
    :class:`~tolokaforge.core.llm.cache_policy.AnthropicEphemeralCache`
    and asserts BOTH ``cache_creation_input_tokens > 0`` on call 1 AND
    ``cache_read_input_tokens > 0`` on call 2. Anthropic-only.
    See :attr:`IMPLICIT_PROMPT_CACHING` for the OpenAI / DeepSeek
    auto-cache surface, which doesn't expose a creation event."""

    IMPLICIT_PROMPT_CACHING = "implicit_prompt_caching"
    """A second identical call hits an upstream auto-cache without
    explicit ``cache_control`` markers from our side. Observable via
    ``Usage.cache_read_input_tokens`` on call 2.

    Distinct from :attr:`PROMPT_CACHING`: OpenAI / DeepSeek routes on
    OpenRouter auto-cache large prompts but do NOT surface a separate
    ``cache_creation_input_tokens`` event on the cold write (the
    creation is implicit on the provider side, not exposed via the
    response usage block). Asserting cache_creation > 0 on those routes
    fails — so this capability omits that assertion and only checks
    cache_read on call 2.

    Evidence motivating the split: OpenRouter routes to ``openai/*``
    and ``deepseek/*`` were reaching
    ~80% cache hit (via ``cached_tokens`` in the per-call usage),
    while ``xiaomi/mimo*`` reported 0 — the provider-side surface is
    route-specific."""

    USAGE_METRICS_POPULATED = "usage_metrics_populated"
    """Post-call ``result.usage`` carries non-zero ``prompt_tokens`` /
    ``completion_tokens`` and a non-empty ``provider_raw`` forensic
    block."""

    COST_USD_POPULATED = "cost_usd_populated"
    """Post-call ``result.cost_usd`` is a positive USD value.

    Sourced via the :func:`tolokaforge.core.llm.client._litellm_response_cost`
    priority ladder — litellm hidden_params, then ``litellm.completion_cost``,
    then the bundled ``pricing.json`` fallback. A live call that comes back
    with ``cost_usd is None`` means none of the three sources knew this
    model, which is a benchmarking-blocking pricing gap (fix by adding the
    model to ``tolokaforge/core/data/pricing.json`` or by upgrading
    litellm). Treated as a core capability — every benchmarked call MUST
    report cost."""

    TOOL_NAME_DISCIPLINE = "tool_name_discipline"
    """Model emits the *exact* registered tool name even when the name
    contains repeated underscore-separated segments
    (e.g. ``workday_api_workday_api_get_employee``).

    Concrete regression captured by this capability: some models
    substitute ``:`` for the duplicated ``_`` and emit names like
    ``workday_api:workday_api_get_employee`` that the harness rejects
    with ``Tool '…' not found in agent tools``. A model that produces
    *any* of ``:``, ``/``, or ``.`` separators when echoing a
    registered name fails this capability."""

    REQUIRED_FIELDS_COMPLETE = "required_fields_complete"
    """Model emits ALL JSON-Schema-``required`` fields when the user
    turn provides values for every one of them.

    Where :attr:`SIMPLE_TOOL_CALL` checks that *some* tool call
    happens, this capability checks the **completeness** of the tool
    call's arguments in a clean single-turn probe.

    Empirical scope: every registered model passes the single-turn
    version of this test. Field-omission failures observed in
    multi-turn evaluations are NOT a deterministic single-turn
    property — they are multi-turn / heavy-context emergent behaviour.
    A future multi-turn variant would be needed to gate that surface
    directly; this capability is the single-turn baseline.

    Treated as core (in :data:`_CORE_CAPABILITIES`) because every
    realistic modern function-calling model passes the single-turn
    contract — there is no model in our fleet for which the
    "both branches exercised" canonical invariant could be honestly
    satisfied via ``known_unsupported``."""

    UNSIGNED_THINKING_REPLAY = "unsigned_thinking_replay"
    """Reasoning text from turn 1 round-trips into turn 2's outgoing
    request payload, even when the provider does NOT carry per-block
    signatures.

    Distinct from :attr:`THINKING_REPLAY_ROUNDTRIP`, which asserts
    *signed* round-trip (the bytes Anthropic requires for interleaved
    thinking continuity). Some providers (e.g. Gemini's OpenRouter
    surface) emit ``reasoning.text`` blocks with no ``signature``
    field; the signed-replay test's ``assert signed`` check would
    never let those certs participate. This capability fills the gap:
    it asserts the *text* survives the round-trip via the
    appropriate codec's ``encode_for_replay`` path through
    ``LLMClient._convert_messages``, landing as
    ``{"role":"assistant", ..., "reasoning_details":[...]}`` on the
    next litellm call.

    Models with signature-bearing reasoning (Anthropic) declare this
    ``known_unsupported`` because their replay is governed by the
    signed contract. Models with summary-only reasoning (OpenAI / Qwen
    / Grok) likewise declare ``known_unsupported`` — there are no
    structured per-block payloads to round-trip."""

    LEXICAL_TOOL_INVENTION = "lexical_tool_invention"
    """Model does NOT fabricate a plausible-but-nonexistent tool name
    derived from the system-prompt vocabulary.

    Where ``TOOL_NAME_DISCIPLINE`` catches *structural* malformations
    (separator substitution), this capability catches *lexical
    invention* — the model reading a phrase like "the knowledge base"
    in the system prompt and inventing ``knowledge_base_search_policy``
    as if that were a real tool, when the registered tool is actually
    ``typesense_search_policy``.

    Test passes when the model selects one of the offered tools by its
    registered name; fails on any other name, with a specific check for
    the ``knowledge_base_*`` family that captures the documented
    failure pattern."""

    RECURSIVE_REF_TOOL_CALL = "recursive_ref_tool_call"
    """Model emits a recursive self-referential tool argument: a node
    whose ``children`` are themselves nodes (a recursive ``$ref`` cycle)
    emitted as a native nested object, not a stringified blob nor a
    flattened/truncated tree. Mirrors the JSON Schema Test Suite
    recursive-``$ref`` surface."""

    HETEROGENEOUS_ARRAY_TOOL_CALL = "heterogeneous_array_tool_call"
    """Model emits a polymorphic array argument: each element a
    discriminated union over distinct variants, with the correct
    per-element branch, not flattened to a single type nor stringified."""

    ALLOF_MERGE_TOOL_CALL = "allof_merge_tool_call"
    """Model emits an argument that satisfies an ``allOf`` composition,
    populating fields required by BOTH merged subschemas, not just one
    side of the merge."""

    PROGRESS_AFTER_SUCCESS = "progress_after_success"
    """Model advances past a successfully-completed tool call rather
    than re-issuing it.

    Where :attr:`MULTI_TURN_ERROR_RECOVERY` probes the model's reaction
    to a tool *failure*, this capability probes its reaction to a tool
    *success*. The failure surface: a tool call succeeds (the tool
    result clearly carries a created resource id, "ok", or similar
    confirmation), and the next user turn does NOT ask for a repeat
    — yet the model re-emits the same tool call with identical (or
    near-identical) arguments instead of acknowledging or moving on.

    Empirical motivation: grok-4.3 successfully called
    ``salesforce_create_case`` on turn 7, then re-called it with
    identical arguments on turns 9, 11, 13, … 37 (17 identical
    repetitions) before the stuck detector killed the trial. Tool
    response was a clean success on every repetition; the user said
    nothing about needing another case. The model simply did not
    treat success as a stop signal.

    Test passes when the model, on the follow-up turn, either:

    1. Emits NO tool call (text-only acknowledgment), OR
    2. Emits a tool call to a DIFFERENT tool (advancement), OR
    3. Emits a tool call to the same tool but with *substantively*
       different arguments (legitimate refinement).

    Test fails when the model re-emits the same tool name with
    arguments that are byte-identical or trivially-different from the
    prior successful call (case-insensitive subject match,
    same-id arguments).

    Distinct from :attr:`MULTI_TURN_ERROR_RECOVERY` because the prior
    turn did NOT fail — there's no error feedback to react to. The
    pathology is "treat success as a non-event"."""
