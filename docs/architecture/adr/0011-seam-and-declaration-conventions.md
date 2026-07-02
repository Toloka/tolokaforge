# 0011. Seam-definition and data-declaration conventions for new components

- **Status:** Proposed
- **Date:** 2026-07-01
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The seam-definition arc (ADRs 0003 – 0009) shipped every architectural plane behind a typed contract with a common shape: a `@runtime_checkable` Protocol, at least one production implementation, an `InMemory*` fixture for orchestrator-level test injection, a canonical contract test pinning the surface, and a per-decision ADR. The shape works — every seam is swappable, every wire format is pinned, and every runtime component has an in-process test fixture. The same is true for the typed data declarations that cross serialization boundaries (`TrialSpec`, `EnvEndpoints`, `EnvironmentManifest`, and grading models): strict Pydantic with `extra="forbid"` and canonical snapshot tests.

But the discipline is currently **implicit**. Nowhere in the repository does a contributor read "this is how you add a new component"; each of the shipped ADRs describes only its own decision. A pattern audit while drafting ADR-0010 (`RuntimeBackend` provisioning contract) surfaced the drift: several runtime components — the rubric judge, the trial runner, the user simulator — are one-implementation top-level functions or concrete classes with no Protocol seam. They work today because there is only one implementation of each. When a second variant lands (a deterministic replay judge, an agent-debate loop, a scripted user simulator), lifting them ad-hoc will be considerably larger than following an already-documented pattern.

This ADR codifies the two patterns that already produced our best components, so future contributors adopt the same shape and existing one-impl components can be lifted with a well-defined target.

## Decision Drivers

- **The pattern is real, not aspirational.** ADRs 0003 – 0009 already follow it consistently. Documenting it is a matter of writing down what shipped, not of prescribing something new.
- **Drift risk grows with component count.** The engine is going to acquire more agents, more loops, more graders, more evaluation modes. Each new component picked ad-hoc widens the divergence between subsystems.
- **The Judge lift** (a follow-up to this ADR, tracked separately) will be the first test of the pattern's generalisability. Codifying now gives that lift a target.
- **Retroactive documentation is cheap.** No code changes; this is a documentation-only ADR that describes existing practice.
- **Existing seam ADRs remain the evidence base.** Contributors read this ADR alongside the concrete ADRs it references, not instead of them.

## Considered Options

1. **One ADR documenting both patterns (Pattern A — seam definition; Pattern B — data declaration) with cross-references to representative examples, naming discipline, extension escape hatch, test-fixture convention, and criteria for when NOT to introduce a Protocol.** **This ADR.**
2. **Two separate ADRs, one per pattern.** Cleaner section-by-section but the two patterns are almost always paired in the same component (a declaration + a runtime consumer), so splitting them creates cross-references and duplication.
3. **CI / lint enforcement of the patterns.** Fail CI when a new Protocol lands without an `InMemory*` fixture, when a new `BaseModel` omits `extra="forbid"`, etc. Rejected as premature — the patterns should be documented before they are encoded in linters. A follow-up can add lint later if drift returns.
4. **Defer until drift bites.** Wait for the second Judge variant (or the second `TrialRunner`) to land ad-hoc, then retrofit. Rejected — that is exactly the drift this ADR is preventing.

## Decision

We adopt **Option 1**.

### Pattern A — seam definition

Use for a runtime component behind a swappable boundary. Every occurrence must ship:

- A **`@runtime_checkable` Protocol** in a canonical module (typically `tolokaforge/core/*.py`) declaring the method surface + any typed attributes.
- **At least two implementations**: one production impl + one **`InMemory*` fixture**. The fixture is deterministic, requires no external services, and carries a **`CallLog` dataclass** (or an equivalent record-keeping structure) so orchestrator-level tests can assert what was called with what arguments.
- **Configurable failure knobs** on the `InMemory*` fixture — constructor kwargs that let tests exercise the failure branches (timeout, partial success, provisioner rejection, etc.) without a real substrate.
- A **canonical contract test** in `tests/canonical/test_*_contract.py`. The test pins method presence, argument shape, expected return types, and — where the Protocol declares them — the failure semantics (idempotency, exception types, ordering rules).
- An **ADR** recording the design, the rejected alternatives, the follow-ups, and the boundary the Protocol establishes.

**Existing examples:**

| Seam | Protocol | Production impl | Fixture | Contract test | ADR |
|---|---|---|---|---|---|
| Per-trial artifacts | `TrialArtifactWriter` | `FileArtifactWriter` | `InMemoryArtifactWriter` | `test_artifact_writer_contract.py` | [0004](0004-trial-artifact-writer-seam.md) |
| Run aggregates | `RunAggregateWriter` | `FileRunAggregateWriter` | `InMemoryRunAggregateWriter` | `test_run_aggregate_writer_contract.py` | [0005](0005-run-aggregate-writer-seam.md) |
| Execution surface | `RuntimeBackend` | `DockerRuntime` | `InMemoryRuntimeBackend` | `test_runtime_backend_contract.py` | [0007](0007-runtime-backend-protocol.md) |
| Per-trial executor | `Conductor` | `InProcessConductor` | `InMemoryConductor` | `test_conductor_contract.py` | [0008](0008-conductor-protocol.md) |

### Pattern B — data declaration

Use for a typed shape that crosses a serialization or task-boundary. Every occurrence must ship:

- A **Pydantic `BaseModel`** with `model_config = {"extra": "forbid"}`. No exceptions — every field is either declared explicitly or rejected. Silent field addition is not allowed.
- **Cross-field validators** encoded as Pydantic `field_validator` / `model_validator` where invariants exist (non-empty lists, referential integrity between fields, format constraints on strings).
- A **canonical wire-shape snapshot test** — a JSON round-trip that fixes the exact byte layout of a fully-populated instance. Any change to field names, defaults, or ordering fails CI on snapshot diff.
- An **ADR** when the shape crosses a public boundary (task pack → engine, engine → gRPC, engine → artifact store).

**Existing examples:**

| Shape | Module | Snapshot test | ADR |
|---|---|---|---|
| Trial wire format | `TrialSpec`, `TrialResult` | `test_trial_spec_contract.py` | [0003](0003-trial-spec-and-trial-result.md) |
| Trial-scoped service URLs | `EnvEndpoints` | *(embedded in `TrialSpec` snapshot)* | [0006](0006-typed-env-endpoints.md) |
| Per-trial environment | `EnvironmentManifest` + `InitialStateRef` + `NetworkPolicy` + `SecurityContext` (points at a Docker Compose file; safety validators run against loaded compose contents) | `test_environment_manifest_contract.py` | [0009](0009-environment-manifest.md) |
| Rubric grading | `Rubric`, `Criterion`, `CriterionResult`, `LLMJudgeConfig` | *(embedded in `TaskDescription` + grading tests)* | — |

### Naming discipline

- **`*Config` suffix** for models whose purpose is *behaviour configuration* — e.g. `LLMJudgeConfig`, `SearchConfig`, `UserSimulatorConfig`, `GradingConfig`, `TranscriptRulesConfig`. These carry knobs that select or shape a runtime component's behaviour.
- **No suffix** for models whose purpose is *data-shape declaration* — e.g. `EnvironmentManifest`, `InitialStateRef`, `SecurityContext`, `EnvEndpoints`, `TrialSpec`, `Rubric`. These describe a structure, not a set of choices.

The distinction is descriptive of what the codebase already does; it is not a new convention. New models should follow whichever suffix matches their role.

### Extension escape hatch

- **`TaskDescription.metadata: dict[str, Any]`** is the single agreed adapter-specific escape hatch. Adapters carrying data the engine does not need to interpret put it here.
- **Every other Pydantic model stays strict** with `extra="forbid"`. Adding a new field to a strict model requires an ADR update + a snapshot regen.
- **Do not introduce additional `metadata: dict[str, Any]` fields** on other models without an explicit ADR. Multiple untyped dicts create ambiguity about which one carries which extension.

### Test-fixture convention

- **Naming**: `InMemory{ProtocolName}` — `InMemoryArtifactWriter`, `InMemoryRuntimeBackend`, `InMemoryConductor`. Consistent prefix makes the fixture easy to find and unambiguous about its role.
- **Determinism**: no time, no network, no external services. Everything the fixture returns is a function of what was passed in + explicit constructor state.
- **`CallLog`**: every fixture keeps a record of calls made against it (method name, arguments) accessible to tests. Enables orchestrator-level assertions like "the conductor called `provision` before `endpoints`".
- **Failure knobs**: constructor kwargs that let tests exercise failure branches (`fail_after_call_n=3`, `raise_on_teardown=True`, etc.). Without these, failure paths need real substrate to test.

### When NOT to introduce a Protocol

Introducing a Protocol has a cost — every future implementation must fulfil it, tests must exist, contract tests must be maintained. That cost is worth paying when swappability is real; it is not worth paying when it is aspirational.

**Do introduce a Protocol when:**

- You can name a second variant that would want to exist (deterministic replay, cross-check ensemble, remote impl, alt substrate, etc.).
- The component's boundary is a wire format, a substrate boundary, or an isolation boundary.
- Orchestrator-level tests currently can't run without spinning up the component's real dependencies.

**Do not introduce a Protocol when:**

- The component is a pure data-transformation utility (functions from typed input to typed output; no side effects).
- The component is a truly internal helper with no cross-boundary concern.
- You cannot name a plausible second variant.

**Rule of thumb**: if you can name the second variant, introduce the Protocol now; if you can't, don't.

### Follow-up lifts for existing one-impl components

Some components in the codebase pre-date this ADR and did not follow Pattern A. Each is tracked separately; each will be lifted when its swappability becomes real (or when active development pressure makes the lift cheaper than continued ad-hoc growth):

- **Rubric judge → `Judge` Protocol** (tracked; see [GH #131](https://github.com/Toloka/tolokaforge/issues/131)). Actively planned; coordinated with the Judge maintainer to sequence after their in-flight follow-ups.
- **`TrialRunner` → `AgentLoop` Protocol** (not yet filed). File when a second loop shape (deep-research, agent-debate, multi-agent) becomes realistic.
- **User simulator → Protocol** (not yet filed). File when a second simulator shape (scripted, adversarial replay) becomes realistic.

## Consequences

### Positive

- New components have a written-down required shape. Drift becomes visible in review — a Protocol without a fixture, a `BaseModel` without `extra="forbid"`, or a strict model without a snapshot test is now a specific reviewable comment, not a subjective preference.
- Orchestrator-level tests get in-memory fixtures uniformly. No new seam ships without a way to isolate it in tests.
- Every typed shape gets snapshot-pinned. Silent additions to serialization boundaries fail CI, not "someone will notice in review".
- Follow-up lifts (Judge, TrialRunner, UserSimulator) have a well-defined target — the pattern this ADR documents.

### Negative / Trade-offs

- Contributors adding a small new runtime component pay the pattern's cost (Protocol + fixture + tests + ADR) even when swappability isn't immediately obvious. Mitigation: the "when NOT to introduce a Protocol" rule gives explicit criteria; single-impl components without a plausible second variant do NOT require the pattern.
- Every field addition to a strict `BaseModel` needs ADR + snapshot update. Mitigation: this is already the practice — no new burden.
- Naming discipline sometimes reads pedantic. Mitigation: it is descriptive of what the codebase already does; contributors don't have to memorise a rule, just look at the model they are adding next to.

### Follow-ups

- **Judge Protocol lift** — the first real test of Pattern A generalisability. Tracked in [GH #131](https://github.com/Toloka/tolokaforge/issues/131).
- **`TrialRunner` / `UserSimulator` lifts** — file when a second variant becomes realistic.
- **CI / lint enforcement** — deferred. Introduce only if drift returns after this ADR lands and future components still ignore the patterns.
- **Flip this ADR's status to `Accepted`** once the first post-ADR component follows the pattern by construction (rather than being retrofit into it).

## Rejected alternatives

- **Option 2 — separate ADRs per pattern.** The two patterns are complementary; components typically ship both together (a declaration + a runtime consumer). Splitting duplicates cross-references.
- **Option 3 — CI / lint enforcement.** Premature before the patterns are documented. A useful follow-up if drift returns.
- **Option 4 — defer.** That's the drift this ADR is preventing.

## Scope notes

- **No code changes.** This ADR is documentation-only. It describes what the codebase already does; it does not change any behaviour.
- **Retroactive documentation.** The patterns were established by ADRs 0003 – 0009, not by this ADR. This ADR consolidates.
- **Descriptive, not prescriptive on style.** The naming discipline (`*Config` vs no suffix) reflects what shipped; it is not a new rule.
- **No enforcement mechanism today.** Enforcement is by review. A future lint / CI step is a named follow-up, not a decision of this ADR.
- **Cross-references, not duplication.** This ADR points at existing seam / declaration ADRs for concrete examples; it does not re-explain their designs.
