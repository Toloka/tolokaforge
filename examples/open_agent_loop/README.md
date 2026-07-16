# Open Agent Loop — copilot attach example

Runs the bundled `tool_use` tasks under **open mode** and attaches an LLM
copilot participant in the same process. Demonstrates the full flow.

For the design behind what this exercises, see
[`docs/OPEN_AGENT_LOOP.md`](../../docs/OPEN_AGENT_LOOP.md). For the
participant contract, see
[`tools/intervener/README.md`](../../tools/intervener/README.md).

---

## What it does

1. Trials run through the normal `Orchestrator` + `InProcessConductor`
   path.
2. `open_agent_loop.enabled: true` in the run config activates the gate —
   each trial gets a live `InProcessTrialSession`.
3. A background thread runs the `LLMIntervener` copilot against each
   session — it observes turn boundaries, tool calls, and assistant
   messages as they happen, and drafts intervention suggestions.
4. Every trial ends with
   `trials/<task_id>/<idx>/open_agent_loop.yaml` next to the usual
   `trajectory.yaml` — the durable, replayable trace of every event and
   every intervention with its ack outcome.

## What it does not do

- **No cross-process attach.** Sessions live inside the `Orchestrator`
  process. Cross-process attach — a separate `tolokaforge session attach`
  CLI — needs a socket transport and lands in a later milestone. The
  Python API (`orchestrator.sessions`) is the attach surface today.
- **No `auto_inject` in this example.** The bundled `LLMIntervener` is
  constructed with `auto_inject=False`, so its suggestions land in the
  participant's session log for inspection but are **not** submitted as
  `InjectMessage` interventions into the running trial. Flip the flag on
  in `run_with_copilot.py` if you want to see the trial actually receive
  the suggested messages.

---

## Running

Requirements:

- Python 3.10+ and `uv` (see the top-level [`docs/GETTING_STARTED.md`](../../docs/GETTING_STARTED.md)).
- Docker running — every trial spawns a Runner container.
- A model API key for the agent-under-test (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, etc., matching `run_config.yaml`).
- Optional: `ANTHROPIC_API_KEY` + the `anthropic` extra for the copilot's
  LLM drafter. If either is missing, the copilot falls back to a
  deterministic heuristic drafter — the pipeline still runs, the drafter
  just produces heuristic-quality suggestions instead of LLM-quality ones.

Run:

```bash
uv sync
# The intervener is a workspace peer; --package installs it into the venv
# so the example's `from intervener.participants import LLMIntervener` resolves.
scripts/with_env.sh uv run --package intervener python examples/open_agent_loop/run_with_copilot.py
```

`scripts/with_env.sh` loads `.env` — put your API keys there, or export
them in the invoking shell.

## What you should see

Stdout, roughly:

```
Attaching copilot to 3 trial session(s): ['email_lookup:0', 'flight_search:0', 'notes_write:0']
Running orchestrator (this makes real LLM calls; costs real tokens)…
[…orchestrator progress bars, per-trial gRPC logs…]
Waiting for copilot threads to drain…

Open-agent-loop trace summaries (results in results/open_agent_loop_example):
  email_lookup:0: 14 events, 3 interventions (outcomes: {'accepted': 3})
  flight_search:0: 9 events, 1 interventions (outcomes: {'accepted': 1})
  notes_write:0: 11 events, 2 interventions (outcomes: {'rejected': 2})
```

The `outcomes` line summarises the ack outcomes for every intervention
this participant submitted. See [Section 4 of the design
doc](../../docs/OPEN_AGENT_LOOP.md#4-event-and-intervention-taxonomy) for
what each outcome means.

## What lands on disk

```
results/open_agent_loop_example/
├── run.yaml                              # copy of the effective run config
├── run_summary.yaml                      # per-task pass@k etc.
└── trials/
    └── <task_id>/<idx>/
        ├── trajectory.yaml               # canonical trajectory (unchanged)
        ├── open_agent_loop.yaml          # NEW — the OAL trace
        └── …grades / artifacts / etc.
```

The OAL trace file is documented in [Section 7 of the design
doc](../../docs/OPEN_AGENT_LOOP.md#7-trace-format--open_agent_loopyaml).
Read the events list to see every published event; read the interventions
list to see every submission and its outcome. Every intervention carries
`attach_to_seq` linking it back to the event it responded to.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `This example expects open_agent_loop.enabled: true` and exit 2. | The run config the example loaded doesn't have the OAL block, or it's `enabled: false`. | Confirm `open_agent_loop.enabled: true` in `examples/open_agent_loop/run_config.yaml`, or pass `--config <path>` pointing at a config that does. |
| `Session registry is None — open mode did not activate.` | The orchestrator built its manager but it turned out to be `None`. Almost always a config-parse issue. | Verify `RunConfig.model_validate(...)` succeeds on your `run_config.yaml`; typos on `open_agent_loop.enabled` are caught by `extra="forbid"`. |
| Copilot summary shows `0 events, 0 interventions`. | The `trial_id` the copilot pre-created did not match the one the orchestrator actually produced. `_discover_trial_ids` is a best-effort walk over `evaluation.task_packs`; a task pack whose `task_id` differs from its directory name can fool it. | Look at `results/…/trials/` for the real trial IDs and adjust the discovery function, or attach the copilot from a different driver (e.g. an `orchestrator.trial_started` hook) instead of pre-creating. |
| `RuntimeError: ANTHROPIC_API_KEY is set but anthropic package is not installed`. | Copilot LLM drafter path selected but package missing. | `uv sync --package intervener` (or `pip install anthropic`), or unset `ANTHROPIC_API_KEY` to fall back to the heuristic drafter. |
| Trials never terminate. | Copilot thread got wedged (unlikely — `iter_events` blocks cleanly on terminal), or a task's own trial got stuck (see `docs/TROUBLESHOOTING.md`). | Check the per-trial `trajectory.yaml` and the orchestrator logs. The copilot thread joins with `timeout=5.0` after orchestrator returns, so it won't block the process. |
| `docker: Cannot connect to the Docker daemon`. | Docker isn't running. | Start Docker and retry. Every tolokaforge run needs a live Docker daemon; the OAL layer has no bearing on this. |

## Anatomy

- **`run_config.yaml`** — same shape as `examples/native/tool_use/run_config.yaml` but with an `open_agent_loop:` block turning the gate on.
- **`run_with_copilot.py`** — driver that:
  1. builds the `Orchestrator`,
  2. discovers the trial IDs the run will produce and pre-creates each session via `orchestrator.sessions.get_or_create(...)`,
  3. spawns an `LLMIntervener` on a background thread per trial,
  4. runs the orchestrator on the main thread (blocks until every trial finishes),
  5. waits for the copilot threads to drain,
  6. prints a per-trial summary of events + intervention outcomes read straight from the trace files.

The task pack itself is the bundled `examples/native/tool_use/dataset` —
no new tasks are introduced. This example is purely about the gate wiring.

## Next steps

- Read [`docs/OPEN_AGENT_LOOP.md`](../../docs/OPEN_AGENT_LOOP.md) end to end for the full design.
- Write your own participant — see the [author guide in `tools/intervener/README.md`](../../tools/intervener/README.md#writing-your-own-participant).
- Flip `auto_inject=True` in `run_with_copilot.py` and watch the trial actually receive the copilot's suggestions.
- Attach a `HumanIntervener` alongside the LLM one — two participants on the same session, role-priority resolution recorded in the trace.
