# 0016. Runtime backend comparison: `shared` vs `per_trial`

- **Status:** Accepted
- **Date:** 2026-07-03
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Two `RuntimeBackend` implementations ship: `SharedStackRuntimeBackend` and `PerTrialRuntimeBackend`. Both satisfy the same Protocol (ADR-0007) and are selected by operators via `orchestrator.runtime` or the `--runtime` CLI flag. `RUNTIME_BACKENDS.md` documents the mechanics — isolation, failure modes, port allocation, timing — but is silent on questions operators ask when picking one:

- Does the grade a task produces depend on which backend I run?
- Does result aggregation differ?
- What resource cost does `per_trial` add over `shared`, and when is that cost justified?
- Which failure modes are mode-specific?
- Where do I look in logs, metrics, and artifacts when something breaks — does that differ by mode?

This ADR fills those gaps. It documents the shipped state (both backends validated end-to-end) rather than proposing new work. It cites the empirical A/B run conducted on `coding_public_example_01` under both modes to confirm grading equivalence.

## Scope note — this ADR covers one of two independent axes

Runtime backends live on a **2×2 matrix**, not a line:

- **Axis 1 — Stack lifecycle** *(this ADR)*: `shared` vs `per_trial` — is the substrate materialised once per run or once per trial?
- **Axis 2 — Stack composition** *([ADR-0018](0018-multi-container-under-shared-runtime.md))*: **built-in stack** (engine's `core_stack`/`full_stack` defaults) vs **task-declared stack** (a task's `environment_manifest` compose file). Is the substrate composed of engine services or task-declared services?

The **lifecycle** axis and the **composition** axis are orthogonal — a run occupies one cell of the 2×2, and each cell has distinct trade-offs. ADR-0018 documents the composition axis end-to-end (with sequence diagrams for each case) and the 2×2 as a whole. This ADR focuses on grading equivalence, resource profile, and decision rubric along the *lifecycle* axis; those claims hold regardless of composition.

## Decision Drivers

- **Operator clarity.** Cost, throughput, and isolation are the real trade-off surface — capture them in one place so operators don't have to reverse-engineer from source.
- **Reviewer evidence trail.** Future PR reviewers should be able to point at a durable statement that grades and aggregates are mode-agnostic, without re-deriving the code path each time.
- **Future substrate compatibility.** When Kubernetes / Modal / microVM backends land, this comparison generalises to "shared-materialisation vs per-trial-materialisation" — the docker specifics belong in `RUNTIME_BACKENDS.md`, the isolation-axis semantics belong here.

## Considered Options

1. **Extend `RUNTIME_BACKENDS.md` in-place** with the missing sections — no separate ADR.
2. **Write ADR-0016 as this comparison + rubric doc**, and add a "See also: ADR-0016" link from `RUNTIME_BACKENDS.md`.
3. **Write ADR-0016 and rewrite `RUNTIME_BACKENDS.md`** to be pointer-only.

## Decision

We will adopt **Option 2**. `RUNTIME_BACKENDS.md` keeps its mechanics-focused content; ADR-0016 owns the durable comparison across the axes below and the decision rubric. The two docs cross-link.

## Grading and aggregation equivalence

The grade a task produces is **independent** of the runtime backend, by construction of the shipped code paths:

- `RunnerRPCTrialGrader.grade()` (`tolokaforge/core/trial_grader.py`) calls `runtime_backend.grade_trial()` — a method every `RuntimeBackend` implementation satisfies with identical shape. `SharedStackRuntimeBackend.grade_trial` dispatches to the shared runner's gRPC endpoint; `PerTrialRuntimeBackend.grade_trial` dispatches to the per-trial runner client looked up by `trial_id`. Both routes hit the same runner-side `GradeTrial` service (`tolokaforge/runner/service.py`), which executes the same three-phase grading algorithm (hash-of-final-state / jsonpath assertions / optional LLM judge) with no branch on which backend called it. The runner is trial-centric, not infrastructure-centric.
- Conductor phase `_capture_final_state` (`tolokaforge/core/conductor.py`) also routes through `runtime_backend.get_state(trial_id)` — same-shape dispatch, same runner-side handler. The final-state snapshot the grader reads is captured through the mode-specific runner instance but shaped identically.
- Result aggregation (`Orchestrator._generate_reports`) operates on the in-memory `Trajectory` objects each trial produces. It does not consult which backend produced the trajectory; it treats every trajectory as opaque data.

**Empirical confirmation.** `coding_public_example_01` was run repeats=3 in each mode with identical model configuration (openrouter → sonnet-4-6, temperature=0.0):

| Mode | Trial 0 | Trial 1 | Trial 2 | Avg grade | Total cost | Avg latency |
|---|---|---|---|---|---|---|
| `shared` | 0.783 | 0.783 | 0.783 | **0.783** | $0.225 | 44 s |
| `per_trial` | 0.783 | 0.783 | 0.783 | **0.783** | $0.246 | 54 s |

**Grade convergence, not trajectory identity.** All six trials scored exactly 0.783, but the underlying trajectories varied within LLM non-determinism (per_trial trial 0 took 8 turns / 8 tool calls; the other 5 trials took 5–6 turns / 7 tool calls). Grade equivalence is therefore the stronger property: the grade is robust to small trajectory perturbations, and the substrate contributes zero systematic bias to the outcome.

**Caveat.** Grading equivalence is deterministic modulo LLM stochasticity. Runs at non-zero temperature or with LLM-judge rubrics will still produce the same *expected* grade distribution across modes, but individual trials will diverge on the LLM axis, not on the substrate axis.

## Resource profile

Order-of-magnitude framing, not exhaustive benchmarks — enough for capacity planning. Concrete numbers come from the A/B run above and the docker-events cross-check.

| Axis | `SharedStackRuntimeBackend` | `PerTrialRuntimeBackend` |
|---|---|---|
| Long-lived containers | Engine services (runner + db-service) up for the run | Engine images built (`:local` alias applied), engine containers **not started** |
| Per-trial containers | None (trials share the engine) | One compose project per trial (runner + task-declared services), created on `provision`, destroyed on `teardown` |
| Docker networks | One shared engine network (`runner-net`) for the whole run | `runner-net` created (so `:local` aliases work) + one docker network per active trial |
| Port allocation | Fixed set of host ports for the shared engine | Testcontainers picks a fresh set of host ports per trial |
| Startup latency | Engine build + start (~seconds on cached build, minutes on first run) | Engine build only; per-trial provision (compose up + healthchecks) is paid at the top of each trial |
| Per-trial latency overhead | None (containers already up) | ~5–15 s for compose up + healthcheck on a task like `coding_public_example_01`; grows with the task's declared service count |
| Docker daemon load | Constant | Bounded by worker count × per-trial compose service count |
| Cross-trial concurrency ceiling | One trial per stateful engine service (writes to the same DB) | Bounded by host CPU / memory / daemon throughput, not by task shape |

The A/B run above burned $0.225 total on `shared` (avg 44 s/trial) and $0.246 total on `per_trial` (avg 54 s/trial). Two effects to separate:

1. **Latency premium is real and reproducible.** ~10 s per trial for a 3-service compose (up + healthchecks + gRPC connect + teardown). A second run of a domain-specific MCP-served task at higher turn cap saw a larger +25 s premium — the compose-up cost scales with service startup time (postgres warm-up, image cache state, healthcheck poll interval). Operators should budget per-trial isolation's wall-clock cost against expected trial duration.
2. **$-cost premium is not systematic at this sample size.** The 3-trial `per_trial` arm's $0.021 total premium was concentrated on the cold-start trial 0 ($0.099 vs shared trial 0's $0.077, driven by a 2-turn-longer trajectory) — trials 1 and 2 costs matched shared within one cent. A follow-up run of `CAP-001` in each mode saw `per_trial` come in *cheaper* than `shared` ($0.068 vs $0.079), which contradicts a systematic per-trial-mode $-overhead hypothesis. At this sample size, cost noise between LLM routing / token counts / trajectory length dominates any putative substrate-driven cost premium. See the follow-up in "Consequences → Follow-ups" for the measurement work needed to characterise this properly.

Tasks that declare many services or slow-starting services (large postgres seeds, image pulls) pay the compose-up latency each trial in `per_trial`. That translates to LLM cost only if the trial clock keeps ticking during compose-up in a way that consumes tokens — an open question flagged for follow-up measurement.

## Failure-mode differences

Cross-referencing `RUNTIME_BACKENDS.md`'s "Failure modes" section, the mode-specific soft failures are:

- **`shared` only — cross-trial state contamination.** Two trials writing to the same DB can see each other's writes. Deterministic-grading tasks (whose grader reads the DB) can produce wrong verdicts if the code that writes to the DB doesn't scope by `trial_id`. Detection is task-author responsibility — the runtime doesn't know what a task's grader expects. Guardrail: the task's `environment_manifest.isolation` declaration; `SharedStackRuntimeBackend` refuses to run tasks that declare `isolation: per_trial`.
- **`per_trial` only — daemon-throughput ceiling.** Running many workers on one host multiplies compose-up cost by worker count. On daemons with low concurrent-project limits or slow image cache, workers can starve at the docker-daemon boundary. Guardrail: bound worker count against observed daemon throughput; ADR-0010 requires provisioners to fail loud on `ProvisionError` and attribute the failure deterministically so throughput ceilings surface in the aggregate report rather than looking like task failures.

Hard failures (image build error, network bring-up error, grader RPC error, runner crash) are shared between modes and covered in `RUNTIME_BACKENDS.md`.

## Decision rubric — which mode when

A concrete flow, per task:

1. **Does the task declare `environment_manifest.isolation: per_trial`?** → run on `PerTrialRuntimeBackend`. The declaration says the task's grader cannot tolerate shared state; the shared backend refuses to run it (isolation-enforcement guard).
2. **Does the task declare `environment_manifest.isolation: shared_ok`?** → `SharedStackRuntimeBackend` is safe and faster. `PerTrialRuntimeBackend` also works and provides stronger isolation than needed; pick based on throughput and cost preference.
3. **Does the task not declare an `environment_manifest`?** → `SharedStackRuntimeBackend` only. `PerTrialRuntimeBackend.provision()` requires a manifest and fails loud without one. Migration to per-trial isolation requires the task-pack side to author a manifest.
4. **Genuinely stateless workload where cost dominates?** → `shared`, with the caveat that the author is responsible for the "no cross-trial contamination" invariant.
5. **Cross-trial isolation matters more than cost?** → `per_trial`, budget the ~10 s and ~9% cost premium per trial visible in the A/B numbers above.

## Observability parity

Logging, metrics, and artifact writes do **not** vary by mode:

- Structured logs use the same event names (`Trial completed`, `Trial graded`, `Trial env teardown complete`, `Aggregate Results`) with the same fields regardless of backend. The `Provisioning trial env` / `Trial env provisioned` events fire in both modes; on `SharedStackRuntimeBackend` they wrap a no-op that returns the shared endpoints.
- Result aggregation writes the same `TrialResult` / `Trajectory` shapes to the artifact writer. Grades, costs, latencies, and termination reasons are all mode-agnostic fields.
- `TerminationReason.PROVISION_ERROR` fires only on `per_trial` (the shared backend has no provisioning step that can fail per trial), so `attribute_failure()` classifying `provision_failure` is a mode-specific *category* — but the classifier itself is mode-blind, and the field is present in the aggregate for both modes.

## Consequences

### Positive

- Operators pick a mode from a rubric, not from source-reading.
- Grade equivalence is documented and empirically confirmed — future refactors that touch grading can be reviewed against this claim.
- Failure-mode language is normalised; incident write-ups reference this section instead of re-explaining.

### Negative / Trade-offs

- The comparison table needs maintenance as backends evolve. Additions to a backend should refresh both this ADR and the side-by-side table in `RUNTIME_BACKENDS.md`.
- Any future backend (Kubernetes, Modal, etc.) that doesn't fit the shared-vs-per-trial dichotomy will need this ADR extended or superseded.

### Follow-ups

- **Task-pack migration to per-trial.** Migration of the existing task packs that declare `isolation: per_trial` semantics (or need it in principle) into `environment_manifest`-shaped tasks is separate task-pack work. This ADR does not gate that migration.
- **Runner image publication.** `RUNTIME_BACKENDS.md` "Follow-up work" already tracks publishing engine images to a public registry so task compose files can reference published tags directly (`:local` stays as the local-dev alias). Independent of this ADR.
- **Per-trial concurrency benchmarks.** The resource profile table gives order-of-magnitude framing; a proper benchmark harness would produce numbers on a range of task shapes and daemon configurations. Filed as a follow-up.
- **Cost-premium characterisation.** The small-sample runs cited above don't distinguish LLM-routing noise from any putative substrate-driven $-cost premium. Open questions: does the trial clock consume LLM tokens during compose-up (e.g. via user-simulator warm-up or agent context establishment)? Does openrouter route consistently at temperature=0 across mode-differentiated runs? A ≥20-trial-per-arm run with token-level accounting would settle both. Filed as a follow-up.
- **Multi-service adapter compatibility.** Some existing adapters materialise their own per-task compose stacks inside the tool-invocation path (see `RUNTIME_BACKENDS.md` "Adapter compatibility with `per_trial`") rather than through `environment_manifest`. Unifying those paths is future work.

## Links

- Related ADRs:
  - [ADR-0007](0007-runtime-backend-protocol.md) — RuntimeBackend Protocol
  - [ADR-0009](0009-environment-manifest.md) — EnvironmentManifest
  - [ADR-0010](0010-runtime-backend-provisioning-contract.md) — Provisioning contract
  - [ADR-0014](0014-trial-grader-protocol.md) — TrialGrader Protocol
  - [ADR-0015](0015-trial-executor-protocol.md) — TrialExecutor Protocol
- Related code:
  - `tolokaforge/core/shared_stack_runtime.py`
  - `tolokaforge/core/per_trial_runtime.py`
  - `tolokaforge/core/trial_grader.py`
  - `tolokaforge/runner/service.py` (GradeTrial handler)
  - `tolokaforge/core/orchestrator.py` — backend selection + isolation guard
- Related docs:
  - `docs/architecture/RUNTIME_BACKENDS.md` — mechanics deep-dive
  - `docs/architecture/ROADMAP.md`
