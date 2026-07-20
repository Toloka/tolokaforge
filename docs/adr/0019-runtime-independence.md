# 0019. Runtime independence — Protocol registries, `run_trial`, and the `tolokaforge agent` subprocess contract

- **Status:** Proposed
- **Date:** 2026-07-20
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tolokaforge's runtime is built from swappable seams: [`RuntimeBackend`](0007-runtime-backend-protocol.md) is the orchestrator's execution surface, [`Conductor`](0008-conductor-protocol.md) is the per-trial executor, and [`TrialGrader`](0014-trial-grader-protocol.md) is the swappable grading strategy. Each is a `@runtime_checkable` Protocol with concrete and `InMemory*` implementations. Milestone #13 turns the runner into a component an external harness can consume, over four additive surfaces:

1. **Entry-point registries** over the three existing Protocols, so a third-party `pip install` can register a backend / grader / conductor and have the orchestrator discover it.
2. A top-level **`tolokaforge.run_trial(...)`** library entry, so a harness can run one trial in-process without reconstructing the orchestrator's wiring.
3. A **`tolokaforge agent`** CLI subprocess mode with a stable wire contract, so a harness in any language can drive one trial over a pipe.
4. A **slim runner Docker image** plus a versioning story that says what "breaking" means per surface.

None of this shape exists in code yet, and none of it is captured as an architectural decision. Discovery of the current state confirms the forces:

- **Backend selection is task-driven, not name-driven.** `Orchestrator._construct_runtime_backend` (`orchestrator.py:749`) calls `_select_backend_from_tasks` (`orchestrator.py:644`), which picks `PerTrialRuntimeBackend` when any task's manifest requires per-trial materialisation, else `SharedStackRuntimeBackend`. The `orchestrator.runtime` config field (`models.py:538`) is only a **deprecated operator override** that emits a `DeprecationWarning` and is slated for retirement (`models.py:560–574`); its `docker` value is a legacy alias for `shared`.
- **There is no registry.** Backend selection is a hard-coded dispatch, not a discovery lookup. Nothing lets an out-of-tree distribution contribute a backend.
- **There is no library or subprocess entry.** `tolokaforge/__init__.py` exposes a lazy `__getattr__` public API with no `run_trial`; the single console script `tolokaforge = tolokaforge.cli.main:cli` has no `agent` subcommand.
- **The runner image is fat.** `runner.Dockerfile` builds from the full `tolokaforge[docker]` wheel plus ~11 extra runtime deps — 659 MB, on a python:3.12-slim base of ≈144 MB.

The five downstream sub-issues (#536–#540) each need to cite one section of a single decision as their contract source. Locking the seam shape, registry mechanics, library signature, subprocess wire format, and versioning story as a Proposed ADR **before** those tickets land is what makes them independently implementable without re-litigating the interface. This ADR is that interface deliverable; it decides nothing about implementation code.

## Decision Drivers

- **Additive only.** No change to the `RuntimeBackend` / `Conductor` / `TrialGrader` Protocol method signatures, and no re-coupling of the name-based backend selection that the `orchestrator.runtime` deprecation is removing.
- **Standard idioms over homegrown machinery.** Prefer mechanisms an external consumer already knows (`importlib.metadata` entry points, JSON Lines) so `pip install` and "pipe a subprocess" just work.
- **Fail fast.** A duplicate registration, an unknown name, or a plugin that fails to import must surface loudly with an actionable message — never silent last-wins, never a swallowed import (AGENTS.md Core Rule).
- **A single coherent versioning story.** One document that names what a breaking change is per surface, so all four seams share one compatibility contract rather than drifting apart across four ADRs.
- **Same package, same wheel.** No multi-package split; runner independence is a packaging-extra and CLI-mode story, not a repository split (#305 constraint).
- **Discoverable in ≤10 minutes.** A reader unfamiliar with the milestone can find the three group names, the `run_trial` signature, and how a harness talks to `tolokaforge agent` from this one file.

## Considered Options

**One ADR vs. four ADRs.**

1. **One ADR covering all four surfaces** — a single keystone that names the group registries, the library entry, the subprocess contract, and the shared versioning story in one place.
2. **Four separate ADRs**, one per surface.

**Entry-point registries vs. a homegrown registry.**

1. **`importlib.metadata` entry points** — declare group tables in `pyproject.toml`; discover with `entry_points(group=…)`.
2. **A homegrown in-process registry** — a module-level dict populated by decorators or explicit registration calls.

(Stage 2 adds the `run_trial` selection-model option row — explicit-name-only vs. an `"auto"` sentinel with explicit override.)

## Decision

We adopt **one ADR** (Option 1 above) covering all four surfaces. A single keystone gives the four seams one coherent versioning story and one place a reader or a downstream ticket can cite, instead of four documents that would each have to restate the shared compatibility rules and risk drifting apart. Each surface is contracted by exactly one sub-issue.

We adopt **`importlib.metadata` entry points** (Option 1 above) for discovery, with **no homegrown registry**. Entry points are the standard Python extension idiom: a third-party distribution declares a group table in its own `pyproject.toml`, and after `pip install` the orchestrator discovers it with no in-tree edit. A homegrown dict would require the plugin to import a tolokaforge module at the right time and would reinvent conflict handling that `importlib.metadata` already models.

This ADR locks four surfaces:

1. **Surface 1 — entry-point Protocol registries** (contract for **#536**). Three entry-point groups over the `RuntimeBackend` / `Conductor` / `TrialGrader` Protocols, their discovery mechanism, duplicate-name policy, and load-error surface. Fully specified in [Surface 1](#surface-1--entry-point-protocol-registries-536) below.
2. **Surface 2 — `tolokaforge.run_trial(...)`** (contract for **#537**). The frozen library signature, its `TrialResult` return, its named error types, and the `runtime="auto"` selection model. Specified in [Surface 2](#surface-2--tolokaforgerun_trial-537) below.
3. **Surface 3 — `tolokaforge agent` subprocess wire format** (contract for **#538**). The JSON-Lines framing, the `"v":1` protocol version, the `start` / `event` / `result` / `error` message shapes, termination signals, and the stable-vs-experimental split. Specified in [Surface 3](#surface-3--tolokaforge-agent-subprocess-wire-format-538) below.
4. **Surface 4 — versioning, slim-image budget, and lifecycle**. What a breaking change is per surface and the CHANGELOG discipline ([4a](#4a--versioning--compatibility), contract for **#540**); the slim runner-image budget with measured baseline ([4b](#4b--slim-image-budget--policy), contract for **#539**); and this ADR's Proposed→Accepted lifecycle ([4c](#4c--adr-lifecycle), referenced by **#540**).

### Surface 1 — entry-point Protocol registries (#536)

The three existing Protocols become discoverable extension points. Each Protocol keeps its governing ADR as the authority on its method contract; this surface adds only the discovery layer over them.

| Group name | Protocol | Governing ADR |
|---|---|---|
| `tolokaforge.runtime_backends` | `RuntimeBackend` | [ADR-0007](0007-runtime-backend-protocol.md) |
| `tolokaforge.trial_graders` | `TrialGrader` | [ADR-0014](0014-trial-grader-protocol.md) |
| `tolokaforge.conductors` | `Conductor` | [ADR-0008](0008-conductor-protocol.md) |

Group names are dotted and namespaced under `tolokaforge.` so an out-of-tree distribution's entry points cannot collide with another package's groups.

**Discovery.** `importlib.metadata.entry_points(group="<name>")`. No homegrown registry — a distribution declares the group table in its own `pyproject.toml`, and discovery is a metadata query at load time.

**Duplicate-name policy.** Fail loud. Two entry points that share a name within one group raise at load, naming **both** providing distributions. Silent last-wins would hide a genuine conflict between two installed plugins; surfacing both distributions makes the collision diagnosable.

**Load-time error surface.** An unknown name raises a specific, actionable error that lists the known registered names for that group — preserving today's `KeyError`-adjacent shape but made actionable. A plugin whose entry point fails to **import** propagates the import error loudly; a broken plugin is never silently skipped.

**Built-in registrations** (declared in tolokaforge's own `pyproject.toml` entry-point tables):

| Group | Name | Target |
|---|---|---|
| `tolokaforge.runtime_backends` | `shared` | `SharedStackRuntimeBackend` |
| `tolokaforge.runtime_backends` | `per_trial` | `PerTrialRuntimeBackend` |
| `tolokaforge.runtime_backends` | `in_memory` | `InMemoryRuntimeBackend` |
| `tolokaforge.trial_graders` | `runner_rpc` | `RunnerRPCTrialGrader` |
| `tolokaforge.conductors` | `in_process` | `InProcessConductor` |
| `tolokaforge.conductors` | `in_memory` | `InMemoryConductor` |

The registry has **no `docker` name.** `docker` is a legacy alias for `shared` that lives on the *deprecated* `orchestrator.runtime` config field (`models.py:538`), resolved at config-load **before** any registry lookup. It is accepted only while that deprecated field exists and disappears when the field is retired — it is not a permanent registry entry.

### Surface 2 — `tolokaforge.run_trial(...)` (#537)

The frozen `tolokaforge.run_trial(...)` signature, its `TrialResult` return contract, its named error types, and the `runtime="auto"` selection model that reconciles the library surface with the codebase's task-driven backend selection.

### Surface 3 — `tolokaforge agent` subprocess wire format (#538)

The JSON-Lines framing on stdin/stdout, the `"v":1` protocol version independent of the package version, the `start` request shape, the `event` / `result` / `error` response shapes, the termination signals and exit codes, and the stable-vs-experimental split.

### Surface 4 — versioning, slim-image budget, and lifecycle

#### 4a — Versioning + compatibility

What a breaking change is per surface (group + built-in names; `run_trial` signature + return; `agent` wire format), which parts are experimental rather than compatibility surfaces, and the CHANGELOG discipline.

#### 4b — Slim-image budget + policy

The runner-image baseline (659 MB, measured 2026-07-20) with its measurement provenance, the ≥40% reduction target, and the `runner`-extra policy — same package, same wheel.

#### 4c — ADR lifecycle

Proposed on merge; flips to Accepted in the #540 consolidation PR, matching the 0009 / 0010 / 0014 / 0015 lifecycle.

## Consequences

### Positive

- External harnesses gain three documented ways to consume the runner — register a plugin, call `run_trial`, or drive `tolokaforge agent` — without a repository or package split.
- A single keystone gives all four seams one versioning contract, so downstream tickets #536–#540 each cite one section rather than re-deriving the compatibility rules.

### Negative / Trade-offs

- Entry-point discovery makes the set of available backends / graders / conductors depend on what is installed in the environment, not only on what ships in-tree — a metadata scan at load time and one more place a misconfigured environment can surface.

### Follow-ups

- Code changes required: Surface 1 in #536, Surface 2 in #537, Surface 3 in #538, the slim runner image in #539; verification and the Proposed→Accepted flip in #540.

## Links

- Related ADRs:
  - [ADR-0007](0007-runtime-backend-protocol.md) — `RuntimeBackend` Protocol
  - [ADR-0008](0008-conductor-protocol.md) — `Conductor` Protocol
  - [ADR-0011](0011-seam-and-declaration-conventions.md) — Seam-definition and data-declaration conventions
  - [ADR-0014](0014-trial-grader-protocol.md) — `TrialGrader` Protocol
  - [ADR-0015](0015-trial-executor-protocol.md) — `TrialExecutor` Protocol
- Related issues:
  - [GH #305](https://github.com/Toloka/tolokaforge/issues/305) — runtime-independence umbrella
  - [GH #156](https://github.com/Toloka/tolokaforge/issues/156) — runner-as-consumable-component design context
