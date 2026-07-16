# intervener

Reference sinks, controllers, and participants for tolokaforge's Open Agent
Loop gate. Two composition shapes are supported side by side:

- **Event-reactive `Participant`** — subclass and implement one
  `handle_event(event) → EventReaction` method. Best fit for LLM drafters
  and rule-based agents where every action is triggered by an event.
- **`ComposedParticipant` + sinks + controllers** — wire N reusable
  `EventSink`s (display / persist events) and M reusable `InputController`s
  (submit interventions) around one `SessionBinding`. Best fit for
  independent-thread inputs like a keyboard REPL, an HTTP webhook, or a
  chaos timer — anything where submissions aren't naturally per-event.

Both shapes consume `tolokaforge.session.TrialEvent`s and produce
`tolokaforge.session.TrialIntervention`s through the same session bus. The
reference `HumanIntervener` and `LLMIntervener` use the event-reactive
shape; the live-attach demo at
`/private/tmp/.../scratchpad/run_with_human.py` uses the composed shape.

For the design + wire types, see
[`docs/OPEN_AGENT_LOOP.md`](../../docs/OPEN_AGENT_LOOP.md) and
[ADR-0019](../../docs/architecture/adr/0019-open-agent-loop-sessions.md).

---

## Layout

```
tools/intervener/
├── intervener/
│   ├── binding.py        # SessionBinding — thin per-participant session façade
│   ├── protocols.py      # EventSink + InputController Protocols
│   ├── sinks/
│   │   ├── rich_console.py  # RichConsoleSink — panels + rules
│   │   ├── plain.py         # PlainLineSink — colored one-liners
│   │   ├── jsonl.py         # JsonlSink — durable machine-readable log
│   │   ├── silent.py        # SilentSink — /dev/null (metrics-only setups)
│   │   └── compound.py      # CompoundSink — fan out to N sinks
│   ├── controllers/
│   │   ├── keyboard.py      # KeyboardController — raw `i` key + step REPL
│   │   ├── scripted.py      # ScriptedController — timed script OR line-triggered
│   │   ├── event_reactive.py # EventReactiveController — callback per event
│   │   └── timer.py         # TimerController — fire every N seconds
│   ├── participants/
│   │   ├── base.py       # Participant ABC + ComposedParticipant + log types
│   │   ├── llm.py        # LLMIntervener — event-reactive drafter
│   │   └── human.py      # HumanIntervener — event-reactive Rich REPL
│   ├── pipeline/         # LLM drafter (+ retrieval/urgency stubs for M3)
│   ├── schema.py         # InterventionSuggestion output model
│   └── demo/attach_recorded.py   # driver — replays a trajectory into either participant
├── tests/                # end-to-end tests for both participants + contract invariants
└── pyproject.toml        # workspace package; installed with `uv sync`
```

---

## Quick start — demo against a recorded trajectory

From the repo root:

```bash
uv sync

# LLM participant against a recorded trajectory. If ANTHROPIC_API_KEY is set
# AND the `anthropic` extra is installed, uses Claude; otherwise falls back
# to a deterministic heuristic drafter (still exercises the full pipeline).
uv run --package intervener intervener-demo \
    --trajectory results/some_run/trials/task_id/0/trajectory.yaml \
    --truncate-turn 5 \
    --as llm

# Human participant. Interactive Rich REPL by default; --script feeds
# one intervention per line for non-TTY demos and tests.
uv run --package intervener intervener-demo \
    --trajectory <path> \
    --truncate-turn 5 \
    --as human \
    --script scripted-lines.txt
```

The demo uses a `RecordedTrialSession` — the trajectory is loaded, events
replay in `seq` order, and the participant reacts as they arrive. This is
how M0 shipped, and how you can develop and test a participant without
running a full live trial.

## Quick start — attaching to a live trial

For a live in-process trial, see the runnable example at
[`examples/open_agent_loop/`](../../examples/open_agent_loop/) — it starts an
`Orchestrator` with `open_agent_loop.enabled: true`, pre-creates sessions
via `orchestrator.sessions.get_or_create`, and spawns `LLMIntervener`s on
background threads.

---

## Building a composed participant (new pattern)

For inputs that aren't purely event-reactive (keyboard, HTTP, timer),
compose an `EventSink` list and an `InputController` list around a
`ComposedParticipant`:

```python
from intervener import (
    ComposedParticipant,
    CompoundSink, RichConsoleSink, JsonlSink,
    KeyboardController,
)
from tolokaforge.session import ParticipantRole

participant = ComposedParticipant(
    participant_id="operator-1",
    role=ParticipantRole.ADMIN,
    sinks=[
        RichConsoleSink(),                     # pretty terminal render
        JsonlSink("/tmp/events.jsonl"),         # durable machine log
    ],
    controllers=[
        KeyboardController(trigger_key="i"),    # press 'i' → pause + REPL
    ],
)
log = participant.run(session)  # blocks until trial terminates
```

`ComposedParticipant.run` handles: attach, spawn each controller (some
spawn threads, some run event-reactively via `on_event`), drain events,
forward to every sink, tear down on `TerminalReached`, detach.

Reference sinks and controllers, and when to pick each:

| `EventSink` | Use case |
| --- | --- |
| `RichConsoleSink` | Interactive terminal — panels + rules |
| `PlainLineSink` | Scripts, CI logs, tail -f monitoring |
| `JsonlSink(path_or_stream)` | Durable machine-readable log |
| `SilentSink` | Metrics-only participants (no display) |
| `CompoundSink([...])` | Fan out to multiple sinks; child failures are isolated |

| `InputController` | Use case |
| --- | --- |
| `KeyboardController(trigger_key="i")` | Human at TTY — raw key trigger + step REPL |
| `ScriptedController(lines=…, line_parser=…, seam_predicate=…)` | Line-triggered scripted replay (used by `HumanIntervener`'s `non_interactive_script` back-compat path) |
| `ScriptedController(timed_script=[(delay_s, intervention), …])` | Time-scheduled replay for deterministic demos |
| `EventReactiveController(callback=(event, binding) → intervention?)` | Wrap any event → intervention function; the shape LLM drafters take |
| `TimerController(interval_seconds, callback)` | Fire an intervention every N seconds — chaos testing |

Roles work the same as the event-reactive path (`admin > participant > observer`).
See ADR-0019 §5 for the multi-participant conflict rule.

---

## Writing your own event-reactive participant (old pattern)

Subclass `Participant` and implement `handle_event`. The base handles:

- attaching + detaching with your `participant_id` and role,
- iterating events in `seq` order until the session terminates,
- submitting the intervention you return (if any),
- appending a structured `SessionLogEntry` per event to `self.log`.

Minimum implementation:

```python
from datetime import UTC, datetime
from intervener.participants.base import EventReaction, Participant
from tolokaforge.session import (
    InjectMessage,
    ParticipantHandle,
    ParticipantRole,
    TrialEvent,
    TrialSession,
)


class LoopWarner(Participant):
    """Injects a warning when the agent emits the same tool call twice in a row."""

    def __init__(self) -> None:
        super().__init__(participant_id="loop-warner", role=ParticipantRole.PARTICIPANT)
        self._last_call: str | None = None

    def handle_event(
        self,
        event: TrialEvent,
        handle: ParticipantHandle,
        session: TrialSession,
    ) -> EventReaction:
        if event.kind != "tool_call_emitted":
            return EventReaction()
        signature = f"{event.tool_name}:{event.arguments_preview}"
        if signature == self._last_call:
            self._last_call = signature
            return EventReaction(
                intervention=InjectMessage(
                    trial_id=handle.trial_id,
                    attach_to_seq=event.seq,
                    participant_id=handle.participant_id,
                    timestamp=datetime.now(UTC),
                    content="You just called the same tool with the same args. Try a different approach.",
                ),
                note="loop-detected",
            )
        self._last_call = signature
        return EventReaction()
```

Drive it:

```python
log = LoopWarner().run(session)  # blocks until the trial terminates
```

### The full `EventReaction` shape

```python
@dataclass(frozen=True)
class EventReaction:
    intervention: TrialIntervention | None = None  # what to submit (if anything)
    note: str | None = None                        # optional log annotation
    payload: dict[str, Any] | None = None          # structured log payload
```

Return `EventReaction()` (all `None`) to observe without acting. The base
records one `SessionLogEntry` per event regardless, so an observer-only
participant still produces a complete audit trail.

### Session log

`Participant.log` accumulates `SessionLogEntry`s — same shape across every
participant subclass:

```python
SessionLogEntry(
    trial_id=...,
    participant_id=...,
    event_seq=...,        # the event this entry reacts to
    event_kind=...,       # e.g. "tool_call_emitted"
    ack_outcome=...,      # None if no intervention was submitted, else accepted / queued / superseded / rejected
    ack_reason=...,
    intervention_kind=..., # e.g. "inject_message"
    note=...,             # free-form string from EventReaction
    payload=...,          # structured dict from EventReaction
    at=...,               # timestamp
)
```

Call `log.to_yaml_dict()` for a JSON-safe list you can dump alongside the
tolokaforge trace.

### Role choice

Pick a role that reflects the participant's authority:

| Role | Choose when |
| --- | --- |
| `OBSERVER` | Passive — metrics collection, live dashboards, replay. Submissions are recorded but always rejected against a higher tier. |
| `PARTICIPANT` | Default for copilots — proposes messages, approves / rejects tools. |
| `ADMIN` | Human operator with kill authority, safety monitor. Supersedes lower tiers. |

Later-wins within a tier. Every attempt lands in the trace — role
resolution never hides an attempted submission.

### Threading model

Participants are ordinary Python objects. `Participant.run` blocks the
calling thread until the trial terminates (via `iter_events` on the events
Protocol). To attach to a live trial without blocking the orchestrator,
run each participant on its own background thread:

```python
thread = threading.Thread(target=my_participant.run, args=(session,), daemon=True)
thread.start()
orchestrator.run()
thread.join(timeout=5.0)
```

There is no async story yet — every transport shipped today
(`InProcessTrialSession`, `RecordedTrialSession`) uses blocking threadsafe
queues. Future socket / WebSocket transports will satisfy the same
Protocols; if you need async, wrap `iter_events` in `asyncio.to_thread`.

### Submitting interventions directly (bypassing the base)

The base takes care of the common path — you rarely need to talk to the
session Protocol yourself. If you do:

```python
handle = session.attach(participant_id, role)
ack = session.interventions().submit(
    handle,
    InjectMessage(
        trial_id=session.trial_id,
        attach_to_seq=event.seq,
        participant_id=handle.participant_id,
        timestamp=datetime.now(UTC),
        content="…",
    ),
)
# ack.outcome is one of "accepted", "queued", "superseded", "rejected"
# ack.reason gives context on non-accepted outcomes
session.detach(handle)
```

Every submission returns an `InterventionAck` — always check `outcome`.

---

## Reference participants

### `LLMIntervener`

Located at `intervener/participants/llm.py`. Reacts at defined intervention
seams (turn boundaries, tool calls, assistant messages) and runs the
drafter pipeline. The drafter:

- Uses `ANTHROPIC_API_KEY` when both the env var and the `anthropic` extra
  are present; otherwise falls back to a deterministic heuristic drafter
  so the pipeline is exercisable with no external dependencies.
- Produces an `InterventionSuggestion` (situation label, urgency,
  suggested message, alternatives) and, when urgency clears the threshold,
  emits an `InjectMessage`.

### `HumanIntervener`

Located at `intervener/participants/human.py`. Rich console UI. At each
intervention seam, prompts for a line of input; empty input observes, a
non-empty line becomes an `InjectMessage`. Accepts `--script <file>` for
non-TTY demos and tests.

Both share the same session-log shape — a test in `tests/` asserts the
invariant by running each against the same recorded trajectory and diffing
the shapes.

---

## Milestone position

Reference implementations for M2. Landed against the recorded transport
(M0). When M1's live in-process transport shipped, the same participants
attached to a live trial without a single-line code change — the interface
they're coded against is exactly the interface `InProcessTrialSession`
satisfies. This is the proof the Protocol split works.

See also:

- [`docs/OPEN_AGENT_LOOP.md`](../../docs/OPEN_AGENT_LOOP.md) — the gate
- [`docs/architecture/adr/0019-open-agent-loop-sessions.md`](../../docs/architecture/adr/0019-open-agent-loop-sessions.md) — the architectural record
- [`examples/open_agent_loop/`](../../examples/open_agent_loop/) — end-to-end live-trial example
