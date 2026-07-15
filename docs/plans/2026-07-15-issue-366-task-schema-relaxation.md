# Plan: task-schema relaxation — minimal `task.yaml` is `task_id` + `description`

Issue: #366
Branch: chore/task-schema-relaxation

## Context

Every task in `examples/native/example-microservices-pack/tasks/*/task.yaml`
uses the minimal Project-schema shape documented in
`docs/architecture/PROJECTS.md` ("A task that declares only `task_id` and
`description` inherits *everything* from the Project") — just `task_id` +
`description` (+ an optional `environment_manifest` override). But
`TaskConfig` still requires four fields from every task, so `tolokaforge run`
refuses to load any of the pack's tasks:

```
TaskConfig
  initial_state    Field required
  tools            Field required
  user_simulator   Field required
  grading          Field required
```

(The issue named three; `tools` is the fourth. Reproduced via
`TaskConfig(task_id="x", description="y")`.) `#240` shipped the *reservations*
half of the relaxation (actor/seed/capability name-squatting) plus a
placeholder test that still passes all four fields; this issue completes the
required→optional change and adds sibling-`grading.yaml` auto-pickup.

## Goal

`TaskConfig` accepts the minimal shape `task_id` + `description`. The four
currently-required fields become optional with the sane defaults the
PROJECTS.md contract names:

- `initial_state` → empty state (`InitialStateConfig()`)
- `tools` → no tools (`ToolsConfig()`)
- `user_simulator` → cooperative LLM user (`UserSimulatorConfig()` — already
  `mode="llm"`, `persona="cooperative"`, `backstory=None`)
- `grading` → `None`, with the loader auto-picking up a sibling `grading.yaml`
  when one exists next to `task.yaml`

All five microservices-pack tasks then load through `NativeAdapter` and
produce a valid `TaskDescription` (verified: with the four defaults injected,
all five validate and the defaults are the only blockers).

## Non-goals

- **Grading-combine correctness.** `project.task_defaults.grading_defaults`
  is dead config (never merged into any task's grading) — filed as **#376**.
  The pack tasks will load and run after this PR but grade with default
  combine weights, not the project's declared `weights: {llm_judge: 1.0}`.
  Out of scope here; do not wire `grading_defaults` in this PR.
- **A "no-grade" grading path.** `Grade.binary_pass` is a required `bool`; a
  task with no grading anywhere (no `grading:` field and no sibling
  `grading.yaml`) is a valid *authoring* state but its run-time grade
  semantics are unchanged by this PR — `to_task_description` already
  synthesises an empty grading config, and the two in-process grading
  accessors fail loud (see Stage 1). No new no-grade behaviour is built.
- **Removing the now-redundant `if task.<field> and …` guards** scattered in
  `native.py`/`conductor.py`. With model-instance defaults they are always
  truthy and harmless; removing them is churn. Leave them.
- **terminal_bench tasks.** They use the upstream terminal-bench schema
  (`instruction:`, `max_agent_timeout_sec`, …) via a separate adapter, not
  `TaskConfig`. Unaffected.

## Stages

### Stage 1: Relax `TaskConfig` — four fields optional with safe defaults

- **Contract** (`tolokaforge/core/models.py`, `class TaskConfig`):
  - `initial_state: InitialStateConfig = Field(default_factory=InitialStateConfig)`
  - `tools: ToolsConfig = Field(default_factory=ToolsConfig)`
  - `user_simulator: UserSimulatorConfig = Field(default_factory=UserSimulatorConfig)`
  - `grading: str | None = None`
  - No other `TaskConfig` field changes — `name`, `category`, `max_turns`,
    `adapter_type`, `metadata`, `policies`, `adapter_settings`,
    `system_prompt`, `stuck_heuristics`, `timeouts`, `environment_manifest`,
    `actors` are already optional.
  - Model-instance defaults (not `None`) are deliberate: the live trial path
    (`conductor.py` `task.user_simulator.mode`, `task.tools.agent`; `native.py`
    `create_environment` `task.initial_state.json_db`) dereferences these
    unguarded, so a default instance keeps every consumer working with **zero
    consumer changes**. The default `UserSimulatorConfig()` is exactly the
    "cooperative user simulator" the issue asks for.
  - **`grading` None-handling (fail-loud, not `TypeError`):** the two
    in-process consumers that dereference `task.grading` unguarded —
    `NativeAdapter.get_grading_config` and `NativeAdapter.compute_golden_hash`
    (which calls it) — must raise a clear `ValueError`
    ("task `<id>` has no grading configured …") when `task.grading is None`,
    instead of `task_dir / None` raising a bare `TypeError`. `to_task_description`
    is already fully guarded (`if task.grading:`) and needs no change.
- **Behaviour to lock (unit):**
  - `TaskConfig(task_id="x", description="y")` validates; `initial_state`,
    `tools`, `user_simulator` are the empty model instances;
    `user_simulator.mode == "llm"` and `persona == "cooperative"`;
    `grading is None`.
  - **Rewrite** the existing placeholder
    `tests/unit/test_schema_reservations.py::TestTaskConfigMinimal::test_task_id_plus_description_is_enough`
    so its body actually omits the four fields (today it passes all four,
    contradicting its own name) and asserts the defaults above.
  - `get_grading_config` on a task with `grading=None` raises `ValueError`
    with a message naming the task, not `TypeError`.
- **Compatibility:** `task.yaml` is a task-pack compatibility surface
  (AGENTS.md Core Rule 5). required→optional is **additive** — verified every
  shipped example pack still validates (`native_shared_domain` supplies
  `tools` via its `_shared/domain.yaml` merge; other packs declare all four).
  CHANGELOG entry under `## Unreleased` → `### Feat`:
  `**schema**: task.yaml minimal shape is task_id + description; initial_state
  / tools / user_simulator / grading now optional with sane defaults (#366)`.
- **Deliverable:** `TaskConfig` accepts the minimal shape; unit tests green.
- **Validation:** `run_tests` marker `unit` (path
  `tests/unit/test_schema_reservations.py`); `run_python` reproducing
  `TaskConfig(task_id="x", description="y")` now succeeds. Reviewer checks the
  defaults are model instances (not `None`) and the fail-loud grading guard.
- **Doc updates:** `docs/TASKS.md` § "task.yaml Essentials" — add a short
  "Minimal task" note stating the minimal shape is `task_id` + `description`
  and listing the four optional fields with their defaults (empty state, no
  tools, cooperative LLM user, sibling `grading.yaml` auto-picked — see Stage
  2). Rewrite so the minimal shape reads as the normal state, not a "now
  relaxed" migration note.

### Stage 2: Sibling `grading.yaml` auto-pickup in the task loader

- **Contract** (`tolokaforge/adapters/_task_loader.py`, `load_task_yaml`):
  after the domain/project/task deep-merge and before
  `_resolve_environment_manifest_paths` / `TaskConfig(**task_data)`, when the
  merged `task_data` has no `grading` key (or it is falsy) **and** a
  `grading.yaml` file exists next to `task.yaml`
  (`task_path.parent / "grading.yaml"`), set `task_data["grading"]` to that
  file's **absolute** path. An explicit `grading:` in `task.yaml` (or from the
  domain/project layers) always wins — auto-pickup only fills an unset field.
  Absolute path is deliberate: it is layout-independent (flat and
  shared-domain), survives `task_dir / task.grading` joins unchanged, and
  needs no `_PATH_FIELD_REWRITERS` entry.
- **Behaviour to lock (unit):**
  - A `task.yaml` omitting `grading` with a sibling `grading.yaml` →
    `TaskConfig.grading` resolves to that sibling and `get_grading_config`
    loads it.
  - The same `task.yaml` with **no** sibling `grading.yaml` → `grading is
    None` (no crash, no fabricated path).
  - An explicit `grading: <other>.yaml` in `task.yaml` is **not** overridden
    by a present sibling `grading.yaml`.
  - Add to `tests/unit/test_task_loader.py`.
- **Compatibility:** internal loader behaviour; additive (packs that declared
  `grading:` explicitly are unchanged). No compatibility-surface migration.
- **Deliverable:** loader auto-picks a sibling `grading.yaml`; unit tests green.
- **Validation:** `run_tests` marker `unit` (path
  `tests/unit/test_task_loader.py`). Reviewer checks explicit-wins precedence
  and the no-sibling → `None` case.
- **Doc updates:** `docs/TASKS.md` — in the same "Minimal task" note from
  Stage 1, state that a `grading.yaml` sitting next to `task.yaml` is used
  automatically without a `grading:` line.

### Stage 3: End-to-end acceptance — the microservices pack loads and builds

- **Contract:** no production code — the acceptance proof. All five
  `example-microservices-pack` tasks, loaded through the same path the
  orchestrator uses (`NativeAdapter` with the pack's `project_task_defaults`,
  as `Orchestrator` wires via `load_task_yaml(project_task_defaults=…)`),
  each yield a valid `TaskConfig` and a `to_task_description()` that carries
  the per-task `llm_judge` rubric (from the auto-picked sibling `grading.yaml`)
  and the cooperative user simulator.
- **Behaviour to lock (integration):** extend
  `tests/integration/test_example_microservices_pack.py` (already the pack's
  wiring proof — deliberately no Docker, no LLM):
  - The adapter discovers all five tasks (`api_endpoint_add`,
    `db_query_tuning`, `long_debugging_session`, `postgres_upgrade_test`,
    `schema_isolation_migration`).
  - Each `to_task_description(task_id)` succeeds and its `grading.llm_judge`
    is populated (rubric criteria present).
  - `user_simulator.mode == "llm"` on each (inherited default).
  - `schema_isolation_migration` resolves its task-local stack (full
    `stack.compose_file` override); the other four resolve the project
    default. (The existing tests already cover env resolution at the
    project level; this adds the per-task `to_task_description` proof.)
  - Keep the marker `integration` and the no-Docker/no-LLM philosophy of the
    existing file.
- **Compatibility:** test-only.
- **Deliverable:** the pack's five tasks provably load + build; the issue's
  repro no longer fails.
- **Validation:** `run_tests` marker `integration` (path
  `tests/integration/test_example_microservices_pack.py`). No API keys /
  Docker required for these assertions.
- **Doc updates:** none (Stages 1–2 own the doc changes).

## Discovered issues

- **Filed as issues:**
  - **#376** — `project.task_defaults.grading_defaults` is dead config (never
    merged into a task's grading). The pack relies on it for combine weights;
    without it the judge-only tasks grade against a non-existent
    `state_checks` component. Distinct from #218. Out of scope for #366.
- **Fix in this PR:**
  - Rewrite the misleading placeholder
    `test_schema_reservations.py::TestTaskConfigMinimal` (Stage 1) — its name
    claims "task_id + description is enough" but its body passes all four
    fields.
- **Noted, not fixed (no churn):** the redundant `if task.<field> and …`
  guards in `native.py`/`conductor.py` become always-true with the new
  defaults; leaving them is cheaper and safer than a guard-removal sweep.

## Risks / open questions

- **The pack loads but does not yet grade to its declared policy** (#376).
  Stage 3 asserts loading + `to_task_description` wiring only, not grade
  correctness — matching the existing test file's no-LLM philosophy. If the
  reviewer expects "runs end-to-end" to include a correct combined grade,
  that is #376, not this PR.
- **Model-instance defaults vs `None`:** chosen so the unguarded live
  consumers keep working without a guard sweep. The trade-off is that a task
  that "declares no user simulator" still gets a cooperative LLM user rather
  than "no user" — which is the intended contract per PROJECTS.md, not a
  workaround.
- **`grading` absolute-path auto-pickup** assumes the sibling sits next to
  `task.yaml` (true for flat and shared-domain case dirs). If a future layout
  puts `grading.yaml` elsewhere, the auto-pickup simply doesn't fire and
  `grading` stays `None` — no wrong-file risk.
