# Plan: Wire project grading_defaults.combine into a task's resolved grading

Issue: #376
Branch: fix/grading-defaults-merge (landing on `feat/project-layer-runnability`)

## Context

`ProjectConfig.task_defaults.grading_defaults.combine` is dead config: no
code path merges it into a task's effective grading. `PROJECTS.md`
§ "Grading model" says a project's `grading_defaults.combine` deep-merges
under each task's own `grading.yaml.combine`, and the `GradingDefaults`
docstring already claims "A task's own `grading.yaml.combine` deep-merges
on top" — but that merge was never implemented.

Reproduced empirically against the shipped `example-microservices-pack`
(five judge-only tasks; project declares `combine.weights: {llm_judge: 1.0}`,
`pass_threshold: 0.8`). Building each task's `TaskDescription` today:

- `api_endpoint_add` (ships no `combine` block) → `weights={'state_checks': 1.0}`,
  `pass_threshold=1.0` — the arbitrary hardcoded fallback in
  `NativeAdapter.to_task_description` (`native.py:659-660`). `state_checks`
  is not even a component these tasks produce, so the trial grades on a
  non-existent component.
- `long_debugging_session` (ships `combine: {pass_threshold: 0.7}` only) →
  `weights={'state_checks': 1.0}`, `pass_threshold=0.7`. The task's own
  `pass_threshold` survives, but the project's `weights` never fills in.

Second, latent, defect found while tracing: `NativeAdapter.get_grading_config`
(`native.py:313`, called live by `conductor.py`) does
`GradingConfig(**grading_data)` where core `GradingConfig.combine` is a
**required** field. A judge-only task shipping no `combine` block raises a
`ValidationError` there today. The four no-`combine` microservices tasks hit
this. The same resolver that fixes the merge also fixes this crash.

## Goal

A task's effective grading `combine` is resolved as a deep-merge of three
layers, later wins per field:

1. Canonical `GradingCombineConfig` defaults (`method="weighted"`,
   `weights={}`, `pass_threshold=0.8`).
2. `project.task_defaults.grading_defaults.combine` (project base).
3. The task's own `grading.yaml.combine` (task delta).

`weights` is a map, so it merges key-by-key per the documented config
algebra (`project_loader.deep_merge`); scalar fields (`method`,
`pass_threshold`) take the highest layer that sets them. A task that ships
no `combine` block inherits the project's whole block; a task that ships a
partial block (e.g. `pass_threshold` only) inherits every field it does not
set. Both `to_task_description` (runner-side `GradingConfig`) and
`get_grading_config` (core-side `GradingConfig`) apply the same resolution.

## Non-goals

- Not touching the combine *execution* logic (`runner/grading.py`,
  `core/grading/combine.py`) — only how the combine *config* is resolved.
- Not fixing #218 (silently-unimplemented `combine_method` values) — the
  resolver preserves whatever `method` the layers specify; unknown-method
  fail-loud is #218's scope.
- Not consolidating the three divergent default sources for `combine` (see
  Discovered issues) beyond routing the adapter through the canonical one.
- Not changing the `grading.yaml` file format, the `GradingDefaults` schema,
  or the orchestrator → adapter plumbing (`project_task_defaults` already
  carries `grading_defaults` to the adapter).

## Stages

### Stage 1: Pure combine resolver + unit tests

- **Contract:** new function in `tolokaforge/core/project_loader.py`
  (the module that owns `deep_merge` and the sibling `resolve_effective_*`
  resolvers; already imports `tolokaforge.core.models`):

  ```python
  def resolve_effective_grading_combine(
      project_combine: dict[str, Any] | None,
      task_combine: dict[str, Any] | None,
  ) -> GradingCombineConfig:
      merged = deep_merge(project_combine or {}, task_combine or {})
      return GradingCombineConfig(**merged)
  ```

  Semantics: `task_combine` wins over `project_combine` per field;
  `weights` merges key-by-key (task key wins, project-only keys survive);
  absent fields fall through to `GradingCombineConfig`'s own defaults.
  Inputs are raw dicts (the shape both call sites already hold — the
  project defaults arrive as a `model_dump` dict, the task combine as
  parsed YAML), so the resolver needs no model on either input side.
- **Behaviour to lock (unit, `tests/unit/test_project_loader.py`):**
  - both `None` → `GradingCombineConfig()` (method `weighted`, weights `{}`,
    pass_threshold `0.8`).
  - project-only `{weights: {llm_judge: 1.0}}`, task `None` → weights
    `{llm_judge: 1.0}`, pass_threshold `0.8` (canonical default fills in).
  - task-only (no project) → task values verbatim; project layer is a no-op
    (locks zero blast radius for packs without `grading_defaults`).
  - partial task delta `{pass_threshold: 0.7}` over project
    `{weights: {llm_judge: 1.0}}` → `{method: weighted, weights: {llm_judge: 1.0},
    pass_threshold: 0.7}` (the `long_debugging_session` shape).
  - scalar conflict: task `pass_threshold` wins over project `pass_threshold`.
  - `weights` key-by-key: project `{a: 1.0}` + task `{b: 1.0}` →
    `{a: 1.0, b: 1.0}`; project `{a: 1.0}` + task `{a: 0.5}` → `{a: 0.5}`.
- **Compatibility:** internal only — new pure function, no caller yet.
- **Deliverable:** `resolve_effective_grading_combine` importable from
  `tolokaforge.core.project_loader`, unit tests green.
- **Validation:** `dev run_tests -m unit -k grading_combine`.
- **Doc updates:** none this stage (behaviour is not yet wired).

### Stage 2: Wire the resolver into NativeAdapter + lock pack inheritance

- **Contract:** `NativeAdapter` applies `resolve_effective_grading_combine`
  at both grading-construction sites, sourcing the project layer from the
  `grading_defaults.combine` sub-dict of `self._project_task_defaults`
  (already passed in by `Orchestrator._create_adapter`; `None`/absent when
  there is no project). No new constructor param, no signature change on
  `to_task_description` / `get_grading_config`.
  - `to_task_description`: replace the hardcoded fallback block
    (`native.py:656-664`) — resolve the effective combine, then build the
    runner `GradingConfig` from it (`combine_method=effective.method`,
    `weights=effective.weights`, `pass_threshold=effective.pass_threshold`).
    The `{"state_checks": 1.0}` / `1.0` literals are deleted.
  - `get_grading_config`: construct the core `GradingConfig` with
    `combine=` the resolved `GradingCombineConfig` (the rest of
    `grading_data` unchanged). Fixes the required-field crash for
    no-`combine` tasks and applies the same inheritance.
- **Behaviour to lock (integration,
  `tests/integration/test_example_microservices_pack.py` — reuses the
  existing `_make_pack_adapter()` real-loader wiring, no Docker/LLM):**
  - `api_endpoint_add.grading` → `combine_method="weighted"`,
    `weights == {"llm_judge": 1.0}`, `pass_threshold == 0.8` (whole block
    inherited).
  - `long_debugging_session.grading` → `weights == {"llm_judge": 1.0}`
    (inherited), `pass_threshold == 0.7` (task override), method `weighted`.
  - `adapter.get_grading_config("api_endpoint_add")` returns without raising
    and its `combine.weights == {"llm_judge": 1.0}` (regression lock on the
    latent crash).
- **Compatibility:** `grading.yaml` is a compatibility surface (AGENTS.md
  Core Rule 5). This is additive for every task that ships a full `combine`
  block (16 of 16 non-microservices example tasks specify both `weights`
  and `pass_threshold`; their resolved combine is byte-identical). Behaviour
  changes only for tasks that ship no/partial `combine`: they now inherit
  the project block instead of the arbitrary `{state_checks:1.0}`/`1.0`
  fallback — fixing a documented-but-unshipped contract. A task shipping no
  `combine` under a project with no `grading_defaults` now resolves to the
  canonical `weights={}` / `pass_threshold=0.8` instead of
  `{state_checks:1.0}` / `1.0`; no shipped pack has this shape (verified via
  `rg` over `examples/`). CHANGELOG "Fix" entry required.
- **Deliverable:** the merge is live; both microservices assertions and the
  crash-regression assertion pass; existing pack tests still pass.
- **Validation:** `dev run_tests -m integration -k microservices_pack`;
  `dev run_tests -m unit` (adapter/loader regressions);
  `dev run_tests -m canonical -k native_adapter` (no snapshot drift).
- **Doc updates:**
  - `CHANGELOG.md` → "Unreleased" / "Fix": one line naming #376 —
    project `grading_defaults.combine` now deep-merges under each task's
    `grading.yaml.combine`.
  - `docs/GRADING.md` → in the combine section, add that `combine` is
    optional per task and inherits from `project.task_defaults.grading_defaults.combine`
    when omitted or partial (task fields win). Rewrite so the inheritance
    reads as the only behaviour — no "now merges" phrasing.

## Discovered issues

- **Fix in this PR:** `NativeAdapter.get_grading_config` raised
  `ValidationError` on any task shipping no `combine` block (core
  `GradingConfig.combine` is required), on the live `conductor.py` path.
  Stage 2's resolver injection fixes it; the crash-regression assertion
  locks it.
- **Recommend filing (GitHub MCP not connected this session — main to
  file against Toloka/tolokaforge):** `combine` has three divergent default
  sources — core `GradingCombineConfig` (`weights={}`, `pass_threshold=0.8`),
  runner `GradingConfig` (`weights={"state_checks":1.0}`, `pass_threshold=0.8`),
  and (pre-fix) the `native.py` hardcode (`{"state_checks":1.0}`, `1.0`).
  This PR routes the adapter through the canonical core model, but the runner
  model's `default_factory=lambda: {"state_checks": 1.0}` still diverges for
  any other construction path. Track a follow-up to unify on the canonical
  default.

## Risks / open questions

- **`weights` merge semantics = key-by-key union, not wholesale replace.**
  This follows `PROJECTS.md` § "Deep-merge, precisely" (maps merge key-by-key;
  only lists replace) and the existing `deep_merge`. Consequence: a task
  cannot *remove* a weight key the project set — it can only override or add.
  Consistent with the rest of the config system, which also cannot express
  map-key removal. Flagging for confirmation; wholesale-replace is the only
  alternative and would diverge from the documented algebra.
- **Empty `weights` with `method="weighted"`** yields `total_weight == 0`;
  both combine implementations guard `if total_weight > 0`, so the score is
  `0.0` (task fails) — no division-by-zero, no crash. Only reachable by a
  no-`combine` task under a project with no `grading_defaults`, which no
  shipped pack has.
- **`exclude_defaults=True`** in `Orchestrator._create_adapter` strips a
  project author's explicitly-written combine fields that equal a model
  default (the microservices `pass_threshold: 0.8` is stripped before it
  reaches the adapter). Benign here: the canonical `GradingCombineConfig`
  default re-supplies the identical value, so the resolved result is correct.
  Called out so a reviewer does not read it as a lost override.
