# Configuration Guide

Tolokaforge uses YAML for three layers of configuration:
- Run configuration (`run.yaml`)
- Task specification (`task.yaml`)
- Grading specification (`grading.yaml`)

For full schemas, see `docs/REFERENCE.md`.

## Run Configuration (`run.yaml`)

```yaml
models:
  agent:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.0
    max_tokens: 4096
    seed: 42
  user:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.3

orchestrator:
  workers: 4
  repeats: 5
  max_budget_usd: 50.0        # optional hard stop
  max_requests_per_second: 2.0 # optional global throttle across workers
  max_attempt_retries: 1      # optional retries for transient infra failures
  queue_backend: "sqlite"     # "sqlite" (default) or "postgres"
  queue_postgres_dsn: null    # required when queue_backend="postgres"
  max_turns: 50
  continue_prompt: "Please proceed to the next step."
  timeouts:
    turn_s: 60
    episode_s: 1200
  stuck_heuristics:
    max_repeated_tool_calls: 5
    max_idle_turns: 8
  runtime: "docker"       # Docker-based tool execution (only supported mode)

evaluation:
  # Optional: external task-pack roots (local paths)
  task_packs:
    - "/abs/path/private-pack-core"
    - "/abs/path/private-pack-mobile"
  # Resolved relative to each task pack root when task_packs is set
  tasks_glob: "**/task.yaml"
  output_dir: "results/run_001"
  cache_images: true
  harness_adapter:
    type: "native"
    params: {}
```

Notes:
- PyPI wheels exclude `tasks/**`; configure benchmark content via `evaluation.task_packs`.
- `runtime: docker` is the only supported runtime; it uses the executor service and environment containers.
- `max_budget_usd` pauses scheduling new trials when cumulative spend reaches the budget.
- `max_requests_per_second` applies a global limiter across worker threads.
- `max_attempt_retries` retries transient failures (`rate_limit`, `api_error`, `timeout`) before marking a trial failed.
- `queue_backend: postgres` enables distributed queue/state using Postgres; set `queue_postgres_dsn`.
- If `evaluation.task_packs` is empty, `tasks_glob` is resolved relative to the working directory.
- If `evaluation.task_packs` is set, relative `tasks_glob` patterns are resolved under each task-pack root and merged.
- If `evaluation.task_packs` is set, `tasks_glob` must be relative (absolute patterns fail fast).
- For Docker runs with external task packs, mount packs into the orchestrator/mock-web containers and set:
  - `TASK_PACKS_DIRS` for orchestrator-visible pack roots
  - `TASKS_DIRS` for mock-web task roots (category directories)
- Recommended: generate compose override from config via
  `uv run python scripts/generate_task_pack_compose_override.py --config my_run_config.yaml --output docker-compose.taskpacks.override.yaml`
- For long runs, inspect progress with:
  `tolokaforge status --run-dir <output_dir_timestamped>`
- For Postgres queue status (no local `run_queue.sqlite`):
  `tolokaforge status --run-dir <any_existing_dir> --config my_run_config.yaml`
- For distributed worker mode:
  `tolokaforge prepare --config my_run_config.yaml --run-dir <run_dir> --reset-queue`
  `tolokaforge worker --config my_run_config.yaml --run-dir <run_dir>`
- For multi-runner distributed execution (e.g., GitHub Actions matrix), use
  `queue_backend: postgres` with a shared `queue_postgres_dsn`.

## Task Specification (`task.yaml`)

```yaml
task_id: "browser_simple_navigation"
name: "Simple Browser Navigation"
category: "browser"
description: "Navigate to the mock Example Domain page"

initial_state:
  json_db: "initial_state.json"          # optional
  filesystem:
    copy:
      - from: "fixtures/file.txt"
        to: "/env/fs/agent-visible/file.txt"
  mock_web:
    base_url: "http://mock-web:8080"
  rag:
    corpus_dir: "rag/corpus"

system_prompt: null

tools:
  agent:
    enabled: ["browser", "read_file", "write_file", "db_query", "search_kb"]
  user:
    enabled: []

user_simulator:
  mode: "scripted"   # "scripted" or "llm"
  persona: "cooperative"
  backstory: ""
  scripted_flow:
    - if_assistant_contains: "done"
      user: "Thanks!"
    - default: "Please continue."

policies:
  guidance:
    - "Use the browser tool to navigate"
  disallowed_actions: []

metadata:
  complexity: "medium"                # optional analytics slice
  expected_failure_modes: ["tool_arguments", "timeout_or_resource"]
  tags: ["onboarding", "browser-basics"]

grading: "grading.yaml"
```

## Grading Specification (`grading.yaml`)

```yaml
combine:
  method: "weighted"
  weights:
    state_checks: 0.6
    transcript_rules: 0.2
    llm_judge: 0.2
  pass_threshold: 0.8

state_checks:
  jsonpaths:
    - path: "$.db.orders[0].status"
      equals: "completed"

transcript_rules:
  must_contain: ["confirmed"]
  disallow_regex: ["(?i)password"]
  max_turns: 40
  tool_expectations:
    required_tools: ["browser"]
    disallowed_tools: []

llm_judge:
  model_ref: "openrouter/anthropic/claude-3.5-sonnet"
  rubric: |
    Grade task completion and correctness.
  output_schema:
    type: object
    properties:
      score: { type: number, minimum: 0, maximum: 1 }
      reasoning: { type: string }
    required: ["score", "reasoning"]
```

## Environment Variables

Common keys:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`
- `GOOGLE_API_KEY`
- `AZURE_API_KEY`, `AZURE_API_BASE`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `OLLAMA_API_BASE`

## Output Structure

```
output_dir/
├── aggregate.json
├── failure_attribution.json
├── per_task_metrics.json
├── metadata_slices.json
├── run_queue.sqlite
└── trials/
    └── <task_id>/<trial_index>/
        ├── task.yaml
        ├── trajectory.yaml
        ├── env.yaml
        ├── metrics.yaml
        ├── grade.yaml
        └── logs.yaml
```

See `docs/OUTPUT_FORMAT.md` for details.
For runner operations and queue workflows, see `docs/RUNNER.md`.
