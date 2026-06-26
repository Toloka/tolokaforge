# Native shared-domain example

Two-task benchmark that demonstrates the **shared-domain layout** for native
TolokaForge tasks. Both testcases reuse one set of tools, models, MCP server,
and system prompt via `_shared/domain.yaml`; only the per-case initial state
and grading rules differ.

## Layout

```
examples/native_shared_domain/
  run_config.yaml                 # entry point (model + orchestrator settings)
  run_config_gate_demo.yaml       # cheap-agent config for the gate-fire testcase
  dataset/
    notes/
      _shared/
        domain.yaml               # category, tools list, system_prompt, mcp_server
        mcp_server.py             # FastMCP stdio server (used by all cases)
        models.py                 # Note Pydantic model
        system_prompt.md          # agent instructions (shared across cases)
        tools/
          __init__.py
          notes.py                # add_note, list_notes
      testcases/
        add_first_note/
          task.yaml               # domain: ../../_shared/domain.yaml
          grading.yaml
          initial_state.json
        recall_existing_note/
          task.yaml
          grading.yaml
          initial_state.json
        summarize_notes_rubric/   # rubric / llm_judge showcase (happy path)
          task.yaml
          grading.yaml
          initial_state.json
        add_note_duplicate_check_gated/  # rubric REQUIRED-gate-fire showcase
          task.yaml
          grading.yaml
          initial_state.json
```

`task.yaml` carries `domain: ../../_shared/domain.yaml` and only the
per-case fields (`task_id`, `name`, `description`, `initial_user_message`,
`initial_state.json_db`, `user_simulator.backstory`, `grading`). Everything
else — tools, mcp_server, system_prompt, category — is inherited.

## Rubric-graded testcase (`summarize_notes_rubric`)

`summarize_notes_rubric` is the showcase for **rubric / `llm_judge` grading**.
The user asks for a rundown of every saved note; the quality and completeness of
the agent's *written* summary is what is graded — something deterministic checks
can't fully capture.

`grading.yaml` combines a transcript rule (gate: `list_notes` was called, weight
0.3) with a structured `llm_judge` rubric (weight 0.7):

| Criterion | kind | required | role |
|---|---|---|---|
| `covers_all_notes` | binary | **yes** | hard gate — summary must cover all four notes |
| `facts_accurate` | graded | no | facts match the notes (uses per-criterion `expected`) |
| `clarity` | graded | no | summary is well-organized / readable |

The rubric carries an author-written `reference` (the four notes' ground-truth
content) shown to the judge — this is the *grading* reference channel, not
`golden_actions`. The judge model is configured at the run level under
`models.judge` (here `openrouter/openai/gpt-4.1-mini` — cheap but capable;
`gpt-4o-mini` loops and never submits), separate from the agent under test.

The judge grades from the **transcript + reference**: the criteria score the
agent's written reply, which only the transcript carries. (The judge's read-only
`get_db_state` / `query_db` tools *can* additionally read the initial notes,
because the per-case `initial_state.json` is loaded into the DB service — but the
rubric does not depend on that.)

The run produces, per trial:

- `grade.yaml` — `criterion_results` (per-criterion `met` / `score` /
  `justification`), `judge_status: completed`, and `judge_usage` (judge cost).
- `judge_trajectory.yaml` — the judge's own message transcript (the audit channel
  for *why* each criterion was scored as it was).

## Required-gate-fire testcase (`add_note_duplicate_check_gated`)

`summarize_notes_rubric` is the happy path (a good agent passes, score 1.0). This
testcase is its counterpart: it demonstrates the **rubric REQUIRED-criterion gate
firing** — a run where a `required` criterion is NOT met, so `gate_failed` forces
`binary_pass: false` **even though the graded criteria still score well**.

The user asks to save a note. The rubric encodes a *check-for-duplicates-first*
policy (the assistant should `list_notes` and warn about a near-duplicate **before**
`add_note`) as a **`required`** criterion. The shared system prompt tells the agent
to call `add_note` directly and the user never asks for the check, so agents
reliably skip `list_notes` and save the note straight away. The initial state
already contains a near-duplicate (`Team stand-up`), so the duplicate genuinely
exists.

| Criterion | kind | required | role |
|---|---|---|---|
| `checked_duplicates_first` | binary | **yes** | gate — `list_notes` + warn must precede `add_note` |
| `note_saved` | graded | no | the note was saved faithfully (uses per-criterion `expected`) |
| `clarity` | graded | no | the reply is clear / professional |

Because the agent skips the check, the judge marks `checked_duplicates_first`
`met: false` → `gate_failed` → `binary_pass: false`. The graded `note_saved` and
`clarity` criteria still score (typically 1.0 each in `criterion_results`).
**`gate_failed` overrides the weighted score**: a failed required criterion fails
the trial outright, independent of `pass_threshold`, and the runner zeroes the
`llm_judge` component (`components.llm_judge: 0.0`) — see `docs/GRADING.md`
§ Required-gate semantics. The per-criterion `note_saved: score 1.0` in
`grade.yaml` is the proof that the gate, not a low weighted average, is what
failed the trial.

The agent is a deliberately cheap model (`openai/gpt-4o-mini`) via
`run_config_gate_demo.yaml`; because the policy is not in the agent's prompt, the
gate fires regardless of agent strength.

## Run

All cases (the happy-path rubric included):

```sh
scripts/with_env.sh uv run tolokaforge run --config examples/native/native_shared_domain/run_config.yaml
```

Just the gate-fire testcase (cheap agent, `repeats: 1`):

```sh
scripts/with_env.sh uv run tolokaforge run --config examples/native/native_shared_domain/run_config_gate_demo.yaml
```

Requires `OPENROUTER_API_KEY` in `.env`. All cases run with the docker
runtime. The default `run_config.yaml` runs every testcase
(`tasks_glob: **/testcases/**/task.yaml`) and writes to
`results/native_shared_domain_example/`; `run_config_gate_demo.yaml` narrows the
glob to `**/testcases/add_note_duplicate_check_gated/task.yaml` and writes to
`results/native_shared_domain_gate_demo/`. To run only the happy-path rubric
testcase, narrow the glob to `**/testcases/summarize_notes_rubric/task.yaml`.

For a **deterministic** (no-agent-luck) version of the gate firing, see the
calibration fixture `tools/rubric-calibrator/fixtures/note_duplicate_gate.yaml`.
