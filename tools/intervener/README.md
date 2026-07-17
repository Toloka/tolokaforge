# intervener

Peer package that consumes tolokaforge's Open Agent Loop gate. Ships
reference LLM and Human participants, a compositional sink + controller
layer, and a plug-in surface for interactive tools.

**Full design guide (start here for architecture / how to build things):**
[`docs/INTERVENER.md`](../../docs/INTERVENER.md).

**The gate itself (event/intervention taxonomy, modes, config, trace format):**
[`docs/OPEN_AGENT_LOOP.md`](../../docs/OPEN_AGENT_LOOP.md).

**Architectural record:**
[ADR-0019](../../docs/architecture/adr/0019-open-agent-loop-sessions.md).

---

## Layout

```
tools/intervener/
├── intervener/
│   ├── binding.py        # SessionBinding — thin per-participant session façade
│   ├── protocols.py      # EventSink + InputController Protocols
│   ├── sinks/            # RichConsoleSink, PlainLineSink, JsonlSink, SilentSink, CompoundSink, RollingEventsSink
│   ├── controllers/      # KeyboardController, ScriptedController, EventReactiveController, TimerController
│   ├── participants/
│   │   ├── base.py       # Participant ABC + ComposedParticipant + log types
│   │   ├── llm.py        # LLMIntervener — event-reactive drafter
│   │   └── human.py      # HumanIntervener — event-reactive Rich REPL
│   ├── pipeline/         # LLM drafter (situation/retrieval/urgency stubs for M3)
│   ├── tools/            # InteractiveTool + ToolRegistry + ContextTool + AnalyzeTool
│   ├── schema.py         # InterventionSuggestion output model
│   └── demo/attach_recorded.py    # driver — replays a trajectory into either reference participant
├── scripts/tools_smoke.py    # consumer-agnostic tool invocation (no keyboard, no session)
├── tests/                # per-layer unit tests
└── pyproject.toml        # registers reference tools under `[project.entry-points."intervener.tools"]`
```

---

## Install

```bash
uv sync                                           # workspace-mode; picks up this package as a workspace member
uv sync --extra anthropic                         # optional: enables the LLM drafter path in LLMIntervener
```

The tolokaforge core is a workspace peer; no extra install step needed.

---

## Quick start — replay a trajectory into a reference participant

Both `--as llm` and `--as human` share the same `Participant` contract
and produce identical session-log shape. The demo drives them against a
`RecordedTrialSession`, so no live orchestrator, no Docker.

```bash
uv run --package intervener intervener-demo \
    --trajectory results/some_run/trials/task_id/0/trajectory.yaml \
    --truncate-turn 5 \
    --as llm

uv run --package intervener intervener-demo \
    --trajectory <path> \
    --truncate-turn 5 \
    --as human \
    --script scripted-lines.txt      # optional: feeds one line per REPL prompt
```

The LLM path uses `ANTHROPIC_API_KEY` + the `anthropic` extra when both
are present; otherwise it falls back to a deterministic heuristic drafter
so the pipeline is exercisable without external dependencies.

## Quick start — attach to a live trial

The runnable example at [`examples/open_agent_loop/`](../../examples/open_agent_loop/)
starts a real `Orchestrator` with `open_agent_loop.enabled: true`,
pre-creates sessions via `orchestrator.sessions.get_or_create`, and
attaches an `LLMIntervener` on a background thread per trial.

For everything else — building composed participants, writing sinks,
writing controllers, writing tools, the decoupling contract, driver
patterns — see [`docs/INTERVENER.md`](../../docs/INTERVENER.md).

---

## Consumer-agnostic tool smoke

Every reference tool works without a keyboard or live session. `scripts/tools_smoke.py`
proves it:

```bash
uv run python tools/intervener/scripts/tools_smoke.py <trajectory.yaml>
```

Runs `ContextTool` and `AnalyzeTool` against a recorded trajectory and
prints their outputs. Useful for verifying an entry-point-registered
third-party tool is being picked up.

---

## Contract summary

- **Two participant shapes:** event-reactive (`Participant` subclass) or
  compositional (`ComposedParticipant` + sinks + controllers).
- **All wire types** live in `tolokaforge.session` — the intervener is a
  pure consumer.
- **No runner-side imports.** The package does not import from
  `tolokaforge.core.llm`, `tolokaforge.secrets`, or
  `tolokaforge.core.models`. Any LLM/credential capability comes in
  through narrow contracts (`SessionBinding`, `LLMCallable`) supplied by
  callers. See `docs/INTERVENER.md` §6.

---

## Testing

```bash
uv run pytest tools/intervener/tests/ -v
uv run ruff check tools/intervener/
```

All tests are offline (recorded transport for participants, stubbed
`LLMCallable` for agentic tools).

---

## Milestone position

Reference implementations for **M2** of the OAL rollout (see
[ADR-0019](../../docs/architecture/adr/0019-open-agent-loop-sessions.md)).
Landed against the recorded transport (M0). When M1's live in-process
transport shipped, the same participants attached to a live trial without
a single line of code change — the interface they're coded against is
exactly the interface `InProcessTrialSession` satisfies. This is the
proof the Protocol split works.
