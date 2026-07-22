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

import os

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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # OpenAI GPT-5.6 family (Terra + Sol tiers) — routed through the same
    # ``openai_gpt5`` preset as gpt-5.4 / gpt-5.5 (strict schema sanitiser +
    # array_dict_map response policy + OpenAI-style reasoning summary). Both
    # variants resolve to a policy fingerprint byte-identical to the gpt-5.5
    # sibling (schema_sanitizer=strict, response_policy=array_dict_map,
    # reasoning_codec=openai, cache_policy=none), confirmed via
    # ``resolve_policy_names``, so no new preset is required. OpenRouter
    # exposes GPT-5.6 only as named tiers (Luna / Terra / Sol, each with a
    # ``-pro`` reasoning-mode twin); there is no bare ``openai/gpt-5.6``.
    # Terra ($2.5/$15) and Sol ($5/$30, flagship) are the same underlying
    # family at different price/quality tiers and returned IDENTICAL live
    # capability postures, so they share the posture below.
    #
    # Live-certified 2026-07-10 via
    # ``scripts/with_env.sh uv run pytest tests/integration/llm/ -k gpt-5.6``
    # (first pass: 28 passed, 8 skipped, 6 failed — the 6 fails are the same
    # 3 capabilities on each variant; final posture 13 required /
    # 7 known_unsupported per variant). Posture set by the ADD_NEW_MODEL.md
    # § 3 disciplined flow: the gpt-5.5 sibling posture (16 required /
    # 4 known_unsupported) was the starting hypothesis, and the THREE
    # capabilities gpt-5.5 holds as ``required`` but gpt-5.6 refuted live were
    # demoted (the key divergence from the sibling — NOT a blind copy):
    #
    #   * RE2_PATTERN_TOLERANCE — gpt-5.6's endpoint now REJECTS tool schemas
    #     carrying a RE2-incompatible lookaround ``pattern`` with ``400
    #     invalid_json_schema: regex lookaround is not supported`` (the error
    #     names both Azure and OpenAI as rejecting). gpt-5.4/5.5 accept the
    #     same payload, so this is a gpt-5.6 validator tightening, matching the
    #     grok-4.3 posture. Production is unaffected:
    #     ``StrictSchema.strip_re2_incompatible_patterns`` (default True on the
    #     openai_gpt5 preset) strips the pattern before it reaches the model;
    #     the raw-probe capability test swaps in PassthroughSchema to reach the
    #     provider unstripped. Paired ratchet fires if OpenAI relaxes it.
    #   * DECIMAL_FIELD_TOOL_CALL — intermittently returns NO tool call when a
    #     Pydantic ``Decimal`` field is present (terra 2/2 fail, sol 1/2 fail
    #     across two runs 2026-07-10). StrictSchema's Decimal-anyOf collapse
    #     keeps the schema valid (no 400), so this is a model-behaviour gap,
    #     not a schema-dialect one; a ``required`` capability must pass
    #     reliably, so it is demoted. Same failure family as the
    #     deepseek-v3.2-exp / tencent-hy3 decimal flakiness.
    #   * IMPLICIT_PROMPT_CACHING — a clean 2-call 8 k-token probe records the
    #     cache WRITE (cache_write_tokens ~8092) but reads back 0
    #     cache_read_input_tokens on call 2 (route served by Azure, which does
    #     not surface the read on a cold back-to-back probe). gpt-5.5's route
    #     auto-caches; gpt-5.6's does not on this probe. Paired with
    #     ``test_implicit_prompt_caching_unsupported_ratchet`` so it flips back
    #     to required the day a 2-call probe observes caching.
    #
    # The four structural entries stay ``known_unsupported`` (all skipped
    # live): the OpenAI reasoning codec surfaces summary-only reasoning (no
    # signed blocks, no-op replay → THINKING_*), and ``cache_policy=none``
    # attaches no explicit cache_control markers (PROMPT_CACHING).
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__openai_gpt-5.6-terra",
        provider="openrouter",
        name="openai/gpt-5.6-terra",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
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
                # Structural to the openai_gpt5 preset (openai reasoning codec
                # + cache_policy=none) — see family header.
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                # Demoted from the gpt-5.5 hypothesis after the live run — see
                # family header for the per-capability evidence.
                C.RE2_PATTERN_TOLERANCE,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    MC(
        model_id="openrouter__openai_gpt-5.6-sol",
        provider="openrouter",
        name="openai/gpt-5.6-sol",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
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
                # Identical live posture to the terra sibling (same underlying
                # family) — see family header for the full evidence.
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
                C.RE2_PATTERN_TOLERANCE,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.IMPLICIT_PROMPT_CACHING,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # Opus 4.8 — Anthropic adaptive thinker. Live-certified 2026-05-29 via
    # ``pytest tests/integration/llm/ -k claude-opus-4.8`` (OpenRouter route).
    # This is NOT a copy of the 4.7 posture: per the runbook anti-pattern
    # (docs/ADD_NEW_MODEL.md § "copying a sibling cert verbatim"), every 4.7
    # ``known_unsupported`` entry was flipped to ``required`` and re-run live.
    # The 4.8 results diverge from 4.7 in two ways:
    #
    #   * DICT_MAP_TOOL_CALL + DECIMAL_FIELD_TOOL_CALL now PASS under
    #     Anthropic's PassthroughSchema — 4.6/4.7 declared them unsupported,
    #     4.8 handles them natively. Promoted to ``required``.
    #   * DISCRIMINATED_UNION_TOOL_CALL still FAILS the explicit_discriminator
    #     variant. Stays ``known_unsupported`` (see per-entry note).
    #
    # IMPLICIT_PROMPT_CACHING and UNSIGNED_THINKING_REPLAY also pass the
    # synthetic probe live, but are kept ``known_unsupported`` deliberately —
    # the probes are satisfied by an artifact / wrong-shape, not by the
    # contract they name. Each entry documents why (verified 2026-05-29).
    MC(
        model_id="openrouter__anthropic_claude-opus-4.8",
        provider="openrouter",
        name="anthropic/claude-opus-4.8",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.PROMPT_CACHING,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
                # Promoted from 4.6/4.7's known_unsupported — 4.8 passes both
                # live under Anthropic PassthroughSchema (verified 2026-05-29);
                # the 4.7 "strict-trio only" rationale no longer holds for 4.8.
                C.DICT_MAP_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
            }
        ),
        known_unsupported=frozenset(
            {
                # explicit_discriminator variant emits ``item`` as a
                # JSON-encoded string instead of a nested dict (bare_union
                # passes); no JsonCoerce recovery on the Anthropic passthrough
                # route. Verified live 2026-05-29.
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # Passes the synthetic probe live, kept unsupported on purpose:
                # the probe only fires because our anthropic_ephemeral
                # cache_policy injects explicit cache_control markers, so it is
                # really measuring EXPLICIT caching (covered by PROMPT_CACHING,
                # required above). The test's own docstring lists anthropic as
                # known_unsupported — Anthropic has no implicit auto-cache
                # surface. Pass-but-artifact, verified 2026-05-29.
                C.IMPLICIT_PROMPT_CACHING,
                # Passes the synthetic probe live, kept unsupported on purpose:
                # test_unsigned_thinking_replay asserts the Gemini-lineage
                # reasoning_details/reasoning.text replay shape. Anthropic's
                # real replay contract is the SIGNED variant
                # (THINKING_REPLAY_ROUNDTRIP, required above). Kept consistent
                # with opus-4.6/4.7. Pass-but-wrong-shape, verified 2026-05-29.
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # claude-fable-5 - Anthropic adaptive thinker. Live-certified 2026-06-10 via
    # ``pytest tests/integration/llm/ -k claude-fable-5`` (OpenRouter route;
    # final posture 16 required / 4 known_unsupported). This is NOT a copy of
    # the opus-4.8 posture: per docs/ADD_NEW_MODEL.md § "copying a sibling cert
    # verbatim", every opus-4.8 ``known_unsupported`` entry was flipped to
    # ``required`` and re-run live, then only the entries that actually failed
    # (or failed unreliably) were moved back. fable-5 differs from opus-4.8 in
    # two ways worth recording:
    #
    #   * It surfaces structured thinking blocks + signed replay through the
    #     GENERIC ``anthropic`` preset (reasoning_codec: anthropic, reasoning
    #     delivered via the OpenRouter extra_body.reasoning overlay), so it
    #     needs NO version-specific preset with the litellm thinking= kwarg the
    #     way opus-4.7/4.8 do. THINKING_EMITS_BLOCKS + THINKING_REPLAY_ROUNDTRIP
    #     pass reliably (3/3 repeated runs 2026-06-10).
    #   * PROMPT_CACHING is demoted (see per-entry note): the cache WRITE fires
    #     every call, but the back-to-back call-2 read-back misses ~2/11 live
    #     runs on this OpenRouter route, which is too flaky for a merge gate.
    #     opus-4.8 keeps it required; fable-5's route does not pass reliably.
    #
    # DICT_MAP_TOOL_CALL + DECIMAL_FIELD_TOOL_CALL pass under Anthropic's
    # PassthroughSchema, same as opus-4.8.
    MC(
        model_id="openrouter__anthropic_claude-fable-5",
        provider="openrouter",
        name="anthropic/claude-fable-5",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
                # Round-trip cleanly under Anthropic PassthroughSchema (verified
                # live 2026-06-10), same posture as the opus-4.8 sibling.
                C.DICT_MAP_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                # Explicit Anthropic-ephemeral cache: both the write (call 1)
                # and the immediate read-back (call 2) fire reliably (8/8
                # isolated runs 2026-06-10, matching the opus-4.8 baseline which
                # was also 8/8). An earlier 2-of-11 read-back miss was a
                # transient OpenRouter cache-propagation blip, not a model gap,
                # so this is required like the opus siblings.
                C.PROMPT_CACHING,
            }
        ),
        known_unsupported=frozenset(
            {
                # explicit_discriminator variant emits ``item`` as a
                # JSON-encoded string instead of a nested dict (bare_union
                # passes); no JsonCoerce recovery on the Anthropic passthrough
                # route. Reliably reproduced 3/3 runs 2026-06-10 (string body
                # ``{"kind": "ticket", "subject": ...}``). Identical failure mode
                # to the opus-4.8 sibling.
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # Flakily passes the synthetic probe (3/5 live 2026-06-10), kept
                # unsupported on purpose: the probe only fires because our
                # anthropic_ephemeral cache_policy injects explicit cache_control
                # markers, so it is really measuring EXPLICIT caching (the same
                # flaky call-2 read-back as the PROMPT_CACHING contract, which is
                # required above). The test's own docstring lists anthropic as
                # known_unsupported (Anthropic has no implicit auto-cache
                # surface), so the classification holds regardless of the
                # synthetic pass/fail. Verified 2026-06-10.
                C.IMPLICIT_PROMPT_CACHING,
                # Passes the synthetic probe live, kept unsupported on purpose:
                # test_unsigned_thinking_replay asserts the Gemini-lineage
                # reasoning_details/reasoning.text replay shape. Anthropic's
                # real replay contract is the SIGNED variant
                # (THINKING_REPLAY_ROUNDTRIP, required above). Kept consistent
                # with opus-4.6/4.7/4.8. Pass-but-wrong-shape, verified
                # 2026-06-10.
                C.UNSIGNED_THINKING_REPLAY,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # Grok-4.5 — xAI's successor to the 4.x line, routed through the same
    # ``xai_grok`` preset as grok-4 / grok-4.3 (strict schema sanitiser +
    # array_dict_map response policy + OpenAI-style reasoning summary). The
    # resolved policy fingerprint is byte-identical to the grok-4.3 sibling
    # (schema_sanitizer=strict, response_policy=array_dict_map,
    # reasoning_codec=openai, cache_policy=none), confirmed via
    # ``resolve_policy_names``, so no new preset is required.
    #
    # Live-certified 2026-07-09 via
    # ``scripts/with_env.sh uv run pytest tests/integration/llm/ -k grok-4.5``
    # (17 passed, 5 skipped; final posture 15 required / 5 known_unsupported).
    # Posture set by the ADD_NEW_MODEL.md § 3 disciplined flow — NOT copied
    # from a sibling: the grok-4.3 sibling's three non-structural
    # ``known_unsupported`` entries were flipped to ``required`` as the
    # hypothesis and re-run live. All three PASS on grok-4.5, so they stay
    # ``required`` (matching grok-4, not grok-4.3):
    #
    #   * MULTI_TURN_ERROR_RECOVERY — grok-4.3 was demoted for a ~17 %
    #     field-name-as-value hallucination on the retry; grok-4.5 recovers
    #     reliably (test passed 2026-07-09).
    #   * ENUM_SLASH_TOLERANCE + RE2_PATTERN_TOLERANCE — grok-4.3's tightened
    #     tool-schema validator rejected ``/`` enum values and RE2-incompatible
    #     ``pattern`` regexes; grok-4.5 accepts both, so the xAI validator
    #     tightening that landed with 4.3 has been relaxed again.
    #
    # The five structural entries stay ``known_unsupported`` (all skipped
    # live): the ``xai_grok`` preset wires the OpenAI reasoning codec (summary
    # only — no signed blocks, no-op replay) and ``cache_policy=none`` (no
    # cache_control markers), so THINKING_* and PROMPT_CACHING cannot fire on
    # this route regardless of model version; IMPLICIT_PROMPT_CACHING keeps the
    # grok-family observation that no upstream cache is surfaced on the
    # OpenRouter x-ai/grok-* routes (paired ratchet
    # ``test_implicit_prompt_caching_unsupported_ratchet`` passed live).
    MC(
        model_id="openrouter__x-ai_grok-4.5",
        provider="openrouter",
        name="x-ai/grok-4.5",
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # Shares the Flash-lineage ``reasoning.encrypted`` envelope. Routed via
    # the model-specific ``gemini_35_flash_recursive`` preset (see
    # model_presets.yaml) rather than the shared ``gemini`` preset: an
    # auto-resolve run (Slack-requested integration, PR #560) found three
    # engine-side schema-loss failures under the observe (``default``) preset
    # that the shared ``gemini`` preset does NOT close, and the overlay adds
    # ``GeminiRecursiveSchema`` (cycle-tolerant ``$ref`` inlining + synthetic
    # scalar dict-map value field) and ``ScalarArrayDictMapResponse``
    # (scalar-value unwrap + nested-object dict-map pivot) on top of the
    # shared Gemini codec:
    #
    #   * RECURSIVE_REF_TOOL_CALL — all four shapes were 0/15 under observe
    #     (StrictSchema raised "$ref resolution exceeded depth 16" in-engine on
    #     the recursive ``TreeNode.children`` ref); the recursive sanitiser
    #     makes them 5/5 on reprobe. Kept ``required``.
    #   * DICT_MAP_TOOL_CALL — the base dict-map probe passed under observe, but
    #     the ``scalar_values`` and ``nested_in_object`` variants were 0/15
    #     (scalar value dropped on the wire; nested pivot never reached). The
    #     overlay's scalar ``value`` carriage + one-level nested pivot make both
    #     variants 5/5 on reprobe. Kept ``required``.
    #   * DISCRIMINATED_UNION_TOOL_CALL — passed under observe on the flattened
    #     Gemini schema; unchanged by the overlay. Kept ``required``.
    #
    # The five ``known_unsupported`` ceilings below are the observe-run
    # ceilings (known_unsupported in decision.json): Flash emits
    # ``reasoning.encrypted`` only (no readable/signed blocks → THINKING_*),
    # and the ``gemini`` codec wires no cache markers / the synthetic
    # auto-cache probe reads 0 (PROMPT_CACHING / IMPLICIT_PROMPT_CACHING).
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # Gemini 3.6 Flash - certified via the auto-resolve integration
    # consolidated into PR #560 (observe run 2026-07-22, 31 probes x 15
    # reps on the default preset; originally opened as PR #559). Routed
    # through the SAME preset axes as its 3.5 sibling above
    # (``gemini_recursive`` sanitizer + ``scalar_array_dict_map``
    # response policy) - the observe signature was identical:
    # recursive_ref x4 0/15 (in-engine $ref-depth crash) and the
    # dict_map scalar_values + nested_in_object variants 0/15.
    #
    # Unlike 3.5, the 3.6 observe PASSED ``thinking_emits_blocks`` and
    # ``unsigned_thinking_replay`` 15/15, so those are ``required`` here
    # while they stay ceilings on the 3.5 cert.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__google_gemini-3.6-flash",
        provider="openrouter",
        name="google/gemini-3.6-flash",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.DICT_MAP_TOOL_CALL,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.REQUIRED_FIELDS_COMPLETE,
                C.UNSIGNED_THINKING_REPLAY,
                C.LEXICAL_TOOL_INVENTION,
                # Fixed by the ``gemini_recursive`` schema sanitizer
                # (finite wire schema for a genuinely recursive $ref);
                # the generic gemini route crashed 0/15 in-engine here.
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.PROGRESS_AFTER_SUCCESS,
            }
        ),
        known_unsupported=frozenset(
            {
                # Genuine Flash-lineage provider ceilings (recorded from
                # the observe baseline, not addressed by the preset):
                # Gemini emits ``reasoning.encrypted`` blocks only, so
                # there is no readable/signed block to replay round-trip;
                # and no cache surface is exposed on the OpenRouter
                # google/gemini-* routes (neither explicit cache_control
                # nor a reproducible 2-call auto-cache read).
                C.THINKING_REPLAY_ROUNDTRIP,
                C.PROMPT_CACHING,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # Kimi-K2.7-Code (Moonshot AI via OpenRouter). Same moonshotai/kimi-k2
    # family + shared ``openrouter_dict_stringify_recovery`` preset as kimi-k2.6,
    # so this cert MIRRORS the kimi-k2.6 sibling (13 required / 7
    # known_unsupported) as the integration starting point. NOT yet
    # live-certified: the code-specialised variant may behave differently
    # (tool-call or reasoning surface), so verify against the live route and
    # promote/demote what it refutes when the model is first evaluated (same
    # approach as the nemotron-3-ultra integration, #65).
    MC(
        model_id="openrouter__moonshotai_kimi-k2.7-code",
        provider="openrouter",
        name="moonshotai/kimi-k2.7-code",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                # kimi-k2.6 emits the discriminated-union arg shape correctly as
                # a native dict but does not follow a turn-2 switch to a different
                # variant; mirrored here pending the k2.7-code live check.
                C.DISCRIMINATED_UNION_TOOL_CALL,
            }
        ),
    ),
    # Kimi-K3 (Moonshot AI via OpenRouter). A new-generation Kimi (1M ctx,
    # $3 / $15), not a copy of the kimi-k2.x siblings: it does not match the
    # ``*kimi-k2*`` glob, so it routes to the ``default`` preset rather than
    # ``openrouter_dict_stringify_recovery``. Base and shape-variant dict-map
    # (nested / wide / scalar) and Decimal tool calls all round-trip cleanly as
    # native dicts on ``default``, so kimi-k3 does not inherit the K2
    # dict-stringify behaviour and needs no stringify-recovery preset.
    #
    # Every position below is live-verified on the ``default`` preset
    # (2026-07-17). Three capabilities the kimi-k2.7-code sibling marks
    # ``known_unsupported`` are supported by kimi-k3 and promoted to
    # ``required`` here: DECIMAL_FIELD_TOOL_CALL, IMPLICIT_PROMPT_CACHING
    # (auto-cache works), and DISCRIMINATED_UNION_TOOL_CALL (kimi-k3 handles the
    # turn-2 union switch the sibling fails). Each was re-tested against the live
    # route, not inherited: see ADD_NEW_MODEL.md "anti-pattern: copying a sibling
    # cert verbatim".
    MC(
        model_id="openrouter__moonshotai_kimi-k3",
        provider="openrouter",
        name="moonshotai/kimi-k3",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.IMPLICIT_PROMPT_CACHING,
                # Sibling (k2.6 / k2.7-code) fails the turn-2 union switch and
                # marks this known_unsupported; kimi-k3 handles it (live-verified,
                # both bare-union and explicit-discriminator).
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
                # Live-verified genuine failures on ``default``: kimi-k3 returns
                # no structured reasoning, so it emits no thinking blocks
                # (THINKING_EMITS_BLOCKS) and neither thinking-replay path works
                # (THINKING_REPLAY_ROUNDTRIP; UNSIGNED_THINKING_REPLAY fails with
                # "turn 1 returned no structured reasoning"). It also does not
                # honour explicit ephemeral cache-control (PROMPT_CACHING).
                # Implicit (auto) caching does work, so IMPLICIT_PROMPT_CACHING is
                # in ``required`` above.
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                C.PROMPT_CACHING,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # -----------------------------------------------------------------
    # DeepSeek V4-Flash — lighter/cheaper sibling of deepseek-v4-pro on
    # the OpenRouter route, shares the existing ``*deepseek-v4*`` preset
    # (openrouter_dict_stringify_recovery). Live-certified 2026-06-05 via
    # ``pytest tests/integration/llm/ -k deepseek-v4-flash`` (16 required,
    # 4 known_unsupported). Stronger than the v4-pro sibling on
    # DECIMAL_FIELD_TOOL_CALL and THINKING_EMITS_BLOCKS (both pass reliably
    # here, both known_unsupported on v4-pro) — re-tested per
    # docs/ADD_NEW_MODEL.md, not copied. Caching matches v4-pro (both ku):
    # an initial warm probe promoted IMPLICIT_PROMPT_CACHING, but a clean
    # cold 2-call probe reads 0, so it stays known_unsupported with the
    # ratchet guarding it.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__deepseek_deepseek-v4-flash",
        provider="openrouter",
        name="deepseek/deepseek-v4-flash",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
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
                # Reasoning surfaces as an UNSIGNED summary on the
                # OpenRouter route: signed-block replay has no source ("no
                # signed blocks on turn 1") and the unsigned codec's
                # ``encode_for_replay`` path is not wired for the v4 route
                # (assistant dict carries no ``reasoning_details``). Same
                # posture as the v4-pro sibling. Verified live 2026-06-05.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No Anthropic-style ephemeral cache: call 1 created 0
                # cache_creation_input_tokens (the OpenRouter DeepSeek
                # route exposes no explicit cache-control markers).
                C.PROMPT_CACHING,
                # Implicit auto-cache is NOT reliably observable on a clean
                # 2-call ~8 k-token probe: a cold run reads
                # cache_read_input_tokens=0 (an earlier warm probe read
                # 8192, but that was contaminated by back-to-back runs).
                # DeepSeek caches in aggregate (~80%) in production. Same
                # posture as the v4-pro sibling; paired with
                # test_implicit_prompt_caching_unsupported_ratchet, which
                # flips it back to required the day a clean 2-call probe
                # observes caching. Verified live cold 2026-06-05.
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # DeepSeek V3.2-Exp experimental V3.2 reasoning line on
    # the OpenRouter route. Live-certified 2026-06-03 via
    # ``pytest tests/integration/llm/ -k deepseek-v3.2-exp`` (14 passed,
    # 6 skipped). Routes through the dedicated ``deepseek_v32`` preset
    # (OpenAI reasoning codec only): unlike the deepseek-v4-pro sibling it
    # round-trips dict-map and discriminated-union calls on the *standard*
    # response policy, so it needs neither json_coerce nor dict_map_hints,
    # only the codec so its reasoning_content summary lands in the
    # trajectory logs like every other reasoning route. Reasoning is
    # requested as ``extra_body.reasoning.effort`` via the openrouter
    # provider overlay (eval configs use reasoning: adaptive).
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__deepseek_deepseek-v3.2-exp",
        provider="openrouter",
        name="deepseek/deepseek-v3.2-exp",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                # Tool-call reliability on a registered Decimal field is
                # flaky: 1 pass / 5 fails across 6 live calls on
                # 2026-06-03, every failure being "no tool call returned"
                # (the model answers in prose instead of invoking the
                # tool). Same posture as the deepseek-v4-pro sibling; a
                # ``required`` capability must pass reliably, so Decimal
                # stays ``known_unsupported`` until the route stabilises.
                C.DECIMAL_FIELD_TOOL_CALL,
                # OpenRouter surfaces only a ``reasoning_content`` summary,
                # which OpenAIReasoningCodec yields as a single
                # ``summary_text`` block, never the structured signed
                # thinking blocks this capability requires. Same posture as
                # the deepseek-v4-pro sibling under the identical OpenAI
                # codec and the GPT-5 family. Verified live 2026-06-03.
                C.THINKING_EMITS_BLOCKS,
                # OpenAI-route reasoning has no replay path:
                # ``OpenAIReasoningCodec.encode_for_replay`` returns ``{}``
                # and DeepSeek does not accept echoed reasoning on later
                # turns, exactly like the deepseek-v4-pro sibling. Verified
                # live 2026-06-03.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No Anthropic-style ephemeral cache: the first call created
                # 0 cache_creation_input_tokens. Verified live 2026-06-03.
                C.PROMPT_CACHING,
                # OpenRouter auto-cache not reproducible in a clean 2-call
                # 8 k-token probe (``cached_tokens=0`` on both calls),
                # exactly like deepseek-v4-pro; production large-prompt runs
                # may still cache in aggregate. Paired with the ratchet in
                # ``test_implicit_prompt_caching_unsupported_ratchet`` (which
                # passes), so the day a 2-call probe observes caching it
                # flips back to ``required``. Verified live 2026-06-03.
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
    # =================================================================
    # Arena lineup refresh (2026-06). Six models, live-certified
    # 2026-06-05 via ``pytest tests/integration/llm/ -k <model>``.
    # Per-model preset routing lives in model_presets.yaml; each entry
    # documents which capabilities were demoted to ``known_unsupported``
    # and why. The universal demotions across this cohort are PROMPT_
    # CACHING (no Anthropic-style ephemeral cache on these OpenRouter
    # routes) and the signed/unsigned thinking-replay pair (OpenAI-codec
    # routes have a no-op replay path).
    # =================================================================
    # GLM-5.1 (Zhipu / Z.AI via OpenRouter). Routes through the shared
    # ``openrouter_dict_stringify_recovery`` preset: it stringified the
    # discriminated-union ``item`` on the default route, which json_coerce
    # decodes, and the openai codec surfaces its reasoning summary so
    # THINKING_EMITS_BLOCKS passes. Implicit auto-cache observed cold.
    # 17 required / 3 known_unsupported. Live-certified 2026-06-05.
    MC(
        model_id="openrouter__z-ai_glm-5.1",
        provider="openrouter",
        name="z-ai/glm-5.1",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
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
                # No Anthropic-style ephemeral cache markers on this route.
                C.PROMPT_CACHING,
                # Reasoning surfaces only as an unsigned summary via the
                # openai codec: no signed blocks to replay, and the codec's
                # replay path is a no-op. Verified live 2026-06-05.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # GLM-5.2 (Zhipu / Z.AI via OpenRouter). Same z-ai/glm-5 family + shared
    # ``openrouter_dict_stringify_recovery`` preset as glm-5.1, so this cert
    # MIRRORS the glm-5.1 sibling (17 required / 3 known_unsupported) as the
    # integration starting point. NOT yet live-certified: verify against the
    # live route, and demote any capability it refutes, when the model is
    # first evaluated (same approach as the nemotron-3-ultra integration, #65).
    MC(
        model_id="openrouter__z-ai_glm-5.2",
        provider="openrouter",
        name="z-ai/glm-5.2",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
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
                # No Anthropic-style ephemeral cache markers on this route.
                C.PROMPT_CACHING,
                # Reasoning surfaces only as an unsigned summary via the openai
                # codec (no signed blocks to replay; the codec replay is a no-op).
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # Mistral-Medium-3.5 (Mistral AI via OpenRouter). Clean tool-caller on
    # the default route — dict-map, discriminated-union, decimal all
    # round-trip natively, so no preset is needed. It is a NON-reasoning
    # model (0 reasoning tokens live), so the thinking capabilities are
    # genuinely out of scope. 15 required / 5 known_unsupported.
    # Live-certified 2026-06-05.
    MC(
        model_id="openrouter__mistralai_mistral-medium-3-5",
        provider="openrouter",
        name="mistralai/mistral-medium-3-5",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                # Non-reasoning model: emits no structured reasoning at
                # all (0 reasoning tokens live 2026-06-05), so neither the
                # emit nor the replay capabilities apply.
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No ephemeral cache markers and the 2-call ~8 k-token
                # auto-cache probe read cached_tokens=0 on both calls.
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # Gemma-4-31B-IT (Google open-weights via OpenRouter). Routes through
    # the ``gemma`` preset (Gemini *schema* sanitizer, no gemini reasoning
    # codec): the dict-map-to-array transform fixes DICT_MAP_TOOL_CALL,
    # but the discriminated union still fails because the model substitutes
    # ``title`` for the registered ``subject`` even on the flattened
    # schema — a field-name-adherence gap, not a schema-construct one, so
    # it stays known_unsupported. Non-reasoning model. 14 required /
    # 6 known_unsupported. Live-certified 2026-06-05.
    MC(
        model_id="openrouter__google_gemma-4-31b-it",
        provider="openrouter",
        name="google/gemma-4-31b-it",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
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
                # Emits ``title`` instead of the registered ``subject`` on
                # the union branch even under the flattened Gemini schema —
                # genuine field-name gap, not a construct the sanitizer can
                # rewrite. Verified live 2026-06-05.
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # Non-reasoning model (0 reasoning tokens live).
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No ephemeral cache markers; auto-cache probe read 0.
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # Nemotron-3-Super-120B-A12B (NVIDIA via OpenRouter). Routes through
    # the shared ``openrouter_dict_stringify_recovery`` preset: it emits
    # the discriminated-union ``item`` as a native dict on some calls and a
    # JSON-encoded string on others (live 2026-06-05), so it needs
    # json_coerce to make DISCRIMINATED_UNION_TOOL_CALL reliable. It is an
    # adaptive reasoner that intermittently returns no reasoning at all
    # (``reasoning=None`` on a re-run), so THINKING_EMITS_BLOCKS is not
    # reliable enough for ``required`` — the openai codec still surfaces
    # the summary in logs when it does reason. 15 required /
    # 5 known_unsupported. Live-certified 2026-06-05.
    MC(
        model_id="openrouter__nvidia_nemotron-3-super-120b-a12b",
        provider="openrouter",
        name="nvidia/nemotron-3-super-120b-a12b",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                # Adaptive reasoner: returns no structured reasoning on
                # some calls (reasoning=None live 2026-06-05), so emit is
                # not reliable. Replay is a no-op on the openai codec.
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No ephemeral cache markers; auto-cache probe read 0.
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # Nemotron-3-Ultra-550B-A55B (NVIDIA via OpenRouter). Same Nemotron-3
    # family and architecture as nemotron-3-super above (MoE, scaled up:
    # 550B-A55B vs 120B-A12B), matched by the same ``nvidia/nemotron*`` glob
    # so it routes through the shared ``openrouter_dict_stringify_recovery``
    # preset — json_coerce makes DISCRIMINATED_UNION_TOOL_CALL /
    # DICT_MAP_TOOL_CALL reliable and the openai codec surfaces reasoning.
    # NOT yet live-certified: the shared OpenRouter pool 429-rate-limits NVIDIA
    # Nemotron (see super), so the live cert + the eval both wait on a BYOK
    # key; the capability split is mirrored from nemotron-3-super pending that
    # verification. 15 required / 5 known_unsupported.
    MC(
        model_id="openrouter__nvidia_nemotron-3-ultra-550b-a55b",
        provider="openrouter",
        name="nvidia/nemotron-3-ultra-550b-a55b",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
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
                # Adaptive reasoner like super: returns no structured reasoning
                # on some calls, so emit is not reliable; replay is a no-op on
                # the openai codec.
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No ephemeral cache markers; auto-cache probe reads 0.
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # GPT-OSS-120B (OpenAI open-weights via OpenRouter). The ``gpt_oss``
    # preset adds only the openai reasoning codec (THINKING_EMITS_BLOCKS
    # passes). It substitutes synonyms for registered field names
    # (``title`` for ``subject``, ``quantity`` for ``qty``) that persists
    # even under the Gemini schema sanitizer's oneOf-flatten +
    # dict-map-to-array, so both schema caps are genuine model gaps, not
    # construct gaps — declared known_unsupported rather than papered over
    # with a sanitizer that gives no lift. 14 required / 6 known_unsupported.
    # Live-certified 2026-06-05.
    MC(
        model_id="openrouter__openai_gpt-oss-120b",
        provider="openrouter",
        name="openai/gpt-oss-120b",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
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
                # Emits ``quantity`` for ``qty`` / ``title`` for
                # ``subject`` — synonym substitution that survives the
                # Gemini schema flatten. Genuine field-name gap. Verified
                # live 2026-06-05.
                C.DICT_MAP_TOOL_CALL,
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # openai-codec reasoning is an unsigned summary, no-op
                # replay.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # No ephemeral cache markers; auto-cache probe read 0.
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
            }
        ),
    ),
    # HY3 (Tencent Hunyuan 3 GA via OpenRouter) — the released successor to
    # the hy3-preview sibling below. Routes through the same shared
    # ``openrouter_dict_stringify_recovery`` preset; the resolved policy
    # fingerprint is byte-identical to hy3-preview (passthrough schema +
    # dict_map_hints + json_coerce response + openai reasoning codec,
    # cache_policy=none), confirmed via ``resolve_policy_names``, so no new
    # preset is required.
    #
    # Live-certified 2026-07-10 via
    # ``scripts/with_env.sh uv run pytest tests/integration/llm/ -k 'hy3 and not preview'``
    # (18 passed, 3 skipped; final posture 17 required / 3 known_unsupported).
    # Posture set by the ADD_NEW_MODEL.md § 3 disciplined flow — NOT copied
    # from the preview sibling: the three non-structural ``known_unsupported``
    # entries the preview holds were flipped to ``required`` as the hypothesis
    # and re-run live. All three PASS on the GA release, so the GA is strictly
    # stronger than the preview and they stay ``required``:
    #
    #   * DECIMAL_FIELD_TOOL_CALL — preview intermittently returned no tool
    #     call (~2/5 runs); GA emits the Decimal call reliably.
    #   * DISCRIMINATED_UNION_TOOL_CALL — preview intermittently renamed the
    #     branch field (``title`` for ``subject``, ~1/5 runs); GA round-trips
    #     both turns cleanly (json_coerce handles any stringified args).
    #   * IMPLICIT_PROMPT_CACHING — preview's cold 2-call probe read 0; GA
    #     surfaces cache_read on call 2.
    #
    # The three structural entries stay ``known_unsupported`` (all skipped
    # live): the openai reasoning codec is summary-only (no signed blocks,
    # no-op replay → THINKING_REPLAY_ROUNDTRIP / UNSIGNED_THINKING_REPLAY) and
    # ``cache_policy=none`` attaches no cache_control markers (PROMPT_CACHING).
    MC(
        model_id="openrouter__tencent_hy3",
        provider="openrouter",
        name="tencent/hy3",
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
                C.THINKING_EMITS_BLOCKS,
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
                C.PROMPT_CACHING,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # HY3-Preview (Tencent Hunyuan 3 via OpenRouter). Routes through the
    # shared ``openrouter_dict_stringify_recovery`` preset (json_coerce
    # decodes its stringified tool args; the openai codec surfaces its
    # reasoning summary so THINKING_EMITS_BLOCKS passes reliably). Two
    # tool-call capabilities are intermittently unreliable across repeated
    # live runs (2026-06-05), so they are known_unsupported rather than a
    # flaky merge gate. 14 required / 6 known_unsupported.
    MC(
        model_id="openrouter__tencent_hy3-preview",
        provider="openrouter",
        name="tencent/hy3-preview",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DICT_MAP_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
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
                # Intermittently answers in prose instead of emitting the
                # tool call ("no tool call returned" on ~2 of 5 live runs
                # 2026-06-05) — same unreliable posture as the DeepSeek
                # Decimal route. Not a reliable required gate.
                C.DECIMAL_FIELD_TOOL_CALL,
                # Mostly round-trips the union (json_coerce handles the
                # stringified calls) but intermittently renames the branch
                # field (``title`` for the registered ``subject``) the way
                # gpt-oss / gemma do — a field-name gap json_coerce cannot
                # fix, on ~1 of 5 live runs 2026-06-05. Not reliable.
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # No ephemeral cache markers, and the 2-call ~8 k-token
                # auto-cache probe read cached_tokens=0 cold. Paired with
                # test_implicit_prompt_caching_unsupported_ratchet, which
                # promotes IMPLICIT_PROMPT_CACHING the day a 2-call probe
                # observes caching. Verified live 2026-06-05.
                C.PROMPT_CACHING,
                C.IMPLICIT_PROMPT_CACHING,
                # openai-codec reasoning is an unsigned summary, no-op
                # replay.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # ------------------------------------------------------------------
    # MiniMax-M3 (MiniMax via OpenRouter): reasoning model on the M-series
    # 1M-context line (arena lineup refresh 2026-06). A solid baseline
    # tool-caller: basic / simple / multi-turn, error-recovery, decimal,
    # enum-slash, re2, tool-name discipline, lexical invention,
    # required-fields and progress-after-success all pass live. The
    # ``minimax`` preset sets the openai reasoning codec (so its
    # reasoning_content summary lands in the trajectory logs) plus the
    # ``minimax_m3_tags`` response policy (PR #55). That policy is a
    # tags-site-scoped, eval-time recovery of M3's XML -> JSON ``tags``
    # corruption at ``updates.tags`` / ``item.tags`` only; it does NOT
    # touch the synthetic capability probes (none exercise those sites), so
    # the live cert posture is unchanged by it. Implicit auto-cache is
    # reliable (cache_read priced on OpenRouter; 4/4 clean 2-call probes
    # 2026-06-08), so IMPLICIT_PROMPT_CACHING stays required (unlike the
    # warmth-dependent DeepSeek route). Like gpt_oss it has a genuine
    # structured-tool-call gap: it intermittently mis-shapes typed
    # Dict[str, T] and declines the turn-2 discriminated-union call (details
    # below), and that gap is NOT schema/stringify-fixable. The
    # ``minimax_m3_tags`` policy is tags-site-scoped and explicitly does NOT
    # address it, so DICT_MAP_TOOL_CALL / DISCRIMINATED_UNION_TOOL_CALL stay
    # known_unsupported rather than being papered over. 15 required / 5
    # known_unsupported. Live-certified 2026-06-08 (codec-only preset);
    # re-certified 2026-06-15 on the PR #55 branch with the
    # ``minimax_m3_tags`` policy active (pytest tests/integration/llm/ -k
    # minimax-m3: 15 passed, 6 skipped on two consecutive runs; the 5
    # known_unsupported caps yield 6 skips since discriminated_union has 2
    # parametrisations). Posture identical to the 2026-06-08 cert.
    # ------------------------------------------------------------------
    MC(
        model_id="openrouter__minimax_minimax-m3",
        provider="openrouter",
        name="minimax/minimax-m3",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.THINKING_EMITS_BLOCKS,
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
                C.RECURSIVE_REF_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.ALLOF_MERGE_TOOL_CALL,
                # minimax-m3 stringifies dict-map/union/nested shapes; its
                # preset is `minimax_m3_tags` (tag-unwrap), NOT `json_coerce`,
                # so these stringify-class shapes have no recovery. Live raw
                # run: recursive/heterogeneous/allof all stringified.
                # Stringify-class shapes (recursive $ref, polymorphic
                # arrays, allOf merge) share the native mis-shaping /
                # stringification failure mode of dict_map below, and the
                # minimax preset is `minimax_m3_tags` (tag-unwrap), NOT
                # `json_coerce`, so there is no stringify recovery. Local
                # raw run 2026-06-30 stringified these heavily, unrecovered.
                # Kept known_unsupported (ratchet target).
                # Typed Dict[str, T] tool args are flaky: the model
                # intermittently emits an array under a literal "item" key
                # ({"item": [{"sku": ...}, ...]}) instead of a dict keyed by
                # the map key. Live 2026-06-08: 2/10 fail codec-only, 4/10
                # fail under openrouter_dict_stringify_recovery, i.e. its
                # dict_map_hints gave NO reliability lift, and the failure is
                # native mis-shaping not stringification (json_coerce N/A). So,
                # like gpt_oss, it routes codec-only and this stays
                # known_unsupported rather than papered over with a sanitizer
                # that gives no lift.
                C.DICT_MAP_TOOL_CALL,
                # Two-turn discriminated-union calls are likewise unreliable:
                # 1 of the 2 parametrisations fails on every one of 5 live
                # runs 2026-06-08 (5/10 param-runs), always "turn 2 returned
                # no tool call": the model answers turn 2 in prose ("Comment
                # posted on TCK-...") instead of emitting the union call. A
                # turn-2 tool-invocation gap, not a schema-dialect one (no
                # preset fixes it).
                C.DISCRIMINATED_UNION_TOOL_CALL,
                # No Anthropic-style ephemeral cache markers on this route;
                # explicit cache_control is not wired for non-Anthropic
                # OpenRouter routes. The auto-cache surface
                # (IMPLICIT_PROMPT_CACHING) IS required (see header).
                # Verified live 2026-06-08.
                C.PROMPT_CACHING,
                # Reasoning surfaces only as an unsigned reasoning_content
                # summary via the openai codec: no signed blocks to replay
                # and the codec's encode_for_replay path is a no-op. Same
                # posture as the glm-5.1 / deepseek-v3.2-exp siblings.
                # Verified live 2026-06-08.
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
            }
        ),
    ),
    # -----------------------------------------------------------------
    # Meta Muse Spark 1.1 — sole OpenRouter endpoint is Meta (the
    # /endpoints list has one entry, so there is no provider pin to
    # route around Meta behaviour) and the model is US-region-locked (a
    # non-US call returns 403 "This model is only available in the
    # United States"), so the capability suite must run from a US
    # runner. Postures below were established by a full live
    # characterisation run from US Codespaces on 2026-07-17 — every
    # capability tentatively flipped to `required` per ADD_NEW_MODEL.md
    # §3, then moved back only where it failed live. This SUPERSEDES the
    # 2026-07-17 observe run (PR #474, Actions run 29562351690), which
    # was infra-dirty and mis-characterised discriminated-union as ~50%
    # (8/15, 6/15) whereas the clean live re-test passed ~84%. The
    # `-k muse` suite output is pasted in PR #474 per ADD_NEW_MODEL.md §4.
    # -----------------------------------------------------------------
    MC(
        model_id="openrouter__meta_muse-spark-1.1",
        provider="openrouter",
        name="meta/muse-spark-1.1",
        env_key="OPENROUTER_API_KEY",
        required=frozenset(
            {
                C.BASIC_COMPLETION,
                C.SIMPLE_TOOL_CALL,
                C.MULTI_TURN_TOOL_USE,
                C.MULTI_TURN_ERROR_RECOVERY,
                C.DICT_MAP_TOOL_CALL,
                C.ENUM_SLASH_TOLERANCE,
                C.RE2_PATTERN_TOLERANCE,
                C.DECIMAL_FIELD_TOOL_CALL,
                C.HETEROGENEOUS_ARRAY_TOOL_CALL,
                C.USAGE_METRICS_POPULATED,
                C.COST_USD_POPULATED,
                C.TOOL_NAME_DISCIPLINE,
                C.LEXICAL_TOOL_INVENTION,
                C.REQUIRED_FIELDS_COMPLETE,
                C.PROGRESS_AFTER_SUCCESS,
                # Two-turn discriminated union is supported with a residual
                # turn-2 flake: live 2026-07-17 both params passed 32/38
                # full runs (~84%), and 12/12 in a dedicated decisive round
                # + 12/12 per-param in isolation. The misses cluster in
                # degraded-provider windows (one round went 5/10 alongside a
                # 288s latency outlier); healthy windows run ~96-100%. When
                # it fails, turn 2 answers in prose instead of emitting the
                # union call. ~84% clears the ~73% floor the other flaky-but-
                # required caps (re2 ~85%, required_fields ~73%) sit at, so
                # this is `required`. The observe run's ~50% (bare 8/15,
                # explicit 6/15) was its documented infra-dirty window, not
                # the steady-state rate — so this is NOT the copied-sibling
                # `known_unsupported` posture. Like re2/required_fields this
                # probe may need a re-run to go green (ADD_NEW_MODEL.md §4).
                C.DISCRIMINATED_UNION_TOOL_CALL,
            }
        ),
        known_unsupported=frozenset(
            {
                # allOf merge works on 2 of 3 shapes (top_level + three_way
                # PASS live) — the model CAN populate both merged
                # subschemas. The `nested` shape (envelope.payload)
                # deterministically trips Meta's content-management filter
                # (content_policy_violation 400, param:prompt) on a benign
                # order/inventory prompt: 0/6 live 2026-07-17 (5× step-4
                # loop + all-required run) + 0/15 observe. A provider-filter
                # false-positive, not a merge-capability gap, and Meta is
                # the sole endpoint so there is no route around it. Ratchet
                # target: flip to required when Meta stops filtering it.
                C.ALLOF_MERGE_TOOL_CALL,
                # API-layer 400 "Recursive JSON schemas are not currently
                # supported" (param:parameters) on every recursive_ref
                # shape — simple / deep_chain / wide_tree / nested_in_object
                # all 0/1 live 2026-07-17 + 0/15 observe. Provider
                # limitation, not a sanitiser gap. Ratchet target.
                C.RECURSIVE_REF_TOOL_CALL,
                # Reasoning is done internally (provider_raw carries
                # reasoning_tokens; reasoning_details is
                # type="reasoning.encrypted") but never surfaced as
                # structured blocks over the OpenRouter route — the tests
                # fail with "StructuredReasoning should be surfaced" /
                # "turn 1 returned no structured reasoning" (all three 0/1
                # live 2026-07-17 + 0/15 observe).
                C.THINKING_EMITS_BLOCKS,
                C.THINKING_REPLAY_ROUNDTRIP,
                C.UNSIGNED_THINKING_REPLAY,
                # Explicit Anthropic-style ephemeral cache_control is not
                # wired for non-Anthropic OpenRouter routes: no cold-write
                # cache-creation event (cache_creation_input_tokens=0,
                # cache_write_tokens=0), so test_prompt_caching's
                # creation assertion fails live 2026-07-17. Distinct from
                # the auto-cache surface (IMPLICIT_PROMPT_CACHING, also
                # known_unsupported — see the entry below). Same
                # explicit-cache posture as the gpt-oss-120b sibling.
                C.PROMPT_CACHING,
                # IMPLICIT_PROMPT_CACHING — known_unsupported because we
                # cannot verify GENUINE auto-caching on this route and its
                # cached-token reporting is not trustworthy as a caching
                # signal. Observed live 2026-07-17 (US Codespaces):
                #  * A price discount IS applied — OpenRouter bills cached
                #    input at the cache_read rate: a call with 8703 cached +
                #    4 uncached prompt tokens costs 0.00131045 ==
                #    8703*0.15e-6 + 4*1.25e-6 (vs 0.0109 at full input rate).
                #  * BUT ~all input is reported `cached` on EVERY call,
                #    including a genuinely cold one: a 19,585-token,
                #    never-before-sent unique prompt returned cached=19,581
                #    on call 1. A real first-call cross-request cache HIT is
                #    impossible, so this counter does not reflect a
                #    verifiable prior-call hit — it tracks the discounted
                #    billing tier (or is a provider mis-report), not caching
                #    we can certify.
                #  * The standard test_implicit_prompt_caching fails 3/3
                #    (cost call1 == call2: with no cold baseline the
                #    "call 2 < call 1" delta cannot exist).
                # Net: the classic cold->warm reuse we would credit as
                # caching is unverifiable here, so we do not claim it. NB:
                # the observable cached_tokens would make the auto-cache
                # ratchet (test_known_unsupported_routes_do_not_auto_cache)
                # fire on this route, so muse is excluded there
                # (_UNRELIABLE_COLD_CACHE_REPORT_NAMES) carrying this same
                # rationale — the counter is not a reliable real-caching
                # signal here. That exclusion is a targeted stopgap, not a
                # real fix; follow-up #484 will teach the ratchet to send a
                # cold probe before trusting cached_tokens.
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


def _candidate_from_env() -> MC | None:
    """Build an ad-hoc, all-capabilities-required certificate from env vars.

    Set by the model auto-integration workflow's observe stage
    (``TF_CANDIDATE_PROVIDER`` + ``TF_CANDIDATE_NAME``) to run the full
    capability suite against a candidate model that is NOT yet listed in
    :data:`ALL_MODELS`. Every capability is declared ``required`` so no probe
    auto-skips: the workflow runs the suite report-only, so a capability the
    candidate does not support is recorded as a failure for the next step to
    classify, not a hard gate. ``model_id`` is derived through the same
    :func:`model_id_slug` the invariant checks against, so the injected cert
    satisfies the slug-consistency contract.

    Returns ``None`` when the env vars are unset, so normal test collection
    and the canonical capability-registry test are untouched.
    """
    provider = os.environ.get("TF_CANDIDATE_PROVIDER", "").strip()
    name = os.environ.get("TF_CANDIDATE_NAME", "").strip()
    if not provider or not name:
        return None

    from tolokaforge.core.output.artifacts import model_id_slug

    return MC(
        model_id=model_id_slug(provider, name),
        provider=provider,
        name=name,
        env_key=f"{provider.upper()}_API_KEY",
        required=frozenset(C),
        known_unsupported=frozenset(),
    )


# Append the onboarding candidate (if any) before the uniqueness check so a
# re-onboarding of an already-listed model reuses its curated certificate
# rather than colliding.
_candidate = _candidate_from_env()
if _candidate is not None and _candidate.model_id not in {c.model_id for c in _ALL}:
    _ALL.append(_candidate)


_validate_unique_model_ids(_ALL)


ALL_MODELS: tuple[MC, ...] = tuple(_ALL)
"""Immutable ordered tuple of every :class:`ModelCertificate` under test.

Deterministic import order is guaranteed — the canonical
capability-registry test pins this so test-collection IDs stay stable
across runs.
"""
