# 0030. Model data as a second PyPI wheel — `tolokaforge-models` from the same monorepo

- **Status:** Proposed
- **Date:** 2026-08-06
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Adding a model to tolokaforge today requires cutting an engine release even when the change is pure data — a price-table entry plus a preset routing that composes *existing* policy classes. The overlay seam described in [ADR-0002](0002-external-model-registry.md) (`--presets-file` / `RunConfig.engine.presets_file`, loaded at [`tolokaforge/core/llm/presets.py`](../../tolokaforge/core/llm/presets.py)) opened the *run-time* half of that decoupling: an operator can point an evaluation at an out-of-tree preset overlay and never touch the engine repo. But the *release-time* half is unfinished. Model onboarding still gates on an engine PR, which drags a wheel release with it.

The pattern is measurable in the release log. Between v0.8.4 → v0.11.2 (2026-07-15 → 2026-07-27, 12 days), **2 of 9 releases were pure zero-engine model shims** — v0.9.1 (kimi-k3 + muse-spark-1.1) and v0.9.3 (gemini-3.5-flash), both with no `### Feat` / `### Fix` sub-sections in [`CHANGELOG.md`](../../CHANGELOG.md) (bare version headers). A third (v0.11.1) bundled claude-opus-5 with unrelated engine work. v0.9.1 was cut hours after v0.9.0; v0.9.3 one day after v0.9.2. The "two releases per model" pattern [ADR-0002 § Context](0002-external-model-registry.md#context-and-problem-statement) called out is still visible on `git log --tags`.

Downstream consequence: consumers that pin `tolokaforge` by tag drift across engine versions purely as a side effect of model onboarding. For any consumer that compares evaluation results across models, "which engine version did this run on" becomes a per-model question instead of a constant, and every difference then needs its own justification.

[ADR-0002 § Considered Options](0002-external-model-registry.md#considered-options) foresaw three follow-on shapes to its Option 2 overlay: (3) an entry-point plugin registry mirroring `tolokaforge.adapters`, (4) a sidecar Python module for out-of-tree policy classes, or a hybrid. This ADR chooses a fourth shape that ADR-0002 did not enumerate: **a single, Toloka-published model-data wheel from the same monorepo**. It also names what the boundary is and what has to move for the seam to become usable — including the load-bearing certification-harness relocation ADR-0002 explicitly deferred.

## Decision Drivers

- **Decouple release cadence from model onboarding.** Engine version stops moving when a model is added.
- **One publisher, one package.** No plugin ecosystem. Squatting risk on `tolokaforge-model-*` names, external-shadow attack surface, and cross-package collision governance are all traded away for maintenance simplicity. If a plugin-ecosystem case ever materialises, it is a superseding ADR, not a Layer-2 addition here.
- **Preserve the [ADR-0025](0025-runner-wheel-split.md) "one PyPI wheel for engine code" clause** without extending it to data. ADR-0025 declared the runner-subset wheel Docker-only, deliberately not published to PyPI, to keep the compat and discovery story one-sided for *code*. This ADR observes that the same argument does not apply to data: `pricing.json` and `model_presets.yaml` are pure data, they change on their own cadence for their own reasons, and their PyPI presence introduces no new code-level compat surface.
- **Certification extraction is load-bearing.** Everything else in this ADR is either already-shipped seam (overlay) or trivial (docs flip, workflow retarget). The one change that unblocks the rest is moving the certification harness (`tests/integration/llm/registry.py` + `_capability.py` + the ~30 `test_*.py` bodies) out of the engine repo's test suite into a public library that both the in-tree suite and out-of-tree model-data CI can invoke.
- **Forward-compat toward a repo split.** The `tolokaforge_models/` tree is chosen to be a drop-in future repo: one `git filter-repo --subdirectory-filter tolokaforge_models` and it is a standalone project. No path renames, no reference rewrites, no engine-file history rewriting.
- **Fail-loud version compat.** A future engine change that alters the loader contract must break in a legible way, not silently produce wrong results. `__api_version__` integer on the models package + engine check at first `LLMClient` construction.

## Considered Options

**Wheel shape — where does model data live?**

1. **Status quo.** Data ships in the engine wheel; every model change is an engine release. Rejected — this is the problem.
2. **Overlay-only (ADR-0002 as shipped).** `--presets-file` is the whole answer; no packaged form of model data exists. Rejected as a stopping point — an overlay is a loose file path each operator carries and keeps in sync by hand; nothing bundles preset routing + pricing + certificate into a single installable, versioned artifact, so there is no way to pin "model data vN" the way an adapter can be pinned.
3. **Entry-point plugin ecosystem (ADR-0002 Option 3).** A `tolokaforge.model_data` entry-point group mirroring `tolokaforge.adapters`. Anyone can publish a `tolokaforge-model-*` package; engine discovers via `importlib.metadata.entry_points`. Rejected for now — introduces squatting risk (nothing stops a third party from publishing `tolokaforge-model-anthropic` and shadowing the official routing), a governance surface (which publishers are trusted?), and the silent-last-wins hazard already open against the adapter registry ([GH #544](https://github.com/Toloka/tolokaforge/issues/544)). The plugin-ecosystem case is not blocked by anything in this ADR — a future ADR can layer it on top of the mechanism this one lands, and the interface contract below is chosen with that door left open.
4. **One PyPI wheel + subset build target, Docker-only (mirror ADR-0025).** The subset is built locally, installed into the runner image, never published to PyPI. Rejected — leaves data movement gated on engine releases (the subset is stamped with the engine version), so does not decouple cadence.
5. **Single Toloka-published `tolokaforge-models` wheel from the same monorepo, independent versioning.** This ADR's decision.
6. **Date-based versioning for `tolokaforge-models` (2026.08.06).** Simpler than semver for a data-only artifact. Rejected — loses the semver-breakage signal (if a certificate schema changes, users need a signal beyond "the date is newer"). `__api_version__` addresses the compat axis; keep the marketing version on semver.

**Certification harness — where does it live?**

1. **Stays where it is** (`tests/integration/llm/registry.py`). Rejected — this is the load-bearing coupling. `docs/ADD_NEW_MODEL.md` makes the harness a mandatory gate: no model change merges without all six certification tests passing. Certification cannot be run without an engine-repo PR, so the release happens anyway, and the overlay seam sits unused.
2. **Move to a separate repo.** Rejected — re-creates the coordination problem elsewhere. The engine + certification move in lockstep because certification is *about* what the engine considers supported on the wire; splitting the repo turns that into a two-repo dance.
3. **Extract into a public `tolokaforge.testing.certify` seam inside the engine wheel.** Selected. The certification bodies remain engine code (the engine is authoritative about what "supported" means). Out-of-tree consumers `pip install tolokaforge` to get the test seam, `pip install tolokaforge-models` (or vend their own overlay) to get the data, and drive `pytest --pyargs tolokaforge.testing.certify.suite`.

## Decision

Adopt **Wheel-shape Option 5** and **Certification-harness Option 3**.

### Two PyPI distributions, one monorepo

The published PyPI surface becomes two independently-versioned wheels, both built from `Toloka/tolokaforge`:

- `tolokaforge` — the engine wheel. Contains everything it does today *except* `tolokaforge/core/data/pricing.json` and `tolokaforge/core/data/model_presets.yaml`. Declares `tolokaforge-models >= 1.0.0` as a runtime dependency.
- `tolokaforge-models` — the new model-data wheel. Contains preset routing (`model_presets.yaml`), pricing (`pricing.json`), and the capability-certificate registry (`Capability` enum, `ModelCertificate` dataclass, `ALL_MODELS`). Declares `__api_version__: int = 1` at the module level; the engine's `tolokaforge.core.model_data` module compares against its own expected value on first `LLMClient` construction and fails loud on mismatch.

This ADR reaffirms [ADR-0025 § Decision Drivers](0025-runner-wheel-split.md#decision-drivers) *"one PyPI wheel"* clause **for engine code**. Model data is a distinct category: it is pure data, it has its own release cadence for its own reasons (a new model is priced, or a preset needs a shape fix), and its PyPI presence adds no code-level compat surface. The runner-subset (`tolokaforge-runner-subset`) remains Docker-only per ADR-0025; nothing in this ADR changes that.

### The one seam the engine uses to reach model data

The engine reaches `tolokaforge_models` through exactly one internal module: [`tolokaforge/core/model_data.py`](../../tolokaforge/core/model_data.py) (to be added). It publishes three functions:

```python
def bundled_presets_path() -> Path: ...
def bundled_pricing_path() -> Path: ...
def declared_api_version() -> int: ...
```

All three do `from tolokaforge_models import ...` internally today. If a future superseding ADR moves to a plugin ecosystem, the internals of these three functions change (entry-point discovery instead of direct import); every caller in the engine — `presets.py:_load_bundled_presets`, `pricing.py:_PRICING_DATA_PATH`, the compatibility check — is untouched. Nothing else in the engine's code paths reaches into `tolokaforge_models` outside this module. The seam is the whole coupling.

### Certification as a public engine seam

`tolokaforge.testing.certify` becomes a first-class engine surface:

- `Capability` enum + `ModelCertificate` dataclass — sourced from `tolokaforge_models.certificates`, re-exported from the seam for callers that want the types without depending on the models package directly.
- `live_client`, `skip_unless_capability_declared` pytest fixtures — moved from `tests/integration/llm/conftest.py`. The `TF_PRESETS_FILE` env-var overlay hook (`conftest.py:47-66`) is preserved so overlay-driven certification runs still work.
- `tolokaforge.testing.certify.suite` — the ~30 `test_*.py` bodies, unchanged in shape, parametrised on `ALL_MODELS` supplied via fixture. Consumers invoke `pytest --pyargs tolokaforge.testing.certify.suite -k <slug>`.

The in-tree suite under `tests/integration/llm/` becomes a thin wrapper that imports the same suite and supplies a small dev-only `ALL_MODELS` — or is deleted, since `tolokaforge-models` CI becomes the source of truth for the real registry.

### Fingerprinting for auditability

`tolokaforge/core/engine_run_state.py:22-34` — `write_engine_run_state` gains one field:

```jsonc
{
  "run_id": "...",
  "presets_file": "/path/or/null",
  "models_fingerprint": {
    "package_version": "1.4.2",              // tolokaforge_models.__version__
    "content_sha256": "...",                 // sha256 of resolved (bundled + overlay) content
    "api_version": 1
  }
}
```

Any completed run can be reconstructed from its `engine_run_state.json`: reinstall the named `tolokaforge-models` version, apply the same overlay, get byte-identical model resolution. ADR-0002 § Follow-ups called for a fingerprint round-trip through an overlay — this delivers it.

### Independent versioning

`tolokaforge_models/pyproject.toml` carries its own `[project] version`, starting at `1.0.0`. A new workflow (`release-models.yml`) runs `cz bump` scoped to `tolokaforge_models/`, tags `models-vX.Y.Z`, and triggers a companion `publish-tolokaforge-models.yml` that runs `hatch build --target models-subset` and `uv publish` with trusted publishing. Engine's existing `release.yml` + `publish-tolokaforge.yml` are untouched — they still cut `tolokaforge-X.Y.Z` on `v*` tags and publish only the engine wheel.

Why integer `__api_version__` rather than PEP 440 range on `Requires-Dist`? PEP 440 couples wire compatibility to marketing version strings. Bumping `tolokaforge-models` 1.4 → 1.5 with pure data changes stays `__api_version__ = 1` — no engine re-release needed, no `Requires-Dist` ceiling shift. Only a real interface break (change to the model-data loader contract) bumps `__api_version__`, and that happens only with an engine change anyway.

### The monorepo layout — chosen to be a drop-in future repo

Model data physically lives at a top-level `tolokaforge_models/` directory:

```
public-tolokaforge/
├── tolokaforge/                            # engine package (unchanged top-level)
│   ├── core/
│   │   ├── llm/presets.py                 # loader reads from tolokaforge_models via model_data seam
│   │   ├── pricing.py                     # loader reads from tolokaforge_models via model_data seam
│   │   ├── model_data.py                  # NEW — the one seam
│   │   ├── engine_run_state.py            # +models_fingerprint field
│   │   └── data/                          # DELETED (contents move)
│   └── testing/certify/                   # NEW public certification seam
├── tolokaforge_models/                     # NEW top-level package → tolokaforge-models wheel
│   ├── __init__.py                        # __version__, __api_version__, accessors
│   ├── pyproject.toml                     # independent versioning
│   ├── data/
│   │   ├── pricing.json                   # moved from tolokaforge/core/data/
│   │   └── model_presets.yaml             # moved from tolokaforge/core/data/
│   └── certificates/
│       ├── _capability.py                 # moved from tests/integration/llm/
│       └── registry.py                    # moved from tests/integration/llm/
└── scripts/hatch/hatch_models_subset_builder.py    # NEW — custom builder
```

If a future ADR chooses to split `tolokaforge_models/` into its own repo, the cost is bounded: one `git filter-repo`, move the hatch custom builder to the new repo (converting it to a standard hatch wheel target, since the multi-target-in-one-monorepo constraint disappears), move the two publish workflows, remove the models hatch target block from the engine `pyproject.toml`. Zero changes to any caller of `tolokaforge.core.model_data.*` or `tolokaforge.testing.certify.*`. Zero changes to `presets.py` or `pricing.py`.

### Runner-subset interaction

The runner-subset wheel today explicitly includes `pricing.json` and `model_presets.yaml` via [`tolokaforge/core/_runner_subset.py:97,102`](../../tolokaforge/core/_runner_subset.py) (the [GH #830](https://github.com/Toloka/tolokaforge/issues/830) fix). After this ADR:

- `tolokaforge-runner-subset` gains `tolokaforge-models >= 1.0.0` as a pip dependency.
- The two data-file entries move out of `_runner_subset.py` and into a new `tolokaforge/core/_models_subset.py` partition file.
- [`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) `pip install`s the runner-subset wheel as today; `tolokaforge-models` is transitively resolved from PyPI (or from a local wheelhouse, when the Docker build runs on a monorepo checkout that also built the models wheel).
- The `MODEL_PRICING` populated-assertion at [`tests/canonical/test_runner_subset_install_smoke.py:472-497`](../../tests/canonical/test_runner_subset_install_smoke.py) still passes — its data source is now the transitively-installed `tolokaforge-models` package instead of vendored files. Positive-imports list gains `tolokaforge_models.data`.

The [ADR-0025](0025-runner-wheel-split.md) container command surface + Docker image name/tag axis are unchanged.

### Docs flip

[`docs/ADD_NEW_MODEL.md`](../ADD_NEW_MODEL.md) — the pre-flight table today routes `pricing.json`, `model_presets.yaml`, and `registry.py` all to `main` in `tolokaforge` with the overlay path documented below as an alternative. Post-this-ADR, out-of-tree becomes the documented default; the table's Branch column becomes `tolokaforge_models/` for the three data artifacts, and a **Bucket A vs. B** pre-flight decision block leads the doc:

- **Bucket A** — data-only change: preset routing composes existing policy classes, adjustments to price data, new capability certificate for a supported model. Target: `tolokaforge_models/`, dispatch: `release-models.yml`, cadence: independent.
- **Bucket B** — engine code change: genuinely new policy / codec / sanitizer *class*. Target: `tolokaforge/`, dispatch: `release.yml`, cadence: engine.

The overlay section (`--presets-file`) stays as the documented private / experimental escape hatch — it is *not* deprecated by this ADR.

### Auto-integration workflow retargeting

[`.github/workflows/integrate-model.yml`](../../.github/workflows/integrate-model.yml) today commits back to the engine branch when a Slack-triggered integration succeeds — the ADR-0002 § Context critique that "automation reinforces the coupling rather than relieving it." Post-this-ADR, the workflow classifies the candidate as Bucket A or Bucket B and commits to `tolokaforge_models/` when Bucket A. Bucket B falls through to the existing engine PR flow, unchanged. This is deliberately the *last* implementation step — the manual `tolokaforge_models/` path is proven end-to-end first.

### Promoting ADR-0002 to Accepted

[ADR-0002](0002-external-model-registry.md) is promoted from `Proposed` to `Accepted` in the same PR. Its Option 2 has been shipping and load-bearing since 2026-06-17; the delay in flipping status has left the seam looking experimental and, per ADR-0002's own § Follow-ups, has discouraged use. The "advanced by ADR-0030" back-link is added to ADR-0002's front matter alongside the status flip.

## Consequences

### Positive

- **Engine version stops moving on model onboarding.** Downstream consumers pin `tolokaforge` and stop drifting across versions purely as a side effect of when a model was added. "Which engine version did this run on" becomes a constant across compared models.
- **Bucket A cycle time collapses.** A price fix or new preset ships without a full engine release chain — cut `models-vX.Y.Z`, `pip install --upgrade tolokaforge-models`, done. The zero-content engine-release shims stop existing.
- **The seam is versioned and pinnable.** Consumers who want reproducibility pin both wheels; consumers who want the latest models let `tolokaforge-models` float and keep `tolokaforge` on a fixed engine.
- **Certification becomes a first-class public seam.** Both in-tree tests and out-of-tree model-data CI run the same test bodies — the engine remains authoritative about what "supported" means without owning the data.
- **Overlay stays intact.** `--presets-file` continues to work as the private / experimental escape hatch; nothing about the run-time seam changes.
- **Forward-compat is cheap.** A future repo split is a bounded, mechanical operation. The internal `model_data` seam and `testing.certify` API make the coupling replaceable in one place.
- **Preserves ADR-0025's clause for engine code.** The runner-subset stays Docker-only; this ADR does not extend the "publish subsets to PyPI" pattern to code artifacts.

### Negative / Trade-offs

- **Two publish workflows.** `release.yml` + `publish-tolokaforge.yml` for the engine wheel, plus new `release-models.yml` + `publish-tolokaforge-models.yml` for the models wheel. Two `cz bump` configurations. Discipline cost, not technical.
- **Runner-subset gains a pip dependency.** Runner Docker image size grows by the two data files (~65 KB uncompressed) plus `tolokaforge_models` wheel metadata. Bounded and one-directional.
- **Two-package install surface.** `pip install tolokaforge` now transitively brings `tolokaforge-models`. Users who want to avoid the models dependency for some reason cannot — this is deliberate; the engine is not functional without it.
- **API-compat guardrail depends on discipline.** `__api_version__` catches loader-contract breakage but not e.g. a policy-class rename that leaves the loader contract intact yet drops presets on the floor. Mitigation: the certification suite. The out-of-tree CI runs the certification bodies against every registered model on every `tolokaforge-models` release, so a silent-preset-drop condition surfaces as a failed certificate before publish.
- **Central-publisher governance concentrates on Toloka.** External contributors who want to add a model still open a PR against `Toloka/tolokaforge` (targeting `tolokaforge_models/`). Cycle-time improvement over today is real (small repo, small tests, no engine release), but there is no path for a third party to publish their own `tolokaforge-model-*` without Toloka merging it. This is the trade-off consciously taken for maintenance simplicity; a future ADR can flip it if the plug-in-ecosystem case emerges.
- **PyPI trusted-publisher setup required.** A new PyPI project (`tolokaforge-models`) needs a trusted-publisher entry linked to `Toloka/tolokaforge` + `publish-tolokaforge-models.yml`.

### Follow-ups

**Sub-issues under the umbrella tracked at [GH #645](https://github.com/Toloka/tolokaforge/issues/645)** — decompose via `/writing-development-tickets` after this ADR merges. Each maps to one PR; landing order below is chosen so each step unblocks the next.

1. **Fail-loud entry-point registry semantics.** Fix the [GH #544](https://github.com/Toloka/tolokaforge/issues/544) pattern on `tolokaforge.adapters` preventatively — the mechanism must be fail-loud on the day someone adds a second registered publisher. Small self-contained PR against [`tolokaforge/adapters/__init__.py`](../../tolokaforge/adapters/__init__.py) at line 86.
2. **Extract certification into a public seam.** Load-bearing — nothing else can be exercised out-of-tree until this ships. Two destinations, as the layout above shows: `_capability.py` (the `Capability` enum + `ModelCertificate` dataclass) and `registry.py` (`ALL_MODELS`) move to `tolokaforge_models/certificates/` — they have zero engine deps and belong with the data they describe. The conftest fixtures (`live_client`, `skip_unless_capability_declared`) and the ~30 `test_*.py` bodies move to `tolokaforge/testing/certify/` — they import engine code (`LLMClient`, `ModelConfig`, presets loader) and belong on that side. The engine seam re-exports `Capability` and `ModelCertificate` from `tolokaforge_models.certificates` so callers can `from tolokaforge.testing.certify import Capability, ModelCertificate` without depending on the models package directly. In-tree suite becomes a thin wrapper.
3. **Fingerprint in `engine_run_state.json`.** Independent — can ship at any point. Adds `models_fingerprint`.
4. **Create `tolokaforge-models` package and cut over the engine loader.** The main structural PR. Move the two data files, add `tolokaforge/core/model_data.py`, wire `presets.py` + `pricing.py`, add the hatch custom builder + partition file + canonical smoke, extend engine dep list, register PyPI project, update the runner-subset in the same PR.
5. **Docs flip.** Invert `docs/ADD_NEW_MODEL.md`. Docs-only.
6. **Auto-integration workflow retarget.** Repoint `integrate-model.yml` to commit against `tolokaforge_models/`. Last — the manual path is proven end-to-end first.

**Documentation to update** — [`docs/ADD_NEW_MODEL.md`](../ADD_NEW_MODEL.md) (bucket-A/B pre-flight + inverted default), [`docs/LLM_LAYER.md`](../LLM_LAYER.md) (model_data seam + certification-library note), [`docs/ROADMAP.md`](../ROADMAP.md) on next release event (this arc's status).

**Tests to add** — `tests/canonical/test_models_subset_partition.py` (mirror `test_runner_subset_partition.py`), `tests/canonical/test_models_subset_install_smoke.py` (mirror `test_runner_subset_install_smoke.py`, including the data-file populated-assertion pattern from [GH #830](https://github.com/Toloka/tolokaforge/issues/830)).

**Deferred / not this ADR:**

- Plugin ecosystem for third-party model-data publishers. The `model_data` seam is chosen so this can be layered on top by a future ADR without touching engine callers.
- Sidecar Python module for out-of-tree *policy classes* (ADR-0002 Option 4). Policy / codec / sanitizer classes stay engine code — that boundary is out of scope here, exactly as ADR-0002 stated.
- Extending the PyPI-publish pattern to any other engine subset (the runner-subset stays Docker-only per ADR-0025).

## Links

- Related ADRs:
  - [ADR-0002](0002-external-model-registry.md) — this ADR lands the packaged model-data artifact ADR-0002 § Context anticipated, deliberately narrower than ADR-0002's own Option 3 (single Toloka-published wheel, not a multi-publisher plugin registry). ADR-0002 is promoted from `Proposed` to `Accepted` in the same PR that lands this one.
  - [ADR-0025](0025-runner-wheel-split.md) — this ADR reaffirms ADR-0025's *"one PyPI wheel for engine code"* clause and explicitly does not extend the PyPI-publish pattern to the runner-subset. The two ADRs cover disjoint artifact categories: data (this ADR) and engine code (ADR-0025).
- Related code:
  - [`tolokaforge/core/llm/presets.py`](../../tolokaforge/core/llm/presets.py) — the preset loader whose data source shifts from an in-tree `core/data/` file to the new `tolokaforge.core.model_data` seam.
  - [`tolokaforge/core/pricing.py`](../../tolokaforge/core/pricing.py) — the pricing loader whose data source shifts the same way.
  - [`tolokaforge/core/engine_run_state.py`](../../tolokaforge/core/engine_run_state.py) — where the `models_fingerprint` field lands.
  - [`tests/integration/llm/registry.py`](../../tests/integration/llm/registry.py), [`tests/integration/llm/_capability.py`](../../tests/integration/llm/_capability.py), [`tests/integration/llm/conftest.py`](../../tests/integration/llm/conftest.py) — the certification harness that becomes `tolokaforge.testing.certify`.
  - [`scripts/hatch/hatch_runner_subset_builder.py`](../../scripts/hatch/hatch_runner_subset_builder.py) — the pattern the new `hatch_models_subset_builder.py` mirrors.
- Related issues:
  - [GH #645](https://github.com/Toloka/tolokaforge/issues/645) — the public issue that catalysed this ADR.
  - [GH #544](https://github.com/Toloka/tolokaforge/issues/544) — the fail-loud registry-collision pattern that follow-up (1) fixes.
  - [GH #353](https://github.com/Toloka/tolokaforge/issues/353) — pricing table location alignment; overlaps with follow-up (4).
  - [GH #830](https://github.com/Toloka/tolokaforge/issues/830) — the runner-subset data-file omission fix; its lesson (non-`.py` files must be listed explicitly) applies to the models-subset custom builder.
