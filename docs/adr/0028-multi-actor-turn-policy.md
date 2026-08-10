# 0028. Multi-actor turn policy — `interaction_mode` + `Actor` Protocol + `TurnPolicy` seam

- **Status:** Accepted
- **Date:** 2026-08-04
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tolokaforge's turn loop assumes a two-party conversation: user simulator utters, agent responds, tools execute, repeat. That shape came from τ-bench-style customer-service benchmarks where the user is a genuine information source — they hold the reservation ID the agent needs, clarify what they want, end the conversation when their goal is met.

Code-migration and other agent-driven evals are the opposite shape. The task lives in the system prompt + workdir seed. The agent knows when it's done (tests pass), not the user. Upstream reference evals for these benchmarks typically run a single-actor MCP-CLI: the model drives tools directly until it declares done or hits a timeout — no user party at all.

Forcing agent-only tasks through the two-party shape produced a cascade of downstream symptoms in real-MB smoke: context bloat from replayed user replies (82K tokens per call by turn 88), doubled per-turn LLM cost, premature `###STOP###` guessed by the LLM simulator, provider timeouts at high turn counts, stuck-heuristics false-positives on legitimate iterative build/test cycles. Every symptom traces back to the same architectural mismatch — no first-class signal for "does this task have a real user party."

Beyond agent-only, the design must accommodate future actor types: adversarial agents, oracle-in-the-loop, evaluator-as-actor. The design goal is not "add an agent-only escape hatch" but "make the actor count and identity a first-class dimension of a task."

## Decision Drivers

- **Name the actual dimension.** The failing status quo hid the mismatch behind cost/timeout patches; a shape flag surfaces it in `TaskConfig` where authors decide.
- **Backward compat.** Every existing pack / canonical fixture / τ-bench-style adapter must be byte-for-byte unaffected. Default value carries today's behavior.
- **Extensibility without core edits.** Future policies (multi-actor, adversarial, oracle) register via entry-point group; the runner has no mode-specific `if` in the loop body.
- **Reuse the fail-loud registry idiom** and the ADR-0011 Pattern-A discipline (Protocol + value objects + built-in + `InMemory*` fixture + canonical contract test) — five entry-point-registry-backed seams now share one pattern.

## Considered Options

1. **`interaction_mode` flag only, no Protocol** — a single `if` in the runner gates the user-turn dispatch. Rejected: doesn't accommodate future actor types; each new mode becomes another `if` in the loop.
2. **`TurnPolicy` Protocol + entry-point registry** — mode selects a policy; policies dispatch the turn cycle. **This ADR.**
3. **Fold user role into system prompt anchor** — deliver `initial_user_message` as a persistent context anchor, no runtime simulator. Rejected: doesn't scale to real multi-turn user tasks.
4. **Agent yields via continuation token** — single agent, orchestrator re-invokes on `###CONTINUE###`. Rejected: reshapes tolokaforge's turn model too far, doesn't fit conversational adapters.

## Decision

We adopt **Option 2**: a three-layer architecture.

**Layer 1 — `TaskConfig.interaction_mode: Literal["conversational", "agent_only"]`** with default `"conversational"`. Mirrored as `TaskDefaults.interaction_mode: … | None` for project-side deep-merge. Room for future values (e.g. `"multi_actor"`) alongside dedicated policies.

**Layer 2 — `Actor` Protocol** at `tolokaforge/core/actors/actor.py`. `@runtime_checkable`, formalizing the `reply(context, *, observation) -> GenerationResult` contract `UserSimulator` implicitly satisfied since it was written. Minimal: only per-invocation evidence on the Protocol; construction-time deps (`llm_client`, budgets, `rate_limit_probe`) stay on concrete impls. Same discipline as the `Judge` Protocol (ADR-0020) and the `ServiceReadinessProbe` Protocol (ADR-0026).

**Layer 3 — `TurnPolicy` Protocol** at `tolokaforge/core/actors/turn_policy.py`, the choreographer:

```python
@runtime_checkable
class TurnPolicy(Protocol):
    def bootstrap(self, task: TaskConfig, initial_user_message: str | None) -> BootstrapDecision: ...
    def next_actor(self, state: TurnState) -> ActorTurn | TerminationDecision | None: ...
```

Two built-in implementations:

- **`ConversationalTurnPolicy`** — today's behavior. `bootstrap` mirrors the `_seed_first_user_message` short-circuit at `runner.py:508` (non-empty `initial_user_message` → skip simulator; otherwise dispatch the simulator's opening reply). `next_actor` returns `ActorTurn(actor_name="user", ...)` when the last agent turn had no tool calls; `None` otherwise. Byte-for-byte parity with the pre-refactor call graph.
- **`AgentOnlyTurnPolicy`** — `bootstrap` requires a non-empty `initial_user_message` and fails loud (`ValueError` naming the `task_id` and the mode) if absent — the agent-only route has no simulator to synthesize a bootstrap. `next_actor` returns `None` unconditionally: the agent runs to `###STOP###` (routed to `TerminationReason.AGENT_DONE` via the existing `_AGENT_DONE_MARKERS`), `max_turns`, or `episode_timeout_s`.

Discovered through a **new entry-point group `tolokaforge.turn_policies`** keyed by mode string, reusing the existing fail-loud `discover_entry_points` / `_load` machinery in `plugin_registry.py`. This is the **fifth entry-point-registry-backed seam** — the four existing groups (`runtime_backends`, `trial_graders`, `conductors`, `service_readiness_probes`) now share the pattern with a fifth, further validating that the registry idiom generalises.

The factory takes a `TurnPolicyContext(user_simulator: Actor | None)` rather than being arg-less like readiness probes: `ConversationalTurnPolicy` needs an `Actor` to hand back on user-turn dispatch. `AgentOnlyTurnPolicy` ignores it (its `next_actor` never returns an `ActorTurn`).

### Loop wiring: no mode-specific `if` in the loop body

`TrialRunner` at `runner.py:317-318` today wires `should_terminate = self._agent_termination` and `user_turn = self._agent_user_turn` as callables directly into `ToolCallingLoop`. Post-ADR-0028, the runner reads `task.interaction_mode`, resolves `policy = load_turn_policy(mode)(TurnPolicyContext(user_simulator=self.user_simulator))`, and wires policy callables into the loop. The loop body has no knowledge of interaction mode — the policy encapsulates the differences. Adding a future `multi_actor` mode is a new policy registration; the loop stays untouched.

The `Conductor` gates `UserSimulator(...)` construction at `conductor.py:646-668` on `task.interaction_mode == "conversational"`; under `agent_only`, no simulator is constructed and the runner receives `user_simulator=None`.

### Stop protocol reuses the existing marker

`_AGENT_DONE_MARKERS = ("###STOP###",)` at `runner.py:46` already routes agent-emitted `###STOP###` to `TerminationReason.AGENT_DONE`. No new marker is introduced — the same convention serves both modes. In `conversational` mode, user-emitted `###STOP###` still terminates with `TerminationReason.USER_STOP`.

### Termination signalling from the policy (refined for #876)

`next_actor` returns one of three things:

- **`ActorTurn`** — dispatch this actor's `reply`. The historical two-party path.
- **`TerminationDecision`** — end the trial with the given reason. `AgentOnlyTurnPolicy` takes this branch: the loop dispatches `next_actor` only when the just-completed agent turn produced no tool calls, and under agent-only that condition is definitional (no user party exists, no more tool actions coming). The trial is done, and the loop must terminate rather than retry the agent against a context ending in `role: assistant` — Anthropic's `opus-4-6` rejects that as an unsupported prefill, and semantically the agent falling silent IS the completion signal.
- **`None`** — no actor speaks this iteration, loop advances to the next agent turn. Reserved for future policies that may want to skip a turn without terminating; not exercised by either built-in today.

This means `agent_only` has three termination paths, all routed to `TerminationReason.AGENT_DONE`:
1. Agent emits `###STOP###` — explicit signal.
2. Agent emits text-only (no tool calls, no `###STOP###`) — implicit completion via `next_actor`'s `TerminationDecision` return.
3. Loop-level bounds: `max_turns` or `episode_timeout_s`.

Path 2 matches Claude Code / MCP-CLI shape: a text-only assistant turn is the natural last turn of an autonomous tool-driven run. The Anthropic API constraint just makes it structural.

## Consequences

### Positive

- The user-party dimension is a named, tested contract; the class of bugs where agent-only tasks silently paid two-party overhead cannot be reintroduced.
- Extension surface is registry-level — future actor types (adversary, oracle, evaluator-in-the-loop) register a `TurnPolicy` without editing the runner or loop.
- The five-seam pattern (runtime backends / trial graders / conductors / service-readiness probes / turn policies) is now a well-established idiom — the next registry-backed seam has near-zero design cost.
- Agent-driven benchmark adapters (Migration Bench, autonomous tool-use, etc.) opt into `interaction_mode: agent_only` and get MCP-CLI-shaped behavior with no simulator overhead — comparable to their upstream reference evals.

### Negative / Trade-offs

- `TurnPolicyContext` differs from the readiness-probe factory signature (which is arg-less). The divergence is honest — a conversational policy legitimately needs a user simulator to hand back — and documented here rather than papered over with an empty context.
- Golden snapshots that serialize `TaskConfig.model_dump()` gained one field (`interaction_mode: 'conversational'`). Six goldens regenerated in Stage 5; future canonical fixtures pick up the field automatically via the default.
- The runner-subset partition now includes `tolokaforge/core/actors` (Stage 5); the module is minimal and stable, so subset growth is bounded.

### Follow-ups

- **`MultiActorTurnPolicy`** for 3+ actor choreography (agent + adversary + oracle, or agent + user + evaluator). The registry accommodates this today; the concrete policy + config shape is a separate design pass.
- **`tolokaforge.actors` entry-point registry** parallel to `turn_policies` — lets adapters register custom `Actor` implementations by name. Not needed today (MB elides the user actor entirely); worth adding when the first non-built-in actor kind lands.
- **Reserved actor sub-keys `tools` / `service`** on `ActorSpec` — reserved for future actor types per the M9 canonicalization comment. This ADR does not flip them to strict; that's a separate migration when a concrete actor kind needs them.

## Links

- Related ADRs: [0011](0011-seam-and-declaration-conventions.md) (Pattern A), [0014](0014-trial-grader-protocol.md) (TrialGrader Protocol), [0020](0020-judge-protocol.md) (Judge Protocol), [0026](0026-service-readiness-contract.md) (Service Readiness Contract — same idiom, fourth seam).
- Related code: `tolokaforge/core/actors/actor.py`, `tolokaforge/core/actors/turn_policy.py`, `tolokaforge/core/runner.py:317-318`, `tolokaforge/core/plugin_registry.py`, `tolokaforge/core/models/task_config.py`.
- External references: #868 (this ticket), Toloka/tolokaforge#872 (initial implementation), #876 (termination-signalling refinement for the agent-only text-only case).
