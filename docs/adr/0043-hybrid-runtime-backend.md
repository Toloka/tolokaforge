# 0043. HybridRuntimeBackend — shared engine services + per-trial task compose

- **Status:** Proposed
- **Date:** 2026-08-31
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —
- **Extends:** [ADR-0018](0018-multi-container-under-shared-runtime.md) — fills the "shared engine substrate + per-trial task compose" quadrant that ADR-0018's 2×2 does not cover.
- **Related:** [ADR-0016](0016-runtime-backend-comparison.md) (runtime-backend trade-offs), [ADR-0022](0022-runtime-independence.md) (task-driven backend selection).

## TL;DR

`SharedStackRuntimeBackend` materialises one substrate for the whole run; `PerTrialRuntimeBackend` materialises everything per trial. Task packs that ship **one docker-compose file per task** (canonical example: T-Bench balanced-10, ten different compose files) force onto `per_trial` — which pays full compose-up + docker-compose-down cost every trial. This ADR introduces a third `RuntimeBackend`, `HybridRuntimeBackend`, which materialises the shared **engine** services (runner + db-service + rag-service if present) **once per run** and the task-declared services (postgres, redis, whatever the task's own compose declares) **per trial**. Backend selection is task-driven via a new `EnvironmentManifest.requires_hybrid_stack` signal derived from the same per-service `isolation` vocabulary ADR-0018's amendment already ships.

## Context and Problem Statement

The 2026-08-30 engine-loop `kimi_k3` sample on `eval/tbench-balanced-10-engine-loop` (110 trials, 10 T-Bench tasks × 11 repeats) landed:

- **92.7% pass rate** (104/110 graded) — validity comparable to Harbor.
- **$103.33** total cost — inside the $50–150 sample-stage envelope.
- **66 hours wall-clock per model**. Harbor on the same task set runs in ~2 hours per model.

The 30× wall-clock gap is not a bug and not per-trial LLM work — engine-loop is actually 2× **faster** per trial than Harbor for the LLM turns (median 449 s vs 990 s). It is per-trial **substrate overhead**:

- Every trial pays a fixed ~20 s: `docker compose up --wait` + readiness gate (~11 s) + teardown (`docker compose down --volumes` + `rmtree`, ~10 s). Across 660 trial attempts on the sample sweep, that is ~13 hours pure overhead.
- The runner container image itself is built per shard (~5–10 min on cold Docker cache) rather than once per run.
- `SharedStackRuntimeBackend`'s task-declared-stack path (Case B in ADR-0018) refuses runs whose tasks name different `environment_manifest.compose_file` values — the check at `orchestrator.py:1090` raises `RuntimeError`. T-Bench declares ten different compose files → shared-stack is unusable.

Harbor achieves its ~2 h wall-clock via three architecture wins (not container reuse — Harbor also runs a fresh compose project per trial):

1. **One Python process per invocation** with `asyncio.Semaphore(N)` concurrency — no gRPC round-trip per trial.
2. **Image-build lock keyed by image name** — deduplicates concurrent builds of the same task image.
3. **Docker daemon image-layer cache shared automatically** across all trials in the invocation.

Point (1) is orthogonal to the runtime-backend seam (it belongs on the conductor/orchestrator layer). Points (2) and (3) apply to any well-behaved compose runner. But **the biggest local win we do not currently have is not rebuilding the runner + engine services per trial** — and that is a runtime-backend concern.

The desired shape: **shared runner + engine services materialised once per run; task-declared services materialised per trial**. Neither `SharedStackRuntimeBackend` (one substrate per run, no per-trial substrate mutation) nor `PerTrialRuntimeBackend` (everything per trial including the runner) implements this. It is the missing quadrant in the ADR-0018 2×2.

## Decision Drivers

- **The vocabulary already exists.** ADR-0018's amendment defines per-service isolation levels — `shared`, `reset`, `ephemeral` — on `EnvironmentManifest.services.<name>.isolation`. Today's backends read the aggregate signal `requires_per_trial` from that map. The hybrid case is *native to that vocabulary*: a manifest whose `services` map declares a **mix** of `shared` and `reset|ephemeral` services is unambiguously requesting the hybrid shape. No new isolation levels needed.
- **The runner already knows how to spawn peer containers.** `mount_docker_socket_into_runner` (`compose_materialisation.py:365`) bind-mounts the host docker socket into the runner container. Both existing backends use it. A hybrid backend uses it too — the shared runner then spawns per-trial task-container compose projects via `docker exec`.
- **`SharedStackRuntimeBackend.provision/endpoints/teardown` are Protocol-conformant no-ops today** (`shared_stack_runtime.py:1272-1304`). The provisioning surface exists at the shared-substrate layer — a hybrid backend can put per-trial task-container bring-up there while keeping the shared runner endpoints stable.
- **Grading validity is preserved.** Every T-Bench task's grader asserts on agent-mutated state (billing ledger, hold rows, transaction records). Sharing state across trials would produce non-deterministic grades. The hybrid backend keeps per-trial isolation on the task-declared services — the shared side is only the runner + engine services, which are stateless per trial.
- **Backend selection stays task-driven.** ADR-0022 established that `orchestrator.runtime` is a deprecated override and the real signal is the task manifest. The hybrid decision is the same shape: the T-Bench adapter grows a `services` map declaring which services are shared vs per-trial, the orchestrator reads it, the right backend is selected.

## Considered Options

### Option A: Retrofit `SharedStackRuntimeBackend` to support per-trial task-container bring-up

Add `provision(spec)` and `teardown(handle)` implementations to `SharedStackRuntimeBackend` that materialise per-trial sub-composes on top of the shared substrate. Reuse the class.

**Rejected.** The class-invariant "one substrate per run" is load-bearing in its docstring, its provision/teardown contract, and its capability advertisement (`{shared_stack}`, single mode). Retrofitting to support per-trial substrate mutation would bloat the invariant into "one substrate per run OR shared-plus-per-trial-sub-substrate depending on manifest shape", which reads as two backends inside one class. Regression risk on the existing shared path is real — a backend that already ships needs its behavior held stable, not widened.

### Option B: New `IsolationMode` enum value alone (no new backend class)

Add `HYBRID_STACK` to `IsolationMode` and wire selection accordingly, but keep implementation dispatch entangled with the existing two backends via conditionals.

**Rejected.** Isolates the naming problem from the code that owns the behavior. The 2×2 grows to a 3×1 with no explicit implementer for the new column. Future readers wondering "where does hybrid actually get provisioned?" find no single class. Explicit backend is easier to reason about and easier to test.

### Option C (chosen): New `HybridRuntimeBackend` class

A third `RuntimeBackend` implementation that composes existing primitives:

- Shared-run lifecycle (`connect` / `close`) borrowed from `SharedStackRuntimeBackend._materialise_manifest` — brings up runner + engine services once per run.
- Per-trial lifecycle (`provision` / `teardown`) borrowed from `PerTrialRuntimeBackend.provision` — brings up ONLY the task-declared services per trial, skipping runner + db-service (already up).
- Uses `mount_docker_socket_into_runner` so the shared runner reaches task containers via `docker exec`.
- Advertises new capability `hybrid_stack` plus the reset-recipe and network-isolation capabilities the per-trial backend already advertises.

**Accepted.** New backend, minimal duplication (composes existing helpers), clean 3rd column on the 2×2, ships behind a distinct capability admission signal.

## Decision

Adopt Option C. Concrete concerns:

1. **New backend class**: `tolokaforge.core.hybrid_runtime.HybridRuntimeBackend`. Advertises `advertised_capabilities = {hybrid_stack, reset_recipes:sql_dump, reset_recipes:filesystem_dir, reset_recipes:redis_dump, reset_recipes:bare, network_isolation:no_internet, network_isolation:limited_internet}`. `isolation_mode = IsolationMode.HYBRID_STACK` (new enum member).

2. **New capability**: `hybrid_stack` in `backend_capabilities.CAPABILITY_REGISTRY`. Admission rule: a task requires `hybrid_stack` if its manifest's `services` map declares AT LEAST ONE service with `isolation: shared` AND AT LEAST ONE service with `isolation: reset|ephemeral`. Pure-shared and pure-per-trial task packs continue to route to their existing backends unchanged.

3. **New manifest signal**: `EnvironmentManifest.requires_hybrid_stack: bool` property. Semantic mirrors the admission rule above. `requires_per_trial` semantics stay unchanged for backward compatibility with existing per-trial-only task packs.

4. **Backend selection**: `_select_backend_from_tasks` (in `orchestrator.py`) grows a new branch — if any task manifest reports `requires_hybrid_stack`, admit hybrid; else fall back to today's shared-vs-per_trial decision. Heterogeneous compose-file check in `_extract_run_env_manifest` is widened: under hybrid, the *engine-services* manifest is one (materialised once per run), and the *task-services* manifests are per-trial (materialised per provision call, may differ per task). The check retains its shared-backend-only enforcement for non-hybrid runs.

5. **Isolation compatibility**: `_verify_isolation_compatibility` accepts `ephemeral` services under `hybrid_stack` (already accepted under per_trial today). Refusal for `SharedStackRuntimeBackend` + `ephemeral` stays intact — that combination is still invalid.

6. **Grading validity contract**: the hybrid backend runs task-declared services per trial from scratch. Grading correctness properties any T-Bench task depends on (fresh DB, fresh filesystem, fresh volumes per trial) are preserved. The shared side of the hybrid materialises only stateless engine services (runner + db-service + rag-service if present) — services whose state is not read by any grader.

7. **Per-service default isolation stays `ephemeral`.** T-Bench adapter opt-in is explicit: the adapter's `_environment_patch` grows a `services` map declaring runner + engine services as `isolation: shared` and task-declared services as `isolation: ephemeral`. Task packs that do not declare a `services` map continue to route to `per_trial` (the default). No silent behavior change for existing tasks.

## Case study — `terminal_bench_balanced_10` under hybrid

Today (per_trial):
- 10 tasks × 11 repeats = 110 trials.
- Every trial: rebuild runner image (once per shard on cold cache), materialise the task's docker-compose stack (~11 s), run agent turns, grade, tear down.
- Per-trial substrate cost: ~20 s × 660 attempts = ~3.7 hours pure overhead across the sweep.
- Per-shard runner image build cost: ~5–10 min on cold cache × 6 shards.

Under hybrid:
- Once per run: build runner image + engine services, `docker compose up --wait` on the shared side. Cost ~5–10 min, amortised across all trials.
- Per trial: `docker compose up --wait` on the per-task compose only (postgres + billing service + tests service for `fix-billing-holds`; scales similarly for the other 9). No runner rebuild. No db-service rebuild.
- Estimated per-trial overhead: ~7 s (task-compose only, no runner startup) vs ~20 s under per_trial.
- Estimated wall-clock savings on 660 attempts: ~2.4 hours + eliminated per-shard runner rebuilds.

## Consequences

### Positive

- **Wall-clock parity with Harbor becomes reachable** on multi-compose-file task packs without regressing grading validity.
- **The 2×2 has an explicit implementer for every quadrant.** Reviewers wondering "where does the hybrid case live?" find a named class.
- **The `services` isolation vocabulary from ADR-0018's amendment gets exercised in production**, proving out the design.
- **No breaking change** for existing task packs. Manifests without a `services` map continue to route to `per_trial` (today's default). All-shared manifests continue to route to `shared_stack`. Only mixed manifests get the new backend.

### Negative

- **Third backend to maintain.** The hybrid class shares helpers with the existing two but adds a code path that reviewers must reason about.
- **`mount_docker_socket_into_runner` becomes load-bearing under hybrid.** The shared runner reaches per-trial task containers via the host docker socket. Any regression to that primitive breaks hybrid runs.
- **Debug story is more complex.** A hybrid run has a live shared runner + N per-trial task compose projects at any time. Troubleshooting a stuck trial requires knowing which layer failed.

### Neutral

- **Capability registry grows by one entry** (`hybrid_stack`). Same shape as existing entries.
- **`IsolationMode` enum grows by one value** (`HYBRID_STACK`). Same shape as existing values.

## Follow-ups

- The wall-clock savings from (2) — task-container image-build lock keyed by image name — and (3) — automatic Docker layer cache — apply orthogonally to any runtime backend. Prime image cache per shard and add an image-build lock in the compose materialisation path as separate improvements (out of scope for this ADR).
- The Harbor architecture win (1) — one Python process for all trials in an invocation — belongs at the orchestrator/conductor layer, not the runtime backend. Separate proposal.
- If task-container compose bring-up becomes the dominant per-trial cost after hybrid ships, warm-container-pool (checkout a container from a pool instead of `docker compose up` from scratch) is the next architectural lever. Out of scope for this ADR.

## References

- [ADR-0016](0016-runtime-backend-comparison.md) — runtime-backend trade-offs.
- [ADR-0018](0018-multi-container-under-shared-runtime.md) — multi-container under shared runtime; per-service isolation vocabulary; 2×2 this ADR extends.
- [ADR-0022](0022-runtime-independence.md) — task-driven backend selection.
- Epic (proposal): https://github.com/Toloka/tolokaforge/issues/1336
- Milestone (implementation): https://github.com/Toloka/tolokaforge/milestone/40
