# 0022. Runtime independence — Protocol registries, `run_trial`, and the `tolokaforge run-trial` subprocess contract

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tolokaforge's runtime is built from swappable seams: [`RuntimeBackend`](0007-runtime-backend-protocol.md) is the orchestrator's execution surface, [`Conductor`](0008-conductor-protocol.md) is the per-trial executor, and [`TrialGrader`](0014-trial-grader-protocol.md) is the swappable grading strategy. Each is a `@runtime_checkable` Protocol with concrete and `InMemory*` implementations. Milestone #13 turns the runner into a component an external harness can consume, over four additive surfaces:

1. **Entry-point registries** over the three existing Protocols, so a third-party `pip install` can register a backend / grader / conductor and have the orchestrator discover it.
2. A top-level **`tolokaforge.runner.run_trial(...)`** library entry, so a harness can run one trial in-process without reconstructing the orchestrator's wiring.
3. A **`tolokaforge run-trial`** CLI subprocess mode with a stable wire contract, so a harness in any language can drive one trial over a pipe.
4. A **slim runner Docker image** plus a versioning story that says what "breaking" means per surface.

None of this shape exists in code yet, and none of it is captured as an architectural decision. Discovery of the current state confirms the forces:

- **Backend selection is task-driven, not name-driven.** `Orchestrator._construct_runtime_backend` (`orchestrator.py:749`) calls `_select_backend_from_tasks` (`orchestrator.py:644`), which picks `PerTrialRuntimeBackend` when any task's manifest requires per-trial materialisation, else `SharedStackRuntimeBackend`. The `orchestrator.runtime` config field (`models.py:538`) is only a **deprecated operator override** that emits a `DeprecationWarning` and is slated for retirement (`models.py:560–574`); its `docker` value is a legacy alias for `shared`.
- **There is no registry.** Backend selection is a hard-coded dispatch, not a discovery lookup. Nothing lets an out-of-tree distribution contribute a backend.
- **There is no library or subprocess entry.** `tolokaforge/runner/__init__.py` exposes no `run_trial`; the single console script `tolokaforge = tolokaforge._entry:main` (delegating to `tolokaforge.dx.cli.main:cli`) has no `run-trial` subcommand.
- **The runner image is fat.** `runner.Dockerfile` builds from the full `tolokaforge` wheel plus a hand-maintained ~11-dependency block — 659 MB, on a python:3.12-slim base of ≈144 MB.

The five downstream sub-issues (#536–#540) each need to cite one section of a single decision as their contract source. Locking the seam shape, registry mechanics, library signature, subprocess wire format, and versioning story as a Proposed ADR **before** those tickets land is what makes them independently implementable without re-litigating the interface. This ADR is that interface deliverable; it decides nothing about implementation code.

## Decision Drivers

- **Additive only.** No change to the `RuntimeBackend` / `Conductor` / `TrialGrader` Protocol method signatures, and no re-coupling of the name-based backend selection that the `orchestrator.runtime` deprecation is removing.
- **Standard idioms over homegrown machinery.** Prefer mechanisms an external consumer already knows (`importlib.metadata` entry points, JSON Lines) so `pip install` and "pipe a subprocess" just work.
- **Fail fast.** A duplicate registration, an unknown name, or a plugin that fails to import must surface loudly with an actionable message — never silent last-wins, never a swallowed import (AGENTS.md Core Rule).
- **A single coherent versioning story.** One document that names what a breaking change is per surface, so all four seams share one compatibility contract rather than drifting apart across four ADRs.
- **Same package, same wheel.** No multi-package split; runner independence is a packaging-extra and CLI-mode story, not a repository split (#305 constraint).
- **Discoverable in ≤10 minutes.** A reader unfamiliar with the milestone can find the three group names, the `run_trial` signature, and how a harness talks to `tolokaforge run-trial` from this one file.

## Considered Options

**One ADR vs. four ADRs.**

1. **One ADR covering all four surfaces** — a single keystone that names the group registries, the library entry, the subprocess contract, and the shared versioning story in one place.
2. **Four separate ADRs**, one per surface.

**Entry-point registries vs. a homegrown registry.**

1. **`importlib.metadata` entry points** — declare group tables in `pyproject.toml`; discover with `entry_points(group=…)`.
2. **A homegrown in-process registry** — a module-level dict populated by decorators or explicit registration calls.

**`tolokaforge.runner.run_trial` backend selection: explicit-name-only vs. `"auto"` sentinel + explicit override.**

1. **`runtime="auto"` sentinel (default) + explicit override** — `"auto"` delegates to the orchestrator's task-driven selection; an explicit registry name forces a backend.
2. **Explicit-name-only** — `runtime` is required and always a registry name; the caller must choose `shared` / `per_trial` / `in_memory` itself.

## Decision

We adopt **one ADR** (Option 1 above) covering all four surfaces. A single keystone gives the four seams one coherent versioning story and one place a reader or a downstream ticket can cite, instead of four documents that would each have to restate the shared compatibility rules and risk drifting apart. Each surface is contracted by exactly one sub-issue.

We adopt **`importlib.metadata` entry points** (Option 1 above) for discovery, with **no homegrown registry**. Entry points are the standard Python extension idiom: a third-party distribution declares a group table in its own `pyproject.toml`, and after `pip install` the orchestrator discovers it with no in-tree edit. A homegrown dict would require the plugin to import a tolokaforge module at the right time and would reinvent conflict handling that `importlib.metadata` already models.

We adopt the **`runtime="auto"` sentinel with explicit override** (Option 1 above) for `run_trial` backend selection. Explicit-name-only would re-couple the name-based selection that the `orchestrator.runtime` deprecation is retiring — a harness wanting the orchestrator's task-driven choice would have to hard-code `shared` / `per_trial`. `"auto"` mirrors the CLI and keeps the library consistent with the codebase's task-driven direction; the full rationale is in [Surface 2](#surface-2--tolokaforgerunnerrun_trial-537).

This ADR locks four surfaces:

1. **Surface 1 — entry-point Protocol registries** (contract for **#536**). Three entry-point groups over the `RuntimeBackend` / `Conductor` / `TrialGrader` Protocols, their discovery mechanism, duplicate-name policy, and load-error surface. Fully specified in [Surface 1](#surface-1--entry-point-protocol-registries-536) below.
2. **Surface 2 — `tolokaforge.runner.run_trial(...)`** (contract for **#537**). The frozen library signature, its `TrialResult` return, its named error types, and the `runtime="auto"` selection model. Specified in [Surface 2](#surface-2--tolokaforgerunnerrun_trial-537) below.
3. **Surface 3 — `tolokaforge run-trial` subprocess wire format** (contract for **#538**). The JSON-Lines framing, the `"v":1` protocol version, the `start` / `event` / `result` / `error` message shapes, termination signals, and the stable-vs-experimental split. Specified in [Surface 3](#surface-3--tolokaforge-run-trial-subprocess-wire-format-538) below.
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

### Surface 2 — `tolokaforge.runner.run_trial(...)` (#537)

A top-level library entry runs one trial in-process without the caller reconstructing the orchestrator's wiring. The signature is keyword-only and frozen as the contract:

```python
def run_trial(
    *,
    task: TaskConfig,
    models: dict[str, ModelConfig | dict[str, Any]],   # role -> model config; widens RunConfig.models
                                                       #   (dict[str, ModelConfig]) to also accept raw dicts
    runtime: str = "auto",                             # "auto" -> task-driven selection; else registry name
    grader: str = "runner_rpc",
    conductor: str = "in_process",
    output_dir: Path | str | None = None,              # None -> no artifacts written to disk
    trial_index: int = 0,
) -> TrialResult: ...
```

**Return.** The existing `TrialResult` ([ADR-0003](0003-trial-spec-and-trial-result.md)) — the typed wrapper carrying the `Trajectory`, the `Grade`, and termination metadata. `run_trial` reuses the wire type; it does not introduce a new wrapper.

**Error types.** No exception is swallowed; each failure mode maps to a named type:

- An unknown `runtime` / `grader` / `conductor` name → the [Surface 1](#surface-1--entry-point-protocol-registries-536) registry error (lists the known registered names for that group).
- An invalid `task` or `models` value → Pydantic `ValidationError`.
- A substrate-provisioning failure → `ProvisionError` ([ADR-0010](0010-runtime-backend-provisioning-contract.md)).

**`runtime="auto"` reconciliation.** Backend selection in the codebase is task-driven by default: `_select_backend_from_tasks` (`orchestrator.py:644`) picks `per_trial` when a task requires per-trial isolation, else `shared`. The name-based `orchestrator.runtime` config field (`models.py:560–574`) is a deprecated, retiring override. A required name-only `run_trial(runtime=…)` would re-couple exactly what that deprecation removes — a harness wanting the orchestrator's auto-selection would have to hard-code `shared` / `per_trial`. So `"auto"` is the default and mirrors the CLI: it inspects `task.environment_manifest` and picks `per_trial` when the task requires per-trial isolation, else `shared`. An explicit registry name (`"shared"` / `"per_trial"` / `"in_memory"` / a plug-in name) forces that backend via the Surface 1 registry.

`auto` is a **reserved parameter value, not a registrable backend name.** The sentinel is intercepted before the registry lookup, so a plug-in cannot register a backend under the name `auto` — Surface 1's fail-loud policy is preserved, and there is no ambiguity between the sentinel and a discovered name.

### Surface 3 — `tolokaforge run-trial` subprocess wire format (#538)

`tolokaforge run-trial` runs one trial as a subprocess a harness in any language drives over a pipe.

**Framing.** JSON Lines — UTF-8, `\n`-delimited, one JSON object per line, on both stdin and stdout. Language-agnostic, streamable, and debuggable by piping; chosen over a length-prefixed binary framing for exactly those properties.

**Protocol version.** Every message carries `"v": 1`. The wire-protocol version is **independent of the tolokaforge package version** — this field is the mechanism behind "no CLI change breaks the contract without a version bump": any breaking change to the envelope or a required field bumps `v`, and additive changes stay within the current `v`.

**stdin (request).** One `start` message, then EOF — one trial per invocation. `task` and `models` mirror the `run_trial` arguments; `runtime` / `grader` / `conductor` are registry names:

```json
{"v":1,"type":"start","task":{},"models":{},"runtime":"shared","grader":"runner_rpc","conductor":"in_process"}
```

An optional `cancel` control message may precede completion:

```json
{"v":1,"type":"cancel"}
```

**stdout (response).** One JSON object per line, discriminated by `type`:

```json
{"v":1,"type":"event","event":"provisioned"}
{"v":1,"type":"result","result":{}}
{"v":1,"type":"error","error_type":"ProvisionError","message":"stack failed to become ready","fatal":true}
```

- `event` — progress notification; the `event` field names the subtype, and subtypes are additive/experimental.
- `result` — terminal success; `result` is the serialised `TrialResult` (Surface 2).
- `error` — a typed error, never a raw traceback; `error_type` is the named error class, `message` is human-readable, `fatal` is a boolean.

**Termination and exit codes.**

- Natural end → a `result` message, then exit 0.
- Error → an `error` message, then a non-zero exit.
- External cancel (SIGTERM, or premature stdin EOF, or a `cancel` message) → clean teardown, then an `error` message with `error_type` `"cancelled"`, then a non-zero exit.

**Stable vs. experimental.** The envelope (`v`, `type`, the JSON-Lines framing) and the `start` / `result` / `error` message shapes are **stable** — changing any of them requires a `v` bump. `event` subtypes are **experimental**: new subtypes may be added within the current `v` without a bump, so a harness must tolerate unknown `event` values.

### Surface 4 — versioning, slim-image budget, and lifecycle

#### 4a — Versioning + compatibility

This milestone adds three compatibility surfaces. Each carries a breaking-change definition:

1. **Group names + built-in registration names.** Breaking = renaming or removing a group or a built-in name, or changing the duplicate / load-error semantics. Additive = a new registered name.
2. **`run_trial` signature + return type** (published Python API). Breaking = removing or renaming a parameter, narrowing an accepted type, or changing the return type or the named error types. Additive = a new keyword-only parameter with a default.
3. **`run-trial` wire format.** Breaking = any change to the envelope / framing, or to a required field of `start` / `result` / `error` under the current `v` — which requires a `v` bump. Additive = a new `event` subtype within the current `v`.

**Experimental — not compatibility surfaces:** `event` message subtypes (they grow additively within `v`), and the internal registry-loader implementation.

**CHANGELOG discipline.** Any future change to the three surfaces requires a CHANGELOG entry. This milestone is additive, so `Unreleased/Feat` entries land with each sub-issue and no version bump is taken (the #305 constraint).

#### 4b — Slim-image budget + policy

**Baseline.** The pre-slim runner image measured **659 MB**, measured 2026-07-20 via `docker images` on the freshly-built `tolokaforge-runner` tag, on a `python:3.12-slim` base of ≈144 MB. It was built from the full `tolokaforge` wheel plus a hand-maintained ~11-dependency block in `runner.Dockerfile`. The number is recorded with this provenance so a future reader can re-measure rather than trust a bare figure.

**Target.** ≥40% smaller uncompressed (the #539 acceptance criterion).

**Outcome.** The multi-stage slim image measures **390 MB — a 40.8% reduction**, meeting the target. The saving is compat-safe: it comes from keeping the build-only apt toolchain and the docker CLI out of the runtime stage, installing the wheel with `--no-compile` (no `*.pyc` bytecode), and stripping the pip/setuptools/wheel toolchain — not from dropping any capability. The domain-tool drivers stay in the image (via the `runner` extra), so `tolokaforge-runner:local` serves every task pack unchanged.

390 MB is the honest reachable minimum given the in-runner LLM-judge requirement: `litellm` (~105 MB, executed in-container for the `GradeTrial` RPC), the `python:3.12-slim` base (~144 MB), and the domain-tool drivers (~71 MB, kept for `:local` compatibility) dominate it. No tighter forward target is committed here — the only remaining headroom is out-of-scope work already tracked (registry publication; a smaller-base investigation deferred as too risky).

**Policy.** A `runner` extra — same package, same wheel, no multi-package split — installs the runner-only subset, with the runner import boundary enforced by a test. The extra's exact dependency list is #539's call.

#### 4c — ADR lifecycle

Accepted at milestone #13 close-out (#540), matching the 0009 / 0010 / 0014 / 0015 Proposed→Accepted lifecycle.

## Consequences

### Positive

- External harnesses gain three documented ways to consume the runner — register a plugin, call `run_trial`, or drive `tolokaforge run-trial` — without a repository or package split.
- A single keystone gives all four seams one versioning contract, so downstream tickets #536–#540 each cite one section rather than re-deriving the compatibility rules.

### Negative / Trade-offs

- Entry-point discovery makes the set of available backends / graders / conductors depend on what is installed in the environment, not only on what ships in-tree — a metadata scan at load time and one more place a misconfigured environment can surface.
- JSON-Lines framing costs one JSON parse per message. Fine for one-trial-per-invocation, but noted so a future high-throughput design can consciously choose a different framing rather than inherit this one by default.

### Follow-ups

- Code changes required: Surface 1 in #536, Surface 2 in #537, Surface 3 in #538, the slim runner image in #539; verification and the Proposed→Accepted flip in #540.
- Documentation to update: `docs/RUNTIME_BACKENDS.md` seam-count drift (#541) and the ADR-index link-integrity test (#542), each deferred to a separate PR.

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
