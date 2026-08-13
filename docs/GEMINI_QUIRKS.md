# Gemini Model Quirks

Empirically-observed behaviors of the Google Gemini family (Flash 3.0,
Flash 3.5, Pro 3.1) on tolokaforge's OTS + tau benchmarks. Sourced from
the 2026-05-21 codec investigation and the 2026-05-22 full re-eval
(715 trials per cell on `ots_07_logistics_internal`, 645 trials per cell
on six other domains).

This document is meant for harness contributors and task-pack authors
who need to:

- Recognise a known Gemini behavior in eval output.
- Know what the harness already does about it.
- Avoid re-investigating it.

Companion docs: [`LLM_LAYER.md`](LLM_LAYER.md) (provider abstractions),
[`ADD_NEW_MODEL.md`](ADD_NEW_MODEL.md) (registry conventions), and the
investigation report at
[`plans/gemini_31_pro_ots_investigation_20260521.md`][report] (local,
not checked in).

[report]: ../plans/gemini_31_pro_ots_investigation_20260521.md

## TL;DR

| Quirk | Affects | Status |
|---|---|---|
| `reasoning_details` `id`/`format`/`index` must round-trip | All Gemini via OpenRouter | **Fixed** in [`GeminiReasoningCodec`](../tolokaforge/core/llm/reasoning_codec.py) |
| Empty assistant content with tool_calls gets echoed by Gemini | All Gemini | **Fixed** via [`NullMessageAssembly`](../tolokaforge/core/llm/message_assembly_policy.py) (only `aws_nova*` opts into the filler) |
| `oneOf`+`discriminator` Pydantic unions → invented arg names | All Gemini | **Fixed** in [`GeminiSchema`](../tolokaforge_models/src/tolokaforge_models/policies/gemini.py) |
| OpenRouter's 48-char placeholder UUID on no-thinking turns | All Gemini | **Fixed** — codec drops it on replay (togglable) |
| `litellm` direct `gemini/*` + `reasoning_effort=medium` → empty response | All Gemini, direct provider only | **Guarded** via `unsupported_effort_levels` in [`model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml) |
| Nullable + optional Pydantic fields treated as opt-in | All Gemini, **most strict in Pro 3.1** | Intrinsic — measured by eval |
| Doubled-prefix tool name mangling (`a_a_foo` → `a:a_foo`) | Pro 3.1 | Known_unsupported `TOOL_NAME_DISCIPLINE` |
| Lexical tool invention (`knowledge_base_search_policy`) | Pro 3.1 | Known_unsupported `LEXICAL_TOOL_INVENTION` |
| Reasoning runaways at `max_tokens` ceiling | Flash 3.5 | Open: [#147](https://github.com/Toloka/tolokaforge/issues/147) |
| Over-eager workflow completion on multi-step tasks | Flash 3.5 | Intrinsic — visible on `ots_bank_hr_d365` |

## 1. Common quirks across the Gemini family

These show up on Flash 3.0, Flash 3.5, AND Pro 3.1. Most are already
neutralised in the harness.

### 1.1 Empty assistant content gets echoed back

Gemini pattern-matches the literal filler text `"I'll help you with
that."` in past assistant turns and reproduces it as its own content on
later turns (~26–38% of OTS trials in the 2026-04-30 evaluation that
surfaced this). The filler was originally introduced as a Bedrock/Nova
workaround for an unrelated provider quirk and was applied universally;
the symmetric fix made it provider-scoped.

**Detection**: assistant `message.content` on later turns contains
`"I'll help you with that."` verbatim despite the system prompt asking
for substantive content.

**Harness mitigation**: the Gemini preset (like every non-Nova preset)
carries [`NullMessageAssembly`](../tolokaforge/core/llm/message_assembly_policy.py),
which declares `inject_empty_assistant_filler = False`. Only the `aws_nova`
and `aws_nova_openrouter` presets opt in via `NovaMessageAssembly`
(`empty_assistant_filler = "I'll help you with that."`); the filler string
is data on the policy instance so a future preset overlay can override it
without engine changes ([`model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml)).

### 1.2 `reasoning_details` `id`/`format`/`index` must round-trip

OpenRouter's Gemini response surface attaches per-block metadata:

```json
{
  "tool_calls": [{
    "id": "tool_get_weather_yyPxUPE2pPLgukg6ouy1",
    "function": {"name": "get_weather", "arguments": "..."}
  }],
  "provider_specific_fields": {
    "reasoning_details": [
      {"type": "reasoning.text",      "text": "...", "format": "google-gemini-v1", "index": 0},
      {"type": "reasoning.encrypted", "data": "...", "format": "google-gemini-v1",
       "index": 1, "id": "tool_get_weather_yyPxUPE2pPLgukg6ouy1"}
    ]
  }
}
```

The `id` on an encrypted block **literally matches a `tool_call.id`** —
OpenRouter uses this to reconstruct Gemini's per-functionCall
`thought_signature` on the next turn. Dropping `id` / `format` /
`index` on replay breaks reasoning continuity past turn 1.

**Measured impact** on `ots_07_logistics_internal` (full eval,
n=715/cell):

| Model | Pass rate before preserving extras | After |
|---|---|---|
| Pro 3.1 | 8.8% | **48.9%** (+40 pp, p≈2e-12) |
| Flash 3.0 | 21.4% | **34.8%** (+13 pp, p≈2e-08) |
| Flash 3.5 | 31.9% | **57.3%** (+25 pp, p≈3e-12) |

**Harness mitigation**: [`ReasoningBlock.extras`](../tolokaforge/core/llm/reasoning.py)
field (frozen tuple of `(key, value)` pairs) plus
[`GeminiReasoningCodec`](../tolokaforge/core/llm/reasoning_codec.py)
extracts and re-emits every unmodeled envelope field byte-for-byte.

### 1.3 `oneOf` + `discriminator` produces invented arg names

When a tool parameter is declared as a Pydantic discriminated union
(`Annotated[..., Field(discriminator='kind')]`), Pydantic emits
`oneOf` + a `discriminator` keyword that Gemini's tool spec subset does
not support. The model **ignores the registered property names and
emits English-sounding ones** instead:

```
registered:  {"qty": 5, "subject": "WMS access"}
emitted:     {"quantity": 5, "title": "WMS access"}   # wrong names
```

Live-verified 2026-05-20 against all three Gemini models.

**Harness mitigation**: [`GeminiSchema`](../tolokaforge_models/src/tolokaforge_models/policies/gemini.py)
flattens `oneOf` + `discriminator` into a single object schema unioning
every branch's properties. Bare `Union[A, B]` (Pydantic emits `anyOf`
without `discriminator`) is left untouched — flattening it caused a
40% → 0% logistics regression on a separate run and the inline `anyOf`
shape Gemini handles correctly at the field-name level.

### 1.4 No-thinking placeholder on tool-follow-up turns

OpenRouter returns a constant 48-char base64 blob (decoded:
`e24830a7-5cd6-42fe-998b-ee539e72b9c3`) in `reasoning.encrypted.data`
when Gemini emitted no real thinking on a turn. Real opaque blobs are
1000–3000+ chars.

**Detection**: a `reasoning.encrypted` block with `len(data) < 100`.

**Harness mitigation**: [`GeminiReasoningCodec._is_placeholder_block`](../tolokaforge/core/llm/reasoning_codec.py)
drops these on replay so they don't waste prompt tokens. The codec
**still records** the placeholder in `trajectory.yaml` so the on-disk
artifact reflects what the wire returned. Togglable via the
`gemini_drop_placeholder_signature` capability override (default
`True`).

### 1.5 `litellm` direct `gemini/*` + `reasoning_effort=medium` is broken

As of `litellm==1.83.14`, the direct `gemini/*` provider returns
`finish_reason=stop` with zero completion tokens and no tool calls
whenever `reasoning_effort=medium` is combined with `tool_choice`.
`low` and `high` work correctly. The OpenRouter route is **unaffected**
because it sends `extra_body.reasoning.effort=medium`, which OpenRouter
translates upstream into Google's `thinking_level=medium`.

**Harness mitigation**: [`unsupported_effort_levels: ["medium"]`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml)
on the `providers.gemini` overlay. Per AGENTS.md rule #1, the harness
**fails loud** rather than silently mapping to `high`:

```
ValueError: ReasoningConfig(effort_hint='medium') is declared unsupported
for this provider+model combination (refused: ['medium']). Evidence:
declared via the unsupported_effort_levels shorthand. Use one of
['low', 'high', 'xhigh'], or route through a transport that supports this
effort level (e.g. OpenRouter rather than the direct provider).
```

### 1.6 Nullable + optional Pydantic fields are treated as opt-in

Gemini reads the JSON Schema fragment

```yaml
organization_id:
  anyOf:
  - type: string
  - type: 'null'
  default: null
  description: ID of the organization
```

combined with "not in `required`" as "this field may be omitted —
default is null." It then omits the field on tool calls **even when
the value is available in the conversation context**. This is consistent
with strict OpenAPI / JSON-Schema reading; other model families
(GPT-5, Opus, DeepSeek) read more permissively and fill the field from
context.

**Severity scales by model**:

| Model | On ACC-001 (full TicketCreate schema) | Behavior |
|---|---|---|
| GPT-5 family | 22/22 fields | Fills every nullable optional |
| Flash 3.0/3.5, Opus 4.7, DeepSeek v4 | 19/22 fields | Fills when value is conversationally evident |
| **Pro 3.1** | **18/22 fields** | Most strict — fills only when overwhelming evidence |

**Harness response**: none. This is intrinsic model behavior and is
correctly measured by the eval. Marking fields `required` in the task
pack would be a Gemini-specific schema accommodation; we deliberately
do not.

The four canonical "Pro-skipped" fields on `ACC-001` all match the
shape: `assignee_id`, `due_at`, `external_reference_id`,
`organization_id` — all `anyOf:[T, null]`, `default: null`, not in
`required`.

## 2. Pro 3.1 — model-specific quirks

### 2.1 Doubled-prefix tool name mangling

When the registered tool name has a doubled namespace prefix (e.g.
`workday_api_workday_api_get_employee`, `hris_hris_get_timecard`), Pro
emits the name with `:` substituted for the duplicated `_` segment:

```
registered:  workday_api_workday_api_get_employee
Pro emits:   workday_api:workday_api_get_employee
```

The harness rejects the synthesised name (`Tool 'workday_api:workday_api_get_employee'
not found in agent tools`) and the model retries — often eventually
hitting `max_turns` without finishing the task.

**Affects**: the `ots_*_internal` task packs use this convention
heavily (`ots_07_logistics_internal` has 19/27 tools with doubled
prefixes; `ots_08_travel_internal` has many; `tau_manufacturing` has
none, hence no exposure).

**Detection**: live reproducer at
[`tolokaforge/testing/certify/suite/test_tool_name_discipline.py`](../tolokaforge/testing/certify/suite/test_tool_name_discipline.py)
captures the symptom. Pro is declared `TOOL_NAME_DISCIPLINE`
known_unsupported in
[`tolokaforge_models/src/tolokaforge_models/certificates/registry.py`](../tolokaforge_models/src/tolokaforge_models/certificates/registry.py).

**Harness response**: known_unsupported declaration. Not silently
worked around — the eval correctly measures the cost.

### 2.2 Lexical tool invention

When the system prompt mentions a conceptual lookup ("consult the
knowledge base"), Pro fabricates a tool name like
`knowledge_base_search_policy` even when the registered tool is named
`typesense_search_policy`. The fabricated name resembles the prompt's
phrasing, not the tool catalog.

**Detection**: live reproducer at
[`tolokaforge/testing/certify/suite/test_lexical_tool_invention.py`](../tolokaforge/testing/certify/suite/test_lexical_tool_invention.py)
captures this. Pro is declared `LEXICAL_TOOL_INVENTION` known_unsupported.

Example trajectory snippet (Pro on `ots_07_logistics_internal`
pre-fix):

```yaml
- role: assistant
  tool_calls:
  - name: knowledge_base_search_policy       # invented; not registered
    arguments: {query: "System Access"}
- role: tool
  content: "Tool 'knowledge_base_search_policy' not found in agent tools"
```

### 2.3 Pre-fix: "stops reasoning after turn 1" on long-context tool flows

(Now fixed by §1.2.) Worth keeping recorded because the surface signature
is distinctive:

- Turn 1: model emits substantial reasoning (~hundreds of tokens), a
  full tool-call plan, and the corresponding tool_calls.
- Turns 2..N: model emits zero `reasoning_tokens` per call, brief
  tool_calls only, no content.
- Final turn: model emits substantial reasoning again when wrapping up
  the user-facing answer.

Pre-fix, this happened on 53.6% of Pro's `ots_07_logistics_internal`
calls vs 7.7% on `tau_manufacturing`. The fix in §1.2 restored
reasoning to every turn.

### 2.4 Most aggressive nullable-optional skip behavior

See §1.6. Pro is the family member most likely to omit nullable
optional fields. Within OTS task packs, the dominant failure pattern is
missing `organization_id` (60% of post-fix failures on logistics; was
92% pre-fix).

### 2.5 Where Pro outperforms the smaller Geminis

Pro is the top Gemini on `ots_travel_marketplace_external_support`
(52.3% vs Flash 3.5's 36.7%) and on `tau_manufacturing` (82.4% vs
76.2%). Both domains reward careful reasoning over workflow completion
speed. On domains where the "right answer" is sometimes "stop early"
or "escalate without acting" (see §3.3), Pro's slower, more deliberate
pattern wins.

## 3. Flash 3.5 — model-specific quirks

### 3.1 Reasoning runaways at `max_tokens` ceiling

Flash 3.5 occasionally enters a "ponder forever" mode where it emits
~15,728 reasoning tokens (just under the YAML `max_tokens: 16384`
ceiling) per call for many consecutive turns without producing any
tool call or content. Worst observed trial:

```
ots_07_logistics_internal / gemini_35_flash / OBH-007 / trial 4

  30 turns (hit max_turns)
  165,361 cumulative reasoning_tokens
  prompt grew 17k → 240k tokens by the last turn
  termination_reason: max_turns
  cost: $2.96
```

Frequency at full eval scale on logistics (n=715): **4 trials** had
≥3 consecutive max-tokens reasoning-only calls (vs 0 pre-fix). The
codec fix in §1.2 correctly preserves reasoning across turns, which
lets Flash 3.5 sustain longer thinking chains than it could pre-fix —
this is the operational side-effect.

**Harness response**: open issue
[#147](https://github.com/Toloka/tolokaforge/issues/147)
proposes a stuck-detector rule for this pattern. Tracking issue
[#148](https://github.com/Toloka/tolokaforge/issues/148)
adds a composable per-call reasoning ceiling. Neither shipped yet.

### 3.2 Heavy completion-token usage (good and bad)

Across OTS post-fix, Flash 3.5 averages 3000–5689 reasoning tokens per
trial — **2-3× more than Pro** on most domains. On most domains
(logistics, qsr, airlines) the extra reasoning translates into the
highest pass rate of the Gemini family. On `ots_bank_hr_d365` it
backfires — see §3.3.

### 3.3 Over-eager workflow completion on multi-step tasks

On `ots_bank_hr_d365`, Flash 3.5 calls `d365_api_send_email_response`
on **64% of trials (410/645)** even when the task expects
escalation/approval before sending. The expected workflow is "create
case → add note → wait / escalate"; Flash 3.5 plows on to "send email"
every time.

Concrete example: `BEN-001` (benefits change request, all 5 trials):

```
expected tool sequence:
  workday_api_get_employee → typesense_search_policy →
  d365_api_create_case → d365_api_add_case_note →
  (stop here — wait for approval before sending email)

Flash 3.5 sequence (every trial):
  workday_api_get_employee → typesense_search_policy →
  d365_api_create_case → d365_api_add_case_note →
  d365_api_draft_email_response → d365_api_send_email_response  ← spurious
```

**Comparison across Gemini family on the same domain**:

| Model | Spurious `d365_api_email_responses` rows | bank_hr pass |
|---|---|---|
| Flash 3.0 | **0** | 31.8% |
| Pro 3.1 | 16 | 25.3% |
| Flash 3.5 | **410** | **12.9%** |

Pre-fix Flash 3.5's broken reasoning continuity made it inconsistent —
it sometimes wandered off-plan and never reached the `send_email` step,
accidentally matching the "don't send" expectation. Pre-fix pass rate:
29.9%. Post-fix Flash 3.5 reliably executes its planned workflow,
including the wrong send — pass rate drops to 12.9%.

**Domain footprint**: this manifests strongly on `bank_hr_d365` because
the workflow legitimately ends mid-flow in some cases. On domains
where the expected workflow is "do these N steps, finish with single
tool call" (logistics, airlines, qsr), Flash 3.5's eagerness is
correctly rewarded.

**Harness response**: none — this is intrinsic Flash 3.5 model
behavior. Not actionable in the harness without biasing per-domain.

### 3.4 Biggest beneficiary of the codec fix

Among the three Gemini variants, Flash 3.5 had the largest aggregate
pass-rate gain from the §1.2 fix (+25 pp average across OTS, vs +20 pp
for Pro and +5 pp for Flash 3.0). The two largest single-domain gains
across the Gemini family are both Flash 3.5:
`ots_07_logistics_internal` (+25.4) and
`ots_11_qsr_food_services_internal` (+30.9).

## 4. Flash 3.0 — model-specific quirks

### 4.1 Less affected by codec continuity than its siblings

Flash 3.0 averaged the smallest aggregate gain from the §1.2 fix —
roughly +5 pp on OTS in aggregate, vs +20–25 pp for the other Geminis.
Two domains (`ots_19_airlines`, `ots_travel_marketplace`) showed mild
negative deltas within noise.

Hypothesis: Flash 3.0's pre-fix reasoning_tokens per trial were already
higher than Pro's (2846 vs 1256 on logistics), so it had less of a
"broken continuity" deficit for the fix to recover.

### 4.2 Wins on `bank_hr_d365` by *not* completing workflows

Flash 3.0 is the highest-scoring Gemini on `ots_bank_hr_d365` (31.8%).
It avoids Flash 3.5's email-send trap — generating zero spurious
`d365_api_email_responses` rows. It still has other failure modes
(spurious notifications, wrong case status) but they aren't as
dominant.

This isn't a "Flash 3.0 is good" signal — it's "Flash 3.0 happens to
do less work, which is sometimes correct." It also wins by accident on
`SHP-006` / `SHP-011`-style task regressions in `ots_07_logistics`
where pre-fix Flash 3.0 was stochastically skipping fields that
post-fix Flash 3.0 reliably fills (sometimes filling them wrong).

### 4.3 Otherwise: middle-of-the-pack Gemini

On the remaining domains, Flash 3.0 lands between Pro and Flash 3.5
without strong distinguishing characteristics. Worth using when:

- The doubled-prefix tool name issue (§2.1) precludes Pro.
- The reasoning-runaway risk (§3.1) precludes Flash 3.5.
- Cost matters: cheapest of the three on most domains.

## 5. Things that look like quirks but are misattributed

### 5.1 "Gemini Pro has a 22 pp gap to Sonnet 4.6 on logistics — must be a Gemini bug"

False after the §1.2 fix. The residual gap is the §1.6 nullable+optional
skip behavior plus the §2.1 / §2.2 known regressions. Removing those
would require Gemini-favoring schema or prompt changes that we
explicitly do not make.

### 5.2 "Empty completion_tokens on tool turns" / "MALFORMED_FUNCTION_CALL"

Not Gemini-specific — these are litellm provider-translation bugs that
apply to multiple providers (Anthropic, Gemini direct paths) and are
caught by the synthetic-envelope detector (see
[`response_policy.py`](../tolokaforge/core/llm/response_policy.py)). The
2026-05-21 logistics re-eval found 0 such envelopes across all three
Gemini models on the OpenRouter route, so this is a real but
non-Gemini-route concern.

### 5.3 "Cost grew after the fix"

Real but expected. Post-fix Pro is reasoning 2× more per trial, which
costs more per call. The cost-per-pass actually **dropped** (Pro on
logistics: $1.86 / pass → $0.41 / pass — 4.5× more efficient).

## 6. Decision matrix — picking a Gemini variant for a domain

| If your domain has… | Prefer | Avoid | Reason |
|---|---|---|---|
| Many doubled-prefix tool names (`ots_*_internal`) | Flash 3.5 or 3.0 | Pro 3.1 | §2.1 |
| Multi-step workflows where stopping early is right (bank_hr-shape) | Flash 3.0 or Pro | **Flash 3.5** | §3.3 |
| Heavy mid-task reasoning required | Pro 3.1 | Flash 3.0 | Pro thinks more carefully |
| Cost-bounded, simple-shape tasks | Flash 3.0 | Flash 3.5 | Cheapest, no runaway risk |
| Many nullable-optional Pydantic fields critical to grading | (any non-Gemini) | All Gemini | §1.6 — fundamental |

## 7. References

- Investigation report: [`plans/gemini_31_pro_ots_investigation_20260521.md`](../plans/gemini_31_pro_ots_investigation_20260521.md) (local, gitignored)
- Open issues:
  [#147](https://github.com/Toloka/tolokaforge/issues/147)
  (stuck detector),
  [#148](https://github.com/Toloka/tolokaforge/issues/148)
  (composable reasoning ceiling),
  [#149](https://github.com/Toloka/tolokaforge/issues/149)
  (Anthropic codec parity),
  [#150](https://github.com/Toloka/tolokaforge/issues/150)
  (config-error retry filter),
  [#151](https://github.com/Toloka/tolokaforge/issues/151)
  (api_error follow-up).
- Capability registry: [`tolokaforge_models/src/tolokaforge_models/certificates/registry.py`](../tolokaforge_models/src/tolokaforge_models/certificates/registry.py)
  (`TOOL_NAME_DISCIPLINE`, `LEXICAL_TOOL_INVENTION` declared
  `known_unsupported` for Pro).
- Codec fix commits: `c394409a0` (extras round-trip), `8b1511d67`
  (`unsupported_effort_levels`).
- Eval data: `output/collected/` (post-fix), 2026-05-21 / 2026-05-22.
