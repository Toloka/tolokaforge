# Native shared-domain example

Two-task benchmark that demonstrates the **shared-domain layout** for native
TolokaForge tasks. Both testcases reuse one set of tools, models, MCP server,
and system prompt via `_shared/domain.yaml`; only the per-case initial state
and grading rules differ.

## Layout

```
examples/native_shared_domain/
  run_config.yaml                 # entry point (model + orchestrator settings)
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
        summarize_notes_rubric/   # rubric / llm_judge showcase
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
`golden_actions`. The judge model is `openrouter/openai/gpt-4.1-mini` (cheap but
capable; `gpt-4o-mini` loops and never submits).

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

## Run

```sh
scripts/with_env.sh uv run tolokaforge run --config examples/native/native_shared_domain/run_config.yaml
```

Requires `OPENROUTER_API_KEY` in `.env`. All cases run with the docker
runtime; outputs land under `results/native_shared_domain_example/`. The default
`run_config.yaml` runs every testcase (`tasks_glob: **/testcases/**/task.yaml`).
To run only the rubric testcase, narrow the glob to
`**/testcases/summarize_notes_rubric/task.yaml`.
