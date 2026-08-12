# LLM Layer

Single reference for the [`tolokaforge/core/llm/`](../tolokaforge/core/llm/)
package. This layer is the **only** place provider-specific shapes
(`thinking_blocks`, `cache_control`, `reasoning_content`, …) are allowed to
appear — callers above it work with the curated Python types described below.

See [`plans/llm_reasoning_and_observability_fix.md`](../plans/llm_reasoning_and_observability_fix.md)
for the design rationale and the canonical litellm surface.

## Two-wheel architecture

Tolokaforge publishes two PyPI wheels from one monorepo — see
[ADR-0030](adr/0030-tolokaforge-models-split.md):

- **`tolokaforge`** (engine wheel) ships the base classes for every
  policy slot (`StrictSchema`, `DictMapHints`, `ResponsePolicy`,
  `ReasoningCodec`, `AssistantTextPolicy`, `ParamsPolicy`,
  `MessageAssemblyPolicy`, `CachePolicy`, `ContentPolicy`), the nine
  `_POLICY_REGISTRIES` slots, and the loader / overlay machinery on
  [`presets.py`](../tolokaforge/core/llm/presets.py). It also ships the
  `Capability` enum and the `ModelCertificate` dataclass at
  [`tolokaforge.testing.certify`](../tolokaforge/testing/certify/).
- **[`tolokaforge-models`](../tolokaforge_models/)** ships the per-model
  policy subclasses at
  [`tolokaforge_models/policies/`](../tolokaforge_models/src/tolokaforge_models/policies/)
  (`gemini.py`, `minimax.py`, `deepseek.py`, `inkling.py`), the 39
  `ModelCertificate` entries at
  [`tolokaforge_models.certificates.ALL_MODELS`](../tolokaforge_models/src/tolokaforge_models/certificates/registry.py),
  and the three data files (`pricing.json`, `model_presets.yaml`,
  `providers.yaml`) at
  [`tolokaforge_models/data/`](../tolokaforge_models/src/tolokaforge_models/data/).

The models wheel registers its policy classes with the engine via the
`tolokaforge.policies` entry-point group declared in
[`tolokaforge_models/pyproject.toml`](../tolokaforge_models/pyproject.toml).
[`load_policy_registrations()`](../tolokaforge/core/model_data.py)
discovers the entry points at
[`tolokaforge.core.llm.presets`](../tolokaforge/core/llm/presets.py)
import and merges the registrations into `_POLICY_REGISTRIES`;
duplicate keys or unknown slots fail loud. Two install-time gates
follow the merge — see § Startup validation. Certificates reach the
engine through [`bundled_certificates()`](../tolokaforge/core/model_data.py),
consumed once at
[`tolokaforge.testing.certify.__init__`](../tolokaforge/testing/certify/__init__.py)
to populate the public `ALL_MODELS` symbol.

`pip install tolokaforge` transitively pulls the models wheel via
`Requires-Dist: tolokaforge-models >=1.0.0,<2.0.0` — the two wheels
version and release independently (see
[`docs/RELEASING.md`](RELEASING.md)) but a working engine install
always carries a matching models wheel.

### Deprecation shim for by-name imports (v0.17.x → v0.18.0)

`from tolokaforge.core.llm import GeminiSchema` (and its seven
siblings — `GeminiRecursiveSchema`, `ScalarArrayDictMapResponse`,
`RefResolvingDictMapHints`, `JsonRecursiveCoerceResponse`,
`ItemRecursiveUnwrapResponse`, `MinimaxM3TagRecoveryResponse`,
`OpenAISummaryReplayReasoningCodec`) resolves via a lazy
[`__getattr__`](../tolokaforge/core/llm/__init__.py) shim to the
subclass in `tolokaforge_models.policies.<family>`. First access
emits a `DeprecationWarning` naming the new import path; subsequent
accesses per name resolve silently through a `_WARNED` cache. **The
shim is removed in v0.18.0** — migrate to
`from tolokaforge_models.policies.<family> import <Class>` before
upgrading past v0.17.x.

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
| [`message_assembly_policy.py`](../tolokaforge/core/llm/message_assembly_policy.py) | Empty-assistant-content filler injection (Nova-only) |
| [`response_policy.py`](../tolokaforge/core/llm/response_policy.py) | Tool-call argument post-processing |
| [`assistant_text_policy.py`](../tolokaforge/core/llm/assistant_text_policy.py) | Assistant-text reshaping between litellm parse and `GenerationResult.text` |
| [`capabilities.py`](../tolokaforge/core/llm/capabilities.py) | `ModelCapabilities` frozen dataclass |
| [`presets.py`](../tolokaforge/core/llm/presets.py) | YAML preset loader → `ModelCapabilities`. Also implements the **operator-overridable preset overlay** (`--presets-file`, `engine.presets_file`) so new model registrations don't require an engine release — see [ADR 0002](adr/0002-external-model-registry.md) and [`docs/CONFIG.md` § Preset overlay file](CONFIG.md#preset-overlay-file-no-engine-release-required). |
| [`litellm_params.py`](../tolokaforge/core/llm/litellm_params.py) | Turns overlay-declared capabilities into litellm's `allowed_openai_params`, so a vendor-native provider does not refuse `tools` for a model its map lacks — see [§ When litellm has never heard of the model](#when-litellm-has-never-heard-of-the-model) |
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
# tolokaforge_models/data/model_presets.yaml
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
* `GeminiSchema(StrictSchema)` — shipped by
  [`tolokaforge-models`](../tolokaforge_models/src/tolokaforge_models/policies/gemini.py) and used
  by the `gemini` preset. Adds `flatten_oneof_discriminator=True` on top of
  `StrictSchema`'s rewrites because Gemini's tool spec is a JSON-Schema
  *subset* that does not document `$defs`/`$ref`, `oneOf`/`anyOf` with
  object branches, or `discriminator` — sending these constructs causes
  Gemini to silently lose every property name inside them and emit
  description-derived English keys instead (verified live 2026-05-20). The
  flattener collapses `oneOf` discriminated unions into a single object
  schema unioning every branch's `properties`; intersects `required` (so
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

## `UserSimulator` request and reply contract

The LLM user simulator converses from the customer's seat: before each
generation it role-flips the shared transcript (its own past USER turns
replay as `assistant`, the agent's ASSISTANT turns as `user`), skips
turns carrying no dialogue text (agent tool-call turns, whitespace-only
replies), and coalesces adjacent same-role turns so the request
alternates strictly. Two invariants hold on the request it sends:

1. **It leads with a user-role turn.** Whenever the flipped context starts
   assistant-side (the simulator's own opening comes first — caller-seeded
   or simulator-bootstrapped), the agent greeting `SIMULATOR_GREETING` is
   prepended: for a bootstrapped opening this replays the greeting the
   runner actually dispatched at turn 0; for a caller-seeded opening it is
   synthetic. The simulator's own opening is always preserved: trimming it
   makes the model believe it never asked and restart the conversation
   after the agent has answered.
2. **It ends on a user-role turn the simulator can answer.** A transcript
   whose last agent turn carries no dialogue text — or that carries no
   dialogue text at all — is unanswerable: a trailing assistant-role
   message is a prefill the provider would continue, and an empty request
   cannot be dispatched. `reply()` raises `RuntimeError` and the trial ends
   `status=error` / `termination_reason=error` instead of silently
   improvising.

The greeting exists only in the simulator's private request; it never
enters the shared transcript or `trajectory.yaml`. Context-shape revisions
bump `Trajectory.simulator_schema_version` (see
[`OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md) § Schema Version Stamps).

### The reply guard

A generated user turn reaches the agent carrying exactly the words the model
wrote, or it does not reach the agent at all. Every generation inside
`_llm_reply` passes through `UserReplyGuard`
([`reply_guard.py`](../tolokaforge/core/actors/reply_guard.py)), which runs a
list of `ReplyDetector`s over the reply text:

- A flagged reply is **discarded whole and regenerated**. No text is edited,
  excised, truncated or substituted — the engine has no path that can put words
  into a turn the model did not write.
- Every discarded attempt logs at `WARNING` with the detector, the reason code,
  the matched excerpt and the `trial_id` that paid for it, and rides back on
  `GenerationResult.guard_rejections`. The guard logs under its own logger name,
  so `_llm_reply` hands it the trial identity the call's `LLMCallObservation`
  carries.
- A generation that *fails* after one or more discards re-raises with the
  discarded reason codes attached as an exception note — the provider's error is
  what the trial reports, and those attempts are otherwise lost with the call.
- When `USER_REPLY_MAX_ATTEMPTS` generations have all been flagged, the guard
  raises `UserReplyRefused`. The trial terminates `reason=error` and is counted
  as a `harness_error` — our defect, in the denominator, never the agent's. The
  exception names the detectors, the reason codes and the attempt count and
  deliberately quotes none of the reply: `classify_loop_error` reads an
  exception's prose, so a quoted reply mentioning a provider would re-attribute
  the failure away from us.
- Those extra generations are a term in the rate-limit-probe budget invariant,
  not an unaccounted multiplier on it (see [`CONFIG.md`](CONFIG.md) §
  `rate_limit_probe`).

What it records, and where: the runner appends one `user_reply_guard_events`
entry to `trajectory.yaml` per user turn the guard did not accept on its first
generation — `message_index` (the position in `messages` the turn was dispatched
at), `outcome` (`delivered` | `refused`), and one `{detector, reason, excerpt}`
per discarded attempt. Both dispatch sites record, the bootstrap turn and every
mid-conversation turn, and the refused path records **before** re-raising so a
trial that died on the guard still carries the evidence for why. A trial whose
every turn was clean carries `[]`. Field reference in
[`OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md) § `trajectory.yaml`.

`DEFAULT_REPLY_DETECTORS` is the registration list every guard runs unless
constructed with another, and the name a defect is recorded under is the
registered detector's. Two detectors are registered, and the tuple order is the
inspection order: `FourthWallDetector` (`name = "fourth_wall"`) first, then
`ScratchpadDetector` (`name = "scratchpad"`). The first detector to flag a reply
owns it, so a detector added at the end of the list cannot move the reason code
recorded for any reply an earlier one already claims.

| detector | family | reason codes | example detection |
|---|---|---|---|
| `fourth_wall` | the speaker identifies itself as a machine, or denies being human | `self_identified_as_model`, `denied_being_human` | `As an AI language model, I cannot do that.` |
| `fourth_wall` | the exercise is named as an exercise, or a party's prompt or persona is named | `named_the_exercise`, `named_a_party_prompt`, `named_own_instructions` | `This is a simulation of the task.` |
| `scratchpad` | the model's own reasoning delimiter survives into the reply | `think_tag` | `</think>` beginning the reply, or beginning a line |

`FourthWallDetector` matches **attributed frames**, not vocabulary: a pattern
fires only when the meta-concept is attributed to a conversational party or to
the exercise itself *and* the noun carrying it heads its own phrase.
Bare `ai`, `model`, `prompt`, `benchmark`, `simulation` and `llm` are ordinary
support vocabulary and never trigger on their own, and neither does a noun used
attributively (`I'm an AI engineer at a fintech startup`, `a benchmark index
fund`, `not a real person of interest`, `your system prompt caching feature`). A
false positive costs the whole attempt budget and then the trial, so precision
outranks recall — and where a demonstrative head cannot separate the two senses,
the frame is given up rather than the support turn. `exercise` and `evaluation`
are exercise nouns only in their compounds (`roleplay exercise`, `training
exercise`, `evaluation exercise`), `benchmark` only under the prepositional frame
(`in this benchmark`), and `test scenario` / `test case` no longer match in any
frame. The prepositional frame itself matches only when the speaker claims a role
inside the exercise (`In this benchmark, I am playing a frustrated customer.`),
because `During the simulation, the app froze and I lost my mesh.` and `In the
simulation I get an error at step 4.` are what a customer of simulation software
says — a first-person subject alone does not separate them. So `This exercise is
not showing up in my activity ring.` passes, and a bare `This benchmark tests
performance.` is missed.
The module's docstring carries the full list of the recall given up and why.

The `named_a_party_prompt` family matches the agent's prompt two ways: as a noun
heading its phrase (`Your system prompt is confusing.`), and as the subject of a
verb reciting what it says (`Your system prompt says to be concise.`). The second
is the family's least ambiguous break — a customer quoting the agent's own
instructions — and no anchor built for nouns can see it, so it is its own branch.
`requires` is not one of those verbs: `Your system prompt requires a role field,
but the docs disagree.` is an API question, not a recitation.

`ScratchpadDetector` matches a think tag only at a **structural position** —
beginning the reply, or beginning a line. Every real leak is structural: the
delimiter is emitted at a channel boundary, never mid-sentence, so a tag
mentioned inside a sentence (`My parser chokes on </think> tags in the streamed
output`) is an ordinary support ticket and passes. Anchoring on the start of the
*string* would miss the shape the measurement reports as dominant — planning
prose, then a lone `</think>` on its own line, then the reply. Its recall is
bounded in one large way and one small one: the **untagged** half of the leak,
plain planning prose carrying no delimiter, is the larger half and is not
separable from ordinary support English by any pattern set (the two-round
measurement that retired five candidate families is #1095); and a pasted
multi-line log whose quoted content starts a line with a think tag is a false
positive, which costs that trial and fails loudly, carrying the matched excerpt,
rather than silently.

The tag is **not stripped**, here or anywhere else on the user path. The user
simulator can be asked again, and a defect curable by regenerating must not be
cured by editing the words the model wrote. The **agent** path carries the same
leak into `trajectory.yaml` and the judge's evidence and cannot regenerate —
re-rolling an agent turn re-rolls the thing being measured — so stripping there
belongs in `AssistantTextPolicy`, tracked as #1094.

What the exposure figure describes: roughly one **opening** message in six on
one reasoning simulator carried a scratchpad, about half of them tagged. It is
an opening-turn rate. A task that pins `initial_user_message` has no generated
opening turn at all, so that surface is absent for it (see
[`TASKS.md`](TASKS.md) § Authoring the opening turn), and the mid-conversation
rate is unmeasured.

The user describing the **agent** as a machine (`You are chatting with an
internal AI agent, right?`) is in frame and passes by design; only the simulator
describing **itself** is a defect. Scripted replies and a task's pinned
`initial_user_message` are authored content delivered verbatim — neither is
generated, so neither passes through the guard.

## litellm OpenRouter routing caveat

`OpenrouterConfig` in litellm inherits from `OpenAIGPTConfig` (generic),
**not** `OpenAIGPT5Config`. When calling GPT-5 through OpenRouter, litellm
does not apply GPT-5-specific parameter handling (e.g.
`max_tokens → max_completion_tokens`). Tool schemas are passed through
unchanged — litellm's native `_remove_additional_properties` only runs for
Vertex AI, hosted vLLM, and WatsonX. Our `StrictSchema` and `DictMapHints`
policies in `tolokaforge/core/llm/` handle **all** GPT-5 tool-schema
adaptation independently of litellm, so this gap is transparent to callers.

## When litellm has never heard of the model

litellm decides which OpenAI parameters a provider may be sent by looking the
model up in its own map. For most providers that decision is generic, but a
**vendor-native** provider answers from the entry alone. A model the map does
not carry is therefore read as supporting NO parameters, and the request is
refused inside our process, before anything is sent:

```
litellm.UnsupportedParamsError: meta does not support parameters:
['tools', 'tool_choice'], for model=muse-spark-1.2
```

Measured 2026-08-10 across litellm versions with an identical request: 1.83.14
passes the tools through, 1.93.0 and 1.96.0 refuse them. The strictness arrived
in a patch release, so a routine dependency bump can turn a working
vendor-native model into one that cannot make a single tool call — and the
error names the provider rather than the missing data, so it reads as "this
vendor does not do tool calls".

It says nothing about the model. The identical request driven through litellm's
`openai` transport against the same `api_base` returns a correct tool call —
the gap is upstream DATA, and the fix is to supply the entry rather than to
wait on someone else's release.

litellm's own answer to this is `allowed_openai_params`, a per-call kwarg
naming the parameters to admit past the map gating for that one request; its
error message says so. `litellm_params.py` turns an operator's declaration into
that list, and it ships with **no list of models**. A model missing from a
third-party map is not a fact about an engine release, and a list in the wheel
would tie every future gap to the release cadence — the argument
[ADR 0002](adr/0002-external-model-registry.md) already
made for preset data. So the entries are operator data, declared in the same
preset overlay (`--presets-file` / `RunConfig.engine.presets_file`):

```yaml
litellm_models:
  meta/muse-spark-1.2:
    supports_function_calling: true
    supports_reasoning: true       # the config sets models.agent.reasoning
    evidence: "2026-08-10, litellm 1.96.0: no entry, so meta refused tools
      before sending; the same request through litellm's openai transport
      against api.meta.ai returned a correct tool call."
```

The key is the litellm model id, because that is the lookup litellm performs.
An entry **declares**; it does not copy. Only the parameters its flags name are
admitted, so a capability nothing observed is never asserted on the model's
behalf.

Two rules keep it honest:

- **The flags are an allow-list.** A parameter stays refused until someone
  declares the capability with evidence, and extending the map is a decision
  about what we are willing to assert. `supports_reasoning` is on it because a
  config that sets `models.agent.reasoning` sends `reasoning_effort`, which
  litellm refuses for an unmapped model exactly as it refuses `tools`.
- **Nothing is written into litellm's global map.** The kwarg is per call, so
  our own price can never end up labelled `cost_source="litellm"` (the label
  meaning provider-authoritative), no entry of ours can outlive the day
  upstream ships a richer one, and there is no process-global mutation to
  synchronise across the trial thread pool. Once upstream carries the model,
  the allow-list is a harmless no-op.

An undeclared capability is **refused**, not dropped: the allow-list only ever
ADDS to what litellm already permits, so a config that sets
`models.agent.reasoning` against an entry that does not declare
`supports_reasoning` fails per call rather than quietly running without it. An
entry has to cover what its config asks for.

Validation is at overlay load and is louder than the preset blocks beside it: a
preset that fails to apply changes how a request is shaped, while a dropped
entry here decides whether a request is sent at all.

Three things that look like fixes and are not:

- **`drop_params: true`** silences the error by stripping `tools`, turning
  every tool-use trial into a no-tool trial. The eval then measures a
  configuration mistake and reports it as model capability.
- **A preset `params:` block** cannot reach this. They validate against a
  closed set introspected from `GenerationParams.__init__`, and the refusal
  happens upstream of every policy slot.
- **`extra_body`** passes ungated, so smuggling a refused parameter through it
  works — and a provider that silently ignores the key is then invisible,
  which is the same failure wearing a different hat.

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
| `LLM_PROXY_HEADERS` | JSON object of static headers added to every request, e.g. `{"X-Team-Id": "research"}`. Wins over the engine's own provider headers on a name collision. A value may reference a secret as `${secret:NAME}`, see below. |
| `LLM_PROXY_REQUEST_ID_HEADER` | Header *name* that receives a fresh UUID4 per request. A static env var cannot express "new value per call". |
| `LLM_PROXY_PROVIDERS` | Comma-separated provider allow-list, replacing the default. Read the routing table below before widening it. |
| `LLM_PROXY_PREFERRED_ROUTE` | Namespace that wins when the gateway serves one model under several names, e.g. `openrouter/`. Without it an ambiguous lookup raises rather than guessing a serving path. |

All six resolve through `SecretManager`, so `.env`, the process environment,
and the runner container's `TOLOKAFORGE_SECRETS_JSON` behave identically.
A malformed value raises `ProxyConfigError` at the first `LLMClient`
construction rather than running a whole evaluation with unattributed spend. Setting
any companion variable while `LLM_PROXY_BASE_URL` is empty also raises, so a typo in
the base-URL name cannot silently fall back to direct provider access.

### Values may reference secrets

A value in `LLM_PROXY_HEADERS` may contain `${secret:NAME}`, resolved through
`SecretManager` by
[`expand_secret_refs`](../tolokaforge/secrets/expand.py):

```
LLM_PROXY_HEADERS={"X-Team-Id":"research","X-Order-Id":"${secret:ORDER_ID}"}
ORDER_ID=9000123
```

This exists so the JSON does not have to be a secret just because one header carries
something sensitive. The header *names* and the overall shape stay legible, which is
what a reviewer needs to see (what is this run telling the gateway about itself?),
while the sensitive halves stay indirect. In CI that means the JSON can live in a
plain repository **variable** without printing a secret into a public workflow log,
because the variable only ever holds the reference.

`${secret:...}` resolves by NAME through the normal provider chain, so what matters
is that the name is set, not whether it is "a secret". A GitHub repository variable
and a GitHub secret both arrive as ordinary environment variables; the difference is
only that GitHub masks the secret's value in the log, which is the point.

#### Rules

* **The form is typed, not shell-style.** A bare `$NAME` would match inside real
  credential text, and plenty of credentials contain a dollar sign: argon2
  (`$argon2id$v=19$...`), bcrypt (`$2b$12$...`), Postgres SCRAM verifiers, generated
  passwords. Under `${secret:NAME}` a lone `$` is never special, so no escape is
  needed and `Pa$$w0rd` passes through untouched.
* **An unresolved name is a hard error**, never an empty substitution. A blank
  attribution header bills the call to nobody and a blank admission header fails at
  the gateway, and both surface a long way from the misconfigured line. The error
  names the value and the missing name, and fires at resolve time, before any
  request goes out.
* **A malformed reference is a hard error too.** `${secret:NAME` (unclosed),
  `${secret:}` and `${secret:bad-name}` are refused rather than passed through as
  literal text, which would put `${secret:...}` on the wire, where a gateway either
  bills the literal string as an account id or rejects the request in a way that
  reads as network trouble.
* **Expansion is single-level.** A resolved value is not rescanned, and a value that
  itself contains a reference is refused by the malformed-reference rule rather than
  silently emitted.

Only string values are expanded; a JSON number or boolean keeps its existing
stringify path.

#### Why this is not inside `get_secret`

`SecretManager.get_secret` stays a verbatim pass-through. It is the universal
credential read path, and two bulk callers resolve *every enumerable key* rather
than the keys the engine asked for: the log-redaction set
([`log_filter.py`](../tolokaforge/secrets/log_filter.py)) and the container
serializer. Expanding there would run this syntax over values nobody wrote for it,
where failing loud takes down logging and failing empty corrupts a credential.
Expansion is a composition concern, so the caller requests it explicitly.

One consequence worth knowing: a referenced name is only carried across the
host→container boundary if it is enumerable, which for the environment means its
*name* matches one of the credential patterns in
[`providers.py`](../tolokaforge/secrets/providers.py). `DotEnvProvider` enumerates
every `.env` key unconditionally, so putting referenced names in `.env` is the way
to make in-container resolution work if that is ever needed.


### Speaking to the gateway

A gateway is an OpenAI-compatible endpoint, so routed calls speak that dialect
(`custom_llm_provider="openai"`) and address the gateway by **its** route name,
resolved from `GET {base}/models` at client construction and cached per base URL.

Both halves are load-bearing, and each replaces a measured failure.

**The dialect.** litellm's OpenRouter transformation unconditionally adds
`usage: {"include": true}` to the body to get cost data back. That field is an
OpenRouter extension. Forwarded by the gateway to any other upstream it is rejected
(`usage: Extra inputs are not permitted` from both Anthropic and Bedrock), so every
call to a non-OpenRouter-backed route failed. The dialect also decides prefix
handling: the OpenRouter transport strips one leading `openrouter/`, the OpenAI one
does not.

**The name.** Those two effects are coupled, so the name that arrives depends on the
dialect, and the gateway's name for a model is not derivable from the engine's model
string. It is whichever of `<provider>/<name>` or `<name>` the catalog contains:

| engine model string | gateway route |
|---|---|
| `openrouter/azure_ai/cohere-command-a-plus-05-2026` | `azure_ai/cohere-command-a-plus-05-2026` |
| `openrouter/anthropic/claude-sonnet-4.6` | `openrouter/anthropic/claude-sonnet-4.6` |

Three outcomes, deliberately different:

* **Catalog names the model** → route through the gateway under that name.
* **Catalog answers and omits it** → the gateway does not serve this model, so the
  call goes to the provider directly. This is what lets one run mix a gateway-only
  candidate with a simulator the gateway does not carry.
* **Catalog unreadable** (or empty) → keep the gateway and send the untranslated
  name. Unreadable is not absence: silently leaving the gateway is the
  unattributed-spend outcome this transport exists to prevent.

When the catalog serves a model under several names, they can be backed by
different upstreams, which is a serving-path choice rather than a transport detail.
`LLM_PROXY_PREFERRED_ROUTE` names the namespace that wins; without it the resolver
raises rather than guessing.

**Hard requirement on the gateway: route names must mirror upstream names.** The
resolver derives exactly two candidates and does no fuzzy matching, so a route named
as an arbitrary alias (`sonnet-4.6`, `team-default`, or a separator variant like
`claude-sonnet-4-6` for a config that says `4.6`) is invisible to it. The model then
resolves as "not served" and the call goes to the provider directly, with only a
per-client warning. On a gateway-only model that direct call fails; on any other it
runs unattributed. If a deployment cannot rename such a route, the exact-name entry
has to be added alongside the alias.

Wildcard entries (`openrouter/*`, `anthropic/*`) are deliberately **not** accepted as
evidence: a wildcard says the gateway will forward the request, not that the model
exists behind it. Measured on a live gateway, `anthropic/*` accepted a name that its
Bedrock backing then rejected as invalid, while the very same wildcard also "covered"
a nonexistent model. If routing every model through the gateway ever becomes the
goal, the extension point is a *namespace-matched* wildcard (accept `openrouter/*`
only for `provider: openrouter` configs, where the fallback is the same upstream the
board is calibrated on), not looser matching.

Preset resolution and pricing are unaffected: both key off `ModelConfig.provider` /
`.name`, not off the wire name.

**Reading the catalog from outside a run.** `resolve_proxy_config()` takes an
optional `SecretManager`, so a caller that must describe the *deployment's* gateway
rather than the checkout's can pass an env-only one:

```python
proxy = resolve_proxy_config(SecretManager([EnvProvider()]))
served = fetch_gateway_catalog(proxy)
```

The Slack integration poller does exactly this
([`automation/gateway_catalog.py`](../tools/automation/src/automation/gateway_catalog.py)).
Two properties come from it and neither is incidental: the catalog request carries
the same attribution headers a run sends, so a gateway that admits callers by a
shared-secret header answers the poll instead of rejecting it; and dropping
`DotEnvProvider` keeps a developer's local `.env` from answering a production poll,
which would report availability nobody else can reproduce. Because it is one
implementation rather than two, an empty answer means "unreadable" on both sides,
so one gateway state cannot produce two different routing decisions.

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

The `providers.yaml` entry for a provider carries `unroutable: bool`. `mock`
and `nova` declare `unroutable: true` and are rejected even when named
explicitly in `LLM_PROXY_PROVIDERS`. `mock` never reaches the wire. `nova`
depends on `_call_with_key_rotation` rewriting its bare model name into
`openai/<name>` next to the endpoint its `ProviderBinding` pins per attempt;
a gateway replaces the base URL but not the rewrite, so litellm would get a
provider-less model string and raise `BadRequestError` before sending
anything.

### Naming a gateway route explicitly

Route resolution (above) makes existing run configs work through the gateway
unchanged, so this section is the **manual override**: pinning a config directly to
one specific gateway route, either because the catalog is unreadable from the
running host or because a deployment needs a route the resolver's two candidates
cannot derive (an alias, see the hard requirement above).

The cautionary tale that motivates being explicit at all: before route resolution
existed, `provider: openrouter` + `name: anthropic/claude-opus-4.7` put the bare
`anthropic/claude-opus-4.7` on the wire (litellm strips one prefix), and on a real
LiteLLM proxy that matched the catch-all `anthropic/*` route, backed by **Bedrock**,
a different upstream than the config asked for. It failed loudly there only
because that particular Bedrock model rejects `temperature=0.0`; a closer-matching
route would have silently evaluated a different serving path. For a leaderboard that
is a comparability break, not a transport detail.

To pin a route by hand, name the model the way the gateway names it, and pick the
provider so that litellm's prefix strip leaves that name intact:

```yaml
# Gateway route "openrouter/anthropic/claude-opus-4.7"
provider: openai                              # wire: openrouter/anthropic/claude-opus-4.7
name: openrouter/anthropic/claude-opus-4.7

# Gateway route "azure_ai/cohere-command-a-plus-05-2026"
provider: openai                              # wire: azure_ai/cohere-command-a-plus-05-2026
name: azure_ai/cohere-command-a-plus-05-2026
```

List the routes a gateway serves with `GET {base_url}/models`.

Gateway-specific names have two consequences, both following from the naming
couplings described below:

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

What `_build_kwargs` does differs by path. **On a resolved route** it rewrites
`model` to the gateway's route name, forces `custom_llm_provider="openai"`, and
drops OpenRouter-only body fields (`extra_body.provider`). **On the
unrouted / unreadable-catalog path** it sets only `api_base`, `api_key` and
`extra_headers`; the model string keeps its `<provider>/<name>` shape, and
OpenRouter's headers and provider pin still apply. Two distinct couplings hang off
model naming, and only the second is to the formatted string:

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

`ModelConfig.provider` is untouched either way, so OpenRouter's
`HTTP-Referer` / `X-Title` headers still apply on top of the gateway; the
`extra_body.provider` upstream pin applies only on the unrouted path (a resolved
route drops it, since a non-OpenRouter upstream rejects it as an unknown field).
On a header-name collision the gateway's configured header wins, since that is
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

Rotation binds to the provider record's `api_keys_env`
(OpenRouter's is `OPENROUTER_API_KEYS`); the rotation logic republishes into
`api_key_env` (OpenRouter's `OPENROUTER_API_KEY`). Whether rotation still
applies depends on **one** thing: is a gateway key pinned?

- **`LLM_PROXY_API_KEY` set** — rotation is skipped. `_rotate_key` republishes
  the provider's `api_key_env` into the environment, but the pinned `api_key`
  kwarg takes precedence in litellm, so rotating would resend byte-identical
  requests and then report an exhausted key chain that was never in play. A
  gateway quota or authorization rejection raises an error naming the gateway
  URL instead.
- **`LLM_PROXY_API_KEY` unset** (gateway authenticates by network position) —
  rotation works and is left alone, because litellm reads the provider env var
  that `_rotate_key` rewrites. Suppressing it here would abort a trial with
  unused keys still in the chain.

The guard mirrors exactly the condition under which `_build_kwargs` pins the
key, so the two can't drift.

### Secrets surface of `_rotate_key`

`_rotate_key` republishes the picked key into `os.environ` via
`binding.api_key_env` (OpenRouter's `OPENROUTER_API_KEY`) so litellm's
inner request builder — which reads that env var — sees the freshly
rotated key on the next attempt. The `SecretManager` subprocess carve-out
sanctions this specific pattern: rewrites of a small named set of env
vars whose consumer is a downstream process that cannot read
`SecretManager` directly.

Post-cutover, `binding.api_key_env` is data — any provider whose YAML
entry declares one now participates in the same republish path. Today
only OpenRouter (rotation enabled) reaches this branch, but a future
provider whose entry declares both `api_keys_env` and `api_key_env` will
transitively acquire the same `os.environ` rewrite. The guarantee that
`api_key_env` is a credential var the SecretManager subprocess carve-out
accepts is `providers.yaml`-authored — the schema does not enforce it.
If a future entry names a non-credential env var here, `_rotate_key` will
still rewrite it. Reviewer note in the PR that widens the rotation set.

## Provider bindings

Provider-specific transport knobs (endpoint URL, credential env-var names,
routability under a gateway, rotation env-var, `custom_llm_provider` litellm
routing hint, per-provider rate-limit text patterns, and Nova-shaped slug /
transport pinning) live in
[`tolokaforge_models/data/providers.yaml`](../tolokaforge_models/src/tolokaforge_models/data/providers.yaml).
The schema is
[`tolokaforge.core.llm.providers.ProviderBinding`](../tolokaforge/core/llm/providers.py)
— a frozen Pydantic model, `extra="forbid"`, one entry per shipped provider
(`openrouter`, `openai`, `anthropic`, `gemini`, `nova`, `mock`). Lookup key is
the first `/`-separated segment of `ModelConfig.provider`, lower-cased;
unknown names resolve to a default `ProviderBinding()` with every field inert.

`LLMClient.__init__` loads the binding into `self._provider_binding` once and
consults it at every provider-specific transport branch — endpoint pinning,
credential lookup, key rotation, slug rewrite, rate-limit text.

### What is data-driven

| Field | Consumer | Effect |
|---|---|---|
| `endpoint` + `api_base_env` | `LLMClient.__init__` and `_call_with_key_rotation` | When both are set the client `os.environ.setdefault(api_base_env, endpoint)` at construction, publishing the default base URL a deployment may override. When `kwargs_pin_transport=true` the endpoint is also pinned into `kwargs["api_base"]` per attempt. Nova's `NOVA_API_BASE` covers both roles. |
| `api_key_env` | `_call_with_key_rotation`; `_rotate_key`; `_load_api_keys` | Primary key env-var name. When `kwargs_pin_transport=true` the client reads it fresh per attempt via `SecretManager` and pins it into `kwargs["api_key"]`, failing loud (`RuntimeError`) if it resolves empty. Also the env var `_rotate_key` republishes into `os.environ` after picking the next key for the direct-provider path (OpenRouter's `OPENROUTER_API_KEY`). |
| `api_keys_env` | `_load_api_keys` | Rotation-list env-var name (comma-separated). `None` disables rotation. OpenRouter's `OPENROUTER_API_KEYS`; a second provider needing rotation is one YAML edit. |
| `key_file_env` | `_load_api_keys` | Env-var pointing at a fallback keys file (one key per line, `#` comments allowed, comma-separated fields taking the first). Populated only when `api_keys_env` is also set — the file is the second key source after the rotation env var. OpenRouter's `OPENROUTER_KEY_FILE` (defaulting to `keys.txt` in cwd). Older shape (`if binding.api_keys_env == "OPENROUTER_API_KEYS"`) was a model-name conditional in data-driven clothing; the field surfaces the same behaviour without a magic value. |
| `unroutable` | `ProxyConfig.applies_to`, `_parse_providers` | The proxy rejects providers whose binding declares `unroutable: true` even when named in `LLM_PROXY_PROVIDERS`. `mock` and `nova` — see § proxy above. |
| `custom_llm_provider` | `_call_with_key_rotation` | Value pinned into `kwargs["custom_llm_provider"]`. Nova: `"openai"`. OpenRouter: `"openrouter"`. When `None`, compound providers (`openrouter/google`) fall back to `provider.split("/")[0]`; simple providers let litellm default. |
| `rate_limit_patterns` | `LLMClient._is_rate_limit_exception` (tier-3 text fallback), `LLMClient.classify_loop_error` | Regex strings compiled once at construction. `DEFAULT_RATE_LIMIT_PATTERNS` in [`providers.py`](../tolokaforge/core/llm/providers.py) is the shipped default every non-mock provider declares verbatim; each entry is a shape an *engine wrapper* produces (`Error code: 429`, `HTTP/1.1 429`, `too many requests`, rate-limit prose in an error construction), not provider quota prose. |
| `slug_rewrite` | `_call_with_key_rotation` | Two-step rewrite of `kwargs["model"]` per attempt: strip `strip_prefix`, then ensure `ensure_prefix`. Nova's binding declares `strip_prefix: "nova/"` and `ensure_prefix: "openai/"` — turning `nova/busan-v1` into `openai/busan-v1` on the wire without a Python conditional on provider name. |
| `format_model_name_bare` | `LLMClient._format_model_name` | When `true`, `_format_model_name` returns `config.name` as-is (no `{provider}/` prefix). Nova only; preserves current log content. |
| `kwargs_pin_transport` | `_call_with_key_rotation` | When `true`, the client reads `endpoint` and `api_key_env` fresh per attempt and pins them into `kwargs["api_base"]` / `kwargs["api_key"]`. Fires the `NOVA_API_KEY is required for nova provider` fail-loud when `api_key_env` resolves empty. Nova only. |

Nova's three sites (init `NOVA_API_BASE` `os.environ.setdefault`,
`_format_model_name` bare-name return, `_call_with_key_rotation` per-attempt
`api_base` / `api_key` / `custom_llm_provider` / slug rewrite) are expressed
entirely through the fields above — a provider whose transport matches Nova's
shape is a `providers.yaml` entry, not a `client.py` edit.

The `LLMClient.classify_loop_error(exc)` bound method closes over the
compiled `binding.rate_limit_patterns` and is what
[`tolokaforge.core.loop.ToolCallingLoop`](../tolokaforge/core/loop.py)
and the grading judge loop consume — the public seam threads the per-provider
patterns to `loop.py` without exposing the compiled tuple across the module
boundary.

### What stays engine code

Not every provider knob is data-shaped, and the schema is deliberately narrow
where the mechanism is genuinely per-provider:

- **`_configure_openrouter_base_url`** reconciles *two* env-var names
  (`OPENROUTER_BASE_URL` and `OPENROUTER_API_BASE`) into one pinned value. The
  single-field `api_base_env` schema cannot express dual-env coordination; a
  schema addition just for one provider is over-engineering.
- **`_openrouter_headers`** (`HTTP-Referer` / `X-Title`) and
  **`provider_order`** (upstream pinning) consume config off
  `ModelConfig.openrouter`, not transport bindings. They stay engine code.
- **Mock's `if self.provider == "mock": return self._mock_generate(...)`
  early-return** — mock's binding declares `unroutable: true` (captures the
  proxy behaviour), but the branch that never constructs kwargs stays
  engine-side. Half of mock is data (routability), half is code (the
  short-circuit); eliminating the last string would require a
  `dispatch_stub: Callable | None` field whose only consumer is mock.

### Fingerprint

`providers.yaml` ships in the `tolokaforge-models` wheel at
[`tolokaforge_models/data/providers.yaml`](../tolokaforge_models/src/tolokaforge_models/data/providers.yaml),
so `engine_run_state.json`'s `models_fingerprint.content_sha256`
covers `{presets, pricing, providers, certificates}` — a provider
binding edit changes the digest. See
[`docs/OUTPUT_FORMAT.md` § `engine_run_state.json`](OUTPUT_FORMAT.md#engine_run_statejson)
and [ADR-0030](adr/0030-tolokaforge-models-split.md) § "Fingerprinting
for auditability".

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
[`tolokaforge_models/data/model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml:22)
with `cache_policy: none`. The override path contract is documented in
`docs/ADD_NEW_MODEL.md` (Stage 8).

## `prompt_policy`

System-prompt enrichment. Today: `NoPromptEnrichment` (default) and
`DictMapHints` (injects explicit dict-map parameter hints to mitigate models
that silently drop `additionalProperties` parameters).

## `params_policy`

Generation-parameter adaptation. `ParamsPolicy` is the abstract base class;
every subclass declares `KNOWN_KEYS: ClassVar[frozenset[str]]` enumerating
the construction kwargs it accepts. The overlay validator reads the union of
every registered subclass's `KNOWN_KEYS` (`_params_slot_known_keys()` in
[`presets.py`](../tolokaforge/core/llm/presets.py)) to decide which preset
`params:` keys are legal — a subclass that forgets `KNOWN_KEYS` raises
`TypeError` at class-body evaluation, so silent drift between the constructor
and the validator is impossible.

```python
class ParamsPolicy(ABC):
    KNOWN_KEYS: ClassVar[frozenset[str]]

    @abstractmethod
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

`GenerationParams` declares its `KNOWN_KEYS` — the preset-driven flags below:

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
```

Three implementations, selected via preset:

* `OpenAIContent` (default) — text-only tool result blocks;
  `supports_images=False`. Used by the `default`, `openai_gpt5`, `xai_grok`,
  `qwen`, and `gemini` presets.
* `AnthropicContent` — Anthropic native content with image block support;
  `supports_images=True`. Used by both Anthropic presets.
* `NovaContent` — OpenAI-shape wire format (no native image blocks on the
  Bedrock OpenAI-passthrough path). Used by `aws_nova`.

## `message_assembly_policy`

```python
class MessageAssemblyPolicy(Protocol):
    @property
    def inject_empty_assistant_filler(self) -> bool: ...
    @property
    def empty_assistant_filler(self) -> str: ...
```

Decides whether empty / whitespace-only assistant `content` on tool-call
turns is substituted with a non-empty filler string, and what that string
is. Wired into `LLMClient._convert_messages`: when
`inject_empty_assistant_filler` is `True`, the assistant dict's `content`
becomes `empty_assistant_filler`; otherwise it stays `""`.

Two implementations ship:

* `NullMessageAssembly` (default) — `inject_empty_assistant_filler=False`,
  `empty_assistant_filler=""`. Every non-Nova preset carries this. The
  provider APIs accept empty assistant content alongside `tool_calls`.
* `NovaMessageAssembly(empty_assistant_filler="I'll help you with that.")`
  — `inject_empty_assistant_filler=True`; the filler string is data on the
  instance. Used by `aws_nova` and `aws_nova_openrouter`. Bedrock/Nova
  rejects empty assistant content on tool-call turns ("The text field in
  the ContentBlock ... is blank").

The filler string is per-instance data rather than an engine constant
because a universal filler caused the 2026-04-30 Gemini regression: Gemini
Pro pattern-matched the substituted string in past assistant turns and
echoed `"I'll help you with that."` back as its own response content
(~26-38 % of trials on ots_19_airlines). A future provider that needs a
different filler declares it at the preset overlay layer via
`message_assembly_policy: {name: nova, params: {empty_assistant_filler: "..."}}`,
without touching engine code. Routing pinned by
[`tests/canonical/test_message_assembly_filler_routing.py`](../tests/canonical/test_message_assembly_filler_routing.py).

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
  corruption (`minimax` preset, registry name `minimax_m3_tags`), shipped
  by [`tolokaforge-models`](../tolokaforge_models/src/tolokaforge_models/policies/minimax.py).
  M3's XML → JSON tool-call conversion mangles the `tags` array on every
  emission (`{"item": X}` 76 %, JSON-encoded / empty string 23 %). The
  composite chains `JsonRecursiveCoerceResponse` (stringified-list → list,
  `''` → `[]`) then `ItemRecursiveUnwrapResponse` (`{"item": X}` → list,
  recursing into the parent so `{"item": {"item": "a"}}` flattens to
  `["a"]`). Both recurse into the `updates` / `item` parent but are scoped
  to the `ARRAY_SITES` allowlist (`updates.tags`, `item.tags`) — the
  empty-string → `[]` coercion is tied to those declared-array sites so
  it can never fire on a scalar field. Scalar strings are never promoted,
  `None` is never touched, multi-key dicts are left unchanged, and
  already-valid `list[str]` tags pass through unchanged (zero false
  positives). M2.7 emits native `tags` lists and is not in this preset.
  See AGENTS.md gotcha #25.

`param_types` is a `Mapping[str, str]` from root-level parameter name to
its post-sanitised JSON-Schema `type`. `LLMClient._assemble_result` builds
this map once per call from the post-`schema_sanitizer` tool list and
passes it on every `parse_arguments` invocation, so schema-aware recovery
fires automatically without policy-level branching. When the model emits
an argument whose root-level shape is wrong (empty string for an array,
JSON-encoded array of `{key,…}` objects for a dict-map), the response
policy recovers the correct native shape before the tool implementation
sees it.

## `assistant_text_policy`

```python
class AssistantTextPolicy(Protocol):
    def parse_assistant_text(
        self, text: str, *, model_config: ModelConfig
    ) -> str: ...
```

Reshapes the assistant's textual reply between litellm's parse and
`GenerationResult.text` — the string that lands in `trajectory.yaml`,
transcript graders, and LLM-judge input. Wired into
[`LLMClient._assemble_result`](../tolokaforge/core/llm/client.py): after
`text = message.content or ""` the client calls
`self.capabilities.assistant_text_policy.parse_assistant_text(text, model_config=self.config)`
and stores the return value. The full text is passed unmodified so a
subclass can dispatch on structure (start/end markers, template tokens)
rather than on a pre-digested slice; `ModelConfig` is threaded through so
a single subclass can match by resolved model name, provider, or
capability overrides.

The mock-generator path at `_assemble_result`'s synthetic branch is
deliberately excluded — offline tests inject deterministic strings and
must stay policy-agnostic, otherwise every offline fixture couples to
whatever preset the run resolves.

One implementation ships:

* `PassthroughAssistantText` (default) — returns the text unchanged.
  Every shipped preset resolves to this class, so wire output is
  byte-identical to a hookless client.

**Load-bearing case — Cohere marker stripping ([#929](https://github.com/Toloka/tolokaforge/issues/929)).**
Cohere Command-A+ wraps every reply in `<|START_TEXT|>…<|END_TEXT|>`
delimiters on the wire; `ResponsePolicy` reshapes only tool-call
arguments, so pre-slot the delimiters flowed into `trajectory.yaml` and
depressed LLM-judge scores. Under this slot a `CohereMarkerAssistantText`
subclass in `tolokaforge_models/policies/` strips the markers without any
engine edit — proven by
[`tests/unit/llm/test_assistant_text_policy_seam.py`](../tests/unit/llm/test_assistant_text_policy_seam.py),
which threads a fixture-scope subclass through
`build_capabilities` → `_assemble_result` and asserts the markers are
gone.

## `capabilities`

```python
@dataclass(frozen=True)
class ModelCapabilities:
    schema_sanitizer:        ToolSchemaSanitizer   = field(default_factory=PassthroughSchema)
    prompt_policy:           SystemPromptPolicy    = field(default_factory=NoPromptEnrichment)
    content_policy:          ToolContentPolicy     = field(default_factory=OpenAIContent)
    params_policy:           GenerationParams      = field(default_factory=GenerationParams)
    response_policy:         ResponsePolicy        = field(default_factory=StandardResponse)
    reasoning_codec:         ReasoningCodec        = field(default_factory=NoReasoningCodec)
    cache_policy:            CachePolicy           = field(default_factory=NoCache)
    message_assembly_policy: MessageAssemblyPolicy = field(default_factory=NullMessageAssembly)
    assistant_text_policy:   AssistantTextPolicy   = field(default_factory=PassthroughAssistantText)
```

## `presets`

`build_capabilities(model_name, provider, overrides)` walks the merge order
`default → matched preset → provider overlay → overrides` and constructs a
fresh `ModelCapabilities`. Presets live in
[`tolokaforge_models/data/model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml).

### Preset coverage

Per-preset policy wiring as shipped today. The three `StrictSchema` presets
all cover the same two failure surfaces — `Decimal` look-ahead regex (P1,
Stage 1) and typed `Dict[str, T]` parameters (P2, Stage 2) — by combining
the same three policies. Keep this table in sync with
[`model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml).

| Preset                  | Match globs                                                      | `schema_sanitizer` | `response_policy`   | `prompt_policy`   | `content_policy` | `reasoning_codec` | `message_assembly_policy` | `assistant_text_policy` |
|-------------------------|------------------------------------------------------------------|--------------------|---------------------|-------------------|------------------|-------------------|---------------------------|-------------------------|
| `default`               | *(fallthrough)*                                                  | `passthrough`      | `standard`          | `none`            | `openai`         | `none`            | `null`                    | `passthrough`           |
| `anthropic_claude_4_7`  | `anthropic/claude-{opus,sonnet}-4.7*`, `*claude-{opus,sonnet}-4.7*` | `passthrough`      | `standard`          | `none`            | `anthropic`      | `anthropic`       | `null`                    | `passthrough`           |
| `anthropic`             | `anthropic/*`, `*claude*`                                        | `passthrough`      | `standard`          | `none`            | `anthropic`      | `anthropic`       | `null`                    | `passthrough`           |
| `openai_gpt5`           | `openai/gpt-5*`, `*gpt-5*`                                       | `strict`           | `array_dict_map`    | `none`            | `openai`         | `openai`          | `null`                    | `passthrough`           |
| `xai_grok`              | `x-ai/*`, `xai/*`, `grok*`                                       | `strict`           | `array_dict_map`    | `none`            | `openai`         | `openai`          | `null`                    | `passthrough`           |
| `qwen`                  | `qwen/*`, `qwen3*`                                               | `strict`           | `array_dict_map`    | `dict_map_hints`  | `openai`         | `openai`          | `null`                    | `passthrough`           |
| `aws_nova`              | `nova*` (+ provider `nova`)                                      | `passthrough`      | `unwrap_input`      | `none`            | `nova`           | `none`            | `nova`                    | `passthrough`           |

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
  `response_policy`, `reasoning_codec`, `cache_policy`,
  `message_assembly_policy`, `assistant_text_policy`). `params_policy`
  is intentionally omitted — it is a stateful `GenerationParams`
  dataclass whose constructor kwargs are already serialised alongside
  the fingerprint via `model_config.<role>.capabilities`, not a
  single-named policy.

Both helpers raise `ValueError` on unknown inputs rather than returning
placeholders — per AGENTS.md rule #1 we surface drift immediately. Unit
guard: [`tests/unit/llm/test_preset_fingerprint.py`](../tests/unit/llm/test_preset_fingerprint.py)
parametrises over every preset in
[`model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml) and
plants a rogue policy instance to confirm the raise path.

### Startup validation

Two install-time gates fire at
[`tolokaforge.core.llm.presets`](../tolokaforge/core/llm/presets.py) import
— before any `RunConfig` load, before the orchestrator or runner spawn any
child — so a bad `tolokaforge-models` install pair fails the process at
boot rather than at the first LLM call.

1. **Minimum-engine-version gate.**
   [`_check_minimum_engine_version()`](../tolokaforge/core/model_data.py)
   imports `tolokaforge_models` and reads
   `tolokaforge_models.minimum_engine_version`. Two failure branches:

   * `tolokaforge_models` is not importable → `RuntimeError` naming the
     `pip install tolokaforge-models` install instruction, chained from
     the underlying `ImportError`.
   * The installed engine version does not satisfy the specifier →
     `RuntimeError` naming both the installed engine version and the
     models-wheel floor (`>=0.17,<0.18`), with the actionable "upgrade
     the engine or downgrade the models wheel" hint.

   Engine version resolution goes through
   [`_resolve_engine_version()`](../tolokaforge/core/model_data.py), which
   tries the `tolokaforge` distribution first and falls back to
   `tolokaforge-runner-subset`; the same gate fires unchanged inside the
   runner subset image, whose distribution name differs from the base
   wheel.

2. **Class-name-existence gate.**
   [`_check_class_names_resolve()`](../tolokaforge/core/llm/presets.py)
   walks the bundled `model_presets.yaml` — every slot on the `default`
   block, every entry under `presets`, every entry under `providers` —
   and asserts that every referenced policy name (either a bare
   `schema_sanitizer: gemini` string or the `name` key of a
   `{name, params}` mapping) is a key of the merged `_POLICY_REGISTRIES`.
   Unresolved names raise `RuntimeError` naming every offending
   `(where, slot, policy)` triple with a
   [`difflib.get_close_matches`](https://docs.python.org/3/library/difflib.html#difflib.get_close_matches)
   suggestion drawn from the registry's live keyset. Runs after the
   `tolokaforge-models` entry-point merge, so it covers both engine
   defaults and out-of-tree registrations.

Canonical locks:
[`tests/canonical/test_models_wheel_absent.py`](../tests/canonical/test_models_wheel_absent.py),
[`tests/canonical/test_minimum_engine_version_gate.py`](../tests/canonical/test_minimum_engine_version_gate.py),
[`tests/canonical/test_class_name_existence_gate.py`](../tests/canonical/test_class_name_existence_gate.py).

## Public helper API

Every engine-general helper and base-class hook a per-model policy subclass
composes with is documented public API. Each name below carries the compat
guarantee **"stable within the v0.17.x minor series; removal or signature
change requires a deprecation announcement."** — the same guarantee ADR-0030
§ Requirements (4) makes the seam meet for out-of-tree per-model classes.
The [ADR-0030](adr/0030-tolokaforge-models-split.md) cutover
([#938](https://github.com/Toloka/tolokaforge/issues/938)) relocates the
per-model subclasses on top of exactly this surface.

### Engine-general helpers

Free functions, re-exported from `tolokaforge.core.llm`:

| Helper | Module | What it does |
|---|---|---|
| [`coerce_json_strings`](../tolokaforge/core/llm/response_policy.py) | `response_policy` | Decode stringified JSON arrays / objects in tool-call arguments back to native values. Heuristic: a `str` whose first non-whitespace character is `[` or `{` and whose `json.loads` returns a `list` / `dict`. Scalar JSON literals (`"42"` → `42`) are never promoted — string IDs would silently corrupt. |
| [`coerce_empty_containers`](../tolokaforge/core/llm/response_policy.py) | `response_policy` | Schema-aware recovery: coerces `""` → `[]` / `""` → `{}` for declared `array` / `object` / `dict_map` parameters. No-op without `param_types`; `""` on a `string` parameter passes through. |
| [`find_additional_properties`](../tolokaforge/core/llm/dict_maps.py) | `dict_maps` | Locate an `additionalProperties` declaration on a property schema or any of its `anyOf` / `oneOf` branches. Handles the Pydantic `Optional[Dict[str, T]]` shape (`anyOf=[{additionalProperties:T}, {null}]`). |

All three are consumed by shipped per-model policies — `JsonCoerceResponse`
and `ArrayDictMapResponse` compose `coerce_json_strings` / `coerce_empty_containers`
(engine-side); the models-wheel `MinimaxM3TagRecoveryResponse` reuses
`coerce_json_strings` for its tags-site recovery, and `RefResolvingDictMapHints`
composes `find_additional_properties`. Both are the intended entry points for
out-of-tree recovery classes.

### `StrictSchema` public hooks

`StrictSchema` is the extensible base for strict-validator sanitisers
(`openai_gpt5`, `xai_grok`, `qwen`, `gemini` presets). Two hook shapes:

**Overridable classmethod:**

* [`inline_refs_in_tool(cls, tool)`](../tolokaforge/core/llm/schema_sanitizer.py) — resolves per-tool `$ref` against the tool's parameter-level `$defs` block and drops the now-stale `$defs`. Subclasses that need cycle tolerance override this hook rather than reaching into `_inline_refs` (see [`GeminiRecursiveSchema`](../tolokaforge_models/src/tolokaforge_models/policies/gemini.py) — it substitutes a permissive open-object schema at any point of cyclic re-entry).

**Class-attribute hooks** — six flags on the class body, declared with
`ClassVar[…]` so a subclass method that mis-writes `self.<hook> = ...`
surfaces as a type-checker error rather than a silent instance-attribute
shadow:

| Attribute | Type | Default | Effect when overridden |
|---|---|---|---|
| `KEY_FIELD` | `ClassVar[str]` | `"key"` | Name of the synthetic key field on dict-map → array conversion. |
| `VALUE_FIELD` | `ClassVar[str]` | `"value"` | Name of the synthetic scalar-value field (only emitted when `carry_scalar_dict_map_value` is `True`). |
| `carry_scalar_dict_map_value` | `ClassVar[bool]` | `False` | Emit a synthetic `value` field for scalar-valued dict-maps (pair with `ScalarArrayDictMapResponse` on the response side). |
| `flatten_oneof_discriminator` | `ClassVar[bool]` | `False` | Flatten `oneOf` discriminated unions into a single object schema — Gemini needs this because its tool spec is a JSON-Schema subset that does not document `oneOf` / `discriminator`. |
| `strip_parameters_root_description` | `ClassVar[bool]` | `True` | Strip Pydantic's class-docstring artefact at the parameters root (redundant with `function.description` for strict validators). Gemini sets `False` — evidence shows the strip hurts on some flat tool schemas. |
| `strip_re2_incompatible_patterns` | `ClassVar[bool]` | `True` | Remove `pattern` values containing lookarounds / backreferences (OpenAI / xAI / Qwen-strict raise 500 on these). Gemini appears to pass RE2-incompatible patterns through unchanged and overrides to `False`. |

The defaults preserve the shipped OpenAI / xAI Grok behaviour.
[`GeminiSchema`](../tolokaforge_models/src/tolokaforge_models/policies/gemini.py) subclasses
`StrictSchema` and toggles the four booleans plus `VALUE_FIELD`;
`GeminiRecursiveSchema` subclasses `GeminiSchema` and additionally overrides
`inline_refs_in_tool`. Neither reaches into any `_`-prefixed symbol.

### `DictMapHints` public hook

`DictMapHints.build_hints(self, tools)` — public overridable instance method
on [`prompt_policy.py`](../tolokaforge/core/llm/prompt_policy.py). Called
by `enrich` when both `system` and `tools` are non-empty; returns the hint
text to append to the system prompt. Subclasses that need to close over
instance state (e.g. `RefResolvingDictMapHints` — the `$ref`-resolving +
one-level-nested variant used by the `thinkingmachines/inkling` route)
override the method directly (see
[`RefResolvingDictMapHints`](../tolokaforge_models/src/tolokaforge_models/policies/inkling.py) for
the shipped example); the shape is an instance method so the override needs
no `# type: ignore[override]` marker.

### Public-API boundary guardrail

[`tests/unit/llm/test_public_api_boundary.py`](../tests/unit/llm/test_public_api_boundary.py)
locks the invariant that every currently-shipped per-model subclass /
composite class reaches the engine through public API only. Four static /
runtime checks parse each entry's source via `ast` and walk
`_POLICY_REGISTRIES`:

* **`test_no_private_symbol_imports`** — rejects any `from tolokaforge.core.llm.<mod> import _<name>` in the subclass module.
* **`test_no_private_base_method_override`** — rejects a subclass method starting with `_` that shadows a base-class method of the same name (via `inspect.getmembers` on the concrete base).
* **`test_no_private_attribute_access_on_self_or_super`** — rejects `self._<attr>` / `cls._<attr>` / `super()._<attr>` reads whose `<attr>` is not defined locally in the subclass body.
* **`test_per_model_subclasses_covers_registered_non_base_classes`** — walks `_POLICY_REGISTRIES` and asserts every registered class inheriting from another in-registry class appears in the guardrail's `PER_MODEL_SUBCLASSES` list. Composite classes that inherit only from `object` (e.g. `MinimaxM3TagRecoveryResponse`) carry no automatic audit and must be added manually to `PER_MODEL_SUBCLASSES` when a new composite lands.

A per-model subclass added to a preset registry that regresses into a
`_`-prefixed name fails one of the four checks at test-import time — before
the [#938](https://github.com/Toloka/tolokaforge/issues/938) cutover can
bake the violation into `tolokaforge_models/policies/`. When adding a new
per-model subclass, list it in `PER_MODEL_SUBCLASSES` and either compose the
public helpers above or promote the private you need to public API in the
same PR (see [ADR-0030 § Follow-ups (8)](adr/0030-tolokaforge-models-split.md)).

## `client`

`LLMClient(config: ModelConfig, *, rate_limit_probe: RateLimitProbeConfig | None = None)`
composes a `ModelCapabilities` and wraps litellm's `completion()`.
`generate(...)` returns a `GenerationResult` carrying `text`, `tool_calls`,
`usage: Usage`, `latency_s`, `cost_usd`,
`reasoning: StructuredReasoning | None`, and `effective_system_prompt`.
See § `usage` above for the full Usage schema and accumulation contract.

`UserSimulator` wraps `LLMClient` for tau-bench-style user simulation with
`scripted` or `llm` modes. An `llm`-mode reply is delivered only if it survives
the guard described in § `UserSimulator` request and reply contract;
`GenerationResult.guard_rejections` carries the defects of the attempts
discarded before it, and is empty everywhere else.

### Outer retry controllers

`generate()` builds a fresh `tenacity.Retrying` per call, so a stubbed
`_retry_sleep` and the call's `LLMCallObservation` are both read at call time
(the client instance is shared across concurrent trials).

| Controller | Selected when | `stop` | `wait` |
|---|---|---|---|
| default (`_build_retrying`) | always, unless probe mode is on | `stop_after_attempt(5)` | `wait_exponential(multiplier=2, min=4, max=60)` |
| probe (`_build_probe_retrying`) | `rate_limit_probe` resolves to an enabled config | 429: `seconds_since_start >= per_call_budget_s`; other: 5 **non-429** attempts | 429: `wait_fixed(retry_interval_s)`, combined with `wait_random(+/- jitter_fraction x interval)` unless the fraction is `0`; other: the same exponential |

Both install the same `before_sleep` hook (`_make_before_sleep`), so
`llm_retry_scheduled` events are identical on either path. `retry` is
`_should_retry_exception` on both.

The probe's split accounting is load-bearing: a 5xx must not inherit the
multi-hour 429 budget, so the non-429 attempt cap counts only non-429
attempts. The non-429 exponential reads the *global* attempt number, so after
a long 429 stretch a later 5xx resumes the curve rather than restarting it —
waits only ever get longer, and the five-attempt cap is unchanged. The jitter
applies only to the 429 wait; it is symmetric, so the mean interval is exactly
`retry_interval_s` and the `1 / retry_interval_s` poll-rate arithmetic the mode
exists for survives in expectation.

429 classification on the probe path is `_is_rate_limit_exception`. It answers
"is this a **transient** 429?" in three tiers, walking the `__cause__` chain
because `_call_with_key_rotation` re-raises provider errors as
`RuntimeError(...) from e`:

1. **Type / status** — `isinstance(exc, openai.RateLimitError)` (which litellm's
   `RateLimitError` subclasses) or `status_code == 429`.
2. **Terminal-condition veto** — `AllApiKeysExhaustedError`. Key rotation is
   triggered by OpenRouter's own per-key **429** ("Key limit exceeded") and the
   final raise chains that typed 429 as its `__cause__`, so tier 1 would classify
   a spent credential set as transient and hand it the multi-hour budget —
   permanently, since `_rotate_key` only ever advances its index. The type stops
   the walk and returns `False`, so the condition takes the ordinary
   five-attempt exponential branch instead. `_should_retry_exception` is
   deliberately unchanged, so a probe-off run retries it exactly as before.
3. **Anchored text** (`binding.rate_limit_patterns` — see § Provider bindings),
   last resort: a 429 must sit in a status position (`Error code: 429`,
   `status_code=429`, `HTTP/1.1 429`), or the message must carry the HTTP reason
   phrase or rate-limit prose in an error construction. An unanchored
   `"429" in str(exc)` matched token counts (`you requested 4429`), request ids
   (`req_8f429ab2`) and JSON bodies. This tier runs **only when no link in the
   chain carried an HTTP status at all** — i.e. for the shape it exists for, a
   wrapper that stringified the provider error instead of chaining it. An
   authoritative non-429 status beats prose, because the outermost message is
   `RuntimeError(f"LLM API call failed: {e}")` and `e`'s message can embed a
   response body that echoes request content: a task conversation about rate
   limiting would otherwise hand a deterministic 400 the multi-hour budget.
   Untyped chains still text-match — under-matching a real 429 is the more
   expensive direction, since the absorption is the whole feature. Every shipped
   provider carries the same anchored default list (`DEFAULT_RATE_LIMIT_PATTERNS`
   in [`providers.py`](../tolokaforge/core/llm/providers.py)); the field is
   per-provider so onboarding a provider whose rate-limit prose differs is a
   `providers.yaml` edit.

The text tier is a catalogue of *engine-wrapper* shapes, not of provider quota
prose. Vertex `RESOURCE_EXHAUSTED`, OpenAI `insufficient_quota`, `TPM limit
reached` and Anthropic `overloaded_error` match nothing there on purpose: they
arrive typed through litellm, so tier 1 catches them.

`core/loop.py`'s `classify_loop_error(exc, patterns)` shares the type tiers
through `is_typed_rate_limit_exception`, because `TerminationReason.RATE_LIMIT`
excludes a trial from every benchmark rate and prose is not evidence strong
enough to spend that (see [`docs/GRADING.md`](GRADING.md:1) § Infrastructure
aborts produce no grade). It uses `matches_rate_limit_text` only as a
diagnostic: a rate-limit-shaped message with no typed exception behind it
terminates as a *counted* reason, not as an abort. The public seam callers use
is `LLMClient.classify_loop_error(exc)` — a bound method that closes over the
compiled `binding.rate_limit_patterns` so the compiled tuple never crosses a
module boundary. `ToolCallingLoop` receives it via
`classify_error=llm_client.classify_loop_error`. The remaining text-matching
classifiers (`core/runner.py`'s user-simulator retry, `core/resume.py`) are
separate and unaffected — `AllApiKeysExhaustedError` subclasses `RuntimeError`.

### Probe telemetry recording sites

Both sides of throughput are recorded by the two ends of the same pair of hooks
`generate()` already installs, so no new plumbing crosses the client boundary:

| Census | Hook | Recorded |
|---|---|---|
| failure (429s) | `_make_before_sleep` | `retries`, `wait_s`, the 429 window |
| success (goodput) | `_record_probe_success`, called from `_fire_call_finished` | successful calls, summed `duration_s`, prompt + completion tokens |

Both are keyed by the call's `role` and the client's model slug — both already in
scope at those sites, which is the whole reason the recording lives there — and
both are gated the same two ways: the trial must carry a `RateLimitProbeStats`
*and* this client's own probe must be active. The second half is load-bearing: a
default-path client (the rubric judge, a fallback-chain member) must never
contribute to a measurement it is not part of.

The agent and the user simulator are different models in an arena config, so
their counters never merge; and `Metrics.usage` cannot answer the same questions —
`usage.calls` holds agent calls only and carries no role field. See
[OUTPUT_FORMAT.md](OUTPUT_FORMAT.md:1) § `rate_limit_*` / `probe_*`.

`duration_s` is the *outer* per-attempt wall time (`generate` brackets
`_generate_once`), i.e. how long the client actually held the call in flight —
summed over successes and divided by wall time it is the Little's-law in-flight
concurrency the provider served. Tokens come off the `Usage` that
`_assemble_result` already built for that call, so nothing is re-extracted and
`usage.calls` is never double-counted.

Successes are additionally bucketed into fixed-width windows whose boundary is
`floor(epoch / bucket_width_s) * bucket_width_s` — **absolute time, not run
start** — so windows emitted by simultaneous run legs in separate processes align
and can be summed window by window. See
`RateLimitProbeStats.bucket_start` for the boundary contract and the bucket-cap
drop policy.

Probe mode is a run policy, not a model property: it is configured under
`orchestrator.rate_limit_probe` (see
[CONFIG.md](CONFIG.md:1) § `rate_limit_probe`), never through the preset
registry, so `effective_preset` in the run artifacts stays the model's real
preset. There is no env override — the passed config block is the only
activation channel, so the paths that must never probe (the rubric judge, a
`--fallback-models` chain) cannot be armed by an environment variable, and the
budget assertions cannot be bypassed.

The client is only half the mode. The orchestrator arms this client; the
**conductor** wires the user-simulator probe, the per-task effective-budget
re-check and the per-trial telemetry accumulator. Conductors are a plugin group,
so `Orchestrator._build_conductor` and `run_trial` both refuse to start an armed
run on a conductor that does not declare `supports_rate_limit_probe` — otherwise
the run would absorb 429s while writing all-default `rate_limit_*` / `probe_*`
metrics, and nothing in the artifacts would show it.
