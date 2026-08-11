# 0030. Model data as a second PyPI wheel — `tolokaforge-models` from the same monorepo

- **Status:** Proposed
- **Date:** 2026-08-06 (widened 2026-08-07 per [issue-#645 comment](https://github.com/Toloka/tolokaforge/issues/645#issuecomment-5213977204); further revised 2026-08-07 per second review pass on PR #908)
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

> **Widening note (2026-08-07).** The original draft of this ADR (open in PR #908 as of 2026-08-06) put policy, codec, and sanitizer *classes* explicitly out of scope, following ADR-0002's own § Considered Options which deferred its Option 4. A review by @bberkes-toloka against the auto-integration history showed that boundary does not hold: 3 of 5 recent auto-integrations needed new engine classes, and the policy modules changed 5 times since 2026-05-28, every one driven by onboarding a specific model. The ADR is widened in place — new decision-drivers, new § Requirements section, new extension-point surface, provider-bindings-as-data, install-time validation — so PR #908 lands the full picture rather than the first half of it. Sections added by the widening are marked in the text.

## Context and Problem Statement

Adding a model to tolokaforge today requires cutting an engine release even when the change is pure data — a price-table entry plus a preset routing that composes *existing* policy classes. The overlay seam described in [ADR-0002](0002-external-model-registry.md) (`--presets-file` / `RunConfig.engine.presets_file`, loaded at [`tolokaforge/core/llm/presets.py`](../../tolokaforge/core/llm/presets.py)) opened the *run-time* half of that decoupling: an operator can point an evaluation at an out-of-tree preset overlay and never touch the engine repo. But the *release-time* half is unfinished. Model onboarding still gates on an engine PR, which drags a wheel release with it.

The pattern is measurable in the release log. Between v0.8.4 → v0.11.2 (2026-07-15 → 2026-07-27, 12 days), **2 of 9 releases were pure zero-engine model shims** — v0.9.1 (kimi-k3 + muse-spark-1.1) and v0.9.3 (gemini-3.5-flash), both with no `### Feat` / `### Fix` sub-sections in [`CHANGELOG.md`](../../CHANGELOG.md) (bare version headers). A third (v0.11.1) bundled claude-opus-5 with unrelated engine work. v0.9.1 was cut hours after v0.9.0; v0.9.3 one day after v0.9.2. The "two releases per model" pattern [ADR-0002 § Context](0002-external-model-registry.md#context-and-problem-statement) called out is still visible on `git log --tags`.

Downstream consequence: consumers that pin `tolokaforge` by tag drift across engine versions purely as a side effect of model onboarding. For any consumer that compares evaluation results across models, "which engine version did this run on" becomes a per-model question instead of a constant, and every difference then needs its own justification.

### Evidence widening the scope (added 2026-08-07)

The first draft of this ADR framed the fix as "move the *data* out, keep the *classes* in", following ADR-0002 § Considered Options's own deferral of its Option 4 (sidecar Python module for policy classes). The auto-integration history since ADR-0002 landed makes that boundary untenable:

| date | model | needed engine code | module |
|---|---|---|---|
| 2026-07-17 | meta/muse-spark-1.1 (#474) | yes | shared certification test body |
| 2026-07-17 | moonshotai/kimi-k3 (#475) | no | — |
| 2026-07-22 | google/gemini-3.5-flash (#560) | yes | `response_policy.py`, `schema_sanitizer.py` |
| 2026-07-27 | anthropic/claude-opus-5 (#614) | no | — |
| 2026-07-31 | thinkingmachines/inkling (#719) | yes | `prompt_policy.py` |
| 2026-08-04 | deepseek/deepseek-v4-flash-0731 (#846) | yes | `reasoning_codec.py` |
| 2026-08-04 | qwen/qwen3.8-max (#845) | no | — |
| 2026-08-01 (in flight) | cohere/command-a-plus-05-2026 (#929) | yes | needs a new lifecycle stage — assistant-text hook does not yet exist |

Of the 5 auto-integrations the pipeline resolved end-to-end, **3 needed new engine classes**. Since the OSS cleanup (#6, 2026-05-28), the policy / codec / sanitizer modules have changed **five times, every one driven by onboarding a specific model, none by engine-internal work**: `response_policy.py` (2), `prompt_policy.py` (1), `schema_sanitizer.py` (1), `reasoning_codec.py` (1). `content_policy.py`, `cache_policy.py`, and `params_policy.py` have not changed on `main` at all in that window.

ADR-0002 § Considered Options deferred its Option 4 on an explicit, measurable condition: *"until eval data shows custom policy classes recurring per model often enough to justify the escape hatch."* That condition is now visibly met.

"Just move the subclasses out" is also insufficient. Three concrete failure modes recur in the tree:

1. **A per-model subclass requires editing its parent to add a hook.** `#560` added `VALUE_FIELD` and `carry_scalar_dict_map_value: bool = False` to `StrictSchema` itself (`schema_sanitizer.py`) plus the branch that honours it; `GeminiRecursiveSchema` sets the flag. Moving the subclass leaves 16 dependent lines in the base.
2. **A per-model class reaches into module-private helpers.** `MinimaxM3TagRecoveryResponse` (`response_policy.py:474`) is a composite — not a subclass — reusing `_coerce_json_strings` / `_coerce_empty_containers`; `RefResolvingDictMapHints` (`prompt_policy.py:107`) imports a symbol deliberately excluded from `_dict_maps.__all__` and overrides a `@staticmethod` `_build_hints` with an instance method carrying `# type: ignore[override]`. Out-of-tree code cannot depend on any of that, and turning `_build_hints` into a proper extension point is a change to the base's *shape*, not a pure rename.
3. **Some adaptations have nowhere to attach.** `_POLICY_REGISTRIES` has six slots; there is no `params` slot (so a new knob is a `GenerationParams` signature change), no `assistant_text` slot (this is what blocks the open Cohere integration #929 — the model wraps response text in `<|START_TEXT|>…<|END_TEXT|>` markers observed 2026-08-01, and `ResponsePolicy` cannot reach it because that hook only post-processes tool-call arguments, so the markers land in `trajectory.yaml` and depress LLM-judge scores; recorded at `registry.py:2487`), and no message-assembly slot either (how the conversation is assembled before it goes on the wire — ordering, merging, filler injection — has no extension point; `ToolContentPolicy.inject_empty_assistant_filler` is a per-preset *flag*, but the string it injects is hardcoded at `client.py:1213`, tuned once for one model, and caused a Gemini regression on 2026-04-30 when applied more broadly).

And the *original* draft of this ADR made three things *worse* rather than neutral:

- **Per-model certification exclusions** (the muse-spark-1.1 implicit-cache exclusion landed in `#474` as one worked example) live in shared test-body code that the original draft moved into the engine wheel as `tolokaforge.testing.certify.suite`. A per-model exclusion becomes an engine-wheel edit.
- **New `Capability` values ship together with their probe body** (`8c8f88b` — three values, three probe files landed together). Under the original draft the enum lives in the models wheel and probe bodies in the engine wheel — two releases for one addition.
- **Per-model cert bodies** (`test_nova_api.py`, `test_gemini_placeholder_signature_replay.py`, `#846`'s codec unit test) go into the engine wheel and lose the models cadence.

This widened ADR states the properties the seam must satisfy — verbatim from bberkes's review — and proposes mechanisms that meet them.

## Decision Drivers

- **Decouple release cadence from model onboarding.** Engine version stops moving when a known-shape model adaptation ships. The realistic target from bberkes's history is ~1 integration in 20 needing an engine release, not 3 in 5.
- **One publisher, one package.** No plugin ecosystem. Squatting risk on `tolokaforge-model-*` names, external-shadow attack surface, and cross-package collision governance are all traded away for maintenance simplicity. If a plugin-ecosystem case ever materialises, it is a superseding ADR, not a Layer-2 addition here.
- **Preserve the [ADR-0025](0025-runner-wheel-split.md) "one PyPI wheel for engine code" clause** without extending it to data or model-specific subclasses. Model data is a distinct category: it changes on its own cadence for its own reasons; its PyPI presence adds no code-level compat surface.
- **Certification extraction is load-bearing.** The one change that unblocks the rest is moving the certification harness out of the engine repo's test suite into a public library that both in-tree and out-of-tree CI can invoke.
- **Extension points must accept enough context to express the adaptation.** A slot that can only answer questions rather than reshape the payload pushes the reshaping into the client, where it cannot be extended out-of-tree at all. `ToolContentPolicy` today only answers `format()` / `supports_images()` / `inject_empty_assistant_filler()`, and Anthropic's multimodal handling lives in `client.py` as a provider branch instead of in a policy — a mistake this ADR does not repeat.
- **Per-model adaptation never edits shared or base implementation.** If expressing a delta means adding a flag or a branch to a parent, the boundary has leaked. Hooks land with the subclass that needs them, in the same PR that adds the subclass.
- **Public API for helpers.** Everything a per-model class needs is documented public API with a compat guarantee, not module-private symbols.
- **Provider and transport binding is data, not a client branch.** Endpoint URL, credential env-var name, rate-limit classification, rotation env-var name, and routability all belong on a provider record in the models wheel — not in `client.py` or `proxy.py`.
- **Forward-compat toward a repo split.** The `tolokaforge_models/` tree is chosen to be a drop-in future repo: one `git filter-repo --subdirectory-filter tolokaforge_models` and it is a standalone project.
- **Fail-loud version compat, at import time.** `__api_version__` integer on the models package + `minimum_engine_version` on the models package + class-name-existence check at engine import. All three fail before any run starts, not at `LLMClient` construction after an evaluation has begun spending.

## What "success" looks like

The measurable target is sharper than *"fewer engine releases"* — it is **"one engine version absorbs any number of model integrations"** (added 2026-08-07 per follow-up review). A lineup refresh onboarding twenty new models onto engine 0.16 should still leave you on engine 0.16 when it is done. That is what makes § Context's comparability argument actually hold (*"which engine version did this run on"* stops being a per-model question), and it is what lets the auto-integration pipeline take a model end-to-end with no human in the release path. Read that way, the useful question is not what fraction of individual integrations need an engine release, but whether the engine version can stay put across a whole lineup refresh.

Progress against this target is measured continuously by [`tests/canonical/test_models_wheel_replay.py`](../../tests/canonical/test_models_wheel_replay.py), which replays every auto-integration commit (`^integrate: <slug> (#N)`) reachable from `main` and classifies each as Bucket A (data + certs + models-wheel content only) or Bucket B (engine-side change). The initial baseline at [#932](https://github.com/Toloka/tolokaforge/issues/932) landing time is **3 Bucket A of 7 integrations**. **Any drift** — a new integration landing, a bucket classification changing, a touched-file set changing — fails the canonical snapshot equality check and requires an explicit reviewer `--update-canon` regen in the PR that occasioned it. Improvements (a new Bucket A pushing the count up) and regressions (a new Bucket B, or a bucket flip on an existing entry) both surface here; the difference is whether the regen is celebrated or debated in the reviewing PR. The `/implement-milestone` post-merge validation therefore forces every metric-affecting change through review — the mechanism is snapshot equality, not one-sided monotonicity. The § Consequences → Known ceiling section names the specific engine shape (bases as single traversals with flags bolted on) that keeps the target out of reach; the replay is what will show when that ceiling starts to bind.

## Considered Options

**Wheel shape — where does model data live?**

1. **Status quo.** Data ships in the engine wheel. Rejected — this is the problem.
2. **Overlay-only (ADR-0002 as shipped).** `--presets-file` is the whole answer. Rejected as a stopping point — an overlay is a loose file path each operator carries by hand; nothing bundles preset routing + pricing + certificate into a single installable, versioned artifact.
3. **Entry-point plugin ecosystem (ADR-0002 Option 3, multi-publisher).** Anyone can publish a `tolokaforge-model-*` package. Rejected for now — squatting risk, governance surface, and the silent-last-wins hazard already open against the adapter registry ([GH #544](https://github.com/Toloka/tolokaforge/issues/544)). The plugin-ecosystem case is not blocked by this ADR — a future ADR can layer it on top of the mechanism this one lands.
4. **One PyPI wheel + subset build target, Docker-only (mirror ADR-0025).** Rejected — leaves data movement gated on engine releases.
5. **Single Toloka-published `tolokaforge-models` wheel from the same monorepo, data only.** The original draft of this ADR. Now superseded by Option 6 below.
6. **Single Toloka-published `tolokaforge-models` wheel from the same monorepo, widened Bucket A.** This ADR's decision. Bundles preset routing, pricing, provider bindings, capability certificates (with per-model exclusion data and probe params), per-model policy subclasses of stable engine bases, per-model certification bodies, and the vendor-inference table. Independent semver; `__api_version__` and `minimum_engine_version` guardrails.
7. **Date-based versioning for `tolokaforge-models` (2026.08.06).** Rejected — loses the semver-breakage signal.

**Certification harness — where does it live?**

1. **Stays where it was** (`tests/integration/llm/registry.py`, the pre-split location). Rejected — this was the load-bearing coupling.
2. **Move to a separate repo.** Rejected — re-creates the coordination problem elsewhere.
3. **Extract into a public `tolokaforge.testing.certify` seam inside the engine wheel, with certificate-borne exclusion data and out-of-tree probe registration.** Selected. Shared test bodies remain engine code (the engine defines what "supported" means on the wire), but exclusion sets, probe parameters, and per-model bodies are certificate data and models-wheel content respectively.

**Policy / codec / sanitizer classes — bucket boundary (widening, per 2026-08-07 review)**

1. **All engine-side, forever (ADR-0002 § Considered Options Option 4 deferred).** Rejected — the deferral condition ("custom policy classes recurring per model") is now visibly met.
2. **All out-of-tree via a sidecar Python module (ADR-0002 Option 4 as originally stated).** Rejected — indiscriminate. Some classes are legitimately engine work (a genuinely new lifecycle stage; a new capability category) and should not be shippable out-of-tree.
3. **Selective adoption of ADR-0002 Option 4 — per-model subclasses of stable bases that reach only public API move to `tolokaforge_models/policies/`; genuinely new base classes / new lifecycle stages / new capability categories stay engine code (Bucket B).** Selected. The selection criterion is testable: if a subclass inherits from a stable base and reaches only public API, it moves; if it needs a not-yet-added base hook or a private helper, the hook lands / the helper is promoted to public API in the same engine PR, and then the subclass moves.

## Decision

Adopt **Wheel-shape Option 6**, **Certification-harness Option 3**, and **Policy-classes Option 3**.

### Two PyPI distributions, one monorepo

The published PyPI surface becomes two independently-versioned wheels, both built from `Toloka/tolokaforge`:

- `tolokaforge` — the engine wheel. Contains everything it does today *except* the `tolokaforge/core/data/` payload, per-model policy subclasses, per-model certification bodies, and the vendor-inference table. Declares `tolokaforge-models >= 1.0.0` as a runtime dependency.
- `tolokaforge-models` — the new model-data wheel. Contains preset routing (`model_presets.yaml`), pricing (`pricing.json`), provider bindings (`providers.yaml`, new), vendor-inference table (`vendor_prefixes.yaml`, new), the capability-certificate registry (`Capability` enum, `ModelCertificate` dataclass with `excluded_capabilities` / `known_unsupported_reasons` / `probe_params` fields, and `ALL_MODELS`), per-model policy subclasses (`tolokaforge_models/policies/`), and per-model certification test bodies (`tolokaforge_models/tests/`). Declares `__api_version__: int = 1` and `minimum_engine_version: str` (PEP 440 spec) at module level.

This ADR reaffirms [ADR-0025 § Decision Drivers](0025-runner-wheel-split.md#decision-drivers) *"one PyPI wheel"* clause **for engine code**. The runner-subset (`tolokaforge-runner-subset`) remains Docker-only.

## Requirements the seam must satisfy

Adopted verbatim from the [2026-08-07 review comment](https://github.com/Toloka/tolokaforge/issues/645#issuecomment-5213977204). These are the design principles; the mechanisms below are proposed answers that meet them. If a mechanism turns out to under-serve a requirement, the mechanism changes and the requirement stands.

1. **Every lifecycle stage a model may need to adapt has an extension point.** Request params, tool/schema shaping, system prompt, tool-result content, assistant text, tool-call arguments, reasoning, caching.
2. **An extension point receives enough context to express the adaptation**, not a pre-digested slice.
3. **Adding a per-model adaptation never requires editing shared or base implementation.** If it does, add the hook first, then the adaptation.
4. **Everything a per-model adaptation needs is public, documented API with a compatibility guarantee** — including the helpers that currently back the shipped recovery classes.
5. **Existing recovery behaviour is reusable without inheriting engine-internal classes.** Inherit-and-delegate stays the pattern; from-scratch reimplementation is forbidden.
6. **Generation parameters are extensible without an engine signature change.** `_VALID_PARAMS_KEYS` derived from `inspect.signature(GenerationParams.__init__)` goes away; params flow through a `ParamsPolicy` slot with an `extras` bag.
7. **Per-model certification exclusions are certificate data, not code in a shared test body.**
8. **A new capability and its probe body can be declared out-of-tree.** The `Capability` enum lives in the models wheel; probe bodies register via a decorator.
9. **Provider and transport binding is data, not a client branch.** Endpoint, credential name, routability, rate-limit patterns, rotation env-var — all declarable per provider.
10. **A policy can be configured, not only selected.** Slot values are `{name, params}`, not bare `name`.
11. **Per-model certification bodies can live out-of-tree.**
12. **Token and cost accounting categories are extensible.** `Usage` gains an `extras: dict[str, int]` bag for new tier categories.

## The one seam the engine uses to reach model data

The engine reaches `tolokaforge_models` through exactly one internal module: [`tolokaforge/core/model_data.py`](../../tolokaforge/core/model_data.py) (to be added). Public API:

```python
def bundled_presets_path() -> Path: ...
def bundled_pricing_path() -> Path: ...
def bundled_providers_path() -> Path: ...              # NEW (widening)
def bundled_vendor_prefixes_path() -> Path: ...        # NEW (widening)
def bundled_certificates() -> list[ModelCertificate]: ...   # NEW (widening)
def declared_api_version() -> int: ...
def declared_minimum_engine_version() -> str: ...      # NEW (widening)
def load_policy_registrations() -> dict[str, dict[str, type]]: ...  # NEW (widening) — entry-point discovery
```

Every function does `from tolokaforge_models import ...` internally today. If a future superseding ADR moves to a plugin ecosystem, the internals change (entry-point discovery from multiple registered publishers); every caller in the engine is untouched. Nothing else in the engine's code paths reaches into `tolokaforge_models` outside this module. The seam is the whole coupling.

## Extension-point surface (new — widening)

`_POLICY_REGISTRIES` has nine slots: `schema_sanitizer`, `prompt_policy`, `content_policy`, `response_policy`, `reasoning_codec`, `cache_policy`, `params_policy`, `message_assembly_policy`, `assistant_text_policy`. The last three landed under [#934](https://github.com/Toloka/tolokaforge/issues/934):

- **`assistant_text_policy` (NEW).** Base class `AssistantTextPolicy` with signature `parse_assistant_text(text: str, *, model_config: ModelConfig) -> str`. This slot is not a refactor of shipped code — it is invented because a model showed up that no existing slot fits. The open Cohere integration (#929) is blocked on this exact gap: response text arrives wrapped in `<|START_TEXT|>…<|END_TEXT|>` markers, `ResponsePolicy` only post-processes tool-call arguments, and there is no hook that reaches assistant text. Under the widened ADR, a `CohereMarkerAssistantText` subclass in `tolokaforge_models/policies/` unblocks the integration without any engine release beyond the one landing the slot itself.
- **`params_policy` (NEW).** Base class `ParamsPolicy` with signature `build_params(overrides: dict[str, Any]) -> GenerationParams`. Replaces the hard-coded `_VALID_PARAMS_KEYS = inspect.signature(GenerationParams.__init__)` mechanism. `_RECOGNISED_OVERRIDE_KEYS` at `presets.py` goes away.
- **`message_assembly_policy` (NEW — narrow first, general later).** The conversation the engine assembles before dispatch has no extension point today (see § Evidence widening the scope, failure mode 3). Two versions are worth distinguishing:
  - **Narrow version (this ADR).** The `inject_empty_assistant_filler` flag stays, but the *filler string* — hard-coded at `client.py:1213` — becomes policy data alongside the flag. Removes a model-specific string from the engine, needs no new slot type, and directly closes the Gemini regression recorded on 2026-04-30.
  - **General version (deferred, gated on a real second use case).** A slot receiving the assembled message list before send. Powerful — a bad rewrite produces wrong evaluation results rather than an error — so it lands paired with a mandatory invariant check on the way out (roles valid, tool-call ids still paired, nothing dropped), and the narrow version's filler is its first user. Deferred because it is only worth the risk once a second lifecycle case appears that the narrow shape cannot cover.

**Slot values become `{name, params}`.** `_POLICY_REGISTRIES` slot values today are bare `name` strings. Widened shape: `{name: str, params: dict[str, Any]}` — the dict is passed to the class constructor. Overlay validator extends to fail-loud on unknown keys nested inside a preset block (today only top-level unknowns fail). The Gemini `gemini_drop_placeholder_signature` special case at `presets.py:609-620` becomes ordinary params flow.

**`GenerationParams.extras: dict[str, Any]`.** Bag for provider-specific knobs the base class does not know about. Read by the provider-specific `ParamsPolicy` subclass; ignored by policies that do not read from it.

**`Usage.extras: dict[str, int]`.** Bag for cost / token categories beyond the fixed `{input, output, cache_read, cache_write}`. `_compute_cost` extends to consume it when a pricing entry declares an `extras` sub-table.

**Base-class hook policy.** Any per-model subclass that today requires a base-class hook lands the hook in the same PR that adds the subclass. The hook is engine work (small); the subclass is models work. The PR crosses both wheels but only one release event on each side. Reviewer checks: the hook has a default value that preserves current behaviour; the base-class shape does not change (no new abstract method, no new required kwarg).

**Public API promotion for helpers.** `_coerce_json_strings`, `_coerce_empty_containers`, and every module-private helper reached by a shipped per-model subclass becomes documented public API with compat guarantee. Existing per-model classes stay engine-side across the promotion PR; the move-out happens once the API is public.

## Provider bindings as data (new — widening)

New file `tolokaforge_models/data/providers.yaml`. Per provider record:

```yaml
openrouter:
  endpoint: https://openrouter.ai/api/v1
  api_key_env: OPENROUTER_API_KEY
  api_keys_env: OPENROUTER_API_KEYS       # for rotation
  unroutable: false
  rate_limit_patterns:
    - "rate limit exceeded"
    - '"code":429'
    - '"code":403 budget exceeded'
  slug_rewrite: null
nova:
  endpoint: https://api.nova.example.com/v1
  api_key_env: NOVA_API_KEY
  api_keys_env: null
  unroutable: true                           # replaces UNROUTABLE_PROVIDERS
  rate_limit_patterns: [...]
  slug_rewrite:                              # two-step, not a single template
    strip_prefix: "nova/"                    # strip if present  (client.py:1914)
    ensure_prefix: "openai/"                 # then ensure       (client.py:1917)
  custom_llm_provider: "openai"              # kwargs override   (client.py:1912)
  base_url_configure_hook: "nova"            # opts-in to legacy _configure_nova_base_url path
```

Consumed at `LLMClient` construction. Concrete moves (Nova is three sites, not one; the ADR schema needs to name each explicitly):

- `_configure_nova_base_url` at `client.py:688` becomes a lookup: any provider with `base_url_configure_hook: nova` gets the same treatment; the branch itself moves to a generic helper indexed by that hook name.
- `_format_model_name` at `client.py:938` (returns the bare name for Nova) becomes a lookup on `providers[provider].slug_rewrite`.
- The `LLMClient._prepare_request` kwargs block that hardcodes Nova at `client.py:1901-1917` (strip `nova/`, ensure `openai/`, set `custom_llm_provider="openai"`) becomes: `providers[provider].slug_rewrite` applied per the two-step recipe, `custom_llm_provider` set from the record if non-null.
- The generic default at `client.py:1734` (`self.provider.split("/")[0]`) stays engine code — it's the fallback when the record has no override.
- `UNROUTABLE_PROVIDERS = frozenset({"mock", "nova"})` at `proxy.py:145` becomes a lookup: `providers[provider].unroutable`.
- `_is_rate_limit_exception`'s hard-coded regexes become data: `providers[provider].rate_limit_patterns`.
- Multi-key rotation at `client.py:731` reads `providers[provider].api_keys_env` instead of the hardcoded `OPENROUTER_API_KEYS`. A second provider needing rotation is now a data change, not a client edit.

`normalize_model_name`'s `startswith` chain in `pricing.py:280-341` (`kimi*` → `moonshot/`, etc.) moves to `tolokaforge_models/data/vendor_prefixes.yaml`. It has already drifted on `main` (`kimi*` → `moonshot/` while the data has `moonshotai/`; six vendors have no branch). Vendor inference travels with the table it serves.

The engine ships default bindings that reproduce today's behaviour for the shipped providers, so this can land before the models-wheel cutover. Once the models wheel exists, its `providers.yaml` is the authoritative source.

## Certification as a public engine seam

`tolokaforge.testing.certify` becomes a first-class engine surface:

- `Capability` enum stays **engine-authoritative** and lives in `tolokaforge/testing/certify/`. Rationale (revised 2026-08-07 per follow-up review): the shared suite references enum members by attribute (`Capability.BASIC_COMPLETION`), so a rename or drop from the models side would break engine code silently through attribute access — and attribute access has no registry the class-name-existence guard could check against. Keeping the enum engine-owned costs an engine release when a genuinely new *category* is coined (rare — the same event a new lifecycle stage or new probe pattern would trigger), and buys silent-break protection for the common case. Requirement 8's decoupling is delivered instead via a certificate-borne `capability_extras` field (below).
- `ModelCertificate` dataclass lives in `tolokaforge_models.certificates`; gains four new fields (widening):
  - `excluded_capabilities: frozenset[Capability]` — replaces engine-side hardcoded per-model exclusion sets in shared bodies. Shared test bodies check `cert.excluded_capabilities` instead of a hardcoded name set.
  - `known_unsupported_reasons: dict[Capability, str]` — human-readable explanation surfaced in reports.
  - `probe_params: dict[Capability, dict[str, Any]]` — per-model tuning for probe bodies (e.g. reduced token budgets, alternate prompts).
  - `capability_extras: dict[str, str]` (NEW — revised 2026-08-07) — free-form capability names the models wheel declares without adding to the engine's `Capability` enum. Used when a per-model quirk needs to be surfaced in the certificate but does not correspond to a shipped shared probe. Read by consumers (report generation, dashboards) as opaque strings; no enum-attribute access, so no silent-break class.
- `live_client`, `skip_unless_capability_declared` pytest fixtures — live at `tolokaforge/testing/certify/fixtures.py`; the `TF_PRESETS_FILE` env-var overlay hook is preserved in the suite conftest.
- `tolokaforge.testing.certify.suite` — the ~30 `test_*.py` bodies, parametrised on `ALL_MODELS` supplied via fixture. Test bodies read exclusion data from certificates and probe params from `cert.probe_params`; no more hardcoded per-model sets.
- `tolokaforge.testing.certify.probes` (NEW) — `@register_probe(Capability.X)` decorator collects per-capability probe bodies from `tolokaforge_models/tests/`. Engine's shared suite dispatches to the registered probe. A genuinely new `Capability` value (rare) is Bucket B and lands in the same engine release that adds the probe pattern; per-model *tuning* of an existing capability's probe travels through `cert.probe_params` on the models cadence.

Per-model test bodies (`test_nova_api.py`, `test_gemini_placeholder_signature_replay.py`, `#846`'s codec unit test) → `tolokaforge_models/tests/`. Collected via `pytest --pyargs tolokaforge_models.tests` from the models CI. Same fixtures, same overlay hooks.

The parametrised suite lives under `tolokaforge/testing/certify/suite/`; `tests/integration/llm/` retains only the per-model bodies (`test_nova_api.py`, `test_gemini_placeholder_signature_replay.py`) and the one-off live gateway test. After the cutover, `tolokaforge-models` CI is the source of truth for the real registry.

## Install-time validation (new — widening)

Three checks all fail before any run starts:

- **`minimum_engine_version` on the models wheel — deliberately a runtime check, not `Requires-Dist`.** Engine reads `tolokaforge_models.minimum_engine_version` and refuses to load a models wheel that names a floor higher than the installed engine. Message: `tolokaforge-models X.Y.Z requires tolokaforge >= A.B; installed A'.B'. Upgrade the engine or downgrade the models wheel.` **Kept off PEP 440 metadata by design.** A pair like *"engine 0.16 needs models ≥ 1.0"* + *"models 1.4 needs engine ≥ 0.17"* declared as `Requires-Dist` on both sides is mutually referential and unsolvable for the pip resolver — the user sees an opaque backtracking failure instead of a clear "upgrade the engine" message. Runtime keeps the message legible and the resolver unconstrained. This is intentional; do not "tidy" it into a real dependency later.
- **`__api_version__` integer.** Unchanged semantics from the original draft — the loader-contract compat guardrail.
- **Class-name-existence, at the earliest point that does not invert the safe import order.** A models wheel referencing a policy class (e.g. `GeminiRecursiveSchema`) that the merged `_POLICY_REGISTRIES` (engine defaults + models registrations) does not know must fail loud, before an evaluation has begun spending. Placement: **not** in `tolokaforge/__init__.py` — that file is a 67-line lazy `__getattr__` shim and the LLM layer + models package are not yet loaded from there, so the check would either force premature imports or itself no-op. Correct placement is **immediately after `presets.py`'s top-of-module imports (lines 21-63)**, once the base classes are resolvable from `sys.modules`, OR in `prepare` alongside `write_engine_run_state` for a run-start gate. Both are early enough to catch a bad models-wheel install before any spending starts, without breaking the import order.

**Overlay validator extension — allow-list is engine keys ∪ models-declared keys.** `_check_block` at `presets.py:302` today validates slot names against the registries and `params:` keys against `_VALID_PARAMS_KEYS`; anything else inside a preset block silently no-ops. Widening: nested unknown keys fail loud with the offending file, key, and closest schema match. **Symmetric with today's top-level check, but scoped carefully:** the legal key set is engine-known keys (`_POLICY_REGISTRIES` slot names + `{name, params}` + capability fields on `ModelCertificate`) **union** the keys the installed models wheel declares in its own registration manifest (e.g. `models_extra_keys: [gemini_drop_placeholder_signature, api_call_timeout_s]`). This lands strictness on typos, not on version skew — a models-wheel-declared knob that the installed engine happens not to consume yet stays silent (as today), but a genuine typo (`gemini_drop_placeholder_signaure` — missing `t`) fails loud.

## Downstream data-resource consumers (new — widening, revised 2026-08-07)

The original widening draft proposed a build-time forwarding stub in `tolokaforge/core/data/` for one release cycle plus a `DeprecationWarning` on access. The follow-up review found both halves of that broken and it is dropped:

- **The `DeprecationWarning` has no delivery path.** The known consumer (`benchmark-results-collector`) reads bytes via `importlib.resources.files("tolokaforge").joinpath("core/data/pricing.json")`. That does run `tolokaforge/__init__.py`, but nothing under `core/` is imported (`core/data/` is not even a package), so the read is indistinguishable from any other engine import. A warning at package init fires for everyone and names no one.
- **A build-time stub goes stale silently.** It is generated when the engine is released; `tolokaforge-models` then ships on its own cadence. From the first independent models release onward the stub serves prices that are internally consistent and *wrong* — worse than today's failure mode, which is loud (`load_pricing_table` logs `engine_pricing_missing` and returns `{}`, so costs read as zero and are noticeable). Plausible-but-stale prices in a leaderboard cost column are not.

**Selected path instead — fix the consumer first, then move with no stub:**

1. **Pre-move (before the cutover PR).** `benchmark-results-collector` and any other resource-path reader migrates to `tolokaforge.core.model_data.bundled_pricing_path()` (and the sibling accessors for the other data files). Consumer fails loud on an empty table instead of returning `{}` — a silent cost-zero regression should surface as an obvious error, not a silent leaderboard artefact.
2. **Prerequisite sweep.** Before the cutover, grep for `importlib.resources.files("tolokaforge")` and `importlib.resources.files\("tolokaforge/core/data"` across public callers. `tolokaforge/core/_runner_subset.py` documents `importlib.resources.files("tolokaforge")` as a supported lookup across both wheel variants, so the contract is broader than one consumer — the sweep is not optional.
3. **Move (cutover PR).** The two data files move to `tolokaforge_models/data/` with no forwarding stub. Any resource-path reader that missed migration in step 1 fails loud on first access (the file is not there), which is the right failure mode.

Migration note in [`docs/RELEASING.md`](../RELEASING.md) + release notes on the first models-wheel-cutover engine release. @bberkes-toloka has committed to landing the collector-side migration ahead of the cutover.

## Fingerprinting for auditability

[`tolokaforge/core/engine_run_state.py`](../../tolokaforge/core/engine_run_state.py) — `write_engine_run_state` carries a `models_fingerprint` field:

```jsonc
{
  "run_id": "...",
  "presets_file": "/path/or/null",
  "models_fingerprint": {
    "package_version": "1.4.2",              // tolokaforge_models.__version__ (or "in-tree" pre-cutover)
    "content_sha256": "...",                 // sha256 over presets + pricing + providers + vendor_prefixes + certificates + policy registrations (post-overlay)
    "api_version": 1,
    "minimum_engine_version": ">=0.16,<0.17"
  }
}
```

Any completed run can be reconstructed: reinstall the named `tolokaforge-models` version, apply the same overlay, get byte-identical model resolution. ADR-0002 § Follow-ups called for a fingerprint round-trip through an overlay — this delivers it, over the widened data surface.

**Pre-cutover sentinel.** While the model data still ships in the engine wheel, `package_version` is the literal string `"in-tree"` and `content_sha256` is computed over the subset already resolvable from `tolokaforge.core.model_data` (`model_presets.yaml`, `pricing.json`, and the certificate registry). `minimum_engine_version` is `">=0.16,<0.17"` — the engine floor the current bundle is compatible with (0.16 is the first release carrying #931's widened `ModelCertificate`, which the certificate portion of the hash depends on). The cutover PR (#938 in this ADR's follow-ups) flips the three constants' source from `tolokaforge.core.model_data` to `tolokaforge_models.__init__` and widens the hashed payload to include `providers.yaml` + `vendor_prefixes.yaml`; the field shape and reader API stay unchanged.

## Independent versioning

`tolokaforge_models/pyproject.toml` carries its own `[project] version`, starting at `1.0.0`. A new workflow (`release-models.yml`) runs `cz bump` scoped to `tolokaforge_models/`, tags `models-vX.Y.Z`, and triggers a companion `publish-tolokaforge-models.yml` that runs `hatch build --target models-subset` and `uv publish` with trusted publishing. Engine's existing `release.yml` + `publish-tolokaforge.yml` are untouched.

Why integer `__api_version__` rather than PEP 440 range on `Requires-Dist`? PEP 440 couples wire compatibility to marketing version strings. Bumping `tolokaforge-models` 1.4 → 1.5 with pure data / new-subclass changes stays `__api_version__ = 1` — no engine re-release needed. Only a real interface break (change to the loader contract) bumps `__api_version__`, and that happens only with an engine change anyway. `minimum_engine_version` (widening) covers the orthogonal axis: models-wheel changes that require newer engine features (e.g., a new policy slot) declare a floor and refuse to load older engines.

## The monorepo layout — chosen to be a drop-in future repo

```
public-tolokaforge/
├── tolokaforge/                            # engine package
│   ├── core/
│   │   ├── llm/
│   │   │   ├── presets.py                 # loader + base-class registries
│   │   │   ├── schema_sanitizer.py        # base classes only; per-model subclasses moved
│   │   │   ├── prompt_policy.py           # base classes only
│   │   │   ├── response_policy.py         # base classes only
│   │   │   ├── reasoning_codec.py         # base classes only
│   │   │   ├── content_policy.py          # base classes only
│   │   │   ├── cache_policy.py            # base classes only
│   │   │   ├── assistant_text_policy.py   # NEW base class (widening)
│   │   │   ├── params_policy.py           # NEW base class (widening — was private)
│   │   │   └── _helpers.py                # NEW — public API for what were _coerce_* privates
│   │   ├── pricing.py                     # loader; consumes vendor_prefixes + providers via seam
│   │   ├── model_data.py                  # NEW — the one seam
│   │   ├── engine_run_state.py            # +models_fingerprint field
│   │   └── data/                          # forwarding stubs for one release, then DELETED
│   └── testing/certify/                   # public certification seam
│       ├── __init__.py                    # re-exports types, fixtures, probe registration
│       ├── fixtures.py                    # live_client, skip_unless_capability_declared
│       ├── suite.py                       # parametrised bodies; reads exclusions from certificates
│       └── probes.py                      # NEW — probe-registration decorator API
├── tolokaforge_models/                     # NEW top-level package → tolokaforge-models wheel
│   ├── __init__.py                        # __version__, __api_version__, minimum_engine_version
│   ├── pyproject.toml                     # independent versioning
│   ├── data/
│   │   ├── pricing.json                   # moved
│   │   ├── model_presets.yaml             # moved
│   │   ├── providers.yaml                 # NEW — provider bindings as data
│   │   └── vendor_prefixes.yaml           # NEW — normalize_model_name inference table
│   ├── policies/                          # NEW — per-model subclasses
│   │   ├── __init__.py                    # entry-point-exposed registrations
│   │   ├── gemini.py                      # e.g. GeminiRecursiveSchema
│   │   ├── minimax.py
│   │   ├── inkling.py
│   │   ├── deepseek.py
│   │   ├── cohere.py                      # e.g. CohereMarkerAssistantText
│   │   └── ...
│   ├── certificates/
│   │   ├── __init__.py                    # exposes ALL_MODELS
│   │   ├── _capability.py                 # Capability enum + widened ModelCertificate
│   │   └── registry.py                    # ALL_MODELS with excluded_capabilities + probe_params
│   └── tests/                             # per-model bodies (pytest-pyargs collection)
│       ├── test_nova_api.py
│       ├── test_gemini_placeholder_signature_replay.py
│       └── ...
└── scripts/hatch/hatch_models_subset_builder.py    # NEW — custom builder
```

If a future ADR chooses to split `tolokaforge_models/` into its own repo, the cost is bounded: one `git filter-repo --subdirectory-filter tolokaforge_models`, move the hatch custom builder to the new repo (converting to a standard hatch wheel target), move the two publish workflows, remove the models hatch target block from the engine `pyproject.toml`. Zero changes to any caller of `tolokaforge.core.model_data.*` or `tolokaforge.testing.certify.*`. Zero changes to `presets.py`, `pricing.py`, `client.py`, `proxy.py`.

**Caveat — moving per-model subclasses out is a public-API deletion.** Every class-adding integration to date also added the new subclass to `tolokaforge/core/llm/__init__.py`'s `__all__` — #560 added 6 names, #719 added 2, #846 added 2. Callers doing `from tolokaforge.core.llm import GeminiRecursiveSchema` will break when those classes move to `tolokaforge_models.policies`. The cutover PR must (a) enumerate every currently-exported per-model class name, (b) either re-export the moved subclasses from `tolokaforge.core.llm.__init__` for one release with a `DeprecationWarning` (analogous to the `_capability.py`/`ModelCertificate` re-export in the certification seam), or (c) document the break in release notes with the exact import-path change. The "zero changes to any caller" claim above holds for callers of the seam surfaces (`model_data.*`, `testing.certify.*`); it does *not* hold for callers who import per-model policy classes directly by name.

## Runner-subset interaction

The runner-subset wheel today explicitly includes `pricing.json` and `model_presets.yaml` via [`tolokaforge/core/_runner_subset.py:97,102`](../../tolokaforge/core/_runner_subset.py) (the [GH #830](https://github.com/Toloka/tolokaforge/issues/830) fix). After this ADR:

- `tolokaforge-runner-subset` gains `tolokaforge-models >= 1.0.0` as a pip dependency.
- The two data-file entries move out of `_runner_subset.py` and into a new `tolokaforge/core/_models_subset.py` partition file.
- [`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) `pip install`s the runner-subset wheel as today; `tolokaforge-models` is transitively resolved.
- The `MODEL_PRICING` populated-assertion at [`tests/canonical/test_runner_subset_install_smoke.py:472-497`](../../tests/canonical/test_runner_subset_install_smoke.py) still passes via the transitive dep. Positive-imports list gains `tolokaforge_models.data`.

The [ADR-0025](0025-runner-wheel-split.md) container command surface + Docker image name/tag axis are unchanged.

## Docs flip

[`docs/ADD_NEW_MODEL.md`](../ADD_NEW_MODEL.md) — the pre-flight table today routes `pricing.json`, `model_presets.yaml`, and `registry.py` all to `main` in `tolokaforge`. Post-this-ADR, out-of-tree becomes the documented default; the table's Branch column becomes `tolokaforge_models/` for the data artifacts *and* the per-model policy subclasses.

A **Bucket A vs. B** pre-flight decision block leads the doc:

- **Bucket A — target `tolokaforge_models/`.** Preset routing composes existing policy classes; adjustments to price data; new capability certificate; per-model subclass of a stable base that reaches only public API; a new provider binding entry; a per-model certification body. Cadence: independent.
- **Bucket B — target `tolokaforge/`.** Genuinely new base class or new lifecycle stage (e.g. a new policy slot type). A new capability *category* whose probe pattern is unlike any shipped probe. A change to an existing base class's *shape* (not just adding a hook). Cadence: engine.

The overlay section (`--presets-file`) stays as the documented private / experimental escape hatch — not deprecated.

## Auto-integration workflow retargeting

[`.github/workflows/integrate-model.yml`](../../.github/workflows/integrate-model.yml) today commits back to the engine branch. Post-this-ADR, the workflow classifies the candidate:

- **Bucket A** → commit to `tolokaforge_models/` (data, subclasses, certificate, tests).
- **Bucket B** → engine PR path, unchanged.

The classification step inspects the resolved policy graph: if every needed policy class exists in the engine's base classes (with or without a new subclass), Bucket A; if a new base class / slot / probe pattern is required, Bucket B. Deliberately the *last* implementation step — the manual `tolokaforge_models/` path is proven end-to-end first.

## Promoting ADR-0002 to Accepted

[ADR-0002](0002-external-model-registry.md) is promoted from `Proposed` to `Accepted` in the same PR. Its Option 2 has been shipping and load-bearing since 2026-06-17. The "advanced by ADR-0030" back-link is added to ADR-0002's front matter alongside the status flip. The widening explicitly reverses ADR-0002's own deferral of its Option 4 — the eval-data condition for that reversal is now met.

## Consequences

### Positive

- **Engine version stops moving on model onboarding for known-shape adaptations.** Realistic target from bberkes's history: ~1 in 20 integrations needs an engine release. An engine release means someone found a genuinely new kind of wire behaviour, not a known-category adaptation.
- **Bucket A cycle time collapses across the widened surface.** Not just data changes — new per-model subclasses ship without an engine release too. Cohere marker handling, Nova's transport rewrites, new provider registrations, per-model cert exclusions all move to the models cadence.
- **The seam is versioned and pinnable.** Consumers pin both wheels for reproducibility or let `tolokaforge-models` float for latest models.
- **Certification is a first-class public seam** with data-driven exclusions and probe registration.
- **Extension points cover every lifecycle stage that history shows models needing to adapt.** The `if target == "anthropic"`-shaped branches in `client.py` stop growing.
- **Provider bindings become data.** A new endpoint, credential-name, rate-limit rule, or rotation strategy is a `providers.yaml` edit, not a client PR.
- **Overlay stays intact.** `--presets-file` still works as the private escape hatch.
- **Forward-compat is cheap.** Future repo split is bounded and mechanical.
- **Preserves ADR-0025's clause for engine code.** Runner-subset stays Docker-only.

### Negative / Trade-offs

- **Two publish workflows.** Discipline cost.
- **Two-package install surface.** `pip install tolokaforge` transitively brings `tolokaforge-models`; the engine is not functional without it.
- **Larger cutover PR.** The cutover crosses more surface than the original narrow ADR: per-model subclasses move, provider bindings move, cert data-shape changes. Mitigated by the landing order — small enabling PRs first (public helper promotion, base-class hooks, new slots, provider-bindings-as-data), then the cutover.
- **Central-publisher governance concentrates on Toloka.** External contributors PR against `Toloka/tolokaforge` targeting `tolokaforge_models/`. Cycle-time improvement over today is real; still no path for a third-party publisher.
- **PyPI trusted-publisher setup required** for `tolokaforge-models`.
- **`__api_version__` still depends on discipline.** Catches loader-contract breakage but not e.g. a policy-class rename that leaves the loader contract intact yet drops presets. Mitigation: the certification suite runs against every registered model on every `tolokaforge-models` release. Silent-preset-drop surfaces as a failed certificate before publish.
- **Capability enum stays engine-authoritative** (revised from the initial widening draft). A genuinely new *category* still requires an engine release; per-model quirks that need to be surfaced but do not map to a shipped shared probe travel through `cert.capability_extras` as opaque strings. Trade-off: an engine release for the rare "new category" event, in exchange for silent-break protection on the common attribute-access path.
- **Base-class hooks land across two wheels in one PR.** The engine PR + models PR sequence is a small workflow discipline cost. Reviewer question: workable, or two-release protocol needed?
- **Known ceiling — bases as single traversals with flags bolted on.** Applied to the § Evidence table, the widened ADR still routes 1 in 7 integrations to engine (specifically gemini-3.5-flash / #560, which needed `StrictSchema.carry_scalar_dict_map_value` — one boolean at `schema_sanitizer.py:214` consulted at exactly one site inside a 630-line class). That is well below today's 3-in-5, but above the 1-in-20 target. The reason bounds this ADR: bases expressed as ordered named steps, with sequence and parameters coming from data, would turn most "new hook" cases into rearrangements. That refactor is bigger than this ADR should absorb and is called out here as the ceiling this design shape reaches, not this design's failure. A future ADR can revisit the base-class shape independently; the acceptance test (§ Follow-ups) is the artefact that will report when the ceiling starts to bind.

### Follow-ups

**Sub-issues under the umbrella tracked at [GH #645](https://github.com/Toloka/tolokaforge/issues/645)** — decompose via `/writing-development-tickets` after this ADR merges. Landing order chosen so each unblocks the next; **the acceptance test is deliberately built early so it reports at every step, and the workflow retarget lands right after the first manual Bucket A succeeds rather than at the end** (both revised 2026-08-07 per follow-up review — see § What "success" looks like and § Consequences → known ceiling). Realistic PR count 9–10 (numbered arcs below are consolidatable).

1. **Fail-loud entry-point registry semantics** — fix the [GH #544](https://github.com/Toloka/tolokaforge/issues/544) pattern on `tolokaforge.adapters` preventatively; apply the same discipline to the new policy-class entry-point mechanism. Resolved by [#930](https://github.com/Toloka/tolokaforge/issues/930); canonical shape lives in [`docs/ADAPTER_ARCHITECTURE.md` § Fail-loud registry pattern](../ADAPTER_ARCHITECTURE.md#fail-loud-registry-pattern) and the `tolokaforge.core.plugin_registry` module docstring.
2. **Certification seam extraction + certificate exclusion data + probe registration.** Load-bearing — the acceptance test in (3) depends on this seam. Move `_capability.py`, `registry.py`, fixtures, ~30 test bodies. Add `excluded_capabilities` / `known_unsupported_reasons` / `probe_params` / `capability_extras` fields. Add probe-registration decorator API. Resolved by [#931](https://github.com/Toloka/tolokaforge/issues/931); `tolokaforge.testing.certify` is the public engine seam, `ModelCertificate` widened with `excluded_capabilities` / `known_unsupported_reasons` / `probe_params` / `capability_extras`, and `@register_probe` ships as the forward-compat dispatch seam.
3. **Acceptance-test scaffolding** — `tests/canonical/test_models_wheel_replay.py`. Replays the last N integrations against the engine tree **as it stood on each integration's date** (git checkout per-integration, not against post-follow-up-8 `main` — otherwise the retroactive hook masks the measurement). Reports the "engine releases avoided" number at every subsequent PR. Initial run reports the status-quo baseline (3-in-5 or whatever the tree of the day yields); each PR (4)–(11) should move the number. Landing this before (4) means every subsequent change is measured against the target. Resolved by [#932](https://github.com/Toloka/tolokaforge/issues/932); canonical test at [`tests/canonical/test_models_wheel_replay.py`](../../tests/canonical/test_models_wheel_replay.py), baseline snapshot at [`tests/canonical/snapshots/models_wheel_replay/metric.json`](../../tests/canonical/snapshots/models_wheel_replay/metric.json).
4. **Fingerprint** — widened `models_fingerprint` on `engine_run_state.json`. Resolved by [#933](https://github.com/Toloka/tolokaforge/issues/933); [`tolokaforge/core/model_data.py`](../../tolokaforge/core/model_data.py) is the pure seam (`ModelsFingerprint`, `compute_models_fingerprint`, `decode_models_fingerprint`), [`tolokaforge/core/engine_run_state.py`](../../tolokaforge/core/engine_run_state.py) persists the field on every run, and [`docs/OUTPUT_FORMAT.md`](../OUTPUT_FORMAT.md) documents the on-disk schema.
5. **New extension slots — `assistant_text_policy` + `params_policy` + narrow `message_assembly_policy`.** Base classes, registry entries, wire-in points in `client.py`. The Cohere text-marker slot unblocks integration #929; the narrow message-assembly version turns the `client.py` filler string into policy data (no new slot type, just data extraction of an existing string). Resolved by [#934](https://github.com/Toloka/tolokaforge/issues/934); [`tolokaforge/core/llm/assistant_text_policy.py`](../../tolokaforge/core/llm/assistant_text_policy.py), [`tolokaforge/core/llm/message_assembly_policy.py`](../../tolokaforge/core/llm/message_assembly_policy.py), and [`tolokaforge/core/llm/params_policy.py`](../../tolokaforge/core/llm/params_policy.py) carry the three new base classes; `_POLICY_REGISTRIES` has nine slots; the Cohere unblock proof lives in [`tests/unit/llm/test_assistant_text_policy_seam.py`](../../tests/unit/llm/test_assistant_text_policy_seam.py).
6. **Policy configuration — slot values become `{name, params}`.** Overlay validator extended with the engine ∪ models-declared allow-list. `_RECOGNISED_OVERRIDE_KEYS` removed. Partially resolved by [#934](https://github.com/Toloka/tolokaforge/issues/934): the `{name, params}` slot shape ships (both bare `name` and `{name, params}` accepted, bare `name` deprecated since v0.17.0 for removal in v0.18.0), the overlay validator rejects nested-key typos with `difflib` suggestions, and the Gemini `if reasoning_name == "gemini"` model-name conditional in `build_capabilities` is gone (`drop_placeholder_signature` flows through ordinary `{name, params}` dispatch). `_RECOGNISED_OVERRIDE_KEYS` full removal deferred to [#1017](https://github.com/Toloka/tolokaforge/issues/1017) — it targets v0.18.0, matching the bare-`name` deprecation window.
7. **Provider bindings as data.** `providers.yaml` schema including the three Nova sites (`_configure_nova_base_url`, `_format_model_name`, kwargs block with `custom_llm_provider`) and the two-step slug handling. `UNROUTABLE_PROVIDERS`, rate-limit regex, `OPENROUTER_API_KEYS` rotation all read from data. Engine ships defaults matching today; models wheel supplies overrides once it lands. Resolved by [#935](https://github.com/Toloka/tolokaforge/issues/935); [`tolokaforge/core/llm/providers.py`](../../tolokaforge/core/llm/providers.py) is the seam (`ProviderBinding`, `SlugRewrite`, `get_provider_binding`, `compile_rate_limit_patterns`, `DEFAULT_RATE_LIMIT_PATTERNS`), and [`tolokaforge/core/data/providers.yaml`](../../tolokaforge/core/data/providers.yaml) ships six provider entries (`openrouter`, `openai`, `anthropic`, `gemini`, `nova`, `mock`). Nova's three sites (init `NOVA_API_BASE` `os.environ.setdefault`, `_format_model_name` bare-name return, `_call_with_key_rotation` per-attempt `api_base` / `api_key` / `custom_llm_provider` / slug rewrite), `UNROUTABLE_PROVIDERS` routability, OpenRouter rotation (`OPENROUTER_API_KEYS` → `OPENROUTER_API_KEY`), the `custom_llm_provider` override, and the rate-limit text-pattern catalogue are data-driven. [`LLMClient.classify_loop_error`](../../tolokaforge/core/llm/client.py) is the public seam consuming `binding.rate_limit_patterns`; `_configure_openrouter_base_url` (dual-env reconciliation), `_openrouter_headers`, `provider_order`, and mock's early-return stay engine code because their shape is either provider-specific coordination the single-field schema cannot express, or config off `ModelConfig.openrouter` rather than a provider binding. Cutover to the models wheel (`providers.yaml` moving out of the engine) is [#938](https://github.com/Toloka/tolokaforge/issues/938) — the `models_fingerprint` payload widens to include `providers.yaml` there.
8. **Public helper promotion.** `_coerce_json_strings`, `_coerce_empty_containers`, `_find_additional_properties` (from `_dict_maps`) — all public with docstrings + compat guarantee. **Scope note:** promoting `_build_hints` on `RefResolvingDictMapHints` is *not* a pure rename — the parent method is a `@staticmethod` at `prompt_policy.py:64` and the child is an instance method with `# type: ignore[override]`. Turning it into a proper extension point is a base-class *shape* change, which is Bucket B by our own definition. This follow-up covers the promotion; the `_build_hints` shape change lands as a companion Bucket-B PR (or is scoped explicitly out and `RefResolvingDictMapHints` stays engine).
9. **Base-class hooks landing.** Sweep every per-model subclass requiring a base-class hook today (`StrictSchema.carry_scalar_dict_map_value` from #560 is the concrete case). Add the hooks; each defaults to current behaviour. Engine half of "add-hook-then-move-subclass".
10. **`benchmark-results-collector` consumer migration + `importlib.resources` reader sweep.** Pre-move: consumers migrate to `tolokaforge.core.model_data.bundled_pricing_path()` and fail loud on empty. Sweep the codebase for any other `importlib.resources.files("tolokaforge")` reader touching data paths. Blocks (11) until every known reader is migrated.
11. **Create `tolokaforge-models` package + cut over.** THE main structural PR. Bundles all moved-out data + subclasses + tests. Adds `model_data.py` widened accessor, hatch custom builder, models-subset target, partition file. Extends engine `pyproject.toml`. Registers PyPI project. Updates runner-subset. Adds install-time validation + `minimum_engine_version` runtime check. Includes the `tolokaforge/core/llm/__init__.py` re-export shim for moved subclasses (one-release deprecation window on the by-name imports).
12. **Auto-integration workflow retarget** — lands right after the first manual Bucket A succeeds (i.e. right after (11)), not deferred to the end. Rationale: until the pipeline commits to `tolokaforge_models/`, every real integration still goes down the engine path and the "engine version stays put across a lineup refresh" property is untested in production. Repoint `integrate-model.yml`. Classification step distinguishes Bucket A vs. Bucket B.
13. **Docs flip.** Invert `docs/ADD_NEW_MODEL.md` per the widened Bucket A / B.

**Documentation to update** — [`docs/ADD_NEW_MODEL.md`](../ADD_NEW_MODEL.md), [`docs/LLM_LAYER.md`](../LLM_LAYER.md), [`docs/RELEASING.md`](../RELEASING.md) (downstream migration note), [`docs/ROADMAP.md`](../ROADMAP.md) on next release event.

**Tests** — `tests/canonical/test_models_subset_partition.py`, `tests/canonical/test_models_subset_install_smoke.py`, `tests/canonical/test_models_wheel_replay.py` (added [#932](https://github.com/Toloka/tolokaforge/issues/932)).

**Deferred / not this ADR:**

- Plugin ecosystem for third-party model-data publishers.
- Extending the PyPI-publish pattern to any other engine subset.

## Colleague review focus points

Points settled by the 2026-08-07 follow-up review are marked ✅ with the resolution; open questions remain unmarked.

1. **Is the § Requirements list complete?** Any known integration pain missed?
2. ✅ **Capability enum ownership.** Resolved: stays **engine-authoritative**. Certificate-borne `capability_extras: dict[str, str]` covers per-model quirks that need surfacing but do not correspond to a shipped shared probe. Silent-break protection via attribute access preserved; the engine release for a genuinely new *category* is accepted as rare.
3. **Base-class hook policy — "add hook + subclass in same PR" across two wheels.** Workable? Or two-release protocol needed?
4. ✅ **Forwarding stub.** Resolved: **no stub.** Consumers migrate to `tolokaforge.core.model_data.bundled_pricing_path()` and fail loud on empty ahead of the cutover; a codebase-wide `importlib.resources` sweep is a prerequisite (follow-up 10). @bberkes-toloka has committed to landing the `benchmark-results-collector` migration.
5. ✅ **Classes that do not fit "inherit from stable base, use only public API".** Enumerated by the review: `GeminiRecursiveSchema` (fits after follow-up 9), `MinimaxM3TagRecoveryResponse` (composite, fits after follow-up 8), `RefResolvingDictMapHints` (deepest — base-class shape change; see follow-up 8 scope note). Confirm nothing has been added since #929.
6. **ADR-0002 Option 4 selective adoption criterion.** YES for subclasses of stable bases + public API only; NO for new bases / new lifecycle stages. Right cut?
7. **Landing-order change.** Acceptance test at position 3 (not last), so it reports progress at every subsequent PR; workflow retarget at 12 (right after cutover, not last), so the property is tested in production. Workable, or does the retarget need more slack for the manual path to settle?
8. **Provider bindings schema — Nova capture.** Three-site mapping (`_configure_nova_base_url`, `_format_model_name`, kwargs block with `custom_llm_provider`) and two-step slug handling. Any provider besides Nova with equivalent hidden hooks the schema needs to name up front?
9. ✅ **`RefResolvingDictMapHints` — shape change landed in [#936](https://github.com/Toloka/tolokaforge/issues/936) as Bucket B companion.** `DictMapHints._build_hints` (`@staticmethod`) became [`DictMapHints.build_hints`](../../tolokaforge/core/llm/prompt_policy.py) (instance method, public overridable hook); the `# type: ignore[override]` is gone from `RefResolvingDictMapHints.build_hints`. `RefResolvingDictMapHints` now fits the ADR-0002 Option 4 criterion (inherits from a stable base + uses only public API) after #936, so [#938](https://github.com/Toloka/tolokaforge/issues/938) can relocate it as a Bucket A cutover.
10. **Message-assembly slot cut.** Narrow version (filler string as policy data) in this ADR; general version (assembled-message-list slot with invariant checks) deferred until a second use case appears. Right cut?

## Links

- Related ADRs:
  - [ADR-0002](0002-external-model-registry.md) — this ADR lands the packaged model-data artifact ADR-0002 § Context anticipated, and (widening) selectively adopts ADR-0002's own § Considered Options Option 4 for per-model subclasses. ADR-0002 is promoted from `Proposed` to `Accepted` in the same PR that lands this one.
  - [ADR-0025](0025-runner-wheel-split.md) — this ADR reaffirms ADR-0025's *"one PyPI wheel for engine code"* clause. The two ADRs cover disjoint artifact categories.
- Related code:
  - [`tolokaforge/core/llm/presets.py`](../../tolokaforge/core/llm/presets.py) — loader whose data source shifts to `tolokaforge.core.model_data`; overlay validator widened.
  - [`tolokaforge/core/llm/schema_sanitizer.py`](../../tolokaforge/core/llm/schema_sanitizer.py) et al. — base classes that stay; per-model subclasses that move.
  - [`tolokaforge/core/pricing.py`](../../tolokaforge/core/pricing.py) — pricing loader + `normalize_model_name` vendor-inference (moves to data).
  - [`tolokaforge/core/llm/client.py`](../../tolokaforge/core/llm/client.py) — Nova branch, rotation, rate-limit classification all move to provider-bindings data.
  - [`tolokaforge/core/llm/proxy.py`](../../tolokaforge/core/llm/proxy.py) — `UNROUTABLE_PROVIDERS` moves to data.
  - [`tolokaforge/core/engine_run_state.py`](../../tolokaforge/core/engine_run_state.py) — where `models_fingerprint` lands.
  - [`tolokaforge/testing/certify/`](../../tolokaforge/testing/certify/) — the public engine-side certification seam; the physical relocation of certificate data and per-model bodies to `tolokaforge_models/` happens in the cutover.
  - [`scripts/hatch/hatch_runner_subset_builder.py`](../../scripts/hatch/hatch_runner_subset_builder.py) — pattern the new `hatch_models_subset_builder.py` mirrors.
- Related issues:
  - [GH #645](https://github.com/Toloka/tolokaforge/issues/645) — the public issue that catalysed this ADR, and its [2026-08-07 review comment](https://github.com/Toloka/tolokaforge/issues/645#issuecomment-5213977204) that catalysed the widening. The follow-up review on this PR (2026-08-07 later that day) drove the revisions marked *"revised 2026-08-07"* throughout — capability-enum flip, no-stub plan, Nova three-site record, class-name-check placement, `minimum_engine_version` runtime rationale, nested-unknown-key allow-list scoping, `__all__` deletion caveat, ninth-slot Cohere correction, and the § What "success" looks like framing note.
  - [GH #929](https://github.com/Toloka/tolokaforge/issues/929) — open Cohere integration blocked on the `assistant_text_policy` slot this ADR introduces. First real test that the widened design unblocks something today rather than moves faster tomorrow.
  - [GH #544](https://github.com/Toloka/tolokaforge/issues/544) — the fail-loud registry-collision pattern that follow-up (1) fixes.
  - [GH #353](https://github.com/Toloka/tolokaforge/issues/353) — pricing table location alignment; overlaps with follow-up (9).
  - [GH #830](https://github.com/Toloka/tolokaforge/issues/830) — the runner-subset data-file omission fix; its lesson applies to the models-subset custom builder.
