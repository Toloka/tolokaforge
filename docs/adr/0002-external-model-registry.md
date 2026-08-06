# 0002. External model registry — operator-overridable preset data

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** @cirogam22
- **Supersedes:** —
- **Superseded by:** —
- **Advanced by:** [ADR-0030](0030-tolokaforge-models-split.md) — completes the seam via a packaged, versioned model-data wheel and the certification-harness relocation this ADR deferred.

## Context and Problem Statement

Adding (or adjusting) a model in TolokaForge today requires editing files that
ship **inside the engine wheel**:

- `tolokaforge/core/data/model_presets.yaml` — preset routing (which existing
  policy/codec classes a model uses).
- `tolokaforge/core/data/pricing.json` — per-model price data.
- The capability declaration consumed by `build_capabilities()` in
  `tolokaforge/core/llm/presets.py`.

The first two are **data**; the third is **code** only when a *new* policy
class is required. In practice the policy classes already in tree
(`StandardResponse`, `AnthropicContent`, `AnthropicEphemeralCache`,
`AnthropicReasoningCodec`, …) cover most new models. So the typical model
registration is a pure-data change that nevertheless triggers an engine
release.

The pain compounds in the eval loop: integration tests pass with the new
preset → release engine → smoke eval surfaces a model-specific failure pattern
fixable by re-routing to a different existing policy → release engine again.
Two engine releases per new model becomes common when custom patterns appear.

The same kind of seam was just opened for **task adapters** by
[ADR 0001's broader project context — entry-point plugin discovery in
`tolokaforge.adapters`](../ADAPTER_ARCHITECTURE.md) and validated at the
host boundary with `ensure_registered_adapter()`
(`tolokaforge/adapters/__init__.py:86`). The LLM preset layer is the next
natural seam.

## Decision Drivers

- Ship pure-data model registrations without an engine release.
- Preserve loud-fail behaviour for typos and unknown policy names
  (`AGENTS.md` rule #1).
- Keep the existing call sites (`build_capabilities`,
  `resolve_effective_preset`, `resolve_policy_names`) untouched — no new
  signatures, no new caller obligations.
- Keep `ModelCapabilities` frozen and deterministic for a given input.
- Do not commit to a particular distribution mechanism (overlay file vs.
  entry-point bundle vs. config dir) before there's evidence about which
  operators actually want.

## Considered Options

1. **Status quo** — every model registration is an engine release.
2. **Operator-pointed preset overlay file** — engine reads bundled
   `model_presets.yaml` *plus* an optional second YAML named by the operator
   (CLI flag, env var, or run-config field). Same schema; merged at load time;
   policy-class names validated against the in-engine registries at load time.
   This ADR's proposal.
3. **Entry-point plugin discovery for preset bundles** — a
   `tolokaforge.model_presets` entry-point group analogous to
   `tolokaforge.adapters`. Operators install a `pip` package that ships
   preset YAML; the engine discovers it on import.
4. **Sidecar Python module for policy classes** — operator points the engine
   at a Python module that registers brand-new policy/codec classes into the
   in-engine registries. Escape hatch for one-off experiments where no
   existing policy class fits.

## Decision

Adopt **Option 2 — operator-pointed preset overlay file** now. Treat 3 as the
natural follow-up if operators want shippable preset packages and we see
evidence of cross-project reuse. Defer 4 until eval data shows custom policy
*classes* recurring per model often enough to justify the escape hatch
(otherwise novel policy classes stay engine code, which is the right boundary).

The overlay file:

- Uses the same schema as `model_presets.yaml` (`default:`, `presets:`,
  `providers:`).
- Is merged onto the bundled file at engine startup: `default:` and
  `providers:` are shallow-merged with the overlay winning; `presets:` is
  prepended so first-match-wins lets operators shadow a bundled preset.
- Policy-name strings (`schema_sanitizer`, `prompt_policy`, `content_policy`,
  `response_policy`, `reasoning_codec`, `cache_policy`) must resolve in the
  in-engine registries (`_SCHEMA_SANITIZERS` … `_CACHE_POLICIES`). Unknown
  names raise `ValueError` at load time, naming both the offending key and
  the overlay file path. Same shape as `ensure_registered_adapter()`.
- Has no effect when unconfigured: behaviour is bit-identical to today, no
  new code paths run.

The overlay path is resolved with precedence `--presets-file` CLI flag >
optional `engine.presets_file` field on `RunConfig`. The CLI flag covers
one-off overlays (smoke iterations, ablations); the config field covers
overlays that are part of a benchmark's committed definition. An env-var
fallback (`TOLOKAFORGE_PRESETS_FILE`) was considered and dropped — its
primary use case (a fleet of subprocesses sharing one overlay) is already
covered by `prepare`'s queue-state persistence; the two surviving paths
cover the rest. If a future workflow surfaces a real need (e.g. CI matrix
steps that need a session-scoped overlay without threading flags), add it
then.

Distributed workers inherit it implicitly — `prepare` persists the resolved
path into the queue run-state, so `worker` subprocesses pick it up without
the operator threading the flag manually.

## Consequences

### Positive

- A new model that reuses existing policy classes ships with zero engine code
  change and zero release. Operator drops a 5-line overlay file alongside
  their run config, points at it, runs.
- Smoke-eval policy adjustments (re-route a misbehaving model to a different
  existing response policy) become same-day fixes instead of release-cycle
  fixes.
- The seam mirrors the adapter plugin pattern's externalization step — same
  loud-fail discipline at the host boundary, same "data + declaration are
  open; new classes stay engine code" boundary.
- Forward-compatible: if Option 3 (entry-point plugin discovery) lands later,
  it can populate the same overlay structure rather than introducing a
  parallel mechanism.

### Negative / Trade-offs

- A new in-engine policy class still requires editing the in-engine
  validator (so it knows the new name is allowed in overlays). This is a
  feature, not a bug — it keeps overlay validation honest — but it must be
  enforced by a unit test that scans the validator body for every registry's
  members. Without the test, adding a new policy class while forgetting the
  validator would silently reject overlays that use it.
- Module-level overlay state (`set_overlay_path()`) is reset between test
  cases via fixture; tests that forget the fixture could leak overlay state
  to neighbours. Documented in the test contract.
- Pricing data is not externalized here. LiteLLM's
  `LITELLM_MODEL_COST_MAP_URL` already provides an external pricing path; we
  do not duplicate it inside the engine. Operators who need to ship new
  pricing without a release use the LiteLLM mechanism.

### Follow-ups

- Code changes required: overlay loader and validator in
  `tolokaforge/core/llm/presets.py`; `EngineConfig` on `RunConfig`; CLI
  flag + env-var precedence; orchestrator persists path into queue run-state.
- Documentation to update: `docs/CONFIG.md` (new field + precedence +
  distributed-worker propagation); `docs/ADD_NEW_MODEL.md` (overlay
  walkthrough); `docs/LLM_LAYER.md` (loader note).
- Tests to add: overlay load + merge, precedence (shadowing), validation
  (unknown policy name, malformed YAML, missing file, top-level typo),
  default/provider merge depth, validator-vs-registries sync, worker
  propagation, fingerprint round-trip through an overlay.

## Links

- Related ADRs:
  - [0001 — Record architecture decisions in ADRs](0001-record-architecture-decisions.md).
  - [0030 — Model data as a second PyPI wheel](0030-tolokaforge-models-split.md) — completes Option 3 of this ADR (packaged model-data artifact) and lands the certification-harness relocation this ADR flagged as required to make the seam usable.
- Related code:
  - `tolokaforge/core/llm/presets.py` (loader, registries, validator).
  - `tolokaforge/adapters/__init__.py:86` (`ensure_registered_adapter`, the
    host-boundary validation pattern this ADR mirrors).
- External references:
  - [GH issue #64 — Decouple model policy/codec layer from engine release cadence](https://github.com/Toloka/tolokaforge/issues/64).
  - LiteLLM external model cost map:
    [`LITELLM_MODEL_COST_MAP_URL`](https://docs.litellm.ai/docs/provider_registration/add_model_pricing),
    [day-0 model sync](https://docs.litellm.ai/docs/proxy/sync_models_github).
