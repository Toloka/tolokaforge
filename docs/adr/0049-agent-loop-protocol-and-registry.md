# 0049. The `AgentLoop` Protocol and entry-point registry

- **Status:** Accepted
- **Date:** 2026-09-27
- **Deciders:** @CiroGamboa
- **Supersedes:** none
- **Realizes:** [ADR-0011](0011-seam-and-declaration-conventions.md) § "Follow-up lifts for existing one-impl components"

## Context and Problem Statement

ADR-0011 codified Pattern A (Protocol + built-in impl + fixture + contract
test + ADR) and listed three one-impl components awaiting the lift. One of
them was named explicitly:

> **`TrialRunner` → `AgentLoop` Protocol** (not yet filed). File when a second
> loop shape (deep-research, agent-debate, multi-agent) becomes realistic.

A second loop shape is now realistic: a terminal-oriented loop, whose prompt
contract and action format differ from the provider-`tool_calls` shape the
engine's loop assumes. Today the engine has exactly one in-process loop, and
`TrialRunner.run` constructs it as a literal `ToolCallingLoop(...)`. There is
no name to select a different one by, and no place a downstream package could
register one — so every alternative loop shape costs a framework PR against
the runner.

Every other comparable decision in the engine is already a registry: turn
policies (ADR-0028), conductors (ADR-0008), judge-model providers, rubric
evaluators, transcript-rule matchers, state-check backends (ADR-0040),
grader kinds (ADR-0043) and judge kinds (ADR-0046). The agent loop is the
conspicuous exception.

## Decision Drivers

- **The idiom is settled.** The engine's entry-point-registry seams share one
  discovery primitive, one fail-loud error surface and one
  `Callable[[<Context>], <Impl>]` factory shape. One more costs almost
  nothing to add and nothing to learn.
- **The default must not move.** This is a pure refactor: every shipped pack
  and run config must grade byte-for-byte identically with no config change.
- **The built-in must not be special-cased.** A registry the native
  implementation bypasses is not a seam, it is an unused branch. The proof
  the seam is real is that `ToolCallingLoop` resolves through it like any
  third-party loop.
- **The invisible contract must become a written one.** The loop's obligation
  to keep declared and recorded call ids identical is enforced at grading time
  by an exception a loop author would only discover after writing one.

## Considered Options

1. **`AgentLoop` Protocol + `tolokaforge.agent_loops` entry-point group +
   `orchestrator.agent_loop` selector.** **This ADR.**
2. **Register loops in the coding-harness registry.** Rejected.
   `ModelConfig.harness` validates against `accepted_harnesses()`, and a
   `HarnessSpec` is CLI-shaped — install command, version probe, `provider_env`
   — with `resolve_harness_spec` refusing a spec-less name. An in-process loop
   runs no CLI, installs nothing, and has no provider env to gate. The harness
   branch is also structurally different: it is *one tool call*, no LLM turn
   cycle at all.
3. **Overload `interaction_mode`.** Rejected. It is a closed
   `Literal["conversational", "agent_only"]`, and per ADR-0028 a turn policy
   answers *who speaks next* — never what prompt contract or action format the
   agent turn uses. A terminal loop and the engine loop can both run under
   either interaction mode; the two axes are orthogonal, and collapsing them
   would make every future (mode × loop) pair a new `interaction_mode` value.
4. **Keep the literal and add an `if` when the second loop lands.** Rejected —
   that is exactly the drift ADR-0011 was written to prevent, and the
   consequence is a framework PR per loop shape.

## Decision

We adopt **Option 1**.

### `AgentLoop` Protocol

`@runtime_checkable` Protocol in `tolokaforge/core/loop.py`, beside the five
seams the module already declares (`LoopLLMClient`, `TerminationPolicy`,
`UserTurn`, `MetricsSink`, `ErrorClassifier`). One method, the signature
`ToolCallingLoop.run` already had:

```python
def run(self, system_prompt: str, messages: list[Message], start_time: float) -> LoopOutcome: ...
```

`messages` is mutated in place — the caller owns the list and assembles the
trajectory from it.

**The Protocol's docstring pins the contracts grading enforces and the type
checker does not**, because they are otherwise invisible. Three of them:

1. **Every call id comes from `context.call_ids.assign(...)`** and is used
   identically as the assistant `Message`'s `ToolCall.id`, as
   `recorder.record(call_id=...)` and as
   `tool_executor.execute(..., call_id=...)`. Agreeing on a raw provider id is
   *not* sufficient: `build_trial_timeline` re-derives keys per view with
   `episode_unique_call_ids` — declaration order for the messages, execution
   order for the records — so a provider that repeats a raw id within an
   episode (`moonshotai/kimi-k3`'s `<tool>:<index>` shape) plus out-of-order
   parallel execution makes the two derivations disagree.
   `_require_records_reconcile` raises `TimelineInconsistencyError` when the
   mis-paired calls name different tools, and the join is silently wrong when
   they name the same one. Pre-assigned ids are already episode-unique, so both
   derivations are the identity and ordering cannot matter.
   `ToolCallingLoop._assign_call_ids` is where the built-in satisfies this.
2. **A failed tool call's `role: tool` message content carries
   `TOOL_ERROR_MESSAGE_PREFIX`.** The message view records no status, so a
   trial re-graded from messages alone recovers the result text by stripping
   that prefix (`_append_message_result`). A loop that formats tool errors any
   other way makes every failed call read as successful to `result:` trace
   checks — a wrong grade, not an error. The constant is declared in the
   stdlib-only leaf `core/tool_message_format.py` and imported by both
   `core/loop.py` and `core/grading/trace_timeline.py`, so producer and
   consumer cannot drift — and the grading path keeps the import footprint
   that makes `tolokaforge retrace` incapable of reaching an LLM client.
3. **`metrics`, `should_terminate` and a supplied `user_turn` are
   obligations.** See `AgentLoopContext` below.

A loop whose action format is text rather than provider `tool_calls` therefore
normalises each parsed action into a `ToolCall` carrying the assigned id
*before* appending the assistant message. Emitting prose and recording
separately produces an ungradeable trial, not a degraded one.

### `AgentLoopContext` + registry

`AgentLoopContext` is a frozen dataclass in the same module carrying the
trial-scoped dependencies the runner supplies today: the LLM client, the tool
executor and its schemas, the loop budget, the metrics sink, the termination
and user-turn seams, the tool-call recorder, the episode call-id assigner, the
rate limiter, the error classifier, the trial logger, and the display /
observability observation sinks.

A factory ignores the genuinely optional fields — the rate limiter, the
argument normaliser, the two observation sinks, the validation schemas, the
per-tool output cap — and the cost is a visibly degraded run. Four are not
optional whatever the annotations allow, because ignoring them is silent:
`metrics` (the run's spend accumulator sums the trials' `metrics.cost_usd`, so
an unfed sink leaves the run's cost limit (`compute.max_budget_usd`) unable
to fire however much the run costs), `should_terminate` (the trial's stuck detection), a supplied
`user_turn` (ignoring it runs a `conversational` trial agent-only, collapsing
the orthogonality this ADR relies on), and the `recorder` / `call_ids` pair
that carries obligation 1.

Loops register under a new entry-point group **`tolokaforge.agent_loops`**,
resolved by `load_agent_loop(name)` and listed by `available_agent_loops()` —
the same fail-loud `discover_entry_points` / `_load` machinery every sibling
loader uses, with `UnknownImplementationError` naming the known registrations
on a typo.

The Protocol, the context and the factory alias are *declared* beside each
other in `core/loop.py`, matching `RubricEvaluatorFactory` /
`StateCheckBackendFactory`: the seam's whole contract stays in one module and
`plugin_registry` owns only the group constant, the loader and the listing.
`plugin_registry` re-exports all three names, so an implementer reaches the
agent-loop seam the same way the turn-policy seam is reached
(`TurnPolicyContext` + `TurnPolicyFactory`) rather than having to know which
module declares which half.

### The built-in registers like anything else

`engine-loop` is a row in `[project.entry-points."tolokaforge.agent_loops"]`
pointing at `tolokaforge.core.loop:_engine_loop_factory`, which builds the
existing `ToolCallingLoop` from the context. The runner has no branch for it.

The name is the repo's existing vocabulary, not a new coinage: `engine-loop`
is already what the terminal-bench adapter's `agent_harness` default means
("tolokaforge's own turn loop, no vendor CLI layered on"). Reusing it keeps one
word for one thing across the two registries — `agent_harness: engine-loop`
says *no CLI harness*, `orchestrator.agent_loop: engine-loop` says *which
in-process loop* — and is why this one name carries a hyphen where the rest of
the registry vocabulary is snake_case.

### One dispatch site

`TrialRunner.run` resolves `load_agent_loop(self.agent_loop)` and calls the
resulting loop's `run`. `self.messages`, `self.tool_call_recorder`,
`self._call_ids`, `_AgentMetricsSink` and `_finalise` are untouched:
`_finalise` is already driver-neutral and serves both the loop branch and the
coding-harness branch.

### The selector is `orchestrator.agent_loop`

`OrchestratorConfig.agent_loop: str = "engine-loop"`, threaded conductor-side
into the `TrialRunner` constructor the way `interaction_mode` already is.

The name is checked twice, and neither check is inside a trial.
`Orchestrator.load_tasks` resolves it once before any trial work, beside the
run's other refusals, so a typo — or an editable install whose `.dist-info`
predates the group — is one error naming the registered loops instead of one
scored failure per trial after each container is already up. The static
`tolokaforge config validate` path checks it too, beside the equivalent
`orchestrator.runtime` check. `TrialRunner.run` resolves the factory before
the `except Exception` that classifies trial faults, so a resolution failure
can never be priced as an agent failure.

Rejected alternative: `models.agent.loop`. `ModelConfig` is the LLM-invocation
wire type — provider, name, sampling parameters — and it is shared by the
agent, user-simulator and judge roles, so a `loop` field would be meaningless
on two of three and would cross the `TrialSpec` wire to a runner that never
reads it. `OrchestratorConfig` is where run-level orchestration choices live,
including the one existing plugin-registry-name selector in run config
(`orchestrator.runtime` → `tolokaforge.runtime_backends`), and the conductor
already reads `max_turns`, `timeouts`, `stuck_heuristics` and
`rate_limit_probe` from that block on the path that builds the `TrialRunner`.

### Placement relative to the runner subset

`tolokaforge/core/loop.py` is inside the runner-subset partition (the rubric
judge runs the loop inside the runner container), so the Protocol, the context
and the built-in factory ship with the subset at no cost. The group itself is
**not** in `RUNNER_REACHABLE_ENTRY_POINT_GROUPS`: `load_agent_loop` is called
only from `tolokaforge/core/runner.py`, which is orchestrator-only. An
orchestrator-only loop implementation stays out of the partition entirely;
`tests/canonical/test_runner_subset_partition` locks it in both directions.

## Consequences

### Positive

- A second loop shape is a `pyproject.toml` row plus one class, not a
  framework PR — the terminal-oriented loop, a deep-research loop, or an
  agent-debate loop all register the same way.
- The declared-id / recorded-id contract is written down where an implementer
  reads it, instead of being discovered as a `TimelineInconsistencyError` in a
  smoke run.
- The tool-error prefix the grading timeline parses is a constant both sides
  import, so the loop seam cannot silently change what a failed call looks
  like to a trace check.
- `orchestrator.agent_loop` resolves once at run start, so an unregistered
  name is one refusal naming the known registrations rather than one scored
  trial failure per trial after each container is already up.
- ADR-0011's first named follow-up lift is closed with the pattern ADR-0011
  itself prescribes.

### Negative / Trade-offs

- `AgentLoopContext` is wide — it is the union of what the engine's loop takes
  today. A narrower context would have to drop dependencies some future loop
  legitimately needs, and widening a frozen dataclass later is the cheaper
  direction than guessing now.
- One more indirection between `TrialRunner.run` and the turn cycle. The cost
  is one registry lookup per trial.

### Follow-ups

- **The terminal-oriented loop itself** — this ADR ships the seam and the
  built-in only.
- **User-simulator Protocol lift** — the remaining ADR-0011 follow-up, still
  unfiled.

## Links

- Realizes: [ADR-0011](0011-seam-and-declaration-conventions.md) (Pattern A; names this lift).
- Related ADRs: [0028](0028-multi-actor-turn-policy.md) (turn policy — the adjacent, orthogonal axis), [0033](0033-external-harness-registry.md) / [0034](0034-external-harness-plugin-discovery.md) (the CLI-shaped harness registry this seam is deliberately not part of), [0046](0046-judgekind-protocol-and-registry.md) (the most recent registry-backed seam, same idiom).
- Related code: `tolokaforge/core/loop.py`, `tolokaforge/core/plugin_registry.py`, `tolokaforge/core/runner.py`, `tolokaforge/core/conductor.py`, `tolokaforge/core/models/run_config.py`, `tolokaforge/core/grading/trace_timeline.py`.
- Docs: [`docs/CONFIG.md`](../CONFIG.md) § `orchestrator.agent_loop`, [`docs/RUNTIME_BACKENDS.md`](../RUNTIME_BACKENDS.md) § Plug-in extension points.
