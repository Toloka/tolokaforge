# 0030. Model data as a second PyPI wheel — `tolokaforge-models` from the same monorepo

- **Status:** Proposed
- **Date:** 2026-08-06 (widened 2026-08-07 per [issue-#645 comment](https://github.com/Toloka/tolokaforge/issues/645#issuecomment-5213977204))
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

Of the 5 auto-integrations the pipeline resolved end-to-end, **3 needed new engine classes**. Since the OSS cleanup (#6, 2026-05-28), the policy / codec / sanitizer modules have changed **five times, every one driven by onboarding a specific model, none by engine-internal work**: `response_policy.py` (2), `prompt_policy.py` (1), `schema_sanitizer.py` (1), `reasoning_codec.py` (1). `content_policy.py`, `cache_policy.py`, and `params_policy.py` have not changed on `main` at all in that window.

ADR-0002 § Considered Options deferred its Option 4 on an explicit, measurable condition: *"until eval data shows custom policy classes recurring per model often enough to justify the escape hatch."* That condition is now visibly met.

"Just move the subclasses out" is also insufficient. Three concrete failure modes recur in the tree:

1. **A per-model subclass requires editing its parent to add a hook.** `#560` added `VALUE_FIELD` and `carry_scalar_dict_map_value: bool = False` to `StrictSchema` itself (`schema_sanitizer.py`) plus the branch that honours it; `GeminiRecursiveSchema` sets the flag. Moving the subclass leaves 16 dependent lines in the base.
2. **A per-model class reaches into module-private helpers.** `MiniMaxM3StrictSchema` consumes `_coerce_json_strings` / `_coerce_empty_containers`; `RefResolvingDictMapHints` imports a symbol deliberately excluded from `_dict_maps.__all__` and overrides a private `_build_hints`. Out-of-tree code cannot depend on any of that.
3. **Some adaptations have nowhere to attach.** `_POLICY_REGISTRIES` has six slots; there is no `params` slot (so a new knob is a `GenerationParams` signature change) and no `assistant_text` slot (Cohere's `<|START_TEXT|>` markers around assistant text land in `client.py` as an `if target == "anthropic"` branch instead).

And the *original* draft of this ADR made three things *worse* rather than neutral:

- **Per-model certification exclusions** (`_UNRELIABLE_COLD_CACHE_REPORT_NAMES` in `#474`) live in shared test-body code that the original draft moved into the engine wheel as `tolokaforge.testing.certify.suite`. A per-model exclusion becomes an engine-wheel edit.
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

1. **Stays where it is** (`tests/integration/llm/registry.py`). Rejected — this is the load-bearing coupling.
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

Today `_POLICY_REGISTRIES` has six slots (`schema_sanitizer`, `prompt_policy`, `content_policy`, `response_policy`, `reasoning_codec`, `cache_policy`). Two more land:

- **`assistant_text_policy` (NEW).** Base class `AssistantTextPolicy` with signature `parse_assistant_text(text: str, *, model_config: ModelConfig) -> str`. Cohere's `<|START_TEXT|>…<|END_TEXT|>` marker handling — today an `if target == "anthropic"`-adjacent branch in `client.py` — becomes a `CohereMarkerAssistantText` subclass in `tolokaforge_models/policies/`.
- **`params_policy` (NEW).** Base class `ParamsPolicy` with signature `build_params(overrides: dict[str, Any]) -> GenerationParams`. Replaces the hard-coded `_VALID_PARAMS_KEYS = inspect.signature(GenerationParams.__init__)` mechanism. `_RECOGNISED_OVERRIDE_KEYS` at `presets.py` goes away.

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
  slug_rewrite: "openai/{name}"              # Nova's bare→openai-prefixed rewrite
```

Consumed at `LLMClient` construction. Concrete moves:

- Nova hardcode at `client.py:1901` (resolves `NOVA_API_KEY` via `SecretManager` and rewrites the model slug) becomes a lookup: read `providers[provider].slug_rewrite`, apply if non-null.
- `UNROUTABLE_PROVIDERS = frozenset({"mock", "nova"})` at `proxy.py:145` becomes a lookup: `providers[provider].unroutable`.
- `_is_rate_limit_exception`'s hard-coded regexes become data: `providers[provider].rate_limit_patterns`.
- Multi-key rotation at `client.py:731` reads `providers[provider].api_keys_env` instead of the hardcoded `OPENROUTER_API_KEYS`. A second provider needing rotation is now a data change, not a client edit.

`normalize_model_name`'s `startswith` chain in `pricing.py:280-341` (`kimi*` → `moonshot/`, etc.) moves to `tolokaforge_models/data/vendor_prefixes.yaml`. It has already drifted on `main` (`kimi*` → `moonshot/` while the data has `moonshotai/`; six vendors have no branch). Vendor inference travels with the table it serves.

The engine ships default bindings that reproduce today's behaviour for the shipped providers, so this can land before the models-wheel cutover. Once the models wheel exists, its `providers.yaml` is the authoritative source.

## Certification as a public engine seam

`tolokaforge.testing.certify` becomes a first-class engine surface:

- `Capability` enum + `ModelCertificate` dataclass — sourced from `tolokaforge_models.certificates`, re-exported from the seam for callers that want the types without depending on the models package directly. `ModelCertificate` gains three new fields (widening):
  - `excluded_capabilities: frozenset[Capability]` — replaces engine-side sets like `_UNRELIABLE_COLD_CACHE_REPORT_NAMES`. Shared test bodies check `cert.excluded_capabilities` instead of a hardcoded name set.
  - `known_unsupported_reasons: dict[Capability, str]` — human-readable explanation surfaced in reports.
  - `probe_params: dict[Capability, dict[str, Any]]` — per-model tuning for probe bodies (e.g. reduced token budgets, alternate prompts).
- `live_client`, `skip_unless_capability_declared` pytest fixtures — moved from `tests/integration/llm/conftest.py`. The `TF_PRESETS_FILE` env-var overlay hook (`conftest.py:47-66`) is preserved.
- `tolokaforge.testing.certify.suite` — the ~30 `test_*.py` bodies, parametrised on `ALL_MODELS` supplied via fixture. Test bodies read exclusion data from certificates and probe params from `cert.probe_params`; no more hardcoded per-model sets.
- `tolokaforge.testing.certify.probes` (NEW) — `@register_probe(Capability.X)` decorator collects per-capability probe bodies from `tolokaforge_models/tests/`. Engine's shared suite dispatches to the registered probe. Adding a new `Capability` value is a models-wheel edit that carries its probe body with it.

**Capability enum authority moves to the models package.** Trade-off: weaker engine authority over "what supported means"; stronger decoupling. Aligns with bberkes's requirement 8. Engine's `certify.suite` reads enum members via `bundled_certificates()`.

Per-model test bodies (`test_nova_api.py`, `test_gemini_placeholder_signature_replay.py`, `#846`'s codec unit test) → `tolokaforge_models/tests/`. Collected via `pytest --pyargs tolokaforge_models.tests` from the models CI. Same fixtures, same overlay hooks.

The in-tree suite under `tests/integration/llm/` becomes a thin wrapper (or is deleted) — `tolokaforge-models` CI is the source of truth for the real registry.

## Install-time validation (new — widening)

Three checks all fail before any run starts:

- **`minimum_engine_version` on the models wheel.** Engine reads `tolokaforge_models.minimum_engine_version` and refuses to load a models wheel that names a floor higher than the installed engine. Message: `tolokaforge-models X.Y.Z requires tolokaforge >= A.B; installed A'.B'. Upgrade the engine or downgrade the models wheel.`
- **`__api_version__` integer.** Unchanged semantics from the original draft — the loader-contract compat guardrail.
- **Class-name-existence at engine import.** A models wheel referencing a policy class (e.g. `GeminiRecursiveSchema`) that the engine's merged `_POLICY_REGISTRIES` (engine defaults + models registrations) does not know: fail loud at import time, not at `LLMClient` construction after an evaluation has begun spending.

**Overlay validator extension.** `_validate_overlay` today rejects unknown top-level keys but accepts unknown keys nested *inside* a preset block — a `totally_unknown_future_knob: 42` inside a preset silently no-ops. Widening: nested unknown keys fail loud with the offending file, key, and closest schema match. Symmetric with today's top-level check.

## Downstream data-resource consumers (new — widening)

Known consumer: `benchmark-results-collector` reads `importlib.resources.files("tolokaforge") / "core/data/pricing.json"` and returns an empty table on failure — a silent cost-zero regression when the resource moves.

**Forwarding stub for one release cycle.** Engine wheel keeps `tolokaforge/core/data/pricing.json` and `model_presets.yaml` as thin re-emit stubs that mirror the `tolokaforge_models` content, plus log a `DeprecationWarning` naming the reader and pointing to `tolokaforge.core.model_data.*`. Stub is deleted on the release after next.

Migration note in [`docs/RELEASING.md`](../RELEASING.md) + release notes on the first models-wheel-cutover engine release enumerating known downstream consumers.

## Fingerprinting for auditability

`tolokaforge/core/engine_run_state.py:22-34` — `write_engine_run_state` gains one field:

```jsonc
{
  "run_id": "...",
  "presets_file": "/path/or/null",
  "models_fingerprint": {
    "package_version": "1.4.2",              // tolokaforge_models.__version__
    "content_sha256": "...",                 // sha256 over presets + pricing + providers + vendor_prefixes + certificates + policy registrations (post-overlay)
    "api_version": 1,
    "minimum_engine_version": ">=0.15,<0.17"
  }
}
```

Any completed run can be reconstructed: reinstall the named `tolokaforge-models` version, apply the same overlay, get byte-identical model resolution. ADR-0002 § Follow-ups called for a fingerprint round-trip through an overlay — this delivers it, over the widened data surface.

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
- **Capability enum authority moves to the models package.** Weaker engine authority; stronger decoupling. Conscious trade-off. Reviewer question: is this the right call, or should the enum stay engine-authoritative?
- **Base-class hooks land across two wheels in one PR.** The engine PR + models PR sequence is a small workflow discipline cost. Reviewer question: workable, or two-release protocol needed?

### Follow-ups

**Sub-issues under the umbrella tracked at [GH #645](https://github.com/Toloka/tolokaforge/issues/645)** — decompose via `/writing-development-tickets` after this ADR merges. Landing order chosen so each unblocks the next. Realistic PR count 8–9 (numbered arcs below are consolidatable).

1. **Fail-loud entry-point registry semantics** — fix the [GH #544](https://github.com/Toloka/tolokaforge/issues/544) pattern on `tolokaforge.adapters` preventatively; apply the same discipline to the new policy-class entry-point mechanism.
2. **Certification seam extraction + certificate exclusion data + probe registration.** Load-bearing. Move `_capability.py`, `registry.py`, fixtures, ~30 test bodies. Add `excluded_capabilities` / `known_unsupported_reasons` / `probe_params` fields. Add probe-registration decorator API.
3. **Fingerprint** — widened `models_fingerprint` on `engine_run_state.json`.
4. **New extension slots — `assistant_text_policy` + `params_policy`.** Base classes, registry entries, wire-in points in `client.py`. Cohere marker handling moves out of the client branch.
5. **Policy configuration — slot values become `{name, params}`.** Overlay validator extended (nested unknown-key rejection). `_RECOGNISED_OVERRIDE_KEYS` removed.
6. **Provider bindings as data.** `providers.yaml` schema. Nova hardcode, `UNROUTABLE_PROVIDERS`, rate-limit regex, `OPENROUTER_API_KEYS` rotation all read from data. Engine ships defaults matching today; models wheel supplies overrides once it lands.
7. **Public helper promotion.** `_coerce_*`, private `_dict_maps` symbols, `_build_hints` — all public with docstrings + compat guarantee.
8. **Base-class hooks landing.** Sweep every per-model subclass requiring a base-class hook today. Add the hooks (the engine half of "add-hook-then-move-subclass").
9. **Create `tolokaforge-models` package + cut over.** THE main structural PR. Bundles all moved-out data + subclasses + tests. Adds `model_data.py` widened accessor, hatch custom builder, models-subset target, partition file. Extends engine `pyproject.toml`. Registers PyPI project. Updates runner-subset. Adds install-time validation + `minimum_engine_version` check.
10. **Downstream forwarding stub + deprecation notice.**
11. **Docs flip.** Invert `docs/ADD_NEW_MODEL.md` per the widened Bucket A / B.
12. **Auto-integration workflow retarget.** Repoint `integrate-model.yml`. Classification step distinguishes Bucket A vs. Bucket B.
13. **Acceptance test.** New canonical `tests/canonical/test_models_wheel_replay.py` — replays the last N integrations, asserts each Bucket A candidate could have shipped without an engine commit. Locks the ~5% engine, ~95% models target.

**Documentation to update** — [`docs/ADD_NEW_MODEL.md`](../ADD_NEW_MODEL.md), [`docs/LLM_LAYER.md`](../LLM_LAYER.md), [`docs/RELEASING.md`](../RELEASING.md) (downstream migration note), [`docs/ROADMAP.md`](../ROADMAP.md) on next release event.

**Tests to add** — `tests/canonical/test_models_subset_partition.py`, `tests/canonical/test_models_subset_install_smoke.py`, `tests/canonical/test_models_wheel_replay.py`.

**Deferred / not this ADR:**

- Plugin ecosystem for third-party model-data publishers.
- Extending the PyPI-publish pattern to any other engine subset.

## Colleague review focus points

1. **Is the § Requirements list complete?** Any known integration pain missed?
2. **Capability enum ownership — models-package-authoritative.** Weaker engine authority over "what supported means" vs. cleaner decoupling. Right call?
3. **Base-class hook policy — "add hook + subclass in same PR" across two wheels.** Workable? Or two-release protocol needed?
4. **Downstream forwarding stub — one-release deprecation window.** Long enough? Consumers to notify beyond `benchmark-results-collector`?
5. **Any policy classes today that do NOT fit "inherit from stable base, use only public API"?** Those stay Bucket B; would like the current list surfaced.
6. **ADR-0002 Option 4 selective adoption criterion.** YES for subclasses of stable bases + public API only; NO for new bases / new lifecycle stages. Right cut?

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
  - [`tests/integration/llm/`](../../tests/integration/llm/) — the certification harness that becomes `tolokaforge.testing.certify`.
  - [`scripts/hatch/hatch_runner_subset_builder.py`](../../scripts/hatch/hatch_runner_subset_builder.py) — pattern the new `hatch_models_subset_builder.py` mirrors.
- Related issues:
  - [GH #645](https://github.com/Toloka/tolokaforge/issues/645) — the public issue that catalysed this ADR, and its [2026-08-07 review comment](https://github.com/Toloka/tolokaforge/issues/645#issuecomment-5213977204) that catalysed the widening.
  - [GH #544](https://github.com/Toloka/tolokaforge/issues/544) — the fail-loud registry-collision pattern that follow-up (1) fixes.
  - [GH #353](https://github.com/Toloka/tolokaforge/issues/353) — pricing table location alignment; overlaps with follow-up (9).
  - [GH #830](https://github.com/Toloka/tolokaforge/issues/830) — the runner-subset data-file omission fix; its lesson applies to the models-subset custom builder.
