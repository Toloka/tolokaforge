"""Single source of truth for which models are under integration test.

To add a new model: append a :class:`~tests.integration.llm._capability.ModelCertificate`
to :data:`ALL_MODELS`. Capability tests auto-pick it up via parametrisation.
See [`docs/ADD_NEW_MODEL.md`](../../../docs/ADD_NEW_MODEL.md) for the full
contributor walkthrough.

Invariants enforced at module-import time:

* Every ``model_id`` is unique across :data:`ALL_MODELS` — duplicates raise
  :class:`RuntimeError`.
* Every ``model_id`` equals
  ``tolokaforge.core.output.artifacts.model_id_slug(provider, name)`` —
  ``tests/canonical/test_capability_registry.py`` owns the cross-module
  slug consistency check so runtime cost stays zero.

Capability coverage discipline is a canonical test responsibility — see
``tests/canonical/test_capability_registry.py`` for:

* No orphan capabilities (every ``Capability`` value is referenced).
* Every capability has at least one certificate in ``required`` AND at
  least one in ``known_unsupported`` — proves both branches of the
  auto-skip machinery are actually exercised.
"""

from __future__ import annotations

from ._capability import Capability as C
from ._capability import ModelCertificate as MC

__all__ = ["ALL_MODELS"]


_ALL: list[MC] = [
    # -----------------------------------------------------------------
    # OpenAI GPT-5 family — strict-schema preset + dict-map prompt hints.
    # Anthropic thinking blocks not exposed via OpenAI routing — GPT-5
    # surfaces only a ``reasoning_content`` summary (handled by
    # OpenAIReasoningCodec, not AnthropicReasoningCodec), so structured
    # ``THINKING_EMITS_BLOCKS`` + ``THINKING_REPLAY_ROUNDTRIP`` are
    # declared ``known_unsupported``. OpenAI has no ephemeral cache
    # wiring in presets — :class:`NoCache` is the default.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__openai_gpt-5.4",
        provider="openrouter",
        name="openai/gpt-5.4",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                # OpenRouter auto-caches large OpenAI prompts and
                # surfaces the cache-read event via
                # ``prompt_tokens_details.cached_tokens`` on call 2.
                # No explicit cache_control markers from our side.
                C.IMPLICIT_PROMPT_CACHING,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
            }
        ),
    ),
    MC(
        model_id="openrouter__openai_gpt-5.5",
        provider="openrouter",
        name="openai/gpt-5.5",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,  # Strict-schema Decimal collapse — see schema_sanitizer.
                C.IMPLICIT_PROMPT_CACHING,  # OpenRouter OpenAI route auto-caches.
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # Anthropic Claude family — structured thinking blocks + ephemeral
    # cache fully wired. Strict schema sanitisation is NOT applied to
    # Anthropic (preset keeps :class:`PassthroughSchema`), so dict-map /
    # Decimal tool calls piggy-back on Anthropic's native schema
    # handling rather than ``StrictSchema`` — declared
    # ``known_unsupported`` here because the capability test asserts
    # the strict trio's behaviour, not Anthropic's passthrough
    # equivalent.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__anthropic_claude-opus-4.6",
        provider="openrouter",
        name="anthropic/claude-opus-4.6",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.PROMPT_CACHING,  # ephemeral cache wired via cache_policy
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                # Anthropic emits SIGNED replay blocks, so the test
                # for unsigned text round-trip is not the right shape
                # for these certs — THINKING_REPLAY_ROUNDTRIP already
                # covers their replay contract.
                C.UNSIGNED_THINKING_REPLAY,
                # Anthropic caches via EXPLICIT ``cache_control`` markers
                # (PROMPT_CACHING above); there is no separate "implicit"
                # auto-cache surface to assert. Declared here for
                # registry completeness only.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    MC(
        model_id="openrouter__anthropic_claude-opus-4.7",
        provider="openrouter",
        name="anthropic/claude-opus-4.7",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.THINKING_EMITS_BLOCKS,  # signed thinking blocks + omitted-display variant
                C.THINKING_REPLAY_ROUNDTRIP,  # thinking-kwarg routing on direct-Anthropic transport
                C.PROMPT_CACHING,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                # Anthropic emits SIGNED replay blocks, so the test
                # for unsigned text round-trip is not the right shape
                # for these certs — THINKING_REPLAY_ROUNDTRIP already
                # covers their replay contract.
                C.UNSIGNED_THINKING_REPLAY,
                # Explicit cache only — see opus-4.6 comment above.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # Qwen — preset routes it through the same strict trio as GPT-5.
    # Reasoning surface is OpenAI-style summary only, no signed blocks,
    # so thinking capabilities are ``known_unsupported``.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__qwen_qwen3.6-plus",
        provider="openrouter",
        name="qwen/qwen3.6-plus",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,  # strict-schema dict-map array conversion
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,  # same strict sanitizer as GPT-5
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                # No implicit upstream cache surfaced on the OpenRouter
                # qwen/* routes.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # Qwen 3.7 Max — Alibaba's flagship Qwen 3.7 generation. Routes
    # through the same ``qwen`` preset as 3.6-plus (passthrough schema +
    # ``DictMapHints`` prompt policy + ``JsonCoerceResponse`` recovery +
    # OpenAI-style reasoning summary). Posture set by the ADD_NEW_MODEL.md
    # § 3 disciplined flow: every sibling-cert ``known_unsupported`` was
    # tentatively flipped to ``required`` and falsified live on
    # 2026-05-26 — the ``IMPLICIT_PROMPT_CACHING`` and
    # ``THINKING_EMITS_BLOCKS`` capabilities silently flipped vs the 3.6
    # baseline and stayed required after re-test. Remaining
    # ``known_unsupported`` entries each carry the live failure mode.
    MC(
        model_id="openrouter__qwen_qwen3.7-max",
        provider="openrouter",
        name="qwen/qwen3.7-max",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
                # Qwen 3.7 Max surfaces structured reasoning content
                # that ``test_thinking_emits_blocks`` accepts as
                # structured (verified live 2026-05-26). Sibling
                # qwen3.6-plus declares this known_unsupported; the
                # Max variant's richer reasoning surface promotes it.
                C.THINKING_EMITS_BLOCKS,
                # Verified live 2026-05-26 on a 2-call 8 k-token probe:
                # call 2 reports ``cached_tokens > 0`` via
                # ``prompt_tokens_details.cached_tokens`` without any
                # cache_control markers from our side. OpenRouter / Qwen
                # now auto-caches large prompts on this route.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
        known_unsupported=frozenset(
            {
                # Verified live 2026-05-26: ``test_decimal_field_tool_call``
                # ($42.50 charge probe) returns ``result.tool_calls == []``
                # — the model produces no tool call when a Pydantic
                # ``Decimal`` field is present. The ``qwen`` preset uses
                # ``PassthroughSchema`` (no Decimal regex strip), and
                # Qwen 3.7 Max appears to reject or bypass the schema
                # rather than emit a structured call. Sibling
                # qwen3.6-plus passes the same probe; this is a Max-only
                # regression worth flipping back to ``required`` if a
                # future release recovers.
                C.DECIMAL_FIELD_TOOL_CALL,
                # Anthropic-style ephemeral cache is not wired on the
                # ``qwen`` preset (cache_policy: none) — ``test_prompt_caching``
                # requires ``cache_creation_input_tokens > 0`` on call 1
                # which cannot happen without explicit cache_control
                # markers. Implicit caching is covered separately above.
                C.PROMPT_CACHING,
                # OpenAI-style reasoning summary only — no signed blocks
                # on the wire, so the signed-replay continuity contract
                # is not the right shape. Verified live 2026-05-26:
                # turn 1 emits zero ``signed`` reasoning entries.
                C.THINKING_REPLAY_ROUNDTRIP,
                # The OpenAI reasoning codec does not currently surface
                # ``reasoning_details`` on the outgoing assistant
                # message — the encode_for_replay path is a no-op for
                # this preset. Verified live 2026-05-26: turn-2 request
                # payload omits ``reasoning_details`` entirely. A
                # future bespoke Qwen reasoning codec would let this
                # flip to ``required``.
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # xAI Grok — strict schema sanitiser + array-dict-map response
    # policy + OpenAI-style reasoning summary (no signed blocks).
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__x-ai_grok-4",
        provider="openrouter",
        name="x-ai/grok-4",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                # No implicit upstream cache surfaced on the OpenRouter
                # x-ai/grok-* routes.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # Grok-4.3 routes through the same ``xai_grok`` preset as Grok-4
    # (strict schema sanitiser + array_dict_map response policy +
    # OpenAI-style reasoning summary). xAI guarantees strict JSON-schema
    # conformance for tool arguments up to the documented limits, so the
    # strict trio (DICT_MAP / DECIMAL / REQUIRED_FIELDS_COMPLETE) is
    # required. No signed thinking blocks, no provider-side prompt cache
    # surfaced via OpenRouter for this family.
    MC(
        model_id="openrouter__x-ai_grok-4.3",
        provider="openrouter",
        name="x-ai/grok-4.3",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
                # MULTI_TURN_ERROR_RECOVERY: grok-4.3 fails this surface
                # *unreliably* (live probe 2026-05-14: 5/6 trials
                # correctly populated contact_email; 1/6 emitted the
                # self-referential hallucination
                # ``contact_email="contact_email"`` — putting the literal
                # field name as the value). 83 % isn't "reliably
                # recovers" — under the contract this capability gates,
                # the model would fail ~17 % of the time in production.
                # Promote to required only after grok stops the
                # field-name-as-value hallucination consistently.
                # Also observed: grok looping on the identical broken
                # call when the original message format was richer;
                # that's the same root cause amplified.
                C.MULTI_TURN_ERROR_RECOVERY,
                # ENUM_SLASH_TOLERANCE: xAI's grok-4.3 endpoint
                # rejects tool schemas whose enum values contain
                # ``/`` with the opaque error
                # ``OpenrouterException - Invalid arguments passed
                # to the model``. Verified locally 2026-05-14:
                # bisected to a single enum value
                # (``income/salary verification letter``) on a
                # single tool in a bank/HR evaluation domain;
                # replacing the ``/`` with any non-slash separator
                # makes the same payload accepted. grok-4 accepts
                # the slash; only grok-4.3's tightened validator
                # rejects. Paired ratchet test fires when xAI
                # relaxes this so we know to flip to required.
                C.ENUM_SLASH_TOLERANCE,
                # RE2_PATTERN_TOLERANCE: companion to ENUM_SLASH —
                # same xAI validator strictness, different surface.
                # grok-4.3 rejects schemas whose ``pattern`` field
                # contains a RE2-incompatible regex (lookarounds /
                # backreferences) with the same opaque
                # ``Invalid arguments passed to the model`` error.
                # Verified live 2026-05-20: direct REST probe with
                # the Pydantic-Decimal-string idiom pattern
                # ``"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$"`` returned
                # the rejection; the same pattern is accepted by
                # gpt-5.4 / gpt-5.5 (OpenAI's previous strictness on
                # this has been relaxed), the Gemini family, and
                # every other registered route.
                # ``StrictSchema.strip_re2_incompatible_patterns``
                # is the workaround — strips the offending pattern
                # before the schema reaches grok-4.3. Paired ratchet
                # test fires when xAI relaxes the validator and the
                # strip can be retired.
                C.RE2_PATTERN_TOLERANCE,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # Google Gemini family — routed through the named ``gemini`` preset
    # (see ``model_presets.yaml``) which adds ``GeminiReasoningCodec``
    # for the OpenRouter ``provider_specific_fields.reasoning_details``
    # envelope. Two block types are decoded: ``reasoning.text`` (Pro
    # lineage, readable thinking) and ``reasoning.encrypted`` (Flash
    # lineage, opaque payload).
    #
    # ``TOOL_NAME_DISCIPLINE`` and ``LEXICAL_TOOL_INVENTION`` are
    # asymmetric across Flash and 3.1 Pro: Flash echoes registered
    # tool names verbatim while 3.1 Pro is observed to substitute ``:``
    # for repeated ``_`` segments and to fabricate names like
    # ``knowledge_base_search_policy`` in a ``known_unsupported``
    # ratchet — the live tests reproduce both regressions and the
    # certificates record them as falsifiable.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__google_gemini-3-flash-preview",
        provider="openrouter",
        name="google/gemini-3-flash-preview",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                # Live-verified 2026-05-14: 4/4 probe + capability test
                # pass — Flash reliably reads the tool-error message and
                # populates the missing contact_email from the original
                # user request.
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                # Verified live: Gemini 3 Flash echoes registered
                # tool names verbatim — no separator substitution.
                C.TOOL_NAME_DISCIPLINE,
                # Verified live: Flash does not invent
                # ``knowledge_base_*`` style tool names from the
                # system-prompt vocabulary.
                C.LEXICAL_TOOL_INVENTION,
                # Single-turn field completeness baseline — verified
                # live.
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
                # Decimal values round-trip as native floats under the
                # default ``passthrough`` schema for Gemini Flash —
                # no need for the StrictSchema collapse that GPT-5 /
                # Grok / Qwen require.
                C.DECIMAL_FIELD_TOOL_CALL,
                # Flash lineage round-trips registered field names
                # correctly under the ``GeminiSchema`` routing
                # (inlines $ref, converts dict-map → array, flattens
                # oneOf discriminated unions). Verified live
                # 2026-05-20. Without this routing the model emits
                # description-derived names like ``quantity``
                # instead of registered ``qty`` because Gemini's
                # tool spec doesn't support the constructs Pydantic
                # generates by default. See gotcha #21 in
                # AGENTS.md.
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
            }
        ),
        known_unsupported=frozenset(
            {
                # Flash emits ``reasoning.encrypted`` blocks only — no
                # readable text to surface, so the structured-blocks
                # capability has nothing to assert on. The codec still
                # preserves the encrypted payload byte-for-byte
                # (verified by ``test_replay_byte_preserves_encrypted_payload``).
                C.THINKING_EMITS_BLOCKS,
                # Replay paths assume signature-bearing blocks
                # (Anthropic) or readable text blocks (Pro lineage);
                # neither applies to encrypted-only Flash. A future
                # ``ENCRYPTED_THINKING_REPLAY`` capability would cover
                # this surface.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No cache policy is wired for Gemini.
                C.PROMPT_CACHING,
                # No implicit upstream cache surfaced on the OpenRouter
                # google/gemini-* routes (Gemini's own context-caching
                # API is not exposed through OpenRouter at this time).
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # Gemini 3.5 Flash — GA Flash-tier successor to 3-flash-preview.
    # Shares the Flash-lineage ``reasoning.encrypted`` envelope, so the
    # same ``gemini`` preset routes it (no new preset entry needed). The
    # certificate mirrors the 3-flash-preview posture as the starting
    # baseline; capability deviations will be ratcheted in based on
    # live ``pytest tests/integration/llm/ -k gemini-3.5-flash`` output.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__google_gemini-3.5-flash",
        provider="openrouter",
        name="google/gemini-3.5-flash",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
                C.DECIMAL_FIELD_TOOL_CALL,
                # 3.5 Flash round-trips registered field names
                # correctly under the ``GeminiSchema`` routing
                # (inlines $ref, dict-map → array, flattens oneOf
                # discriminated unions). Verified live 2026-05-20.
                # Same root cause as gemini-3-flash-preview — the
                # earlier "Flash renames qty → quantity" finding
                # was actually schema-loss in unsupported JSON-Schema
                # constructs, not a model behaviour. See gotcha #21
                # in AGENTS.md.
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
            }
        ),
        known_unsupported=frozenset(
            {
                # Flash lineage emits ``reasoning.encrypted`` blocks only
                # — no readable text to surface and no signatures to
                # replay. Codec preserves the encrypted payload
                # byte-for-byte (verified by
                # ``test_replay_byte_preserves_encrypted_payload``).
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No explicit ``cache_control`` markers wired in the
                # ``gemini`` preset.
                C.PROMPT_CACHING,
                # Synthetic-vs-production asymmetry: the 8 k-token probe
                # in ``test_implicit_prompt_caching`` returns
                # ``cached_tokens: 0``, but production evals (13 k+ token
                # prompts) show steady ~16 k
                # cache_read_input_tokens per call from turn 3 onward
                # (56 % effective hit rate). Gemini's implicit context
                # cache appears to require a higher minimum-prompt-size
                # threshold than the OpenAI / DeepSeek routes the test
                # was calibrated against. A larger-probe variant of the
                # capability test would let us flip this to ``required``;
                # until then the synthetic contract is unmet so we
                # honestly declare ``known_unsupported``.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # Moonshot Kimi K2.6 / DeepSeek V4 Pro / Xiaomi MiMo V2.5 Pro —
    # routed via the ``openrouter_dict_stringify_recovery`` preset
    # (passthrough schema + DictMapHints + JsonCoerceResponse + OpenAI
    # reasoning codec). These OpenRouter routes are OpenAI-API-compatible
    # native function-calling chat models that occasionally stringify
    # nested container arguments — same failure mode the qwen preset
    # already handles. Adding the preset turned the eval-time
    # ``zendesk_create_item`` retry loop from 20–25 failed attempts per trial
    # into 0; ``DICT_MAP_TOOL_CALL`` is now ``required`` for all three
    # routes since the recovery policy makes the contract real.
    #
    # ``DECIMAL_FIELD_TOOL_CALL`` stays ``known_unsupported`` — that
    # contract pins ``StrictSchema``-specific Decimal coercion, which
    # is intentionally NOT wired for these open-weights routes
    # (passthrough schema lets the native shape through, same as Qwen).
    #
    # Thinking blocks stay ``known_unsupported`` — the OpenAI reasoning
    # codec surfaces a flat ``reasoning_content`` summary, not signed /
    # replayable thinking blocks. Add a bespoke codec + fixture (per
    # ADD_NEW_MODEL § 5) before promoting any THINKING_* capability.
    #
    # ``PROMPT_CACHING`` (the explicit Anthropic-ephemeral contract)
    # stays ``known_unsupported`` because we don't attach
    # ``cache_control`` markers; ``IMPLICIT_PROMPT_CACHING`` covers the
    # OpenRouter-side auto-caching surface separately (only DeepSeek's
    # route auto-caches; Kimi / MiMo do not).
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__moonshotai_kimi-k2.6",
        provider="openrouter",
        name="moonshotai/kimi-k2.6",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
                # Kimi K2.6 emits the discriminated-union arg shape
                # CORRECTLY as a native dict on turn 1 (no
                # stringification — the failure surface this
                # capability was built for) but does NOT follow the
                # turn-2 instruction to switch to a different variant.
                # Verified live 2026-05-14: turn 1 wrote
                # ``table=tickets`` with ``item.kind=ticket`` cleanly,
                # turn 2 reused the same ticket-shaped call instead of
                # producing the requested ``table=comments`` /
                # ``item.kind=comment``. Wire shape contract passes;
                # multi-turn intent-following gap is separate model
                # behaviour — a future single-turn variant of the test
                # would let kimi flip to ``required``.
                C.DISCRIMINATED_UNION_TOOL_CALL,
            }
        ),
    ),
    MC(
        model_id="openrouter__deepseek_deepseek-v4-pro",
        provider="openrouter",
        name="deepseek/deepseek-v4-pro",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                # DeepSeek's OpenRouter route caches statistically in
                # aggregate (~80% cache hits over many large-prompt
                # calls in production evals), but a clean 2-call probe
                # with an 8 k-token prompt does NOT reliably reproduce
                # it — verified live 2026-05-14, ``cached_tokens=0`` on
                # both calls. The cache appears to need either a larger
                # prefix volume or many priming calls before it
                # registers. Until we have a reproducible per-call
                # contract, the safe declaration is ``known_unsupported``
                # paired with the ratchet test in
                # ``test_implicit_prompt_caching_unsupported_ratchet`` —
                # the day a 2-call probe does observe caching, the
                # ratchet fails and forces promotion to ``required``.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    MC(
        model_id="openrouter__xiaomi_mimo-v2.5-pro",
        provider="openrouter",
        name="xiaomi/mimo-v2.5-pro",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # OpenRouter / xiaomi enabled implicit prompt caching for
                # this route between 2026-05-14 14:29 and 16:20 UTC —
                # earlier evals saw cached_tokens=0 across every
                # mimo call, but a live
                # 2-call 8 k-prompt probe at 16:20 reports
                # ``cache_read_input_tokens=8192/8254`` (99% cached) on
                # call 2 with a clear cost reduction. The ratchet test
                # forced this promotion.
                C.IMPLICIT_PROMPT_CACHING,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
            }
        ),
    ),
    MC(
        model_id="openrouter__google_gemini-3.1-pro-preview",
        provider="openrouter",
        name="google/gemini-3.1-pro-preview",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                # GeminiReasoningCodec extracts readable
                # ``reasoning.text`` blocks from
                # ``provider_specific_fields.reasoning_details`` for
                # the Pro lineage. Replay is NOT covered by this
                # capability — see ``UNSIGNED_THINKING_REPLAY`` below.
                C.THINKING_EMITS_BLOCKS,
                # The Gemini codec's ``encode_for_replay`` runs on
                # the outgoing assistant message dict and ships the
                # turn-1 ``reasoning.text`` blocks back as
                # ``reasoning_details`` on turn 2. Verified by
                # ``test_unsigned_thinking_replay.py`` end-to-end via
                # the ``litellm.completion`` mock-and-capture trick.
                C.UNSIGNED_THINKING_REPLAY,
                # Round-trip cleanly under ``GeminiSchema`` routing
                # — verified live 2026-05-20. The pre-2026-05-20
                # ``DISCRIMINATED_UNION_TOOL_CALL`` failures (Pro
                # emitting ``title`` instead of ``subject``) were
                # caused by sending Gemini ``oneOf`` /
                # ``discriminator`` keywords its tool spec doesn't
                # support; ``GeminiSchema`` flattens those into a
                # property union.
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                # Single-turn baseline: Gemini 3.1 Pro PASSES the
                # required-fields test. Field-omission failures
                # observed in multi-turn evaluations do not reproduce
                # in this synthetic single-turn probe — they are
                # multi-turn / heavy-context emergent behaviour. A
                # future multi-turn variant of the test would be
                # needed to gate that surface directly.
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                # Gemini does NOT emit per-block ``signature`` fields
                # — the existing replay test asserts at least one
                # signed block exists on turn 1. Without signatures,
                # signed-replay continuity cannot be exercised by this
                # capability. The codec still round-trips
                # reasoning_details bytes verbatim (see
                # GeminiReasoningCodec unit tests); a future capability
                # for unsigned-replay continuity would land here.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.PROMPT_CACHING,
                # Open regression: 3.1 Pro substitutes ``:`` for the
                # duplicated ``_`` segment and emits names like
                # ``workday_api:workday_api_get_employee`` instead of
                # the registered ``workday_api_workday_api_get_employee``.
                # The live ``test_tool_name_discipline.py`` reproduces
                # this. Declared known_unsupported so the test skips
                # loudly on this certificate; flipping to ``required``
                # is the falsifiable ratchet for the future fix.
                C.TOOL_NAME_DISCIPLINE,
                # Open regression: 3.1 Pro fabricates
                # ``knowledge_base_search_policy`` (and similar
                # ``knowledge_base_*`` names) when the system prompt
                # mentions "the knowledge base" — even when the
                # registered tool is named ``typesense_search_policy``.
                # The live ``test_lexical_tool_invention.py``
                # reproduces this. Symmetric posture to
                # TOOL_NAME_DISCIPLINE — both are falsifiable open
                # regressions.
                C.LEXICAL_TOOL_INVENTION,
                # No implicit upstream cache surfaced on the OpenRouter
                # google/gemini-* routes — same as Flash.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
]


def _validate_unique_model_ids(certificates: list[MC]) -> None:
    """Raise :class:`RuntimeError` if any ``model_id`` is duplicated.

    Runs at module import so a dishonest copy-paste in :data:`ALL_MODELS`
    fails loudly on test collection rather than silently masking one
    certificate behind another during pytest parametrisation.
    """
    seen: dict[str, int] = {}
    for cert in certificates:
        seen[cert.model_id] = seen.get(cert.model_id, 0) + 1
    duplicates = sorted(slug for slug, count in seen.items() if count > 1)
    if duplicates:
        raise RuntimeError(
            f"Duplicate model_id in ALL_MODELS: {duplicates}. "
            "Every certificate must have a unique filesystem-safe slug."
        )


_validate_unique_model_ids(_ALL)


ALL_MODELS: tuple[MC, ...] = tuple(_ALL)
"""Immutable ordered tuple of every :class:`ModelCertificate` under test.

Deterministic import order is guaranteed — the canonical
capability-registry test pins this so test-collection IDs stay stable
across runs.
"""
