# Open Agent Loop

The Open Agent Loop (OAL) is a decoupled participant gate that lets external
observers — LLM copilots, humans at a terminal, safety monitors, cross-trial
orchestrators — attach to a running trial, receive typed events, and submit
typed interventions. It is off by default; sealed batch behaviour is unchanged.

For the architectural rationale, see
[ADR-0019 — Open Agent Loop: TrialSession Protocols and gate](architecture/adr/0019-open-agent-loop-sessions.md).
For a runnable example, see [`examples/open_agent_loop/`](../examples/open_agent_loop/).

---

## 1. Two modes: sealed and open

Every run is either **sealed** or **open**. The choice is a single boolean in
the run config.

| Mode | Config | Guarantee | Use case |
| --- | --- | --- | --- |
| Sealed *(default)* | `open_agent_loop` absent, or `enabled: false` | Byte-identical to pre-OAL behaviour. No session objects allocated, no events published, no observer / handler wired into the loop. | Benchmarks, reproducibility, batch scoring. |
| Open | `open_agent_loop.enabled: true` | Every trial gets a live in-process session bus. Events publish for the whole trial lifetime; interventions are accepted, queued, superseded, or rejected per role priority. Every attempt is recorded in `open_agent_loop.yaml` next to `trajectory.yaml`. | Interactive debugging, human-in-the-loop, LLM copilots, live safety monitors. |

The sealed guarantee is deliberate: adding the OAL module to the codebase must
not change what a benchmark measures. If a run does not set the flag, the
observer + intervention seams on `ToolCallingLoop` resolve to null implementations
and the conductor never asks the manager for a provider.

Turning open mode on in `run.yaml`:

```yaml
open_agent_loop:
  enabled: true
```

---

## 2. Quick start

Prerequisites: the same as any other tolokaforge run — Python 3.10+, Docker
running, model API key(s) set. The bundled example uses the `native/tool_use`
task pack and an LLM copilot participant.

```bash
uv sync
scripts/with_env.sh uv run --package intervener python examples/open_agent_loop/run_with_copilot.py
```

What you'll see:

1. `Orchestrator` launches trials through the standard `InProcessConductor`.
2. A background thread per trial runs an `LLMIntervener` — it iterates
   `TurnStarted` / `ToolCallEmitted` / `AssistantMessage` events as they happen
   and drafts intervention suggestions.
3. Each trial ends with a companion file
   `results/open_agent_loop_example/trials/<task_id>/<trial_idx>/open_agent_loop.yaml`
   next to the usual `trajectory.yaml`. It contains every event and every
   intervention with its ack outcome.
4. The driver prints a per-intervention summary to stdout.

The `run_config.yaml` in that example is the minimal open-mode diff over the
plain `native/tool_use` config: one `open_agent_loop:` block.

---

## 3. Architecture at a glance

```
                        ┌──────────────────────────────┐
                        │        Orchestrator          │
                        │   (scheduler + trial fan-out)│
                        │                              │
                        │   ┌──── OrchestratorDeps ────┤
                        │   │                          │
                        │   │  oal_manager: Optional   │
                        │   │  ─ SessionRegistry       │
                        │   │  ─ observer_provider()   │
                        │   │  ─ intervention_h…()     │
                        │   │  ─ write_trace()         │
                        │   └──────────────────────────┤
                        └───────────┬──────────────────┘
                                    │ ConductorContext
                                    │  (observer_provider,
                                    │   intervention_handler_provider)
                                    ▼
                        ┌──────────────────────────────┐
                        │      InProcessConductor      │
                        │   (per-trial executor)       │
                        │                              │
                        │   ┌── ToolCallingLoop ───────┤
                        │   │                          │
                        │   │  LoopObserver seam       │
                        │   │  InterventionHandler seam│
                        │   └──────────────────────────┤
                        └───────────┬──────────────────┘
                                    │ SessionLoopObserver.publish()
                                    │ SessionInterventionHandler.drain_and_apply()
                                    ▼
                        ┌──────────────────────────────┐
                        │   InProcessTrialSession      │
                        │   (bus per trial_id)         │
                        │                              │
                        │   attach() / detach()        │
                        │   iter_events() / submit()   │
                        └───────────┬──────────────────┘
                                    │
                                    ▼
                        ┌──────────────────────────────┐
                        │        Participants          │
                        │   ─ LLMIntervener            │
                        │   ─ HumanIntervener          │
                        │   ─ your custom participant  │
                        └──────────────────────────────┘
```

Key points:

- **`Orchestrator` is unchanged in sealed mode.** Deps.oal_manager is `None`,
  so nothing about session lifecycle enters the trial path.
- **`InProcessConductor` is session-agnostic.** It receives generic
  `observer_provider` / `intervention_handler_provider` callables in its
  `ConductorContext`. It knows nothing about sessions, participants, or
  interventions — it just asks its context for an observer + handler per trial
  and hands them to `ToolCallingLoop`.
- **`ToolCallingLoop` has two Protocol seams**: `LoopObserver` (outbound —
  called at turn start, tool call, tool result, assistant message, budget
  update, terminal) and `InterventionHandler` (inbound — `drain_and_apply` at
  the top of every turn; `intercept_tool_call` before every tool dispatch).
  Both default to null implementations when nothing is provided.
- **`OpenAgentLoopManager` is the run-scoped coordinator.** It owns the
  `SessionRegistry`, hands out the two provider closures, and writes the trace
  companion file at trial completion. When a caller does not supply one, the
  orchestrator auto-constructs one iff `config.open_agent_loop.enabled` is
  true.

The seams are the important part. Every session-specific class (
`SessionLoopObserver`, `SessionInterventionHandler`,
`InProcessTrialSession`, `SessionRegistry`, `OpenAgentLoopManager`) is on the
session side of the seam. The loop and the conductor stay generic.

---

## 4. Event and intervention taxonomy

The wire types are Pydantic v2 models with `extra="forbid"` and discriminated
unions keyed on `kind`. Every event carries a monotonic per-trial `seq`.

### Events (`tolokaforge.session.TrialEvent`)

| `kind` | Fired when | Payload highlights |
| --- | --- | --- |
| `turn_started` | Start of every agent turn. | `turn_index` |
| `tool_call_emitted` | Agent emitted a tool call, before execution. | `call_id`, `tool_name`, `arguments_preview` |
| `tool_result_observed` | Tool executor returned. | `call_id`, `tool_name`, `duration_ms`, `truncated_preview` |
| `assistant_message` | Assistant produced a message. | `content_preview`, `has_reasoning` |
| `budget_update` | Budget accounting ticked after a turn. | `spent_usd`, `spent_ms`, `remaining_turns` |
| `pause_acknowledged` | The loop entered a paused wait after a `Pause` intervention. | `triggered_by_participant` |
| `resume_acknowledged` | The loop left a paused wait after a `Resume` intervention. | `triggered_by_participant` |
| `terminal_reached` | Trial finished. | `status`, `termination_reason`, `final_grade_summary` |

Truncation of previews is deliberate — the trace is intended for human
inspection and diff-friendly YAML, not full replay of arbitrarily large
payloads. If you need the exact transcript, `trajectory.yaml` still has it.

### Interventions (`tolokaforge.session.TrialIntervention`)

| `kind` | Effect on the running trial | Applied at | Status |
| --- | --- | --- | --- |
| `inject_message` | Prepends a user-role message to the next agent turn. | Turn boundary. | Working end-to-end. |
| `approve_tool` | Confirms a pending tool call may execute. | Before `tool_executor.execute()`. | Working end-to-end. |
| `reject_tool` | Blocks a pending tool call. Synthesises a tool-error message for the agent. | Before `tool_executor.execute()`. | Working end-to-end. |
| `pause` | The loop enters a poll loop and stops advancing turns. Publishes `pause_acknowledged`. | Turn boundary. | Working end-to-end. |
| `resume` | Wakes a paused loop. Publishes `resume_acknowledged`. | Poll loop. | Working end-to-end. |
| `kill` | Terminates the trial with `TerminationReason.USER_STOP` and the participant's reason string. | Turn boundary. | Working end-to-end. |
| `edit_state` | Would mutate sandbox state (a runner-side key). | — | **Deferred.** Requires runner-side mediation surface that does not exist yet. Recorded in the trace with a `rejected` outcome and a clear reason. See [Section 9 — Deferred work](#9-deferred-work). |

Every intervention returns an `InterventionAck` with an `outcome`:

| `outcome` | Meaning |
| --- | --- |
| `accepted` | Applied at the next appropriate boundary. |
| `queued` | Held until a later boundary — for example, a `resume` submitted before a `pause`. |
| `superseded` | A later intervention from a higher-priority participant took its place. |
| `rejected` | Not applied. `reason` explains why (role priority, unsupported kind, malformed payload). |

Under open mode **every attempt is recorded in `open_agent_loop.yaml`**,
including the ones that were superseded or rejected. That is the audit trail —
participants know what actually happened, not just what they asked for.

---

## 5. Multi-participant model

A session is a **broadcast bus for events** and a **serialised queue for
interventions**. N participants may attach to the same trial concurrently.
Each attached participant sees every event; interventions from different
participants are resolved by role priority.

Roles (`tolokaforge.session.ParticipantRole`):

| Role | Priority | Typical caller |
| --- | --- | --- |
| `admin` | Highest — supersedes lower tiers. | Human operator, safety monitor with kill authority. |
| `participant` | Middle — can inject messages and approve/reject tools. | LLM copilot proposing next moves. |
| `observer` | Lowest — cannot supersede, but every submission is still recorded. | Passive monitoring, metrics collection. |

Within a tier, **later wins**: if two `participant`s submit an
`inject_message` at the same turn boundary, the later one is applied and the
earlier one is recorded as `superseded`.

Attach flow:

```python
handle = session.attach(participant_id="llm-copilot-1", role=ParticipantRole.PARTICIPANT)
for event in session.events().iter_events(handle):
    if event.kind == "assistant_message":
        session.interventions().submit(
            handle,
            InjectMessage(
                trial_id=session.trial_id,
                attach_to_seq=event.seq,
                participant_id=handle.participant_id,
                timestamp=datetime.now(timezone.utc),
                content="Try the /v2 endpoint — the current one 401s for org tokens.",
            ),
        )
session.detach(handle)
```

`iter_events` blocks until the next event arrives or the trial reaches its
terminal state, then returns cleanly.

---

## 6. Configuration

The full open-mode block today:

```yaml
open_agent_loop:
  enabled: true
```

That is the entire schema. It lives on `RunConfig` as
`open_agent_loop: OpenAgentLoopConfig | None = None`. `None` (default) keeps
every trial sealed. The `OpenAgentLoopConfig` model uses `extra="forbid"`; any
field not listed above is a validation error, and future fields will land
here additively (transport selection is planned for M4).

There are **no environment variables** specific to the OAL gate. Model API
keys still come from your usual environment (`ANTHROPIC_API_KEY`, etc.) — the
gate itself reads no secrets; only participants that call LLMs do.

The bundled `LLMIntervener` in `tools/intervener/` prefers
`ANTHROPIC_API_KEY` when both the env var and the `anthropic` extra are
present; otherwise it falls back to a deterministic heuristic drafter, so the
example runs even without a key set.

---

## 7. Trace format — `open_agent_loop.yaml`

Every open-mode trial writes a companion file next to its trajectory:

```
results/<run>/trials/<task_id>/<trial_idx>/
├── trajectory.yaml       # canonical trajectory — unchanged
└── open_agent_loop.yaml  # OAL trace (open mode only)
```

The OAL trace is a snapshot of the session at trial completion:

```yaml
trial_id: "email_lookup:0"
participants:
  - participant_id: "llm-copilot-1"
    role: "participant"
    attached_at: "2026-07-15T18:22:01.334Z"
    detached_at: "2026-07-15T18:22:44.017Z"
events:
  - kind: "turn_started"
    seq: 0
    timestamp: "2026-07-15T18:22:01.402Z"
    turn_index: 0
  - kind: "tool_call_emitted"
    seq: 1
    timestamp: "2026-07-15T18:22:02.108Z"
    call_id: "call_abc123"
    tool_name: "database_query"
    arguments_preview: '{"table":"users","filter":{"email":"a@b.c"}}'
  # …
interventions:
  - kind: "inject_message"
    trial_id: "email_lookup:0"
    attach_to_seq: 3
    participant_id: "llm-copilot-1"
    timestamp: "2026-07-15T18:22:04.812Z"
    content: "Try the /v2 endpoint — the current one 401s for org tokens."
    ack_outcome: "accepted"
    ack_reason: null
  # …
```

The shape is deliberately a flat list of `events` and a flat list of
`interventions` rather than a merged log — participants that only care about
one side (a metrics collector reading events, an audit tool reading
interventions) can slice cheaply. The `attach_to_seq` field on each
intervention links back to the event it responds to.

The companion-file layout (vs. extending the `Trajectory` model) is
intentional: canonical trajectory snapshot tests stay undisturbed and
existing analytics tooling that reads `trajectory.yaml` is unaffected.

---

## 8. Writing your own participant

The complete participant contract, plus reference LLM and Human
implementations, lives in [`tools/intervener/`](../tools/intervener/). See
its [README](../tools/intervener/README.md) for the author's guide. Minimum
outline:

```python
from tolokaforge.session import (
    ParticipantRole,
    TrialSession,
    InjectMessage,
)


class MyMonitorParticipant:
    def run(self, session: TrialSession) -> None:
        handle = session.attach("my-monitor", role=ParticipantRole.OBSERVER)
        try:
            for event in session.events().iter_events(handle):
                self._on_event(event, session, handle)
        finally:
            session.detach(handle)
```

Participants are ordinary Python objects — nothing here mandates threading,
async, or a specific execution model. The bundled `LLMIntervener` runs on a
background thread; the `HumanIntervener` runs in the foreground with a Rich
REPL. Any object implementing the loop above is a valid participant.

To attach from your own driver (as opposed to running through the bundled
example), pre-create the session before the orchestrator asks for it, then
give the session to your participant:

```python
orchestrator = Orchestrator(config=config)  # config has open_agent_loop.enabled: true
session = orchestrator.sessions.get_or_create(f"{task_id}:{trial_idx}")
threading.Thread(target=my_participant.run, args=(session,), daemon=True).start()
orchestrator.run()  # blocks; loop observer + intervention handler wire in automatically
```

The `get_or_create` call is idempotent and threadsafe. Pre-creating is
optional — if you attach later, you just miss the events that fired before
your attach.

---

## 9. Deferred work

- **`EditState`.** Requires runner-side mediation (a `WriteState` RPC, a
  per-task policy for writable state keys, and a role model for who can
  edit). Rejected in the trace with a clear reason. Landing it means
  finishing the runner-side surface first; see the follow-up issue.
- **Cross-process attach.** Sessions today live inside the orchestrator's
  process. `orchestrator.sessions.get_or_create` is the attach surface. A
  future `tolokaforge session attach <run> <trial>` CLI needs a Unix-socket
  or WebSocket transport — the M4 milestone.
- **Additional transports.** The Protocols in `tolokaforge.session` are
  transport-orthogonal by design; `InProcessTrialSession` and
  `RecordedTrialSession` are the two shipped today. Unix-socket JSON-lines
  and WebSocket transports satisfy the same Protocols and land later.

Every deferred item is a **schema-compatible** future addition. Nothing in
today's on-disk trace format, wire types, or public API needs to change to
accommodate them.

---

## 10. Non-goals

- **Not a general-purpose messaging framework.** The event set is trial-shaped
  (turns, tool calls, budget). Adding an event kind is a Pydantic schema
  addition and a publish call on the loop observer — but the intent stays
  narrow.
- **Not a training data pipeline.** Interventions are not automatically fed
  into any training loop. If you want that, read `open_agent_loop.yaml` and do
  it yourself.
- **Not a replacement for `Actor` or `Participant` semantics inside the
  agent loop.** The gate is orthogonal. The agent's `user`-role turns come
  from wherever they already come from (task config, live user, another
  LLM). OAL sits alongside — an OAL `InjectMessage` becomes an *additional*
  user-role message; it does not replace the actor's role.

---

## See also

- [ADR-0019 — Open Agent Loop: TrialSession Protocols and gate](architecture/adr/0019-open-agent-loop-sessions.md) — the architectural record.
- [`examples/open_agent_loop/`](../examples/open_agent_loop/) — the runnable example.
- [`tools/intervener/README.md`](../tools/intervener/README.md) — reference participants and the author's guide.
- [`tolokaforge/session/`](../tolokaforge/session/) — the module itself. Every public class is `__all__`-exported and docstringed.
- [`docs/CONFIG.md`](CONFIG.md#open-agent-loop) — where the config block lives in the wider run-config schema.
- [`docs/architecture/README.md`](architecture/README.md) — the C4 view; the session block sits alongside the conductor.
