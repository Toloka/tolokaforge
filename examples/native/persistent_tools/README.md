# Persistent shell and editor example

Self-contained dataset with one public task that enables both persistent
agent tools in their local variants:

- `bash_session` — a held bash shell whose working directory, environment,
  and functions persist across calls.
- `str_replace_editor` — a file editor with `view` / `create` /
  `str_replace` / `insert` commands.

The agent establishes a working directory in the session, patches a
changelog draft with a single `str_replace`, authors a release-notes file,
and confirms the result by relying on the session's persisted state. Both
tools operate on the runner's `/work`, so no Docker substrate is required.
See `docs/TOOLS.md` for the full tool contracts.

## Validate

```bash
uv run tolokaforge validate --tasks "examples/native/persistent_tools/dataset/**/task.yaml"
```

## Run

```bash
scripts/with_env.sh uv run tolokaforge run --config examples/native/persistent_tools/run_configs/dev.yaml
```

## Tasks included

- `dataset/tasks/persistent_tools/persistent_tools_public_example_01/`
