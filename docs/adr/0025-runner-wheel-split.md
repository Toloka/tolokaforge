# 0025. Runner image slimming via a subset build target — one PyPI wheel, one runner-only Docker install

- **Status:** Proposed
- **Date:** 2026-08-03
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

M13 ([ADR-0022](0022-runtime-independence.md)) delivered runtime independence at the *code* level: entry-point registries, a `run_trial(...)` library entry, a JSON-Lines subprocess wire, and an import-boundary invariant test ([`tests/canonical/test_runner_import_boundary.py`](../../tests/canonical/test_runner_import_boundary.py)) that proves the runner's runtime imports never reach the orchestrator, adapters, or CLI. M14 ([ADR-0023](0023-runner-image-internals.md)) shipped the runner as a `docker pull`-able artifact on Docker Hub — but did so **monolithically**: the published image installs the full `tolokaforge` wheel plus its `[runner]` extra, physically shipping orchestrator / adapter / CLI `.py` files that the runner's runtime graph never loads. [ADR-0023](0023-runner-image-internals.md) explicitly scoped the M14 stability promise to the *image name + tag contract*, declined to commit the image internals, and deferred the wheel-carve conversation to this milestone (M15, umbrella [#622](https://github.com/Toloka/tolokaforge/issues/622); keystone review [#578](https://github.com/Toloka/tolokaforge/issues/578)).

Two forces make M15 due now:

- **The runner image is a compat surface**, even though its internals aren't. Every version we ship monolithic strengthens the informal expectation that the image internals look like they do today. [ADR-0023](0023-runner-image-internals.md) reserved the right to change internals, but the door narrows the longer we wait.
- **New grader work is starting immediately.** Grading logic lives on both sides of the future partition today: [`tolokaforge/core/grading/`](../../tolokaforge/core/grading/) (19 modules) + [`tolokaforge/core/evaluators/`](../../tolokaforge/core/evaluators/) + runner-side [`tolokaforge/runner/grading.py`](../../tolokaforge/runner/grading.py) + [`tolokaforge/runner/grading_ledger.py`](../../tolokaforge/runner/grading_ledger.py). `core/models.py` (2265 lines; imported by 45 files: 32 in `core/`, 5 in `runtime/reset_recipes/`, 3 in `dx/`, 3 in `adapters/`, 2 in `runner/`) and `runner/models.py` (1898 lines, still importing `ModelConfig` / `CriterionResult` / `LLMJudgeConfig` from `core.models`) duplicate wire types. New grader features on today's layout risk being written twice.

The direction is documented on [`docs/ROADMAP.md`](../ROADMAP.md), row for the runtime-independence arc: *"Runner as an independently-usable component… slim the runner Docker image so it installs a runner-only subset… **Same package, same wheel; no multi-package split.**"* This ADR specifies the implementation: a **second hatch build target** that produces a runner subset wheel built inside the Docker build for the runner image. The subset wheel is not published to PyPI; the published surface remains one `tolokaforge` wheel.

## Decision Drivers

- **One PyPI presence.** The published distribution stays `tolokaforge`, single wheel. Third-party consumers do not gain a `tolokaforge-core` or `tolokaforge-runner` PyPI target; the compat and discovery story stays one-sided.
- **Multiple Docker images are a first-class option.** [ADR-0023](0023-runner-image-internals.md) already declared image internals uncommitted precisely so slim-runner-image work like this could land without a compat event. The subset-wheel install is exactly the kind of internal change [ADR-0023](0023-runner-image-internals.md) reserved room for.
- **Runtime import boundary as the source of truth.** The existing invariant test ([`tests/canonical/test_runner_import_boundary.py`](../../tests/canonical/test_runner_import_boundary.py)) proves the runner's execution graph is clean; the subset build target makes the artifact match that truth. A CI-enforced positive smoke closes the loop the invariant test can't (i.e. "the subset actually contains everything the runner needs at runtime").
- **De-risk the upcoming grader milestone by removing the duplication first.** Any new grading feature added while `core/models.py` ↔ `runner/models.py` still overlap has to be written twice. The reconcile step lands before the image slim.
- **Preserve every M14 compat commitment.** The `docker.io/tolokasoft1/tolokaforge-runner` image name / tag axis ([ADR-0023](0023-runner-image-internals.md)) and the container command surface ([ADR-0024](0024-container-command-surface.md)) — entrypoint, healthcheck, env vars, exec subcommands — are unchanged.
- **Reversibility.** If the subset build target proves not worth the maintenance overhead, the Dockerfile can revert to installing the full `tolokaforge[runner]` without touching PyPI or consumer surfaces. If in a future milestone the plug-in-ergonomics case for a public `-core` wheel strengthens, this ADR is superseded, not built on top of.

## Considered Options

**Wheel shape — how many PyPI distributions?**

1. **One PyPI wheel + subset build target for the image (Option B).** `tolokaforge` remains the sole published distribution. A second hatch build target enumerates the runner's package graph and produces a subset wheel built inside the Docker build for the runner image only.
2. **One PyPI wheel + `[runner-only]` extras refactor (Option A).** Refactor `pyproject.toml` so `pip install tolokaforge` is minimal and orchestrator dependencies move behind extras. The runner image installs the bare wheel. Only slims the dependency tree, not the shipped `.py` files.
3. **Three published wheels — `-core` + `-runner` + `tolokaforge`.** Full PyPI split. Semantically cleanest but violates the "one PyPI wheel" constraint.
4. **Install-then-prune at Docker build time.** Keep one wheel; the runner Dockerfile installs it, then deletes files not reachable from the runner's import graph.
5. **Stay monolithic.** No M15 work. Accept that the runner image continues to ship code it never runs.

**Grader / models placement — what the ADR is asked to fix.**

1. **Reconcile inside `-core`-shaped code, delivered as part of M15.** Untangle `core/models.py` and collapse `runner/models.py` before the subset build target lands, so the subset consumes shared types.
2. **Reconcile separately, ship subset build target on top of the duplication.** Leaves the duplicated wire types in place; the upcoming grader milestone still has to write grading types twice.

## Decision

We adopt **Wheel-shape Option 1** and **Grader-placement Option 1**. Together they reverse the *"same package, same wheel"* clause of [ADR-0022 § Decision Drivers](0022-runtime-independence.md#decision-drivers) **only at the Docker-image level** — the PyPI-level "same package, same wheel" stands, and this ADR reaffirms it.

### Wheel shape — one PyPI wheel, subset build target for the image

The published PyPI surface is unchanged: one wheel, `tolokaforge`, containing everything. A second hatch build target is added to [`pyproject.toml`](../../pyproject.toml):

```toml
[tool.hatch.build.targets.wheel]
packages = ["tolokaforge"]

[tool.hatch.build.targets.wheel.runner-subset]
# built only inside the runner Docker build; not published to PyPI
packages = [
    "tolokaforge/runner",
    "tolokaforge/secrets",
    "tolokaforge/tools",
    # tolokaforge/core — enumerated at submodule level in sub-issue (3);
    # today's core/ mixes shared-spine and orchestrator-only modules
    # (see § "The module partition" below).
]
```

(The exact hatch-multi-target syntax is a step-4 implementation call — the shape above illustrates the enumeration; hatch may prefer a separate `[tool.hatch.envs.…]` or `[tool.hatch.build.targets.custom]` layout, and the `core/` enumeration depends on the reorganization decided by sub-issue (3).)

The runner subset enumerates the packages the runner's runtime graph reaches: `tolokaforge.runner` (the service, gRPC glue, tool factory, db/rag clients, runner-side grading), `tolokaforge.core` (post-reconcile: shared wire types + LLM + grading substrate + trial contract + Protocols + evaluators), `tolokaforge.secrets` (universal secret abstraction), and `tolokaforge.tools` (registry + built-in tool drivers).

The subset wheel is a **local build artifact**, not a PyPI presence. It is produced during the Docker build stage (see below) and installed into the runtime stage from the local wheelhouse. Third-party consumers cannot `pip install tolokaforge-runner-subset` from PyPI; the constraint is deliberate and stated in [ADR-0022 § Decision Drivers](0022-runtime-independence.md#decision-drivers) and on the public [`docs/ROADMAP.md`](../ROADMAP.md) row for the runtime-independence arc: *"Same package, same wheel; no multi-package split."*

**Options 2–5 are rejected:**

- **Option 2 (`[runner-only]` extras refactor)** slims only the dependency tree, not the shipped Python files. Every `.py` in `tolokaforge/` still lands in the runner image's `site-packages/`. It also reverses the current install pattern (today `pip install tolokaforge` = everything), which is a broader migration than the goal warrants.
- **Option 3 (three published wheels)** violates the "one PyPI wheel; no multi-package split" constraint documented on [`docs/ROADMAP.md`](../ROADMAP.md).
- **Option 4 (install-then-prune)** is brittle. A new intra-package import silently drops files from the image without warning. A hatch build target has explicit package enumeration and manifest-level enforcement.
- **Option 5 (stay monolithic)** leaves the artifact-level cleanup undone and lets the "runner image ships code it doesn't run" gap widen.

### The module partition

The subset build target's package list assumes a documentable partition across `tolokaforge/`. The partition is directory-level for the clean cases (`runner/`, `secrets/`, `tools/`, `adapters/`, `dx/`, `docker/`, `env/`) and **submodule-level for `core/`** — today `tolokaforge/core/` mixes the shared spine (models, LLM, grading substrate, Protocols, evaluators) with orchestrator-only modules (the `Orchestrator` class at [`tolokaforge/core/orchestrator.py:326`](../../tolokaforge/core/orchestrator.py), dry-run, output writer, config validator, compose materialisation, engine run state, backend-selection logic, `RuntimeBackend` implementations). Sub-issue (3) locks the exact partition; it may require reorganizing `core/` into a shared-spine subpackage and an orchestrator subpackage — or an equivalent file-level enumeration — so the hatch target in sub-issue (4) can name stable directory boundaries.

**Belongs in the runner subset (the runner's runtime graph reaches these):**

- `tolokaforge/runner/` — `service.py`, `__main__.py`, `db_client.py`, `db_proxy.py`, `rag_client.py`, `tool_factory.py`, `protocol.py`, `capabilities.py`, `id_resolution.py`, `grading.py`, `grading_ledger.py`, `runner.proto`, `runner_pb2.py`, `runner_pb2_grpc.py`, and (post-reconcile) whatever remains of `runner/models.py`.
- `tolokaforge/secrets/` — `SecretManager` singleton + providers (`AGENTS.md` § Secrets — single abstraction).
- `tolokaforge/tools/` — `registry.py`, `builtin/` drivers, `persistent_shell.py`, `str_replace_editor.py`, `user_tools.py`. Used by both `adapters/native/` (in the full wheel) and `runner/tool_factory.py` (in the subset).
- **Selected `tolokaforge/core/` submodules** — post the untangle in sub-issue (1): the split `models` submodules, plus `trial.py`, `runtime.py` (`RuntimeBackend` Protocol), `conductor.py` (`Conductor` Protocol), `trial_grader.py` (`TrialGrader` Protocol), `run_display_events.py` ([ADR-0019](0019-front-end-plugin-namespace.md) seam), `pricing.py`, `loop.py` (`ToolCallingLoop`), `core/llm/` (16 modules — LLM-as-judge executes in-runner), `core/grading/` (19 modules — grading substrate), and shared utility modules the closure reaches (`logging.py`, `logging_context.py`, `hash.py`, etc. — enumerated by (3)).

**Not in the runner subset (base wheel only):**

- `tolokaforge/_entry.py` — the console-script shim.
- **Orchestrator-side `tolokaforge/core/` submodules** — `core/orchestrator.py` (the `Orchestrator` class), `core/dry_run.py`, `core/output_writer.py`, `core/output/`, `core/config_validator.py`, `core/compose_materialisation.py`, `core/engine_run_state.py`, `core/backend_capabilities.py`, `core/shared_stack_runtime.py`, `core/per_trial_runtime.py`. Their entry-point factories (registered in [`pyproject.toml:140-150`](../../pyproject.toml)) stay wired from the base wheel.
- `tolokaforge/adapters/` — `native`, `tau`, `tlk_mcp_core`, `terminal_bench` (external adapter).
- `tolokaforge/dx/` — CLI (Click command tree, `run_trial_command.py`, panels) behind the `[dx]` extra.
- `tolokaforge/docker/`, `tolokaforge/env/`.

**Modules under `tolokaforge/core/` not classified above** — `assets.py`, `budgets.py`, `deprecations.py`, `docker_adapter.py`, `duration.py`, `env_identity.py`, `env_state.py`, `failure_attribution.py`, `metrics.py`, `mounts.py`, `netpolicy_constants.py`, and any siblings — are classified in sub-issue (3) by walking the runner's actual runtime import closure. Modules the closure reaches join the subset; modules it doesn't stay base-wheel-only.

**Non-goal — grader semantics.** This ADR relocates nothing about how grading computes. Semantic changes are the grader milestone ([#684](https://github.com/Toloka/tolokaforge/issues/684)) — landing after sub-issue (2) closes.

### Runner Dockerfile — multi-stage subset install

[`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) becomes a multi-stage build:

1. **Builder stage.** Runs `hatch build --target runner-subset` (or the equivalent uv invocation) against the source tree, producing the subset wheel into a local wheelhouse.
2. **Runtime stage.** `pip install /wheelhouse/tolokaforge_runner_subset-<version>-py3-none-any.whl` plus the domain-tool runtime deps currently declared in the `[runner]` extra ([`pyproject.toml:103-112`](../../pyproject.toml)) — those deps stay named in one place; the subset build's `dependencies` list enumerates them.

Every element of the container command surface ([ADR-0024](0024-container-command-surface.md)) is preserved: the default `CMD ["python", "-m", "tolokaforge.runner"]`, the gRPC healthcheck on `:50051`, the `TOLOKAFORGE_SECRETS_JSON` / `DB_SERVICE_URL` / `RAG_SERVICE_URL` env contract, the `tolokaforge run-trial` and `tolokaforge --version` exec subcommands. The image name and tag axis (`docker.io/tolokasoft1/tolokaforge-runner:X.Y.Z` immutable, `:X.Y` + `:latest` moving) is unchanged.

**Non-runner service images are out of scope for M15.** The `db-service`, `rag-service`, and `mock-web` images keep their current install patterns (rag-service on the full base wheel because it uses `secrets` + `env.rag_service`; db-service and mock-web wheel-less). Extending the subset-build pattern to those services is a separate future milestone if the runner-subset pattern proves out.

### Version coupling — one wheel, one version

There is one PyPI wheel to publish, so the version-coupling question doesn't arise at the PyPI level. The subset wheel built inside the Docker build carries the same `tolokaforge` version as the published wheel — it's the same source tree, same commit, same version string.

### Publish workflow — unchanged shape

`publish-images.yml` (`AGENTS.md` § CI / GitHub Actions table) continues to publish one PyPI wheel + four Docker images per tag. The only change is inside the runner image's build steps: install the locally-built subset wheel instead of `pip install tolokaforge[runner]`. No new PyPI publish steps; no additional secrets; the rc-smoke against the built image continues to lock the [ADR-0024](0024-container-command-surface.md) command surface.

### Open-agent-loop work — frozen

The "Open agent loop" row on [`docs/ROADMAP.md`](../ROADMAP.md) — streaming event emission on `Conductor` plus an opt-in `ConductorControl` Protocol for pause/resume + external-message injection — **is frozen as of 2026-08-03** and does not proceed until the freeze is lifted. Recorded here so future readers know the arc is intentional-pause, not forgotten; the roadmap row will be updated on the next release event to reflect this state per the `docs/ROADMAP.md` update convention.

## Consequences

### Positive

- **Runner image contains only code the runner runs.** The "runner as an independent component" claim becomes honest at the artifact level, matching the code-level truth the invariant test already enforces.
- **The upcoming grader milestone gets a clean substrate.** After sub-issue (2) (models reconcile) merges, new grading logic goes into `tolokaforge.core.grading` (soon inside the runner subset) once; both orchestrator and runner consume it via the shared package.
- **The invariant test grows a positive check.** Sub-issue (6) extends [`tests/canonical/test_runner_import_boundary.py`](../../tests/canonical/test_runner_import_boundary.py) to build the subset wheel, install it into a clean venv, and boot-smoke the runner. Any module the runner actually reaches at runtime but that the subset omits fails CI.
- **PyPI-side simplicity preserved.** No new distribution to name, version, or document; `pip install tolokaforge` and `pip install tolokaforge[runner]` continue to mean what they mean today.
- **Reversible.** The subset build target is a `pyproject.toml` addition + a Dockerfile change. If it doesn't earn its maintenance overhead, both revert cleanly.
- **Sets a pattern for future service slimming.** The subset-build approach generalises to `db-service`, `rag-service`, and `mock-web` if the pattern proves out — deferred as an explicit follow-up.

### Negative / Trade-offs

- **Two build targets in `pyproject.toml`.** Adding a new top-level module means deciding whether it belongs in the runner subset. Discipline problem, not a technical one; sub-issue (6)'s positive smoke catches the omission case.
- **Docker build gets a wheel-build step.** Multi-stage build adds ~30-60s to `runner.Dockerfile` in exchange for the slim image. Bounded; layer caching mitigates on rebuild.
- **The subset wheel is a Docker-only artifact.** A contributor debugging install issues against `pip install tolokaforge-runner-subset` from PyPI will find it doesn't exist. Documented in [`docs/RUNNER.md`](../RUNNER.md) and [`docs/STANDALONE_RUNNER.md`](../STANDALONE_RUNNER.md).
- **Third-party plug-in authors installing to import a Protocol still `pip install tolokaforge`.** No slimmer install path for out-of-tree plug-ins exists under this ADR. If a plug-in ecosystem case for a public `-core` wheel emerges, that is a future superseding ADR, not a Layer 2 addition to this one.

### Follow-ups

**Sub-issues under [#622](https://github.com/Toloka/tolokaforge/issues/622)** — decompose via `/writing-development-tickets` after this ADR merges:

1. Untangle `tolokaforge/core/models.py` (2265 lines) into per-concern submodules (`grade`, `trajectory`, `task_config`, `model_config`, `grade_components`, `run_config`), with a re-export shim for a soft-migration window.
2. Reconcile `tolokaforge/runner/models.py` (1898 lines) — collapse duplicated wire types onto `core.models`; keep only runner-specific extensions.
3. Module partition audit — walk the runner's runtime import closure, classify every `tolokaforge/core/` submodule as shared-spine vs orchestrator-only, and reorganize `core/` if a stable directory boundary requires it (see § "The module partition"). Deliverable: the concrete `packages = [...]` enumeration that sub-issue (4) drops into `pyproject.toml`.
4. Add the `tolokaforge-runner-subset` hatch build target in `pyproject.toml` enumerating the runner's package graph.
5. Re-point `tolokaforge/docker/dockerfiles/runner.Dockerfile` to a multi-stage build installing the subset wheel; preserve every [ADR-0024](0024-container-command-surface.md) image contract element.
6. Extend `tests/canonical/test_runner_import_boundary.py` with a positive subset-install smoke.

Ordering: (1) → (2) unblocks grader work. Then (3) → (4) → (5) → (6) deliver the slim image and can proceed alongside grader code work.

**Documentation to update** — [`docs/STANDALONE_RUNNER.md`](../STANDALONE_RUNNER.md) (runner image now installs the subset wheel), [`docs/RUNNER.md`](../RUNNER.md) (build story), [`docs/ROADMAP.md`](../ROADMAP.md) on next release event (open-agent-loop freeze status; M15 status flip).

**Tests to add** — the positive subset-install smoke in step (6). rc-smoke against a published image already locks the [ADR-0024](0024-container-command-surface.md) surface and works unchanged under the split.

**Deferred / not this ADR:**

- Extending the subset-build pattern to `db-service` / `rag-service` / `mock-web` — separate milestone if runner subset proves out.
- Publishing `-core` or `-runner` to PyPI — off the table per the "one PyPI wheel" constraint. Reversible via a superseding ADR if the plug-in ecosystem case strengthens.
- Open-agent-loop work — frozen (see § *Open-agent-loop work — frozen* above).

## Links

- Related ADRs:
  - [ADR-0022](0022-runtime-independence.md) — runtime independence at the code level. This ADR reverses its § Decision Drivers *"Same package, same wheel"* clause **only at the image level**; the PyPI-level clause stands.
  - [ADR-0023](0023-runner-image-internals.md) — image internals uncommitted. This ADR is the internal change [ADR-0023](0023-runner-image-internals.md) reserved compat room for.
  - [ADR-0024](0024-container-command-surface.md) — the container command surface this ADR preserves in full.
  - [ADR-0003](0003-trial-spec-and-trial-result.md) / [ADR-0007](0007-runtime-backend-protocol.md) / [ADR-0008](0008-conductor-protocol.md) / [ADR-0014](0014-trial-grader-protocol.md) / [ADR-0019](0019-front-end-plugin-namespace.md) — seams whose Protocol definitions live in the runner subset (all under `tolokaforge/core/`).
- Related code:
  - [`tests/canonical/test_runner_import_boundary.py`](../../tests/canonical/test_runner_import_boundary.py) — the module-graph invariant; sub-issue (6) adds a positive subset-install smoke.
  - [`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) — the multi-stage build target of sub-issue (5).
  - [`pyproject.toml`](../../pyproject.toml) — where the second hatch build target lands (sub-issue 4).
- Related issues:
  - [GH #578](https://github.com/Toloka/tolokaforge/issues/578) — this ADR's originating review.
  - [GH #622](https://github.com/Toloka/tolokaforge/issues/622) — M15 umbrella; the six sub-issues above.
  - [GH #610](https://github.com/Toloka/tolokaforge/issues/610) — M14 umbrella (the shipped monolithic image whose internals this ADR slims).
