# 0051. The `UserSimulator` Protocol and entry-point registry

- **Status:** Accepted
- **Date:** 2026-09-28
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Realizes:** [ADR-0011](0011-seam-and-declaration-conventions.md) § "Follow-up lifts for existing one-impl components"

## Context and Problem Statement

ADR-0011 codified Pattern A (Protocol + built-in impl + fixture + contract test
+ ADR) and listed the one-impl components awaiting the lift. [ADR-0050](0050-agent-loop-protocol-and-registry.md)
closed the `AgentLoop` lift and named the last one still open:

> **User-simulator Protocol lift** — the remaining ADR-0011 follow-up, still
> unfiled.

A second simulator shape is now realistic: a task whose dialogue is produced by
a simulator with its own system prompt, its own sampling and its own multi-step
turn structure — a shape the built-in simulator does not produce and that a
downstream package would rather own than push into the engine field by field.
Today the conductor constructs the one simulator as a literal
`UserSimulator(...)` (`tolokaforge/core/conductor.py`). There is no name to
select a different simulator by, and no place a downstream package could
register one — so every alternative dialogue producer costs a framework PR
against the conductor, or a new opt-in field on the engine's own simulator.

Every other comparable decision in the engine is already a registry: turn
policies (ADR-0028), agent loops (ADR-0050), conductors (ADR-0008), runtime
backends, grader kinds (ADR-0043) and judge kinds (ADR-0049). The user
simulator is the conspicuous exception — and the one whose absence pulls
benchmark-shaped prompt and sampling knobs onto a shared engine component.

## Decision Drivers

- **The idiom is settled.** The engine's entry-point-registry seams share one
  discovery primitive, one fail-loud error surface and one
  `Callable[[<Context>], <Impl>]` factory shape. One more costs almost nothing
  to add and nothing to learn.
- **The default must not move.** This is a pure refactor: every shipped pack and
  run config grades byte-for-byte identically with no config change, and the
  built-in simulator's behaviour is untouched.
- **The built-in must not be special-cased.** A registry the built-in bypasses
  is not a seam, it is an unused branch. The proof the seam is real is that the
  built-in simulator resolves through it like any third-party simulator.
- **Benchmark-shaped configuration stays out of the engine schema.** A simulator
  written to reproduce another harness's dialogue carries fields the engine has
  no business validating; the seam gives that configuration an opaque home so it
  never enters the engine's own config models.

## Considered Options

1. **`UserSimulator` Protocol + `tolokaforge.user_simulators` entry-point group
   + `actors.user.simulator` selector + an opaque `actors.user.simulator_config`
   passthrough.** **This ADR.**
2. **Keep adding opt-in fields to the built-in simulator.** Rejected. Every new
   dialogue behaviour (a task-authored prompt, a sampling knob, a multi-step
   turn structure) becomes a field on `ActorSpec` / `UserSimulatorConfig` and a
   branch inside the one simulator, so the shared engine component accretes
   surface that exists only to reproduce one benchmark — the drift ADR-0011 was
   written to prevent.
3. **A run-level selector `orchestrator.user_simulator`, mirroring
   ADR-0050's `orchestrator.agent_loop`.** Rejected — see the selector decision
   below. ADR-0050 put the loop selector on `OrchestratorConfig` *because*
   `ModelConfig` is shared across the agent, user and judge roles; that
   rationale does not apply to the user simulator, which is already
   actor-scoped config.
4. **Overload the `AgentLoop` seam.** Rejected. A user simulator and an agent
   loop are orthogonal axes: the loop drives the agent's turn cycle, the
   simulator produces the user's turns, and a trial pairs any loop with any
   simulator. The loop seam is also the wrong action-format axis — a simulator
   that keeps the provider `tool_calls` shape needs no new loop.

## Decision

We adopt **Option 1**.

### `UserSimulator` Protocol

`@runtime_checkable` Protocol in `tolokaforge/core/actors/user_simulator.py`,
extending the existing `Actor` Protocol (`tolokaforge/core/actors/actor.py`)
with the one attribute the runner reads off a simulator directly:

```python
@runtime_checkable
class UserSimulator(Actor, Protocol):
    last_system_prompt: str | None
```

`Actor` already pins the reply contract
(`reply(context, *, observation) -> GenerationResult`); a user simulator adds
`last_system_prompt`, which `TrialRunner` captures after the first user turn and
writes to the trial bundle's `prompts.yaml`. A simulator that never dispatches
an LLM turn leaves it `None`. Formalising the attribute turns the runner's
`getattr` soft-contract into a declared one an implementer reads.

### `UserSimulatorContext` + registry

`UserSimulatorContext` is a frozen dataclass in the same module carrying the
per-trial inputs the conductor supplies today: `mode`, `persona`, `backstory`,
`scripted_flow`, `tool_schemas`, `llm_config`, `rate_limit_probe`, `tool_turns`,
the opaque `simulator_config`, and `task_dir`. The first four are the engine's
built-in simulator fields; `llm_config` / `tool_schemas` / `rate_limit_probe`
are the trial dependencies the built-in needs. `task_dir` is the task's
directory, against which a simulator resolves the paths its `simulator_config`
names (#1666).

Simulators register under a new entry-point group
**`tolokaforge.user_simulators`**, resolved by `load_user_simulator(name)` and
listed by `available_user_simulators()` — the same fail-loud
`discover_entry_points` / `_load` machinery every sibling loader uses, with
`UnknownImplementationError` naming the known registrations on a typo.

The Protocol, the context and the `UserSimulatorFactory` alias are *declared*
together in `core/actors/user_simulator.py`; `plugin_registry` owns only the
group constant, the loader and the listing, and re-exports the three names — the
same split ADR-0050 uses for `AgentLoop`. The built-in factory
`_builtin_user_simulator_factory` lives beside the concrete class in
`tolokaforge/core/llm/client.py`, as `AgentLoop`'s built-in factory lives beside
`ToolCallingLoop`.

### The opaque `simulator_config` passthrough

`actors.user.simulator_config` is a mapping the engine passes to the factory on
the context verbatim and never interprets. A non-built-in simulator declares its
own fields there and validates them into its own model; the built-in simulator
ignores it. This is a deliberate divergence from ADR-0050, whose loops need no
per-implementation configuration: a simulator written to reproduce another
harness's dialogue carries prompt-template paths, sampling settings and persona
framing that are that simulator's concern, and routing them through an opaque
mapping keeps them out of the engine's own config models — the engine schema
stays generic, and the benchmark's fields live with the benchmark's simulator.

### The built-in registers like anything else

`builtin` is a row in `[project.entry-points."tolokaforge.user_simulators"]`
pointing at `tolokaforge.core.llm.client:_builtin_user_simulator_factory`, which
builds the existing simulator from the context. The conductor has no branch for
it. The concrete class is renamed `BuiltinUserSimulator` so the Protocol takes
the clean `UserSimulator` name, matching `AgentLoop` / `ToolCallingLoop` and
`Actor` / its implementations.

### One construction site

`InMemoryConductor` resolves `load_user_simulator(sim.simulator)` and calls the
resulting factory with a `UserSimulatorContext` built from the resolved
`actors.user` config, in place of the former literal. The `TrialRunner`,
`TurnPolicy` and `UserTurn` seams are untouched: the built simulator satisfies
`Actor`, which is what the turn-policy seam already dispatches.

### The selector is `actors.user.simulator`

`ActorSpec.simulator: str | None` and `UserSimulatorConfig.simulator: str =
"builtin"` (with the matching `simulator_config`), resolved through
`TaskConfig.resolve_user_simulator()` like every other simulator field.

Rejected alternative: a run-level `orchestrator.user_simulator`. ADR-0050 put
`agent_loop` on `OrchestratorConfig` because `ModelConfig` — the type its
rejected `models.agent.loop` alternative would have extended — is shared across
the agent, user and judge roles, so a per-role field there is meaningless on two
of three. The user simulator has no such problem: it is already actor-scoped
config under `actors.user`, it is what a benchmark adapter sets on the
`TaskConfig` it builds in Python, and a project can carry it in
`task_defaults.actors.user` so a whole pack inherits one simulator. Putting the
selector anywhere but beside the rest of the simulator's config would split one
actor's configuration across two blocks.

The name is resolved before any trial work. `Orchestrator.load_tasks` calls
`_refuse_an_unregistered_user_simulator` after tasks load: it walks each
conversational task, resolves its `actors.user.simulator`, and refuses the run
once per distinct name — so a typo, or an editable install whose `.dist-info`
predates the `tolokaforge.user_simulators` group, is one error naming the
registered simulators and the task that asked for it, not one scored failure per
trial after each container is already up. `agent_only` tasks resolve no user
actor and are skipped.

### Placement relative to the runner subset

`load_user_simulator` is called only from the conductor and the orchestrator,
which are orchestrator-side. The group is therefore **not** in
`RUNNER_REACHABLE_ENTRY_POINT_GROUPS`; `tests/canonical/test_runner_subset_partition`
locks the loader-to-group mapping so a runner-subset call site would fail the
partition test. The Protocol module imports only `Actor` and, under
`TYPE_CHECKING`, the config value types, so it carries no orchestrator-only
weight.

## Consequences

### Positive

- A second simulator shape is a `pyproject.toml` row plus one class, not a
  framework PR and not a new field on the engine's own simulator.
- A benchmark simulator's configuration lives in the opaque `simulator_config`
  it validates itself, so the engine's `ActorSpec` / `UserSimulatorConfig` stay
  generic instead of accreting benchmark-shaped fields.
- The `last_system_prompt` attribute the runner reads is written down in the
  Protocol where an implementer reads it, instead of being an undeclared
  `getattr`.
- `actors.user.simulator` resolves once before any trial, so an unregistered
  name is one refusal naming the known registrations and the task, not one
  scored trial failure per trial.
- ADR-0011's last named follow-up lift is closed with the pattern ADR-0011
  itself prescribes.

### Negative / Trade-offs

- Two classes read `UserSimulator` in the tree's history — the Protocol keeps
  the name and the concrete becomes `BuiltinUserSimulator`. The rename is a
  clean internal refactor in one change, per AGENTS.md's internals rule.
- `simulator_config` is an opaque mapping the engine cannot validate; a
  malformed value surfaces only when the simulator that owns it reads it. That
  is the cost of keeping benchmark-shaped fields out of the engine schema, and
  the owning simulator validates the mapping into its own model at build time.

### Follow-ups

- **A shipped non-built-in simulator** — this ADR ships the seam and the
  built-in only. A benchmark-parity simulator is the first consumer.

## Links

- Realizes: [ADR-0011](0011-seam-and-declaration-conventions.md) (Pattern A; names this lift).
- Related ADRs: [0050](0050-agent-loop-protocol-and-registry.md) (the sibling in-process seam, same idiom; names this follow-up), [0028](0028-multi-actor-turn-policy.md) (turn policy — who speaks next, the adjacent axis), [0032](0032-agent-completion-is-structural.md) (`###STOP###` is the user simulator's), [0046](0046-judgekind-protocol-and-registry.md) (registry-backed seam, same idiom).
- Related code: `tolokaforge/core/actors/user_simulator.py`, `tolokaforge/core/llm/client.py`, `tolokaforge/core/plugin_registry.py`, `tolokaforge/core/models/task_config.py`, `tolokaforge/core/conductor.py`, `tolokaforge/core/orchestrator.py`.
- Docs: [`docs/CONFIG.md`](../CONFIG.md) § `actors:`, [`docs/PROJECTS.md`](../PROJECTS.md), [`docs/RUNTIME_BACKENDS.md`](../RUNTIME_BACKENDS.md) § Plug-in extension points, [`docs/AUTHORING_AN_ADAPTER.md`](../AUTHORING_AN_ADAPTER.md).
