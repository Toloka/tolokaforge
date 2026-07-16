# tolokaforge-copilot

Reference **LLM** and **Human** participants for tolokaforge's Trial Session gate. Both consume `tolokaforge.session.TrialEvents` and produce `tolokaforge.session.TrialIntervention`s via the shared `Participant` contract.

Peer to `public-tolokaforge/` — the gate lives inside tolokaforge; participants live here and depend on tolokaforge from the local worktree.

## Layout

```
copilot/
├── participants/     # base + LLM + Human reference implementations
├── pipeline/         # LLM stages: situation classifier + message drafter
├── schema.py         # CopilotSuggestion output
└── demo/attach_recorded.py   # driver — replays a trajectory into either participant
```

## Quick start

```bash
uv sync
uv run copilot-demo --archive <path-to-run-dir> --trial <task_id> --truncate-turn 3 --as copilot
uv run copilot-demo --archive <path-to-run-dir> --trial <task_id> --truncate-turn 3 --as human
```

The same demo module produces the same session-log format for both participant types — the contract is genuinely shared.
