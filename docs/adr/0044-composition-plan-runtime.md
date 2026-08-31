# 0044. Composition-plan runtime — redesigned `SharedStackRuntimeBackend`

- **Status:** Proposed
- **Date:** 2026-08-31
- **Deciders:** @CiroGamboa
- **Supersedes:** — (replaces an unshipped ADR-0043 draft — `HybridRuntimeBackend` as a distinct class — that was never merged to `main`; number 0043 is intentionally skipped, see `docs/adr/README.md` index note)
- **Superseded by:** —
- **Extends:** [ADR-0018](0018-multi-container-under-shared-runtime.md), [ADR-0022](0022-runtime-independence.md), [ADR-0040](0040-standalone-grader.md)

## TL;DR

Add a first-class **composition plan** to `EnvironmentManifest`: a list of `StackDecl` entries, each with `stack_scope: Literal["run","task","trial"]`, so one manifest can declare multiple compose files that persist at different lifecycle scopes. `SharedStackRuntimeBackend` becomes the sole compose-mode backend, driven by three detachable adapter Protocols (`ComposeMaterialiser`, `ServiceLifecycleDispatcher`, `SubstrateComposer`) discovered through entry-point groups. `PerTrialRuntimeBackend` is retained as a thin preset factory (single-stack `trial`-scope plan). `HybridRuntimeBackend` — proposed by the superseded ADR-0043 — never ships. Existing `ServiceIsolation` closed vocab `{shared, reset, ephemeral}` is preserved. Backward compat is a HARD invariant: manifests with scalar `compose_file` and no `stacks` block continue to work byte-identically.

## Context and Problem Statement

The 2026-08-30 engine-loop `kimi_k3` sample on `eval/tbench-balanced-10-engine-loop` produced 92.7% pass rate (104/110 graded) on \$103 but took **66 hours** per model wall-clock. Harbor runs the same task set in ~2 hours. The 30× gap is not per-trial LLM work (engine-loop is 2× faster per-trial for LLM turns) but per-trial **substrate overhead**:

- Every trial pays ~20 s of `docker compose up --wait` + teardown under `PerTrialRuntimeBackend`.
- The runner + engine services rebuild once per shard, then per trial.
- `SharedStackRuntimeBackend` refuses multi-compose-file task packs — T-Bench's balanced-10 declares 10 different compose files (one per task), so the shared model is unusable.

**ADR-0043** proposed `HybridRuntimeBackend` — a new class filling one specific 2-stack topology (shared engine services + per-trial task compose). Three PRs merged onto its integration branch. Empirical baseline confirmed the design's problem statement was correct, but the abstraction was wrong.

**The wrong abstraction: class.** A dedicated `HybridRuntimeBackend` class encodes one specific 2-stack topology into a class name. It does not generalise to N stacks. It duplicates lifecycle bracketing across three backends. It does not accommodate future variants (K8s materialiser, `task_shared` scope, snapshot-based reset) without more new classes.

**The right abstraction: scope.** A compose file has a lifecycle scope (`run` / `task` / `trial`) — the same way a service has an isolation label. A manifest can declare multiple compose files, each at its own scope. One backend executes the plan.

## Decision Drivers

- **The vocabulary is already close.** ADR-0018's amendment ships per-service `isolation: shared | reset | ephemeral`. The missing axis is *per-compose-file* scope, not another per-service value.
- **The existing seams already generalise cleanly.** `RECIPE_REGISTRY` in `tolokaforge/runtime/reset_recipes/__init__.py:41-47` is a strategy-dispatch registry keyed by a closed vocab (`SeedKind`). The same pattern applies to per-service between-trial lifecycle. `tolokaforge/core/plugin_registry.py:319-352` (`discover_entry_points` / `_load`) already handles factory discovery for `RuntimeBackend`, `TrialGrader`, `Conductor`, etc. — reuse for the new seams.
- **Two backends are enough.** Once composition plan is a first-class field, "shared" and "per_trial" are just extreme points on the scope axis. `PerTrialRuntimeBackend` collapses to a thin preset factory. `HybridRuntimeBackend` does not exist as a class — it becomes a two-stack plan.
- **State-contamination invariants are load-bearing.** Multi-compose-file support opens 9 new failure modes (container-name collision, volume collision, runner endpoint ambiguity, reset-recipe scope drift, network cross-reachability, fixed host-port collisions, `limited_internet_allowlist` merge ambiguity, credential-injection blast radius, log-router bookkeeping). The redesign preserves the 12 existing invariants and closes each new failure mode via named enforcement points.
- **Backward compat is a HARD invariant.** Every existing task pack continues to work byte-identically. Scalar `compose_file` is a valid ergonomic single-stack representation, not a deprecated form.
- **Design for far-future extensions.** The seams accommodate: K8s / Modal materialiser (register in `tolokaforge.compose_materialisers`); `task_shared` scope (extend `stack_scope` Literal + register `TaskScopeDispatcher`); snapshot-based isolation (register a new dispatcher under existing `reset` label). All without new backend classes.

## Considered Options

### Option A: Keep `HybridRuntimeBackend` as-is (ADR-0043)

**Rejected.** Ossifies one 2-stack topology into a class name. Duplicates lifecycle bracketing across three backends. No extension story for N-stack or future substrates.

### Option B: Minimal patch to `SharedStackRuntimeBackend` — accept a list of compose files but keep scope as ambient convention

**Rejected.** Hides state-contamination invariants behind ordering conventions ("first file is shared"). Every new failure mode multi-compose opens returns. No structured extension story.

### Option C: One god-backend, no `PerTrialRuntimeBackend`

**Rejected.** Churns import surface unnecessarily. Third-party tools and task packs already reference `PerTrialRuntimeBackend`. A 20-LOC preset factory keeps the import identity while collapsing the implementation.

### Option D (chosen): Composition plan as a first-class manifest field + one flexible backend + three detachable adapter Protocols

**Accepted.** Scope becomes a property of a compose file (not a class). One class executes the plan. Three detachable adapters open the design for far-future substrates and lifecycle variants. Two backends survive (SharedStack + PerTrial-as-preset). Backward compat is a HARD invariant via scalar↔plan coercion.

## Decision

Adopt Option D. Concrete concerns follow.

### 1. Manifest surface — `EnvironmentManifest.stacks: list[StackDecl]`

New type `StackDecl` (in `tolokaforge/runner/models.py`):

```
class StackDecl(BaseModel):
    stack_id: str                            # unique within the manifest
    compose_file: Path                       # required, absolute
    stack_scope: Literal["run","task","trial"]
    runner_service: str | None = None        # exactly one stack in the plan sets this
    inputs: dict[str, str] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}
```

New type `CompositionPlan` (a validated ordered list of `StackDecl`).

New enum `PlanShape`:

```
class PlanShape(str, Enum):
    SINGLE_RUN          = "single_run"           # one run-scope stack, no others
    TASK_SCOPED_ONLY    = "task_scoped_only"     # only task-scope stacks
    TRIAL_SCOPED_ONLY   = "trial_scoped_only"    # only trial-scope stacks (today's per_trial default)
    MULTI_SCOPE         = "multi_scope"          # mix — the canonical T-Bench shape
```

`EnvironmentManifest.stacks` (new field, default empty list) is populated by `project_loader.resolve` either from the merged `stacks` patch OR synthesised from the legacy scalar `compose_file`. The legacy fields (`compose_file`, `runner_service`, `stack_inputs`, `runner_port`, `db_service`, `db_port`, `rag_service`, `rag_port`) remain and mirror the sole synthetic stack when the manifest was loaded from the pre-plan surface. A validator refuses "both a `stacks` block AND scalar `compose_file` in the same patch layer" — they are representations of the same field.

`EnvironmentManifest.plan_shape: PlanShape` is a computed property. `requires_per_trial` becomes a derived `@property` that returns `True` iff `plan_shape != SINGLE_RUN`. It is retained for external consumers but no longer participates in backend selection.

### 2. Three detachable adapter Protocols

```
@runtime_checkable
class ComposeMaterialiser(Protocol):
    name: str
    def materialise(self, decl: StackDecl, ctx: MaterialiseContext) -> StackHandle: ...
    def teardown(self, handle: StackHandle) -> None: ...

@runtime_checkable
class ServiceLifecycleDispatcher(Protocol):
    isolation: ServiceIsolation
    def cycle(
        self, spec: ServiceSpec, service_name: str,
        handle: StackHandle, seeds: Mapping[str, SeedRef],
    ) -> None: ...

@runtime_checkable
class SubstrateComposer(Protocol):
    def materialise_run(self, plan: CompositionPlan, ctx: RunCtx) -> RunSubstrate: ...
    def provision_trial(
        self, plan: CompositionPlan, spec: TrialSpec, run_sub: RunSubstrate,
    ) -> EnvHandle: ...
    def cycle_between_trials(self, run_sub: RunSubstrate, spec: TrialSpec) -> None: ...
    def teardown_trial(self, handle: EnvHandle) -> None: ...
    def teardown_run(self, run_sub: RunSubstrate) -> None: ...

@dataclass
class RunSubstrate:
    run_id: str
    run_stack_handles: tuple[StackHandle, ...]
    task_stack_handles: dict[tuple[str, str], StackHandle]
    runner_client: RunnerClient | None
    endpoints: EnvEndpoints | None
    seeds: Mapping[str, SeedRef]
    mount_docker_socket: bool
    log_capture: LogCaptureConfig | None
    events: RunDisplayEvents
```

The three trailing fields (`mount_docker_socket`, `log_capture`, `events`) carry the run-wide policy `materialise_run` threads from `RunCtx` onto the substrate so `provision_trial` materialises task-scope and trial-scope stacks under the same policy the run-scope stacks were materialised with.

Three new entry-point groups on `plugin_registry.py`:
- `tolokaforge.compose_materialisers` → `type[ComposeMaterialiser]`
- `tolokaforge.service_lifecycle_dispatchers` → `type[ServiceLifecycleDispatcher]`
- `tolokaforge.substrate_composers` → `type[SubstrateComposer]`

Built-in registrations (wheel entry points):
- `docker_compose` materialiser (extracted from today's `_materialise_manifest`).
- `shared` dispatcher (no-op — service persists).
- `reset` dispatcher (wraps `RECIPE_REGISTRY`).
- `ephemeral` dispatcher (new — targeted `docker compose rm -f -v <service> && docker compose up -d <service>`).
- `default` composer.

### 3. `SharedStackRuntimeBackend` composer-driven

The class becomes:

```
class SharedStackRuntimeBackend:
    composer: SubstrateComposer                                 # detachable (default = default_substrate_composer)
    materialiser_registry: dict[str, ComposeMaterialiser]       # keyed by name
    dispatcher_registry: dict[ServiceIsolation, ServiceLifecycleDispatcher]
    # (dataclass fields with entry-point-loader defaults, following tolokaforge/core/per_trial_runtime.py:182 readiness_probe_loader idiom)
```

At `connect()`: for each `stack_scope="run"` decl → `composer.materialise_run(...)`. At `provision(spec)`: for each `stack_scope="task"` decl whose task not yet materialised → `composer.materialise_task(...)`; for each `stack_scope="trial"` decl → `composer.materialise_trial(...)`. Between trials sharing a `run`/`task` scope stack → `composer.cycle_between_trials(...)` walks the stack's services and invokes the dispatcher registered for each isolation label.

Reserved-prefix `stack_inputs` keys (`TOLOKAFORGE_*`) are refused uniformly at both composer entry points: `materialise_run` raises `ProvisionError(stage="materialise_run", trial_id=run_id)` before `_validate_plan` runs (so a bad run-scope manifest fails before any docker call); `provision_trial` raises `ProvisionError(stage="provision", trial_id=trial_id)` before any per-trial materialise call. Reason text is byte-identical past the id — one refusal helper, two entry points.

### 4. `PerTrialRuntimeBackend` retained as thin preset

Its class stays exported for backward-compat imports and entry-point registration. Its body reduces to ~30 LOC: build `SharedStackRuntimeBackend` with a single-stack `trial`-scope plan synthesised from `env_manifest`. Behaviour is byte-identical to today's per-trial: docker-compose up per trial, teardown per trial.

### 5. `IsolationMode` + capability admission

Add `IsolationMode.COMPOSED_STACK`. `SharedStackRuntimeBackend.isolation_mode` becomes a computed property derived from `plan_shape`:

- `SINGLE_RUN` → `SHARED_STACK`
- `TRIAL_SCOPED_ONLY` → `PER_TRIAL_STACK`
- `TASK_SCOPED_ONLY` or `MULTI_SCOPE` → `COMPOSED_STACK`

Add `hybrid_stack`-analog capability entry `composed_stack` to `CAPABILITY_REGISTRY`. `SharedStackRuntimeBackend.advertised_capabilities` computed from `plan_shape × dispatcher_registry` — union of the plan-shape-appropriate scope capability + reset-recipe capabilities from registered dispatchers + network isolation capabilities.

### 6. Selection + admission

`Orchestrator._construct_runtime_backend` collapses to: always construct `SharedStackRuntimeBackend` (the sole compose-mode backend). The task-driven "which backend" decision disappears; it becomes "what shape of plan does the manifest declare".

`Orchestrator._extract_run_env_manifest` refuses cross-task divergence of the `run`-scope subset of the plan (same-shape check, keyed on canonicalised per-scope compose bytes). `task`- and `trial`-scope stacks may differ freely across tasks.

`Orchestrator._verify_isolation_compatibility` becomes a per-scope check: for each stack, verify every service's isolation label has a registered dispatcher for that stack's scope. Refusals name `(stack_id, service_name, isolation, scope)`.

**Deprecated `orchestrator.runtime` override.** ADR-0018's amendment declared the operator-level `config.orchestrator.runtime` override deprecated but still-honoured with a `DeprecationWarning`. Under this ADR the override is honoured as follows: `orchestrator.runtime = "per_trial"` coerces the resolved manifest to a single-stack `trial`-scope plan (equivalent to today's behaviour); `orchestrator.runtime = "shared"` coerces to a single-stack `run`-scope plan and continues to raise the ADR-0018 refusal when the task set contains `reset|ephemeral` services with no dispatcher registered for the `run` scope (unchanged semantics — the built-in `reset` and `ephemeral` dispatchers make the previously-refused combinations legal by default, so a task pack that would have been rejected under the old override now succeeds; operators who need the strict refusal register a `None`/refusing dispatcher). A new value `orchestrator.runtime = "composed"` is not introduced — plan shape is inferred from the manifest, and the override remains a coercion knob for pinning a legacy shape.

### 7. Backward compat as a HARD invariant

Every existing task pack continues to work byte-identically. Manifest with scalar `compose_file` and no `stacks` block → `project_loader.resolve` synthesises a single-entry composition plan with `stack_scope="run"` (if `requires_per_trial=False` under today's rules) or `stack_scope="trial"` (if `True`). Every downstream consumer sees identical `compose_file` / `services` / `stack_inputs` fields via legacy mirror. Canonical test locks this at load time.

`ServiceSpec` and `ServiceIsolation` do NOT change. The vocab `{shared, reset, ephemeral}` stays closed — extension is via new dispatcher registrations, not new enum values.

## Invariant preservation matrix

| INV | Old enforcement | New enforcement point |
|---|---|---|
| INV-1 (one substrate per SharedStack run) | `tolokaforge/core/orchestrator.py:1091-1097` refuses heterogeneous `compose_file` | `_extract_run_env_manifest` refuses cross-task divergence of the `run`-scope subset only. `task`/`trial` scope stacks may differ freely per task. |
| INV-2 (SharedStack refuses `ephemeral`) | `tolokaforge/core/orchestrator.py:1404-1411` | Enforced per-stack-scope in `_verify_isolation_compatibility`: `ephemeral` is legal iff the stack is `trial`-scope OR the stack has an `ephemeral` dispatcher registered (which the built-in dispatcher provides). |
| INV-3 (SharedStack refuses `reset` via `requires_per_trial`) | INV-2's twin | Same as INV-2 for `reset`. All four built-in reset recipes work at any scope. |
| INV-4 (closed `ServiceIsolation` vocab) | `tolokaforge/runner/models.py:2222` | Unchanged. |
| INV-5 (reset iff seed) | `ServiceSpec._check_reset_agrees_with_isolation` | Unchanged. |
| INV-6 (empty services ⇒ per-trial) | `EnvironmentManifest.requires_per_trial` | Preserved via legacy coercion. |
| INV-7 (`SHARED_STACK` = contamination structural) | `tolokaforge/core/runtime.py:47-63` | `IsolationMode` becomes computed from `plan_shape`. Structural-contamination refusal fires only for `SHARED_STACK`. |
| INV-8 (capability admission subset-only) | `tolokaforge/core/backend_capabilities.py:87-115` | Unchanged mechanism. `advertised_capabilities` computed from plan × registry. |
| INV-9 (endpoints XOR env_manifest) | shared_stack ctor guard | Endpoints resolved from the plan's materialised stacks; guard preserved as "operator-supplied endpoints OR composition plan, not both". |
| INV-10 (unique compose project name) | `make_project_temp_dir` slug | **Extended**: `make_project_temp_dir(run_id, stack_id, scope_key)` where `scope_key = run_id` for `run`, `f"{run_id}-{task_id}"` for `task`, `trial_id` for `trial`. Closes container-name and volume collision. |
| INV-11 (compose safety refusals) | per-file at manifest load | Iterated per `StackDecl`. |
| INV-12 (runner-credential scope) | `inject_runner_credentials` on `runner_service` | Composer enforces "exactly one stack in the plan sets `runner_service`" at manifest resolve. Credential injection targets only that stack. |

## New failure modes closed

1. **Container-name collision** → extended INV-10 slug.
2. **Named-volume collision** → composer refuses plans where two stacks declare the same top-level `volumes:` name unless both are `run`-scope and point at the same declared volume.
3. **Runner endpoint ambiguity** → single-runner invariant (§1, §7 of Decision) forces exactly one stack to own the runner. Grading dispatch keeps a single `runner_client`.
4. **Reset-recipe scope drift** → `RECIPE_REGISTRY` wrapped as one `ServiceLifecycleDispatcher`; each dispatch call takes the stack handle the service belongs to.
5. **Network cross-reachability** → network-policy transform runs per stack; composer refuses inter-stack shared networks unless declared `external: true` and marked in the plan's `bridges` list (deferred capability).
6. **Fixed host-port collisions** → manifest validator refuses `ports: [X:Y]` on any stack whose scope != `run`; `task`/`trial` scope stacks must use dynamic ports.
7. **`limited_internet_allowlist` merge ambiguity** → allowlist is per-manifest (run-wide), not per-stack; cross-task reconciliation requires identical allowlists.
8. **Credential-injection blast radius** → closed by INV-12 extension.
9. **Log-router / teardown bookkeeping** → `RunSubstrate` holds `list[StackHandle]`, each with its own log routers; teardown walks all handles.

## Extension seams (proving the design)

- **K8s materialiser**: register `k8s` in `tolokaforge.compose_materialisers`; `StackDecl` gains optional `materialiser: str = "docker_compose"` field. No changes to composer or dispatchers.
- **`task_shared` scope** (share across repeats of one task, cycle between tasks): add to `stack_scope` Literal + register a `TaskScopeDispatcher`. No Protocol change.
- **Filesystem overlay snapshot** for isolation transform: register a new `ServiceLifecycleDispatcher` under existing `reset` label — or a future `snapshot` label if the closed vocab needs extending (its own ADR).

## Case study — `terminal_bench_balanced_10` under the composition plan

Today's per_trial (baseline in ADR-0043):
- 10 tasks × 11 repeats = 110 trials.
- Every trial: rebuild runner image (once per shard on cold cache), materialise task's docker-compose stack (~11 s), run turns, grade, tear down.
- 66 h wall-clock per model.

Under composition plan (T-Bench adapter opts in at Ticket #1385):
- Manifest declares two stacks:
  - `stack_id: "engine"`, `stack_scope: "run"`, `compose_file: <engine.yaml>` (runner + db-service + rag-service if present).
  - `stack_id: "task"`, `stack_scope: "trial"`, `compose_file: <task's docker-compose.yaml>` (postgres + billing service for `fix-billing-holds`, etc.).
- Backend: `SharedStackRuntimeBackend` with `plan_shape=MULTI_SCOPE`, `isolation_mode=COMPOSED_STACK`.
- Once per run: engine substrate materialised. Runner + db-service stay up across all 110 trials.
- Per trial: only the task-scope compose is materialised + torn down. ~7 s per trial vs ~20 s under per_trial.
- Estimated wall-clock: < 8 h per model.

## Consequences

### Positive

- **One class, N topologies.** The `SharedStackRuntimeBackend` executes any composition plan the manifest declares.
- **Far-future extensions slot in cleanly.** K8s materialiser, `task_shared` scope, snapshot-based isolation — all without new backend classes.
- **State-contamination invariants strengthened.** 12 named invariants preserved; 9 new failure modes closed at named enforcement points.
- **Backward compat is a HARD invariant.** No existing task pack breaks.
- **`ServiceIsolation` closed vocab preserved.** Extension via new dispatcher registrations, not new enum values.

### Negative

- **The refactor touches load-bearing code**. `SharedStackRuntimeBackend`, `PerTrialRuntimeBackend`, `Orchestrator._construct_runtime_backend`, `Orchestrator._extract_run_env_manifest`. Careful staged rollout via 8 tickets.
- **Multi-compose debug story is more complex.** A composed run has a live shared runner + task compose projects per trial. Troubleshooting requires knowing which stack failed. Log-router bookkeeping per-stack helps.
- **Composer becomes a load-bearing abstraction.** A bug in `DefaultSubstrateComposer` affects every task pack.
- **`PerTrialRuntimeBackend` becomes a ~30-LOC shim** retained purely for import compatibility (§4). The class survives but no longer participates in backend selection (§6). This is a permanent code-path duplication with its own maintenance surface — worth it for the import-stability guarantee but not free.
- **Three new Protocols + three new entry-point groups + five new built-in registrations** (§2). A real complexity increase for readers navigating `tolokaforge/core/plugin_registry.py` and the seam catalogue in `docs/ADAPTER_ARCHITECTURE.md`.

### Neutral

- **`IsolationMode` enum grows one value** (`COMPOSED_STACK`).
- **Three new entry-point groups** in the plug-in registry.
- **Manifest surface grows one required-list field** (`stacks`), plus per-stack fields on `StackPatch` / `EnvironmentPatch`.

## Follow-ups

- **Live smoke** (Ticket #1385 code portion is in this milestone; the 6-model P0 sweep + release cut is a post-consolidation-PR handoff).
- **K8s materialiser** — separate ADR + implementation. Design already slotted.
- **`task_shared` scope** — separate ADR + implementation. Design already slotted.
- **Migration of other adapters** (Native, Coding-Harness, MCP-Adapter) to declare composition plans where they'd benefit. Case-by-case.

## References

- [ADR-0016](0016-runtime-backend-comparison.md) — runtime-backend trade-offs.
- [ADR-0018](0018-multi-container-under-shared-runtime.md) — per-service isolation vocabulary + 2×2 this ADR redesigns as N-scope composition.
- [ADR-0022](0022-runtime-independence.md) — task-driven backend selection.
- [ADR-0040](0040-standalone-grader.md) — precedent for topology-agnostic multiple impls behind one Protocol.
- Epic: https://github.com/Toloka/tolokaforge/issues/1336
- Milestone: https://github.com/Toloka/tolokaforge/milestone/41
- Prior attempt (unshipped): the `HybridRuntimeBackend`-as-distinct-class approach was drafted as ADR-0043 in a Milestone 40 integration branch that was closed as wrong-premise. Number 0043 is intentionally skipped in the ADR index; the historical draft is preserved in the closed Milestone 40 ticket #1363.
