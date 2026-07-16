# Open Agent Loop — copilot attach example

Runs the bundled `tool_use` tasks under **open mode** and attaches an LLM copilot participant in the same process. Demonstrates the full flow:

1. Trials run through the normal `Orchestrator` + `InProcessConductor` path.
2. `open_agent_loop.enabled: true` in the run config activates the gate — each trial gets a live `InProcessTrialSession`.
3. A background thread runs the `LLMIntervener` copilot against each session — it observes turn boundaries, tool calls, and assistant messages as they happen, and drafts intervention suggestions.
4. Every trial ends with `trials/<task_id>/<idx>/open_agent_loop.yaml` next to the usual `trajectory.yaml` — the durable, replayable trace of every event and every intervention with its ack outcome.

## What it does not do

- **No cross-process attach.** Sessions live inside the `Orchestrator` process. Cross-process attach — a separate `tolokaforge session attach` CLI — needs a socket transport and lands in a later milestone. The Python API (`orchestrator.sessions`) is the attach surface today.
- **No mid-run intervention that reaches the agent** — beyond `InjectMessage`. `Kill` / `Pause` / `Resume` / tool-approval interventions land in the trace as `rejected` with a per-kind reason (M1 sub-5b follow-up).

## Running

Requires `ANTHROPIC_API_KEY` (or the equivalent for whichever provider you route through) in `.env` for both the agent-under-test and the copilot's drafter. Optional: install `anthropic` for the LLM drafter path; otherwise the copilot uses its deterministic heuristic drafter (still exercises the whole pipeline, just with heuristic-quality suggestions).

```bash
uv sync
# The intervener package is a workspace member; --package installs it into the venv
# so the example's `from intervener.participants import LLMIntervener` resolves.
scripts/with_env.sh uv run --package intervener python examples/open_agent_loop/run_with_copilot.py
```

Output lands under `results/open_agent_loop_example/`. Inspect `trials/<task_id>/<idx>/open_agent_loop.yaml` for the event + intervention trace.

## Anatomy

- `run_config.yaml` — same shape as `examples/native/tool_use/run_config.yaml` but with an `open_agent_loop:` block turning the gate on.
- `run_with_copilot.py` — driver that (a) builds the `Orchestrator`, (b) spawns an `LLMIntervener` on a background thread per expected trial, (c) runs the orchestrator, (d) waits for the copilots to drain, (e) prints a summary of every intervention that was proposed and its ack outcome.

The task pack itself is the bundled `examples/native/tool_use/dataset` — no new tasks introduced; this example is purely about the gate wiring.
