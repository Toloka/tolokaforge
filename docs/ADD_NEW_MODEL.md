# Adding a New Model

Six-step process. No PR merges a model change without all six passing.

## Pre-flight: 30-second checklist

Before writing any code, verify the model exists and decide where each
change belongs:

```bash
# 1. Confirm the OpenRouter slug actually exists.
curl -s https://openrouter.ai/api/v1/models | \
  python3 -c "import json, sys; print('\n'.join(m['id'] for m in json.load(sys.stdin)['data'] if '<vendor>' in m['id']))"
```

| File / dir | Branch | Reason |
|---|---|---|
| `tolokaforge/core/data/pricing.json` | **main** | Shared cost catalog |
| `tolokaforge/testing/certify/_registry.py` | **main** | Capability certificate is shared |
| `tolokaforge/core/data/model_presets.yaml` | **main** | Only if new preset needed |

Evaluation-specific configs **must not land on `main`** — see
`AGENTS.md` § "No Project-Specific Content on main".

## 1. Add pricing

Append an entry to [`tolokaforge/core/data/pricing.json`](../tolokaforge/core/data/pricing.json).
Use current OpenRouter / Nova / direct-provider pricing; document the
capture date in the adjacent comment. Schema: `{input, output}` per
1M tokens, USD.

```jsonc
"openrouter/x-ai/grok-4": {
  "input": 3.00,         // per 1M prompt tokens (captured 2026-04-15)
  "output": 15.00        // per 1M completion tokens
}
```

**Auto-fetch alternative.** `uv run pricing-updater update` reads the
OpenRouter `/api/v1/models` endpoint and merges any newly-priced
models into `pricing.json`. Use this when refreshing a batch of
prices; for a single new model the hand-edit above is cleaner and
keeps the diff minimal. Either way **verify the resulting numbers
match the OpenRouter API response** — a mis-priced model corrupts
every cost report that touches it.

**Both `input` and `output` are per-1M tokens, not per-token.** A
common bug is to copy the `pricing.prompt` field from the OpenRouter
API response verbatim (it's per-token, scientific notation, e.g.
`"0.0000015"`) and forget the ×1,000,000 conversion.
## 2. Add a preset (or confirm fallthrough is OK)

[`tolokaforge/core/data/model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml)
owns every **non-default** policy combination. If the model needs
specialised schema sanitisation / cache policy / reasoning codec /
prompt hints, add a new entry with explicit `match:` globs. If the
generic defaults are correct, skip — the fallthrough `default` preset
applies.

Presets resolve via **first-match-wins** ordering. Anthropic 4.7 is
listed before the generic `anthropic` preset so its `thinking`-kwarg
routing takes precedence — see [`AGENTS.md`](../AGENTS.md) gotcha #15.

Available policy slots (see
[`docs/LLM_LAYER.md`](LLM_LAYER.md) for the authoritative spec):

| Slot | Default | Alternatives |
|---|---|---|
| `schema_sanitizer` | `passthrough` | `strict`, `gemini`, `gemini_recursive` |
| `prompt_policy` | `none` | `dict_map_hints`, `dict_map_hints_ref` |
| `content_policy` | `openai` | `anthropic`, `nova` |
| `response_policy` | `standard` | `unwrap_input`, `json_coerce`, `array_dict_map`, `scalar_array_dict_map`, `minimax_m3_tags` |
| `reasoning_codec` | `none` | `anthropic`, `openai`, `openai_summary_replay`, `gemini` |
| `cache_policy` | `none` | `anthropic_ephemeral` |
| `params_policy` | `generation_params` | arbitrary `{name, params}` — see `GenerationParams.KNOWN_KEYS` |
| `message_assembly_policy` | `null` | `nova` (only `aws_nova*` presets opt in — the filler string is data on the policy instance) |
| `assistant_text_policy` | `passthrough` | out-of-tree via subclass (e.g. Cohere `<|START_TEXT|>…<|END_TEXT|>` marker stripper) |

If a new provider needs a lifecycle stage not covered by the nine slots,
that is Bucket B per [ADR-0030](adr/0030-tolokaforge-models-split.md) —
extending the engine with a new slot type rather than a new policy class.
Bucket A additions (new policy class for an existing slot) reuse the slot
mechanism and require only a class definition plus a preset entry.

> **Rate-limited model?** If OpenRouter's default provider 429s the eval
> (`is_byok:false`, "rate-limited upstream"), pin the model to a provider that
> has capacity with an `openrouter:` block in the model config (OpenRouter-only,
> validated): `openrouter: {provider_order: ["Together"], allow_fallbacks: false}`.
> Slugs are case-sensitive OpenRouter provider names. This is a per-model routing
> knob, separate from the preset policy above.

### Adding a model without an engine release — preset overlay

If the model reuses existing policy classes (which is the common case), you
don't need to cut a new wheel. Put the same preset entry in a separate YAML
file and point the engine at it:

```yaml
# my_overlay.yaml — same schema as model_presets.yaml
presets:
  my_provider_new_model:
    match: ["my-provider/new-model*"]
    content_policy: anthropic
    reasoning_codec: anthropic
    cache_policy: anthropic_ephemeral
```

Then run:

```bash
tolokaforge run --config run.yaml --presets-file my_overlay.yaml
```

Or set the overlay declaratively in `run.yaml` under `engine.presets_file`.
Precedence is CLI > config field. See
[`docs/CONFIG.md`](CONFIG.md#preset-overlay-file-no-engine-release-required)
and [ADR 0002 — External model registry](adr/0002-external-model-registry.md).

A few rules the overlay enforces (loud-fail at engine startup, naming the
overlay path and the offending key):

- Policy-name strings must resolve to existing classes shipped in the engine.
  Adding a brand-new policy class still requires an engine release.
- Overlay presets are prepended to the iteration order, so overlapping
  `match:` globs let you shadow a bundled preset.
- Same-named overlay presets replace the bundled entry (logged at INFO so the
  swap is visible).

For distributed runs, the overlay path passed to `tolokaforge prepare` is
persisted into the run queue's state file. Subsequent `tolokaforge worker`
invocations pick it up automatically without re-specifying `--presets-file`.

## 3. Add a ModelCertificate

Append to `ALL_MODELS` in
[`tolokaforge/testing/certify/_registry.py`](../tolokaforge/testing/certify/_registry.py).
Pick `required` and `known_unsupported` from the
[`Capability`](../tolokaforge/testing/certify/_capability.py) enum.

**Rules (non-negotiable):**

- **Be honest.** `required` means "the model MUST pass this capability
  or the PR is blocked". `known_unsupported` means "the model can't do
  this; we're certain; the capability test auto-skips".
- **Take a position on every core capability.** The canonical test
  [`tests/canonical/test_capability_registry.py`](../tests/canonical/test_capability_registry.py)
  rejects certificates that silently omit `BASIC_COMPLETION`,
  `SIMPLE_TOOL_CALL`, `MULTI_TURN_TOOL_USE`,
  `USAGE_METRICS_POPULATED`, `COST_USD_POPULATED`, or
  `REQUIRED_FIELDS_COMPLETE`.
- **Slug discipline.** `model_id` MUST equal
  `model_id_slug(provider, name)` from
  [`tolokaforge/core/output/artifacts.py`](../tolokaforge/core/output/artifacts.py).
  The canonical test pins this so per-trial
  `results/tools_schemas/<task>__<model_id>.json` sidecars share the
  same identifier as the registry.
- **Overlap is a bug.** Listing the same capability in both `required`
  and `known_unsupported` raises `ValueError` at construction time.

**Anti-pattern: copying a sibling cert verbatim.**

When a new variant ships in an existing family (e.g. Gemini 3.5 Flash
after 3-flash-preview), it's tempting to copy the sibling's
`known_unsupported` set wholesale. **Don't.** Every entry in
`known_unsupported` is a hypothesis that must be re-tested against
the new variant. Vendors silently fix regressions between versions,
and a stale `known_unsupported` mis-credits the new model.

The disciplined flow:

1. Copy the sibling cert as a *starting hypothesis*.
2. Tentatively flip every `known_unsupported` entry to `required`.
3. Run `pytest --pyargs tolokaforge.testing.certify.suite -k <model_id> -v`.
4. Move back to `known_unsupported` only the capabilities that
   *actually* failed live. The PR diff records exactly which
   regressions persisted vs which were silently fixed.

When a `known_unsupported` declaration stays, leave a code comment
naming the specific failure mode and the date you verified it (e.g.
"emits ``quantity`` instead of registered ``qty`` — verified live
2026-05-20"). That comment is your falsifiable record.

Example:

```python
MC(
    model_id="openrouter__newvendor_new-model-1",
    provider="openrouter",
    name="newvendor/new-model-1",
    env_key="OPENROUTER_API_KEY",
    required=frozenset({
        C.BASIC_COMPLETION,
        C.SIMPLE_TOOL_CALL,
        C.MULTI_TURN_TOOL_USE,
        C.USAGE_METRICS_POPULATED,
    }),
    known_unsupported=frozenset({
        C.DICT_MAP_TOOL_CALL,        # No strict schema support yet
        C.DECIMAL_FIELD_TOOL_CALL,
        C.THINKING_EMITS_BLOCKS,
        C.THINKING_REPLAY_ROUNDTRIP,
        C.PROMPT_CACHING,
    }),
),
```

**Widened certificate fields.** `ModelCertificate` also carries four
default-empty widening fields, populated only when a specific per-model
quirk needs to travel as certificate data rather than test-body code:

- `excluded_capabilities: frozenset[Capability]` — shared-body per-probe
  opt-outs orthogonal to `known_unsupported` (e.g. the muse-spark-1.1
  auto-cache ratchet exclusion — the counter is unreliable on cold
  calls, so the ratchet consults `cert.excluded_capabilities` and skips
  the cert).
- `known_unsupported_reasons: Mapping[Capability, str]` — human-readable
  rationale keyed by capability, surfaced by report generators and
  dashboards.
- `probe_params: Mapping[Capability, Mapping[str, Any]]` — per-model
  probe-parameter overrides (e.g. `{Capability.PROMPT_CACHING: {"prompt_tokens": 12000}}`)
  for shared bodies that consult the map.
- `capability_extras: Mapping[str, str]` — opaque per-model quirks that
  do not fit the `Capability` enum, consulted by adapter code paths.

## 4. Run the capability suite locally

```bash
scripts/with_env.sh uv run pytest --pyargs tolokaforge.testing.certify.suite \
    -k <your_model_id> -v --tb=short
```

Substitute the last slug component of your `model_id`, e.g.
`-k grok-4` or `-k new-model-1`.

**Paste the green output verbatim into the PR description. PRs without
this block do not merge.** The maintainer uses the output to confirm
which capabilities passed live, which were skipped with
`known_unsupported`, and which (if any) skipped because they weren't
declared — the last case is a contributor bug.

**Synthetic-vs-production capability asymmetry.** A capability test
that fails synthetically can still pass in production, and vice
versa. Examples observed in this codebase:

- `IMPLICIT_PROMPT_CACHING` — the synthetic probe uses an 8 k-token
  system prompt, comfortably above the OpenAI / DeepSeek auto-cache
  minimum (~1 k tokens). Gemini 3.5 Flash's auto-cache requires a
  larger minimum; production runs with 13 k+ token prompts show 56 %
  cache hit rates, but the synthetic test returns
  `cached_tokens: 0`. The cert honestly declares
  `known_unsupported` and the comment cites the production
  evidence.
- `REQUIRED_FIELDS_COMPLETE` is `_CORE_CAPABILITIES`-exempt because
  every model passes the single-turn baseline, but multi-turn /
  heavy-context evals surface field-omission failures the synthetic
  probe doesn't catch (gotcha #22 in `AGENTS.md`).

**The cert MUST follow the synthetic test result, not your production
hunch.** If you observe a capability in production that the synthetic
test misses, file the asymmetry in the cert comment and consider
ratcheting the synthetic probe to be more demanding — don't fudge
the cert.

**Field-rename failures are a schema-dialect symptom, not a model
flaw.** If `test_dict_map_tool_call` or `test_discriminated_union_tool_call`
fails with the model emitting "natural English" keys (e.g.
`quantity` for `qty`, `title` for `subject`), the provider almost
certainly does not support some construct in the schema you sent —
typically `$defs`/`$ref`, `oneOf`/`discriminator`, or
`additionalProperties:{schema}`. Bisect by hitting the provider's
REST endpoint directly with a hand-built schema: replace each
suspect construct with explicit `properties` and watch for the
property names to start round-tripping. The fix belongs in a new
[`ToolSchemaSanitizer`](../tolokaforge/core/llm/schema_sanitizer.py)
subclass (see `GeminiSchema` for the worked example) — never in
prompt engineering or in renaming task-pack fields to match the
model's preference.

## 4a. Run a smoke eval before declaring the model ready

The capability suite checks per-request invariants; it does not catch
emergent multi-turn issues like field-omission, looping, or
defensive over-population of optional fields. Before opening a PR
that adds configs across the eval fleet, run **one sampled domain**
through the orchestrator end-to-end (pick the most regression-prone
domain available) and skim the trajectories for:

- Tool calls succeeding then immediately re-firing with identical
  args (the loop pattern guarded by
  `Capability.PROGRESS_AFTER_SUCCESS`).
- Repeated `MULTI_TURN_ERROR_RECOVERY` triggers — if the model
  needs the same tool-error correction loop more than once per
  trial, the production capability is weaker than the synthetic
  test suggests.
- `tool_success_rate < 0.9` per trial.
- `cost_source != 'litellm'` in any `metrics.yaml` — that means
  litellm doesn't know this model and we're falling back to
  `pricing.json`. The fallback works but indicates the pricing layer
  isn't optimal.

## 5. For new reasoning models

If the model exposes reasoning in a new format (not OpenAI
`reasoning_content` summary nor Anthropic `thinking_blocks`):

1. Extend or introduce a
   [`ReasoningCodec`](../tolokaforge/core/llm/reasoning_codec.py)
   subclass.
2. Register it on the preset via the `reasoning_codec:` YAML key.
3. Add a unit-test fixture under
   [`tests/unit/llm/fixtures/`](../tests/unit/llm/fixtures/) capturing
   a real response shape — so the codec round-trip is unit-testable
   without burning provider spend.

## 6. For new non-OpenRouter providers

If the model lives on a provider that isn't already routed through
`LLMClient` (not OpenRouter, not Anthropic direct, not OpenAI direct):

1. **Add a `providers.yaml` entry — no `client.py` / `proxy.py` edit
   required.** Provider transport knobs (endpoint, credential env-var
   names, routability under a gateway, rotation env-var, per-provider
   rate-limit patterns, `custom_llm_provider` litellm hint, and
   Nova-shaped slug rewrite / per-attempt transport pinning) are
   declared in
   [`tolokaforge/core/data/providers.yaml`](../tolokaforge/core/data/providers.yaml).
   See [`docs/LLM_LAYER.md` § Provider bindings](LLM_LAYER.md#provider-bindings)
   and [`docs/CONFIG.md` § Provider bindings](CONFIG.md#provider-bindings-providersyaml)
   for the full `ProviderBinding` schema. A hypothetical new provider
   is onboarded by adding a `providers.yaml` entry only — the ADR-0030
   acceptance criterion.
2. **Never** branch on literal model-name / provider-name substrings
   outside the preset registry and `providers.yaml` — per
   [`AGENTS.md`](../AGENTS.md) § "Adding a new model / provider" rule #3.
3. Declare the new `env_key` on the certificate (e.g.
   `env_key="NOVA_API_KEY"`). The shared `live_client` fixture reads
   this at test-collection time.
4. Add a `@pytest.mark.<provider>` marker to the capability tests you
   expect to run against that provider only — optional.
5. Consider whether the provider deserves its own bespoke test file
   alongside the capability suite (see
   [`tests/integration/llm/test_nova_api.py`](../tests/integration/llm/test_nova_api.py)
   for the Nova precedent — provider-scoped, NOT capability-scoped).

Example `providers.yaml` entry for a hypothetical `acme` provider that
publishes an OpenAI-compatible endpoint, uses rotation, and needs no
slug rewrite:

```yaml
acme:
  endpoint: "https://api.acme.example.com/v1"
  api_base_env: "ACME_API_BASE"
  api_key_env: "ACME_API_KEY"
  api_keys_env: "ACME_API_KEYS"       # comma-separated; enables rotation
  unroutable: false                    # gateway may route it
  custom_llm_provider: null            # let litellm default
  rate_limit_patterns:                 # anchored shapes (see DEFAULT_RATE_LIMIT_PATTERNS)
    - '\bRateLimitError\b'
    - '(?i)(?:error\s+code|status(?:[\s_-]*code)?|http(?:/[\d.]+)?)\s*[:=]?\s*429(?!\d)'
    - '(?i)\btoo\s+many\s+requests\b'
    - '(?i)\brate[\s_-]?limit(?:s|ed|ing)?[\s:;,.-]*(?:error|exceeded|reached|hit)\b'
  format_model_name_bare: false
  kwargs_pin_transport: false
  slug_rewrite: null
```

## Capability definitions

| Capability | Guards | Test file |
|---|---|---|
| `basic_completion` | Model returns non-empty text for a simple turn. | [`test_basic_completion.py`](../tolokaforge/testing/certify/suite/test_basic_completion.py) |
| `simple_tool_call` | Model emits a valid structured tool call when offered a calculator + distractor pair. | [`test_simple_tool_call.py`](../tolokaforge/testing/certify/suite/test_simple_tool_call.py) |
| `multi_turn_tool_use` | Two-turn flow: tool call → tool result → final answer OR chained tool call. | [`test_multi_turn_tool_use.py`](../tolokaforge/testing/certify/suite/test_multi_turn_tool_use.py) |
| `dict_map_tool_call` | Typed `Dict[str, T]` parameters round-trip as native dicts. | [`test_dict_map_tool_call.py`](../tolokaforge/testing/certify/suite/test_dict_map_tool_call.py) |
| `decimal_field_tool_call` | Pydantic `Decimal` fields don't trip the provider's regex validator. | [`test_decimal_field_tool_call.py`](../tolokaforge/testing/certify/suite/test_decimal_field_tool_call.py) |
| `thinking_emits_blocks` | Provider surfaces structured thinking blocks, not a concatenated summary. | [`test_thinking_emits_blocks.py`](../tolokaforge/testing/certify/suite/test_thinking_emits_blocks.py) |
| `thinking_replay_roundtrip` | Signed thinking blocks from turn 1 echo back verbatim on turn 2. | [`test_thinking_replay_roundtrip.py`](../tolokaforge/testing/certify/suite/test_thinking_replay_roundtrip.py) |
| `prompt_caching` | Second identical call hits the provider-side ephemeral cache. | [`test_prompt_caching.py`](../tolokaforge/testing/certify/suite/test_prompt_caching.py) |
| `usage_metrics_populated` | `result.usage.prompt_tokens`, `.completion_tokens`, and `.provider_raw` populated. | [`test_usage_metrics_populated.py`](../tolokaforge/testing/certify/suite/test_usage_metrics_populated.py) |
| `tool_name_discipline` | Model echoes the EXACT registered tool name even when it contains repeated `_`-segments (e.g. `workday_api_workday_api_get_employee`). Catches the Gemini 3.1 Pro `:` substitution regression. | [`test_tool_name_discipline.py`](../tolokaforge/testing/certify/suite/test_tool_name_discipline.py) |
| `lexical_tool_invention` | Model does NOT fabricate a plausible-but-nonexistent tool name from the system-prompt vocabulary (e.g. inventing `knowledge_base_search_policy` when the registered tool is `typesense_search_policy`). | [`test_lexical_tool_invention.py`](../tolokaforge/testing/certify/suite/test_lexical_tool_invention.py) |
| `required_fields_complete` | Single-turn baseline: when a tool's schema marks N fields as `required` and the user provides values for every one, the model emits all N. **Core capability** — every modern function-calling model passes. | [`test_required_fields_complete.py`](../tolokaforge/testing/certify/suite/test_required_fields_complete.py) |
| `unsigned_thinking_replay` | Reasoning *text* from turn 1 round-trips into turn 2's request payload via the codec's `encode_for_replay` path, even when blocks lack signatures. Gemini-Pro-applicable variant of `thinking_replay_roundtrip`. | [`test_unsigned_thinking_replay.py`](../tolokaforge/testing/certify/suite/test_unsigned_thinking_replay.py) |
| `progress_after_success` | Single-turn baseline: after a successful tool call + acknowledgment from the user, the model does NOT re-emit the same tool call with the same arguments. **Core capability** — every modern function-calling model passes the synthetic probe. Catches the grok-4.3 production loop pattern (re-calling `salesforce_create_case` 17× after success) at its single-turn surface. | [`test_progress_after_success.py`](../tolokaforge/testing/certify/suite/test_progress_after_success.py) |

## Related reading

- [`AGENTS.md`](../AGENTS.md) § "Adding a new model / provider" —
  non-negotiable merge rules.
- [`docs/LLM_LAYER.md`](LLM_LAYER.md) — policy slot contracts +
  implementation notes.
- The Gemini certificates in
  [`tolokaforge/testing/certify/_registry.py`](../tolokaforge/testing/certify/_registry.py)
  are a worked example of an entire model family registered with
  asymmetric postures across variants (Flash `required` vs Pro
  `known_unsupported` for the same capability) — useful when a new
  family ships with a known open regression.
