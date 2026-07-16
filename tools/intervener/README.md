# intervener

Reference **LLM** and **Human** participants for tolokaforge's Trial Session gate. Both consume `tolokaforge.session.TrialEvents` and produce `tolokaforge.session.TrialIntervention`s via the shared `Participant` contract.

The name refers to the interventions submitted into a running trial — an LLM intervener drafts a next-turn message from a paused trajectory, a human intervener types one in a Rich console. Both drive the same `Participant` contract and emit identical session-log shape.

## Layout

```
tools/intervener/
├── intervener/
│   ├── participants/     # base + LLM + Human reference implementations
│   ├── pipeline/         # LLM drafter (+ retrieval/urgency stubs for M3)
│   ├── schema.py         # InterventionSuggestion output
│   └── demo/attach_recorded.py   # driver — replays a trajectory into either participant
└── tests/                # end-to-end tests for both participants
```

## Quick start

From the repo root:

```bash
uv sync
uv run --package intervener intervener-demo --trajectory <path-to-trajectory.yaml> --truncate-turn 5 --as llm
uv run --package intervener intervener-demo --trajectory <path-to-trajectory.yaml> --truncate-turn 5 --as human --script <lines-file>
```

`--as llm` runs the LLM intervener (uses `ANTHROPIC_API_KEY` when both the env var and the `anthropic` extra are present; otherwise falls back to a deterministic heuristic drafter). `--as human` opens an interactive Rich REPL — or, with `--script`, a file of one intervention per line for non-TTY demos and tests.

Both participants produce the same session-log shape (proven by a shape-invariant test) — the contract is genuinely shared.

## Milestone position

Reference implementations for **M2** (`[umbrella] M2 — Reference participants` #409). Landing as a scaffold against the recorded transport (M0). When M1's live in-process transport merges, the same participants attach to a live trial without code changes — the interface they're coded against is the same.
