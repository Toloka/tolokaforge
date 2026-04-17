# Task Packs Guide

Task packs let you keep benchmark content outside the harness repository.

## Run Config

```yaml
evaluation:
  task_packs:
    - "/abs/path/private-pack-core"
    - "/abs/path/private-pack-mobile"
  tasks_glob: "**/task.yaml"
  output_dir: "results/task_pack_run"
```

Resolution behavior:
- If `task_packs` is provided, relative `tasks_glob` patterns are evaluated under each pack root.
- If `task_packs` is provided, `tasks_glob` must be relative (absolute paths are rejected).
- If `task_packs` is omitted, `tasks_glob` is resolved relative to the current working directory.

## Pack Structure

Expected task-pack layout:

```
<pack-root>/
└── tasks/
    ├── browser/
    ├── mobile/
    ├── tool_use/
    └── ...
```

Notes:
- Pack root can be either `<pack-root>` (contains `tasks/`) or direct task root (`.../tasks`).
- Task-relative files (`grading.yaml`, `initial_state.json`, `rag/`, `www/`, `mock_web/`) are resolved from each task directory.

## Authoring Guide

Minimum task directory contract:

1. `task.yaml`
2. `grading.yaml`
3. Optional task assets:
   - `initial_state.json`
   - `www/`
   - `mock_web/`
   - `rag/`
   - task-local fixtures/files referenced by tools

Recommended scorer patterns:

1. Deterministic checks (`state_checks`, tool expectations) for objective outcomes.
2. Transcript checks (`must_contain`, `disallow_regex`) for policy/error constraints.
3. Optional rubric/LLM judge for nuanced synthesis quality.

Validation commands:

```bash
uv run tolokaforge validate --tasks "/abs/path/private-pack/tasks/**/task.yaml"
scripts/tests/validate_public_examples.sh
```

## Mock-Web Multi-Root

Mock web supports multiple task roots via:
- `TASKS_DIRS=/path/to/tasksA,/path/to/tasksB`
- Backward-compatible fallback: `TASKS_DIR`

Shared data/assets lookup order:
1. Task-local files
2. Pack-shared assets (`<tasks-root>/<category>/_assets`)
3. Remaining task roots in configured order

Conflict behavior:
1. Root list order is precedence (`task_packs[0]` wins first).
2. Duplicate task IDs/routes across packs are `first-wins` with warning logs.

## Docker Usage

When running in Docker, task packs must be mounted into both `orchestrator` and `mock-web`.

Use the override generator:

```bash
uv run python scripts/generate_task_pack_compose_override.py \
  --config my_run_config.yaml \
  --output docker-compose.taskpacks.override.yaml
```

Then launch with both compose files:

```bash
docker compose \
  -f docker-compose.yaml \
  -f docker-compose.taskpacks.override.yaml \
  --profile test up --build --abort-on-container-exit
```

The generated override sets:
- `TASK_PACKS_DIRS` for orchestrator-visible pack roots
- `TASKS_DIRS` for mock-web multi-root routing

Fallback for single-host-path setups:
- `TASK_PACKS_HOST_ROOT` in `docker-compose.yaml` can still bind one host root into `/taskpacks/default`.
