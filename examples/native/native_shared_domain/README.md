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
```

`task.yaml` carries `domain: ../../_shared/domain.yaml` and only the
per-case fields (`task_id`, `name`, `description`, `initial_user_message`,
`initial_state.json_db`, `user_simulator.backstory`, `grading`). Everything
else — tools, mcp_server, system_prompt, category — is inherited.

## Run

```sh
scripts/with_env.sh uv run tolokaforge run --config examples/native_shared_domain/run_config.yaml
```

Requires `OPENROUTER_API_KEY` in `.env`. Both cases run with the docker
runtime; outputs land under `results/native_shared_domain_example/`.
