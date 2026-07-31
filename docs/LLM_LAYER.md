# LLM Layer

Single reference for the [`tolokaforge/core/llm/`](../tolokaforge/core/llm/)
package. This layer is the **only** place provider-specific shapes
(`thinking_blocks`, `cache_control`, `reasoning_content`, …) are allowed to
appear — callers above it work with the curated Python types described below.

See [`plans/llm_reasoning_and_observability_fix.md`](../plans/llm_reasoning_and_observability_fix.md)
for the design rationale and the canonical litellm surface.

## Module map

| Module | Purpose |
|---|---|
| [`reasoning.py`](../tolokaforge/core/llm/reasoning.py) | `ReasoningConfig`, `ReasoningBlock`, `StructuredReasoning` datatypes |
| [`reasoning_codec.py`](../tolokaforge/core/llm/reasoning_codec.py) | Per-provider extract + replay Protocol |
| [`usage.py`](../tolokaforge/core/llm/usage.py) | Normalized `Usage` dataclass + extractor |
| [`schema_sanitizer.py`](../tolokaforge/core/llm/schema_sanitizer.py) | Tool-schema sanitizer Protocol + `SchemaCapability` enum |
| [`cache_policy.py`](../tolokaforge/core/llm/cache_policy.py) | Prompt / tool cache-control injection Protocol |
| [`prompt_policy.py`](../tolokaforge/core/llm/prompt_policy.py) | System-prompt enrichment (`DictMapHints`) |
| [`params_policy.py`](../tolokaforge/core/llm/params_policy.py) | Generation parameter adaptation |
| [`content_policy.py`](../tolokaforge/core/llm/content_policy.py) | Tool-result content format (OpenAI / Anthropic) |
| [`response_policy.py`](../tolokaforge/core/llm/response_policy.py) | Tool-call argument post-processing |
| [`capabilities.py`](../tolokaforge/core/llm/capabilities.py) | `ModelCapabilities` frozen dataclass |
| [`presets.py`](../tolokaforge/core/llm/presets.py) | YAML preset loader → `ModelCapabilities`. Also implements the **operator-overridable preset overlay** (`--presets-file`, `engine.presets_file`) so new model registrations don't require an engine release — see [ADR 0002](adr/0002-external-model-registry.md) and [`docs/CONFIG.md` § Preset overlay file](CONFIG.md#preset-overlay-file-no-engine-release-required). |
| [`proxy.py`](../tolokaforge/core/llm/proxy.py) | Optional LLM-gateway transport (`ProxyConfig`), e.g. a LiteLLM proxy; configured entirely by env |
| [`client.py`](../tolokaforge/core/llm/client.py) | `LLMClient`, `GenerationResult`, `UserSimulator` |

## `reasoning`

Provider-agnostic declarative types for thinking / reasoning:

```python
from tolokaforge.core.llm import ReasoningConfig, ReasoningBlock, StructuredReasoning

# Config lives on ModelConfig.reasoning; never a bare string.
cfg = ReasoningConfig(mode="budget", budget_tokens=8000, display="visible")

# Extracted response reasoning.
reasoning = StructuredReasoning(
    blocks=(ReasoningBlock(type="thinking", text="step 1", signature="sig"),),
    summary=None,
    budget_used=512,
)
```

`ReasoningMode` is `Literal["off", "adaptive", "budget"]`. `display` is
`Literal["visible", "summary", "omitted"]`.

## `reasoning_codec`

Provider-specific adapters for two operations:

```python
class ReasoningCodec(Protocol):
    def extract(self, response_message: Any) -> StructuredReasoning | None: ...
    def encode_for_replay(self, reasoning: StructuredReasoning) -> dict[str, Any]: ...
```

Three concrete codecs ship today. Selection is driven by the preset registry —
the client never branches on provider. See
[`plans/llm_reasoning_and_observability_fix.md`](../plans/llm_reasoning_and_observability_fix.md)
§ "Canonical litellm surface" for the interface-design rationale.

| Codec | Used by | `extract` source | `encode_for_replay` |
|---|---|---|---|
| `NoReasoningCodec` | default | — (always `None`) | `{}` |
| `AnthropicReasoningCodec` | `anthropic` preset | `message.thinking_blocks` + `message.reasoning_content` | `{"thinking_blocks": [...]}` |
| `OpenAIReasoningCodec` | `openai_gpt5` / `xai_grok` / `qwen` presets | `message.reasoning_content` | `{}` (no replay contract) |

### `AnthropicReasoningCodec` contract (Stage 3, fixes P4a + P4c)

Per [Part 5.3 of the diagnosis](../plans/eval_output_new_diagnosis.md:571),
the pre-Stage-3 client flattened `thinking_blocks` into a concatenated string,
dropping `signature` bytes and `redacted_thinking` markers — exactly the bytes
Anthropic requires us to echo back on the next turn to sustain interleaved
thinking.

`AnthropicReasoningCodec.extract`:

* Prefers `thinking_blocks` over the `reasoning_content` summary — block
  types, signatures, and `redacted_thinking` payloads are preserved verbatim.
* Returns each block as a :class:`ReasoningBlock`:
  * `{"type": "thinking", "thinking": str, "signature": str}` →
    `ReasoningBlock(type="thinking", text=..., signature=...)`
  * `{"type": "redacted_thinking", "data": str}` →
    `ReasoningBlock(type="redacted_thinking", text="", encrypted_data=...)`
* **Raises** `ValueError` on unknown block types or non-dict entries — we do
  not silently drop data we cannot interpret (AGENTS.md rule #1).
* Returns `None` when both `thinking_blocks` and `reasoning_content` are
  empty/absent.
* Empty `.thinking` text plus populated `.signature` (Claude 4.7's
  `display="omitted"` default) is preserved as-is so the
  interleaved-thinking replay (Stage 4) can round-trip the signatures.

### Interleaved-thinking replay contract (Stage 4, fixes P4b)

`_convert_messages` in [`client.py`](../tolokaforge/core/llm/client.py)
splices `encode_for_replay`'s return dict onto every assistant message
that carries a `StructuredReasoning`:

```python
# tolokaforge/core/llm/client.py
if msg.role == MessageRole.ASSISTANT and msg.reasoning is not None:
    replay_payload = self.capabilities.reasoning_codec.encode_for_replay(
        msg.reasoning
    )
    if replay_payload:
        litellm_msg.update(replay_payload)
```

Consequences:

* **Zero provider-specific conditionals.** The codec Protocol is the only
  abstraction the client touches — no `isinstance(codec, AnthropicReasoningCodec)`,
  no `if provider == "anthropic"`. Adding a new reasoning-capable provider
  is a preset entry + codec class, nothing more.
* **`NoReasoningCodec.encode_for_replay` returns `{}`**, so non-reasoning
  presets never grow a `thinking_blocks` key even if a stray
  `msg.reasoning` leaked in from a cross-provider replay attempt.
* **`OpenAIReasoningCodec.encode_for_replay` also returns `{}`** — OpenAI
  has no stateful interleaved-thinking contract; echoed reasoning is not
  accepted on the wire.
* **`AnthropicReasoningCodec.encode_for_replay` returns
  `{"thinking_blocks": [...]}`** — the canonical litellm first-class field
  per the `litellm thinking_blocks` docs above. litellm forwards the
  payload untouched to Anthropic; no hand-crafted content-block arrays.
* **Empty blocks tuple → `{}`.** `StructuredReasoning(blocks=(), summary="x")`
  yields no `thinking_blocks` key. The assistant dict only grows the key
  when there is something concrete to replay.

`AnthropicReasoningCodec.encode_for_replay` emits the canonical litellm
first-class assistant-message field per
[`litellm thinking_blocks` docs](https://docs.litellm.ai/docs/reasoning_content):

```python
{"thinking_blocks": [
    {"type": "thinking", "thinking": "...", "signature": "..."},
    {"type": "redacted_thinking", "data": "..."},
]}
```

Note the asymmetry: `redacted_thinking` blocks carry only `type` + `data`
in the replay shape — **no** `thinking` or `signature` keys. Attempting to
encode a `summary_text` block raises `ValueError` (summary_text is an
OpenAI-family shape and is not replayable to Anthropic).

### `OpenAIReasoningCodec` contract

OpenAI surfaces reasoning as a single `reasoning_content` string; no
structured blocks and no signatures. `extract` returns a
:class:`StructuredReasoning` with a single
`ReasoningBlock(type="summary_text", text=reasoning_content)` plus the same
text on `.summary`. `encode_for_replay` is a no-op (`{}`) — OpenAI has no
stateful interleaved-thinking contract and does not accept echoed
reasoning on subsequent turns.

### Preset wiring

```yaml
# tolokaforge/core/data/model_presets.yaml
presets:
  anthropic:
    match: ["anthropic/*", "*claude*"]
    reasoning_codec: anthropic
    # …
  openai_gpt5:
    match: ["openai/gpt-5*", "*gpt-5*"]
    reasoning_codec: openai
    # …
  xai_grok:
    reasoning_codec: openai
  qwen:
    reasoning_codec: openai
```

Unset → `none` default. The client always calls
`self.capabilities.reasoning_codec.extract(message)` — there is no inline
branching on provider or on `hasattr(message, "thinking_blocks")`.

## `usage`

Full, normalized token + cache accounting — Stage 5 (P7) deleted the
flat `{input, output}` dict and replaced it with the frozen
[`Usage`](../tolokaforge/core/llm/usage.py:1) dataclass. Every field is
populated from the litellm-canonical `ModelResponse.usage` surface by
`UsageExtractor`; there is no inline branching on provider attributes
anywhere else in the codebase.

```python
from tolokaforge.core.llm import Usage, UsageExtractor

extractor = UsageExtractor()
usage: Usage = extractor.extract(response)
# Access pattern:
usage.prompt_tokens                 # usage.prompt_tokens (all providers)
usage.completion_tokens             # usage.completion_tokens (all providers)
usage.reasoning_tokens              # usage.completion_tokens_details.reasoning_tokens
usage.cached_tokens                 # usage.prompt_tokens_details.cached_tokens
usage.cache_creation_input_tokens   # Anthropic; writes to ephemeral cache (dual-path — see below)
usage.cache_read_input_tokens       # Anthropic; reads from ephemeral cache  (dual-path — see below)
usage.provider_raw                  # dict — JSON-safe dump of the raw usage block
```

The extractor never raises — missing attributes default to `0`, so
observability degrades gracefully when a provider returns a partial
usage block.

### Dual-path Anthropic cache counters (Stage 6 follow-up)

Anthropic cache counters are surfaced under two different usage paths
depending on provider routing. `UsageExtractor` reads both and normalises
onto the same `Usage.cache_creation_input_tokens` /
`cache_read_input_tokens` fields — downstream code stays
routing-agnostic.

| Routing | Cache-write field | Cache-read field |
|---|---|---|
| Direct Anthropic API | `usage.cache_creation_input_tokens` (top-level) | `usage.cache_read_input_tokens` (top-level) |
| OpenRouter-routed Anthropic | `usage.prompt_tokens_details.cache_write_tokens` (nested) | `usage.prompt_tokens_details.cache_read_tokens` (nested) |

OpenRouter zeroes the top-level Anthropic fields and re-surfaces cache
counters under the nested `prompt_tokens_details` block — see
[OpenRouter prompt-caching docs](https://openrouter.ai/docs/use-cases/prompt-caching).
`UsageExtractor` gives **top-level precedence**: the nested path is read
only when the top-level field is zero and the nested field is non-zero.
Direct-Anthropic callers therefore never have their values overridden,
while OpenRouter-routed callers see the same observable shape as direct
callers. Regression guard:
[`tests/unit/llm/test_usage.py::TestUsageExtractorFixtures::test_openrouter_anthropic_usage_surfaces_cache_write_tokens`](../tests/unit/llm/test_usage.py:1)
against fixture
[`openrouter_anthropic_usage.json`](../tests/unit/llm/fixtures/openrouter_anthropic_usage.json:1).

### Accumulation (`__add__`)

`Usage` is frozen and addable:

```python
total = Usage() + result1.usage + result2.usage
# total.prompt_tokens == result1.usage.prompt_tokens + result2.usage.prompt_tokens
# (every field is summed field-wise)
```

`provider_raw` follows a **latest-wins** convention — per-call raw
dicts describe different provider calls with potentially different
shapes, so accumulating them produces junk. The right-hand operand's
dict wins; the final accumulated `Usage.provider_raw` is the last
call's raw block (typically the most useful for auditing cache
hit-rate on the terminal turn).

Adding a non-`Usage` returns `NotImplemented`, so Python surfaces a
`TypeError` — we never silently coerce.

### Where `Usage` lands in the output

`LLMClient.generate(...)` returns a `GenerationResult` whose `usage`
attribute is always a `Usage` (never a dict, never `None`). The runner
accumulates via `self.metrics.usage = self.metrics.usage + result.usage`;
`Metrics.usage` is the field embedded in the trajectory, and
`Metrics.model_dump(mode="json")` emits the full dict shape into
[`metrics.yaml`](OUTPUT_FORMAT.md:98). Per-trial aggregates live on
[`tolokaforge.core.metrics`](../tolokaforge/core/metrics.py:1) under
`avg_<field>` / `total_<field>` keys for each `Usage` field
(`avg_prompt_tokens`, `avg_reasoning_tokens`, `avg_cache_read_input_tokens`,
etc.).

## `schema_sanitizer`

```python
class ToolSchemaSanitizer(Protocol):
    def sanitize(self, tools: list[dict]) -> list[dict]: ...
    def supported_capabilities(self) -> frozenset[SchemaCapability]: ...
```

`SchemaCapability` enumerates `DICT_MAP_TYPED`, `REGEX_PATTERN`,
`DATE_TIME_FORMAT`, `ANYOF_NUMERIC_STRING`. Presets declare which are required
by the target model; a sanitizer advertises which pass through unchanged.

Three concrete sanitizers ship today:

* `PassthroughSchema` — preserves the full capability set. Used for models
  that accept arbitrary JSON Schema (Anthropic; the `default` preset).
* `StrictSchema` — used by `openai_gpt5` and `xai_grok`. Removes only
  `DICT_MAP_TYPED` (typed dict-maps → array-of-objects) and
  `ANYOF_NUMERIC_STRING` (Pydantic Decimal idiom → plain `number`). All
  other capabilities (`REGEX_PATTERN`, `DATE_TIME_FORMAT`, metadata
  keywords like `title` / `examples`) pass through unchanged. The `qwen`
  preset uses `passthrough` instead — see § `response_policy` for the
  rationale.
* `GeminiSchema(StrictSchema)` — used by the `gemini` preset. Adds
  `flatten_oneof_discriminator=True` on top of `StrictSchema`'s rewrites
  because Gemini's tool spec is a JSON-Schema *subset* that does not
  document `$defs`/`$ref`, `oneOf`/`anyOf` with object branches, or
  `discriminator` — sending these constructs causes Gemini to silently
  lose every property name inside them and emit description-derived
  English keys instead (verified live 2026-05-20). The flattener
  collapses `oneOf` discriminated unions into a single object schema
  unioning every branch's `properties`; intersects `required` (so
  typically only the discriminator survives); special-cases the
  discriminator field by merging per-branch `const` values into a single
  `enum`. Paired with `response_policy: array_dict_map` to reverse the
  dict-map → array transform. See [`AGENTS.md`](../AGENTS.md) gotcha #21
  for the wire-level symptom.

### `StrictSchema` contract — preserve information by default, fail loudly on hazards

The sanitiser is **position-aware**: it walks the JSON-Schema tree
distinguishing metadata-keyword positions (`type`, `properties`, `items`,
`description`, …) from property-name positions (children of
`properties: {…}`, `patternProperties: {…}`, `$defs: {…}`,
`definitions: {…}`). Property names are opaque strings and are never
matched against any metadata-strip list — this closes the bug class where
a property literally named `title`, `examples`, or `format` was deleted as
if it were a JSON-Schema keyword.

The sanitiser performs only the rewrites known to break GPT-5 /
xAI / Qwen-strict tool-schema validators:

1. **`$defs` / `$ref` resolution** — refs are inlined and `$defs` is
   removed from the output (per-tool, with cycle detection).
2. **Pydantic `Decimal` `anyOf` collapse** — the only `anyOf` rewrite —
   `[{type:number}, {type:string, pattern:…}]` becomes plain
   `{type:"number"}` with `description` preserved. The negative-lookahead
   regex Pydantic embeds in the string branch is RE2-incompatible
   (causes upstream 500s), and there's no portable cross-provider mapping
   for the union, so we collapse. Every other `anyOf` shape (e.g.
   `Optional[str]` → `[{type:string}, {type:null}]`) is preserved.
3. **Typed dict-map → array** — `{type:object, additionalProperties:{schema}}`
   becomes `{type:array, items:{type:object, properties:{key, …value_props}}}`
   so GPT-5 / xAI-Grok / Qwen-strict has structural type info to anchor on.
   `ArrayDictMapResponse` reverses this on the model's emitted arguments.
4. **RE2-incompatible `pattern` strip** — the *only* metadata-strip the
   sanitiser still performs, and it is **value-conditional**: a `pattern`
   value is removed only when it contains a lookaround (`(?!`, `(?=`,
   `(?<!`, `(?<=`) or a backreference (`\1`..`\9`). Safe patterns
   (e.g. `^SKU-[A-Z0-9]+$`) pass through unchanged.
5. **Parameters-root `description` strip** — the one place this is
   redundant noise (Pydantic emits the model class's docstring there);
   `function.description` already covers what the model needs.

Everything else passes through verbatim: `title`, `examples`, `format`,
`minProperties`, `maxProperties`, `additionalProperties: true`,
`additionalProperties: false`, plain regex patterns, enum values, default
values, `minimum` / `maximum` / `minLength` / `maxLength`, and any user-
supplied metadata. Removing this signal silently caused three production
regressions (post-PR-#88 diagnosis); the position-aware contract closes
all three.

### Structural-invariant validation (loud-fail post-condition)

After sanitisation, `StrictSchema._validate_invariants` walks every output
tool and raises `SchemaInvariantError` (subclass of `ValueError`) on the
first violation of either invariant:

* **`set(required) ⊆ set(properties.keys())`** for every object schema in
  every tool. This is the regression guard for the property-name-as-
  metadata-key bug class — any future code path that drops a property
  without dropping it from `required` raises here, instead of shipping a
  broken schema to the provider.
* **No RE2-incompatible regex remains** anywhere in the output. Catches
  the case where the Decimal collapse missed a non-canonical `anyOf`
  shape.

Both invariants are also pinned by the canonical contract test (every
preset × a fixture tool with property names colliding with JSON-Schema
keywords).

### Past failure modes (now guarded)

The pre-position-aware sanitiser produced three classes of silent
corruption observed in the post-PR-#88 production run:

| Bug | Symptom | Domain impact |
|---|---|---|
| Property literally named `title` deleted | `required: […, "title", …]` survived but `properties.title` was gone — provider rejected every call with `Field required` | ots_bank_hr_d365 (5.6 % pass@1), ots_travel_marketplace_external_support (4.9 %) |
| Free-form `{type:object, additionalProperties:true, examples:[…]}` reduced to bare `{type:object}` | Model alternated between omit / flat-pack / right-shape | ots_19_airlines (999/1000 trials with schema errors) |
| `examples` stripped from primitive strings | Model lost the only formatting hint for non-obvious shapes | logistics domain (438 `pay_period` errors) |

See [`plans/eval_post_pr88_schema_sanitizer_diagnosis.md`](../plans/eval_post_pr88_schema_sanitizer_diagnosis.md)
for the full evidence trail.

## litellm OpenRouter routing caveat

`OpenrouterConfig` in litellm inherits from `OpenAIGPTConfig` (generic),
**not** `OpenAIGPT5Config`. When calling GPT-5 through OpenRouter, litellm
does not apply GPT-5-specific parameter handling (e.g.
`max_tokens → max_completion_tokens`). Tool schemas are passed through
unchanged — litellm's native `_remove_additional_properties` only runs for
Vertex AI, hosted vLLM, and WatsonX. Our `StrictSchema` and `DictMapHints`
policies in `tolokaforge/core/llm/` handle **all** GPT-5 tool-schema
adaptation independently of litellm, so this gap is transparent to callers.

## `proxy` — routing calls through an LLM gateway

Some deployments forbid direct provider access: calls must go through a gateway
that holds the upstream keys, enforces budgets, and attributes spend. A
[LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy) is the reference
target; any gateway presenting the same surface works.

[`tolokaforge/core/llm/proxy.py`](../tolokaforge/core/llm/proxy.py) resolves
that transport from the environment. It is deployment-neutral — the module knows
nothing about any specific gateway product or organisation. Everything
deployment-specific, including the attribution headers a given gateway
demands, is supplied as configuration.

**"The same surface" is a narrower contract than "OpenAI-compatible".** What is
actually required is: *for each routed provider, the gateway serves the route
that litellm's transport for that provider targets.* This layer only overrides
the base URL — litellm decides the path, the auth header, and the body shape,
per provider. That is why routing is an allow-list rather than a blanket
redirect, and why the allow-list is pinned against the installed litellm by
[`tests/canonical/test_llm_gateway_envelope_contract.py`](../tests/canonical/test_llm_gateway_envelope_contract.py):
a dependency bump that changes a provider's transport fails there instead of
posting to a route the gateway does not serve.

| Variable | Meaning |
|---|---|
| `LLM_PROXY_BASE_URL` | Gateway base URL. **Setting this enables the transport**; everything else is optional. |
| `LLM_PROXY_API_KEY` | Credential presented to the gateway. Omit only for gateways that authenticate by network position — litellm then falls through to its provider-env lookup and forwards the *provider's* key to the gateway host instead. |
| `LLM_PROXY_HEADERS` | JSON object of static headers added to every request, e.g. `{"X-Team-Id": "research"}`. Wins over the engine's own provider headers on a name collision. |
| `LLM_PROXY_REQUEST_ID_HEADER` | Header *name* that receives a fresh UUID4 per request. A static env var cannot express "new value per call". |
| `LLM_PROXY_PROVIDERS` | Comma-separated provider allow-list, replacing the default. Read the routing table below before widening it. |

All five resolve through `SecretManager`, so `.env`, the process environment,
and the runner container's `TOLOKAFORGE_SECRETS_JSON` behave identically.
A malformed value raises `ProxyConfigError` at the first `LLMClient`
construction rather than running a whole evaluation with unattributed spend.
Setting any companion variable while `LLM_PROXY_BASE_URL` is empty also raises,
so a typo in the base-URL name cannot silently fall back to direct provider
access.

### Which providers can be routed

**Setting `api_base` does not make litellm speak OpenAI to that URL — it makes
litellm speak that provider's native protocol to that URL.** Captured against
litellm 1.87.0:

| provider | request litellm sends to the gateway |
|---|---|
| `openrouter/…` | `POST {base}/chat/completions`, bearer auth |
| `openai/…` | `POST {base}/chat/completions`, bearer auth |
| `anthropic/…` | `POST {base}/v1/messages`, `x-api-key` |
| `gemini/…` | `POST {base}/models/<m>:generateContent`, `x-goog-api-key` |

Only the first two are an OpenAI-envelope request, so `DEFAULT_ROUTED_PROVIDERS`
is exactly `{openrouter, openai}`. Naming another provider in
`LLM_PROXY_PROVIDERS` is allowed and means "my gateway also serves that
provider's native route" — true of a LiteLLM proxy's `/v1/messages`
passthrough, false of a plain OpenAI-compatible gateway.

`mock` and `nova` are in `UNROUTABLE_PROVIDERS` and are rejected even when
named explicitly. `mock` never reaches the wire. `nova` depends on
`_call_with_key_rotation` rewriting its bare model name into `openai/<name>`
next to its own hardcoded base URL; a gateway replaces the base URL but not the
rewrite, so litellm would get a provider-less model string and raise
`BadRequestError` before sending anything.

### The model name must be the gateway's route name

Enabling the gateway is **not** a drop-in for existing run configs. litellm
strips exactly one provider prefix before sending, so `provider: openrouter` +
`name: anthropic/claude-opus-4.7` puts `anthropic/claude-opus-4.7` on the wire.
A gateway resolves that against *its own* model table, which need not mean what
the provider would mean by it.

Observed on a real LiteLLM proxy: that name matched the gateway's catch-all
`anthropic/*` route, which is backed by **Bedrock**, so the request was served
by a different upstream than the config asked for. It failed loudly there only
because that particular Bedrock model rejects `temperature=0.0`; a
closer-matching route would have silently evaluated a different serving path.
For a leaderboard that is a comparability break, not a transport detail.

So name the model the way the gateway names it, and pick the provider so that
litellm's prefix strip leaves that name intact:

```yaml
# Gateway route "openrouter/anthropic/claude-opus-4.7"
provider: openai                              # wire: openrouter/anthropic/claude-opus-4.7
name: openrouter/anthropic/claude-opus-4.7

# Gateway route "azure_ai/cohere-command-a-plus-05-2026"
provider: openai                              # wire: azure_ai/cohere-command-a-plus-05-2026
name: azure_ai/cohere-command-a-plus-05-2026
```

List the routes a gateway serves with `GET {base_url}/models`.

Gateway-specific names have two consequences, both following from the
model-string coupling described below:

- **Cost may be unknown.** `normalize_model_name` cannot map
  `openai/openrouter/anthropic/claude-opus-4.7` to a `pricing.json` key, so
  `cost_usd` depends on the gateway returning cost in the response. Measured on
  a LiteLLM proxy: its `azure_ai/…` route did (`cost_source="litellm"`), its
  `openrouter/…` route did not (`cost_source="unknown"`). Add a `pricing.json`
  entry keyed on the exact formatted model string when the gateway is silent.
- **`provider: openai` does not get the `providers.openrouter` preset overlay**,
  so reasoning routes through `reasoning_effort` rather than
  `extra_body.reasoning`. That is correct for an OpenAI-shaped gateway endpoint,
  but verify it for reasoning models before trusting a run.

**This is a transport swap and nothing else.** `_build_kwargs` sets `api_base`,
`api_key`, and `extra_headers`; the litellm model string keeps its original
`<provider>/<name>` shape. That invariant is load-bearing in two places:

- Preset `match:` globs resolve off `ModelConfig.name` and the `providers:`
  overlay off `ModelConfig.provider` (see [`presets`](#presets)). Re-prefixing
  the model, or renaming the provider to something gateway-specific, silently
  drops the matched preset and the `reasoning_via_extra_body` overlay — the
  reported `effective_preset` would not change, but the reasoning wire format
  would.
- [`normalize_model_name`](../tolokaforge/core/pricing.py) strips exactly one
  leading `openrouter/` and then returns any remaining slash-bearing name
  verbatim. A second prefix guarantees a pricing-table miss, degrading
  `cost_source` to `"unknown"` and tripping `Capability.COST_USD_POPULATED`.

Because the provider string is untouched, provider-specific request shaping
still applies on top of the gateway: OpenRouter's `HTTP-Referer` / `X-Title`
headers and its `extra_body.provider` upstream pinning both survive. On a
header-name collision the gateway's configured header wins, since that is
explicit operator configuration and the other is an engine default.

### Verifying a gateway from CI

[`tests/integration/llm/test_gateway_live.py`](../tests/integration/llm/test_gateway_live.py)
answers what unit tests structurally cannot: whether a real gateway accepts what
this engine sends it. The unit suite stops at the kwargs dict, and the two
failure modes that matter most — a gateway resolving our model name to a route we
did not intend, and a gateway rejecting a request shape litellm produced — both
live past that boundary.

It runs in the normal `tests/integration/` lane and **skips unless its own
credential is present**, so a checkout without the secret is quiet:

| Variable | Secret? | Meaning |
|---|---|---|
| `LLM_PROXY_INT_TEST_API_KEY` | **yes** | Gateway credential dedicated to integration testing. Its presence is the on-switch. The fixture overrides `LLM_PROXY_API_KEY` with it for the test's duration, so CI spend stays on its own budget and a local `.env` cannot charge the production key. |
| `LLM_PROXY_INT_TEST_MODEL` | no | The model name **as the gateway routes it**. Required rather than defaulted: a wrong guess would exercise the gateway's fallback behaviour instead of this transport. Plain config — belongs in a workflow's `env:`. |
| `LLM_PROXY_INT_TEST_BASE_URL` | depends | Gateway base URL; falls back to `LLM_PROXY_BASE_URL`. Keep it out of a public workflow file if the hostname is internal. |
| `LLM_PROXY_INT_TEST_PROVIDER` | no | Optional, default `openai` — see the model-naming section above. |

Three tests: one asserts the transport is applied and billed to the test key
without spending, two make one small call each (a completion and a tool call,
capped at 256 output tokens).

**The gating is asymmetric on purpose.** No credential → skip, quietly, which is
the state of any checkout without the secret. Credential present but a companion
missing → **fail**. Holding the key is an explicit statement that this
environment means to run the test, so a missing route name is a misconfiguration
rather than an opt-out. Skipping there would let a pipeline report green while
testing nothing.

### Key rotation under a gateway

Rotation stays bound to the provider key chain (`OPENROUTER_API_KEYS` and
friends), and whether it still applies depends on **one** thing: is a gateway
key pinned?

- **`LLM_PROXY_API_KEY` set** — rotation is skipped. `_rotate_key` republishes
  `OPENROUTER_API_KEY` into the environment, but the pinned `api_key` kwarg
  takes precedence in litellm, so rotating would resend byte-identical requests
  and then report an exhausted key chain that was never in play. A gateway
  quota or authorization rejection raises an error naming the gateway URL
  instead.
- **`LLM_PROXY_API_KEY` unset** (gateway authenticates by network position) —
  rotation works and is left alone, because litellm reads the provider env var
  that `_rotate_key` rewrites. Suppressing it here would abort a trial with
  unused keys still in the chain.

The guard mirrors exactly the condition under which `_build_kwargs` pins the
key, so the two can't drift.

## `cache_policy`

Explicit prompt-caching marker injection. The policy is invoked inside
[`LLMClient.generate`](../tolokaforge/core/llm/client.py) **after** prompt
enrichment + tool-schema sanitisation, **before** `_convert_messages` —
so the sanitizer never sees a `cache_control` key, and the wire-level request
carries the marker on the final cacheable prefix.

```python
class CachePolicy(Protocol):
    def apply(
        self,
        system: str | list[dict] | None,
        tools: list[dict] | None,
        messages: list[dict],
    ) -> tuple[str | list[dict] | None, list[dict] | None, list[dict]]: ...
```

Two concrete policies ship today.

| Policy | Default for | Effect |
|---|---|---|
| `NoCache` | `default` / `openai_gpt5` / `xai_grok` / `qwen` / `aws_nova` | Pure passthrough — inputs returned verbatim. |
| `AnthropicEphemeralCache` | `anthropic` / `anthropic_claude_4_7` | Marks the **last** system content-block + **last** tools entry with `cache_control: {type: ephemeral}` (5-minute TTL, Anthropic default). |

### `AnthropicEphemeralCache` contract (Stage 6, fixes P8)

Per Part 4.R4 of
[`plans/eval_output_new_diagnosis.md`](../plans/eval_output_new_diagnosis.md:390),
every Claude turn pre-Stage-6 re-billed the 18 k-token system prompt + 8 k-token
tool schemas because we never emitted a `cache_control` hint. Observable via
zero `Metrics.usage.cache_read_input_tokens` on any second call with an
identical system prompt.

`AnthropicEphemeralCache.apply`:

* Accepts `system` as either `str`, a list-of-content-blocks, or `None`.
  Unknown types raise `TypeError` — no silent drop.
* Wraps a string `system` as `[{"type": "text", "text": <s>, "cache_control": {"type": "ephemeral"}}]`.
* When `system` is already a list of content-blocks (e.g. re-invoked on a
  cached payload), it marks the **last** block only — any prior
  `cache_control` on that block is replaced, not stacked.
* Empty string / empty list / `None` are no-ops — we never ship a cached
  empty block.
* Tools: when non-empty, marks the **last** entry with `cache_control` —
  this caches the whole tools-array prefix. Also replaces any caller-supplied
  `cache_control` on the last entry (idempotent).
* Messages are returned unchanged — Stage 6 caches system + tools only.
  Message-level caching is deferred (see Residual risk in the Stage 6
  report).
* Operates on shallow copies — caller dicts are never mutated.
* The 5 m TTL is the Anthropic default; Stage 6 exposes no TTL knob.

### Example — Anthropic request transformation

Input to `apply`:

```python
system = "You are a helpful assistant."
tools = [
    {"type": "function", "function": {"name": "a", "parameters": {}}},
    {"type": "function", "function": {"name": "b", "parameters": {}}},
]
```

Output sent to `litellm.completion`:

```python
{
  "messages": [
    {"role": "system", "content": [
        {"type": "text", "text": "You are a helpful assistant.",
         "cache_control": {"type": "ephemeral"}},
    ]},
    # ... user / assistant turns unchanged
  ],
  "tools": [
    {"type": "function", "function": {"name": "a", "parameters": {}}},
    {"type": "function", "function": {"name": "b", "parameters": {}},
     "cache_control": {"type": "ephemeral"}},
  ],
}
```

LiteLLM forwards the content-blocks list untouched to the Anthropic provider —
this is the canonical Messages-API shape for prompt caching (verified against
context7).

### `effective_system_prompt` on `GenerationResult`

The cache policy transforms the system prompt into a list of content-blocks
on the wire, but
[`GenerationResult.effective_system_prompt`](../tolokaforge/core/llm/client.py)
is always a plain **`str`** — captured after prompt enrichment and **before**
cache policy application, so downstream consumers (trajectory writer,
analytics consumers) never have to flatten a
list-of-blocks back to text.

### User-visible configuration

`cache_policy` is preset-driven, not user-overridable via `ModelConfig.capabilities`
in Stage 6. To disable caching for an ablation study, override the preset in
[`tolokaforge/core/data/model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml:22)
with `cache_policy: none`. The override path contract is documented in
`docs/ADD_NEW_MODEL.md` (Stage 8).

## `prompt_policy`

System-prompt enrichment. Today: `NoPromptEnrichment` (default) and
`DictMapHints` (injects explicit dict-map parameter hints to mitigate models
that silently drop `additionalProperties` parameters).

## `params_policy`

Generation-parameter adaptation.

```python
class ParamPolicy(Protocol):
    def adapt(
        self,
        kwargs: dict,
        config_temperature: float | None,
        config_seed: int | None,
        config_reasoning: ReasoningConfig,
        temperature: float | None,
        seed: int | None,
        reasoning: ReasoningConfig | None,
    ) -> dict: ...
```

`GenerationParams` reads six preset-driven flags:

| Flag | Default | Effect |
|---|---|---|
| `fixed_temperature` | `None` | Override caller-supplied temperature (legacy compat knob). |
| `supports_seed` | `true` | Forward `seed` kwarg when caller or config supplies one. |
| `reasoning_via_extra_body` | `false` | Adaptive reasoning → `extra_body.reasoning={effort, enabled:true}` (OpenRouter non-Anthropic path). |
| `reasoning_via_thinking_kwarg` | `false` | Budget reasoning → top-level `thinking={"type":"enabled","budget_tokens":N}` (Anthropic-native). |
| `drop_sampling_when_thinking` | `false` | Pop `temperature` / `top_p` / `top_k` whenever the `thinking` kwarg was emitted (P3b — OpenRouter silently strips them today; Anthropic raw 400s). |
| `reasoning_budget_default` | `None` | Default `budget_tokens` when `ReasoningConfig(mode="budget")` omits its own budget. |

### Reasoning routing matrix

Which kwargs does `adapt` emit for each `ReasoningConfig.mode`?

| `mode`       | thinking-kwarg preset        | `reasoning_via_extra_body` preset | plain preset                |
|--------------|------------------------------|-----------------------------------|-----------------------------|
| `off`        | *(nothing)*                  | *(nothing)*                       | *(nothing)*                 |
| `adaptive`   | **`ValueError`** (mis-config) | `extra_body.reasoning={...}`      | `reasoning_effort=<hint>`   |
| `budget`     | `thinking={type,budget}` + drop sampling | effort fallback (uses `effort_hint` if set) | effort fallback             |

Rules made explicit:

1. **Fail-loud mis-configuration.** A preset that declares
   `reasoning_via_thinking_kwarg: true` but receives `ReasoningConfig.mode == "adaptive"`
   raises `ValueError`. Thinking-kwarg-native presets (e.g. Claude 4.7) have no
   adaptive path — surface the mis-config instead of silently stripping.
2. **Fail-loud missing budget.** `ReasoningConfig(mode="budget")` without
   `budget_tokens` AND no `reasoning_budget_default` on the preset raises
   `ValueError` at `adapt` time — we never ship an undefined request shape.
3. **`drop_sampling_when_thinking` is atomic with the `thinking` kwarg.**
   It only pops sampling params when the `thinking` kwarg was actually
   emitted; `mode="off"` on a thinking-kwarg-native preset keeps
   temperature / top_p / top_k intact.
4. **Non-Anthropic budget fallback.** Budget mode on a preset that lacks
   `reasoning_via_thinking_kwarg` re-uses the effort path — OpenAI has no
   canonical budget-tokens kwarg. Passing `budget_tokens=N` without an
   `effort_hint` on such a preset emits nothing (refuse to fabricate).

### Preset → routing table

| Preset | `reasoning_via_extra_body` | `reasoning_via_thinking_kwarg` | `drop_sampling_when_thinking` | `reasoning_budget_default` |
|---|---|---|---|---|
| `anthropic_claude_4_7` (Claude 4.7 Opus + Sonnet) | `true`* | **`true`** | **`true`** | **`8000`** |
| `anthropic` (Claude 4.5 / 4.6 / Sonnet 3.x) | `true`* | `false` | `false` | — |
| `openai_gpt5` / `xai_grok` / `qwen` | `true`* | `false` | `false` | — |
| `default` / `aws_nova` | `false` | `false` | `false` | — |

\* `reasoning_via_extra_body` comes from the `openrouter` provider overlay, not
the preset itself. Anthropic direct (non-OpenRouter) would have `false`.

## `content_policy`

```python
class ToolContentPolicy(Protocol):
    @property
    def format(self) -> str: ...               # "openai" | "anthropic"
    @property
    def supports_images(self) -> bool: ...
    @property
    def inject_empty_assistant_filler(self) -> bool: ...
```

Three implementations, selected via preset:

* `OpenAIContent` (default) — text-only tool result blocks; `supports_images=False`;
  `inject_empty_assistant_filler=False`. Used by the `default`, `openai_gpt5`,
  `xai_grok`, `qwen`, and `gemini` presets.
* `AnthropicContent` — Anthropic native content with image block support;
  `supports_images=True`; `inject_empty_assistant_filler=False`. Used by both
  Anthropic presets.
* `NovaContent` — OpenAI-shape wire format (no native image blocks on the
  Bedrock OpenAI-passthrough path); `inject_empty_assistant_filler=True`.
  The only preset that opts into the filler. Used by `aws_nova`.

`inject_empty_assistant_filler` gates a single substitution in
`LLMClient._convert_messages`: when an assistant turn carries `tool_calls`
but `content` is empty / whitespace, replace it with the literal string
`"I'll help you with that."`. Bedrock/Nova rejects empty assistant content
in that shape, so the filler is necessary there. Every other provider
accepts empty content alongside `tool_calls`, so the filler stays off —
injecting it elsewhere creates a few-shot pattern that some models
(notably Gemini) echo back as their own response content (2026-04-30 OTS
regression). Routing pinned by
[`tests/canonical/test_content_policy_filler_routing.py`](../tests/canonical/test_content_policy_filler_routing.py).

## `response_policy`

Tool-call argument post-processing.

```python
class ResponsePolicy(Protocol):
    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...
```

Implementations:

* `StandardResponse` — no-op (default for OpenAI / Anthropic).
* `UnwrapInputResponse` — strips Nova/Bedrock's `{input: {...}}` wrapper.
* `JsonCoerceResponse` — defence against open-weights stringification:
  decodes JSON-encoded array / object arguments back to native shape.
  When `param_types` is supplied, also coerces `''` → `[]` / `''` → `{}`
  for declared `array` / `object` parameters (the qwen `equipment: ''`
  bug class).
* `ArrayDictMapResponse` — composes `JsonCoerceResponse` plus the reverse
  pivot of `StrictSchema`'s dict-map → array conversion. Used by
  `openai_gpt5` and `xai_grok` presets.
* `MinimaxM3TagRecoveryResponse` — composite for the MiniMax-M3 `tags`
  corruption (`minimax` preset, registry name `minimax_m3_tags`). M3's
  XML → JSON tool-call conversion mangles the `tags` array on every emission
  (`{"item": X}` 76 %, JSON-encoded / empty string 23 %). The composite
  chains `JsonRecursiveCoerceResponse` (stringified-list → list, `''` → `[]`)
  then `ItemRecursiveUnwrapResponse` (`{"item": X}` → list, recursing into the
  parent so `{"item": {"item": "a"}}` flattens to `["a"]`). Both recurse into
  the `updates` / `item` parent but are scoped to the `ARRAY_SITES` allowlist
  (`updates.tags`, `item.tags`) — the empty-string → `[]` coercion is tied to
  those declared-array sites so it can never fire on a scalar field. Scalar
  strings are never promoted, `None` is never touched, multi-key dicts are
  left unchanged, and already-valid `list[str]` tags pass through unchanged
  (zero false positives). M2.7 emits native `tags` lists and is not in this
  preset. See AGENTS.md gotcha #25.

`param_types` is a `Mapping[str, str]` from root-level parameter name to
its post-sanitised JSON-Schema `type`. `LLMClient._assemble_result` builds
this map once per call from the post-`schema_sanitizer` tool list and
passes it on every `parse_arguments` invocation, so schema-aware recovery
fires automatically without policy-level branching. When the model emits
an argument whose root-level shape is wrong (empty string for an array,
JSON-encoded array of `{key,…}` objects for a dict-map), the response
policy recovers the correct native shape before the tool implementation
sees it.

## `capabilities`

```python
@dataclass(frozen=True)
class ModelCapabilities:
    schema_sanitizer: ToolSchemaSanitizer = field(default_factory=PassthroughSchema)
    prompt_policy:    SystemPromptPolicy  = field(default_factory=NoPromptEnrichment)
    content_policy:   ToolContentPolicy   = field(default_factory=OpenAIContent)
    params_policy:    GenerationParams    = field(default_factory=GenerationParams)
    response_policy:  ResponsePolicy      = field(default_factory=StandardResponse)
    reasoning_codec:  ReasoningCodec      = field(default_factory=NoReasoningCodec)
    cache_policy:     CachePolicy         = field(default_factory=NoCache)
```

## `presets`

`build_capabilities(model_name, provider, overrides)` walks the merge order
`default → matched preset → provider overlay → overrides` and constructs a
fresh `ModelCapabilities`. Presets live in
[`tolokaforge/core/data/model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml).

### Preset coverage

Per-preset policy wiring as shipped today. The three `StrictSchema` presets
all cover the same two failure surfaces — `Decimal` look-ahead regex (P1,
Stage 1) and typed `Dict[str, T]` parameters (P2, Stage 2) — by combining
the same three policies. Keep this table in sync with
[`model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml).

| Preset                  | Match globs                                                      | `schema_sanitizer` | `response_policy`   | `prompt_policy`   | `content_policy` | `reasoning_codec` |
|-------------------------|------------------------------------------------------------------|--------------------|---------------------|-------------------|------------------|-------------------|
| `default`               | *(fallthrough)*                                                  | `passthrough`      | `standard`          | `none`            | `openai`         | `none`            |
| `anthropic_claude_4_7`  | `anthropic/claude-{opus,sonnet}-4.7*`, `*claude-{opus,sonnet}-4.7*` | `passthrough`      | `standard`          | `none`            | `anthropic`      | `anthropic`       |
| `anthropic`             | `anthropic/*`, `*claude*`                                        | `passthrough`      | `standard`          | `none`            | `anthropic`      | `anthropic`       |
| `openai_gpt5`           | `openai/gpt-5*`, `*gpt-5*`                                       | `strict`           | `array_dict_map`    | `none`            | `openai`         | `openai`          |
| `xai_grok`              | `x-ai/*`, `xai/*`, `grok*`                                       | `strict`           | `array_dict_map`    | `none`            | `openai`         | `openai`          |
| `qwen`                  | `qwen/*`, `qwen3*`                                               | `strict`           | `array_dict_map`    | `dict_map_hints`  | `openai`         | `openai`          |
| `aws_nova`              | `nova*` (+ provider `nova`)                                      | `passthrough`      | `unwrap_input`      | `none`            | `openai`         | `none`            |

Order matters — first match wins. `anthropic_claude_4_7` is declared
*before* the generic `anthropic` preset so Claude 4.7 picks up its
thinking-kwarg routing instead of falling through to the adaptive-effort
path that 4.7 ignores (see
[plans/eval_output_new_diagnosis.md](../plans/eval_output_new_diagnosis.md)
Part 4).

`qwen` additionally enables `dict_map_hints` (GPT-5-class presets currently
opt-in to this via the legacy `capabilities: {dict_map_prompt_hints: true}`
override on the model config — see the translation layer in
[`tolokaforge/core/llm/presets.py`](../tolokaforge/core/llm/presets.py) §
`_apply_config_overrides`). Qwen bakes the hint in unconditionally because
its stringification failure mode is not opt-in — every Qwen call with a
typed dict-map needs the hint.

### Fingerprint helpers (Stage 7, P6)

Two public helpers on [`tolokaforge.core.llm.presets`](../tolokaforge/core/llm/presets.py)
produce the JSON-serialisable preset fingerprint landed on
`task.yaml.model_config.<role>.resolved` (see
[`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md) § `task.yaml`).

* `resolve_effective_preset(model_name, provider) -> str` — mirrors
  `_match_preset`'s first-match-wins routing but returns the preset
  identifier only. Returns `"default"` when no preset matched. Use this
  to label a run with its effective preset name.
* `resolve_policy_names(capabilities) -> dict[str, str]` — reverse-lookup
  from policy instances on a `ModelCapabilities` to the registry names
  (`schema_sanitizer`, `prompt_policy`, `content_policy`,
  `response_policy`, `reasoning_codec`, `cache_policy`). `params_policy`
  is intentionally omitted — it is a stateful `GenerationParams`
  dataclass, not a single-named policy.

Both helpers raise `ValueError` on unknown inputs rather than returning
placeholders — per AGENTS.md rule #1 we surface drift immediately. Unit
guard: [`tests/unit/llm/test_preset_fingerprint.py`](../tests/unit/llm/test_preset_fingerprint.py)
parametrises over every preset in
[`model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml) and
plants a rogue policy instance to confirm the raise path.

## `client`

`LLMClient(config: ModelConfig)` composes a `ModelCapabilities` and wraps
litellm's `completion()`. `generate(...)` returns a `GenerationResult`
carrying `text`, `tool_calls`, `usage: Usage`, `latency_s`, `cost_usd`,
`reasoning: StructuredReasoning | None`, and `effective_system_prompt`.
See § `usage` above for the full Usage schema and accumulation contract.

`UserSimulator` wraps `LLMClient` for tau-bench-style user simulation with
`scripted` or `llm` modes.
