# Plan: B4 — `tolokaforge run --dry-run` renders first prompts and exits

Issue: Toloka/tolokaforge#284 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-284-b4-run-dry-run` (already created; branches off `feat/terminal-dx`; PR targets `feat/terminal-dx`)

## Context

Milestone Terminal DX has landed A1/A3/A4/A5/B1/B2/B3. The stdout / stderr contract from A4 (#280), the `--display` toggle from B2 (#282), the Live panel from B1 (#285), and the start/end banners from A5 (#281) are the contracts B4 must compose with:

- **A4 (#280)** — `emit_artifact_path(path)` is the ONE sanctioned stdout write. `docs/CLI.md § stdout / stderr contract` says every failure path leaves stdout empty. B4 must render its panels on **stderr** (through the shared `console`) and emit **nothing** on stdout, because a dry-run never produces a run directory.
- **A5 (#281)** — `print_run_start_banner` / `print_run_end_banner` (in `tolokaforge/cli/_run_banner.py`) print run-id + `file://` report URL on stderr. In dry-run there is no run directory to link to. B4 skips both.
- **B1 (#285)** — `LiveRunDisplay.for_mode(display_mode, cost_budget_usd=…)` opens a Rich `Live` region during a real run. Dry-run bypasses this entirely — there is no execution loop to progress-track.
- **B2 (#282)** — `--display=none` sets `console.quiet = True` in the group callback. Under `--display=none` the dry-run panels are silenced automatically (no per-command branching needed).

**Grep confirms the surface is net-new**: `rg -n "dry_run|--dry-run|dry-run" tolokaforge/` returns zero hits. No prior `--dry-run` flag on any tolokaforge command.

### Where the first-turn wire request is assembled today

`InProcessConductor.run()` (`tolokaforge/core/conductor.py:312`) drives five phases; the first assistant generation is emitted inside `_run_agent_loop` → `TrialRunner.run()` → `ToolCallingLoop.run()` → `ToolCallingLoop._generate()` (`tolokaforge/core/loop.py:309`), which calls `llm_client.generate(system=system_prompt, messages=messages, tools=tool_schemas, tool_choice="auto")`.

The three inputs to that first call are assembled by the conductor before the loop runs:

1. **`system_prompt`** — built by `InProcessConductor._build_system_prompt(task, tool_schemas, task_dir)` (`tolokaforge/core/conductor.py:836-982`). Priority chain: `task.policies["agent_system_prompt"]` (inline) → `task.system_prompt == "__adapter__"` (delegates to `adapter.get_system_prompt`) → `task.system_prompt` (file path) → `main_policy.md` legacy split → minimal default with guidance + browser URL. **Every branch is a local file read or in-memory string ops — no network I/O.**
2. **`tool_schemas`** (OpenAI-format list) — built inside `_setup_trial` (`tolokaforge/core/conductor.py:465-477`) from `runtime_backend.register_trial(...)["tool_schemas"]`. That gRPC call to the runner requires Docker to be up. BUT the underlying schemas originate orchestrator-side at `adapter.to_task_description(task_id).agent_tools` (`tolokaforge/adapters/native.py:465, 469, 490-499`), which produces `list[ToolSchema]` (a runner-side Pydantic model). `_load_rich_tool_schemas` / `_builtin_tool_schemas` read `fixtures/tools.json` or introspect the builtin tool classes — again, local file reads and in-process Python.
3. **`initial_user_message`** — from `TrialRunner._seed_first_user_message` (`tolokaforge/core/runner.py:301-353`). Priority: `task.initial_user_message` (string in `task.yaml`) → user-simulator LLM call. The second branch fires HTTP; the first is a static string.

**Consequence for dry-run**: the materialization seam that avoids all HTTP is `adapter.to_task_description(task_id).agent_tools` + `InProcessConductor._build_system_prompt` + `task.initial_user_message`. When `initial_user_message` is unset, dry-run cannot render the actual first user message (it would require the user-simulator LLM call) — it renders a labelled placeholder naming the simulator mode / persona / backstory.

**Schema-sanitizer parity**: what the LLM actually sees on the wire is `capabilities.schema_sanitizer.sanitize(tool_schemas)` (`tolokaforge/core/llm/client.py:1019`, and mirrored by `TrialArtifactWriter.write_tools_schemas` at `tolokaforge/core/conductor.py:721`). Dry-run runs the same pipeline: convert `ToolSchema` → OpenAI dict → `sanitize()`.

**`build_capabilities` is HTTP-free**: `LLMClient.__init__` (`tolokaforge/core/llm/client.py:358-392`) builds capabilities eagerly, then loads API keys via `SecretManager`. **Neither step opens a socket.** HTTP is exclusively inside `LLMClient.generate` / `_call_litellm`. Constructing an `LLMClient` (or, cleaner for dry-run, calling `build_capabilities(name, provider, overrides=…)` directly) to obtain the schema sanitizer is safe.

### Reproduced current behaviour

- `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml 2>&1 | head -20` (with an OpenRouter key) prints `Loading configuration…`, `Runtime backend:`, `Output base:`, start banner, then log lines as the first trial runs. Confirms the current run entry path (`tolokaforge/cli/main.py:503-708`) is a linear read-config → activate-preset-overlay → resolve-run-dir → construct-orchestrator → `load_tasks` → `orchestrator.run()` sequence.
- The `examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml` sets `initial_user_message` inline (line 6). Ideal fixture for the CLI-level test — its first user message is a static string, so dry-run can render it verbatim.

### A4/A5/B1/B2 composition contract (what dry-run inherits)

- **stdout**: empty (no run directory materialises → `emit_artifact_path` is not called).
- **stderr**: the panels themselves (via the shared `console`); NO start/end run banner; NO Live panel region; log lines from `configure_root_logging` still emit under the resolved `--log-format` / `-v` / `-q`.
- **`--display=none`**: `console.quiet = True` short-circuits the panel writes at buffer-check time (Rich contract). Dry-run does no explicit `if display_mode is NONE` branching — the silencing is inherited from the group callback.
- **Exit code**: 0 on successful render; non-zero on config load / task load failure (surfaces the same exception `tolokaforge run` would today).

### Files touched (audit)

| File | Stage | Reason |
|------|-------|--------|
| `tolokaforge/core/system_prompt.py` (new) | 1 | Module-level `build_system_prompt` (extraction from conductor). |
| `tolokaforge/core/conductor.py` | 1 | Delegate `InProcessConductor._build_system_prompt` to the module-level helper (unchanged behaviour). |
| `tolokaforge/core/dry_run.py` (new) | 1 | `DryRunSample` value + `materialize_dry_run_sample` + `load_tasks_for_dry_run`. |
| `tolokaforge/cli/_dry_run_render.py` (new) | 2 | `render_dry_run_sample(sample, console) -> None` + `render_dry_run_note`. |
| `tolokaforge/cli/main.py` | 2 | `--dry-run` / `--dry-run-samples` flags; dry-run branch in `run`. |
| `tests/unit/core/test_system_prompt.py` (new) | 1 | Lock `build_system_prompt` behaviour across the five branches. |
| `tests/unit/core/test_dry_run.py` (new) | 1 | Lock `materialize_dry_run_sample` fields + no-HTTP assertion. |
| `tests/integration/cli/test_run_dry_run.py` (new) | 2 | End-to-end: `--dry-run` exit 0 / stdout empty / stderr non-empty / no HTTP via `respx`. |
| `tests/canonical/test_dry_run_goldens.py` (new) | 3 | SVG snapshots (80 / 120 cols). |
| `tests/canonical/golden/dry_run/*.svg` (new) | 3 | Two golden files. |
| `docs/CLI.md` | 2 (stub) + 3 (full) | New `## Dry run` section. |
| `CHANGELOG.md` | 3 | Non-BREAKING addition. |

## Goal

`tolokaforge run --config <valid> --dry-run [--dry-run-samples N]` resolves the run config with full parity to a real run (preset overlay, `--user-model`, `--judge-model`, `--presets-file`, `--runtime`, `--workers`, `--cost-limit`, `--time-limit`, `--sample-limit`, `--fallback-models`, `--model-cost-config`), loads tasks via the adapter, renders one Rich panel per task (up to N, default N=3) describing the first-turn wire request, and exits 0 without opening a single HTTP connection to any LLM provider.

### Rendered panel — locked contract

For each sample (a `(task_id, trial_index=0)` pair — see D1), one `rich.panel.Panel` on the shared `console` (stderr) with:

- **Title**: `Task <task_id> · Trial 0`
- **Body** (ordered `rich.console.Group`):
  1. `[muted]System prompt:[/muted]` — literal text (multi-line preserved).
  2. Blank line.
  3. `[muted]User prompt:[/muted]` — either the literal `task.initial_user_message` OR (when unset) the placeholder `<generated at runtime by user simulator — mode={llm|scripted}, persona={persona}, backstory={backstory[:120]}…>`.
  4. Blank line.
  5. `[muted]Tools ({count}):[/muted]` — the sanitized OpenAI-shape tool list rendered via `rich.syntax.Syntax(json_dump, "json", theme="ansi_dark", word_wrap=True)`. When the list is empty, one line `[muted]  (no agent tools declared)[/muted]` replaces the syntax block.
  6. Blank line.
  7. One line: `[muted]Model:[/muted] <agent.provider>/<agent.name> · [muted]preset:[/muted] <effective_preset>`.
  8. One line: `[muted]Judge:[/muted] <judge.provider>/<judge.name>` OR `[muted]Judge:[/muted] [muted](none)[/muted]`.
  9. One line: `[muted]Runtime:[/muted] <shared|per_trial>`.

Between panels, one blank line. Before the first panel, one preamble line: `[bold]Dry run:[/bold] rendering first {n_rendered} sample(s) (of {n_available} task(s) available)`.

### Parity assertion (acceptance-criteria driven)

The `--cost-limit`, `--time-limit`, `--sample-limit`, and `--fallback-models` flags are **parsed and validated** exactly as in a real run (same `parse_duration` / `_parse_fallback_models` code paths — a bad `--time-limit=30xyz` fails `click.BadParameter` under dry-run identically). They are **not applied** because no trials execute. `--runtime` is validated and reflected in the `Runtime:` line. `--user-model` / `--judge-model` overlay `config_data["models"]["user"|"judge"]` and are reflected in resolved fields. `--presets-file` overlay is activated via `_activate_presets_overlay` and reflected in the `preset:` field via `resolve_effective_preset(agent.name, agent.provider)`.

The canonical parity assertion (Stage 2 test): given a fixture run config, `dry_run.materialize_dry_run_sample`'s resolved-model line == `_build_resolved_block(agent_config)["effective_preset"]` computed the same way `_write_artifacts` would. Same `preset_registry` walk, same overlay.

## Non-goals

- **Do NOT preempt B5 `--resume` (#286).** `--dry-run` and `--resume` compose logically only after B5 decides resume-time UX. This plan **rejects** `--dry-run --resume` with `click.UsageError` at flag-validation time; B5 may relax later. Rationale: the current `--resume` reads `RunState` from an existing run dir; dry-run has no run dir. Silently ignoring `--resume` would violate AGENTS.md Core Rule 1 ("surface failures explicitly").
- **Do NOT start TypeSense / Docker on dry-run.** `Orchestrator.load_tasks()` calls `_ensure_typesense_started()` (`tolokaforge/core/orchestrator.py:992`) which is a Docker side-effect. Dry-run uses a new lightweight helper `load_tasks_for_dry_run(run_config, project)` (in `dry_run.py`) that constructs the adapter and loops `adapter.get_task(task_id)` **without** the TypeSense preflight. Consequence: a run config that declares `orchestrator.typesense.enabled=true` renders panels showing the search config with `port="auto"` / `api_key=None` (unresolved). Documented in Stage 3 doc update; separate follow-up issue captures the enhancement path (see "Discovered issues"). Rationale: dry-run's contract is "no provider call" AND (implied by the AC's speed / test-mock feasibility) "no heavy side effects" — running Docker containers to render three panels is scope creep.
- **Do NOT run `Orchestrator.run()`, `resolve_run_directory`, `_generate_reports`, or any queue backend setup.** No `results/run_<ts>/` directory is created. No `run_state.json`, no `run_queue.sqlite`, no `LIMIT_HIT.json`.
- **Do NOT emit `emit_artifact_path` on dry-run.** stdout stays strictly empty.
- **Do NOT emit start/end run banners on dry-run.** The banners rely on `run_id` + `file://` URL derived from an actual run dir. Dry-run has neither. A preamble line `[bold]Dry run: rendering first N sample(s)…[/bold]` inside the shared `console` is the sole "banner" analogue.
- **Do NOT open `LiveRunDisplay`.** No progress panel, no `run_started` / `trial_started` events. Dry-run does not touch `tolokaforge/cli/_run_display.py`.
- **Do NOT render trial indexes other than 0.** A repeats-N run config would fire N trials per task; each one starts from the identical first-turn request (deterministic prompt assembly). Rendering `Trial 0..N-1` for the same task would be pure repetition. Sample = one `(task_id, trial_index=0)` per task.
- **Do NOT render the tool spec via `runtime_backend.register_trial`.** Dry-run uses the orchestrator-side `adapter.to_task_description(task_id).agent_tools` and applies the capability schema sanitizer — see D2. The runner may transform schemas further at register time (e.g. property-name sanitisation), but that is exactly the `sanitize_schema_properties` pass that already runs on both sides. Dry-run applies the same pass so the rendered shape matches what a real run would send.
- **Do NOT add a `--dry-run-json` structured-output flag.** The rendered panels are for human eyes; a machine-readable dump is scope creep. Filed as a follow-up (see "Discovered issues") in case ticket-writing / debugging workflows want it.
- **Do NOT change `docs/CLI.md § stdout / stderr contract` for other commands.** Only the run row gets a "dry-run" clarification.
- **Do NOT touch `tolokaforge prepare`.** Only `tolokaforge run` gains the flag. `prepare` writes an artifact (the queue directory) and is orthogonal to first-prompt preview.

## Stages

### Stage 1: prompt materialization primitives — extract `build_system_prompt`, introduce `dry_run.py`

**Contract**:

- **New module** `tolokaforge/core/system_prompt.py` exposes one public function:

  ```python
  def build_system_prompt(
      *,
      task: TaskConfig,
      task_dir: Path,
      adapter: BaseAdapter,
  ) -> str:
      """Return the agent's task-scope system prompt (pre-policy).

      Priority (unchanged from the previous conductor-private method):
      1. ``task.policies["agent_system_prompt"]`` — inline string.
      2. ``task.system_prompt == "__adapter__"`` → wrap ``adapter.get_system_prompt(task.task_id)``.
      3. ``task.system_prompt`` (file path) → read and return.
      4. Legacy ``main_policy.md`` + additional-policy split.
      5. Minimal default with ``policies["guidance"]`` and any ``tools.agent.browser.initial_url``.

      Deterministic and side-effect-free: only local file reads. Never
      opens a network connection. The returned string is the same string
      the runner would pass as ``system=`` on the first ``LLMClient.generate`` call
      **before** the ``prompt_policy`` enrichment layer runs.
      """
  ```

- **`InProcessConductor._build_system_prompt` becomes a two-line delegator** to the module-level helper. The private-method form stays (callers keep the method call site untouched) — it invokes `system_prompt.build_system_prompt(task=task, task_dir=task_dir, adapter=self.adapter)`.

- **New module** `tolokaforge/core/dry_run.py` exposes:

  ```python
  @dataclass(frozen=True)
  class DryRunSample:
      task_id: str
      trial_index: int  # always 0 in this stage
      system_prompt: str
      user_prompt_text: str  # literal message OR the placeholder note
      user_prompt_is_literal: bool  # False → placeholder for LLM-generated
      tool_spec: list[dict[str, Any]]  # sanitized OpenAI shape
      agent_model_line: str  # e.g. "openrouter/anthropic/claude-sonnet-4.6 · preset: anthropic_claude_4_7"
      judge_model_line: str  # e.g. "openrouter/openai/gpt-5" or "(none)"
      runtime_line: str  # "shared" | "per_trial"


  def load_tasks_for_dry_run(
      *,
      run_config: RunConfig,
      project: Project | None,
  ) -> tuple[BaseAdapter, list[TaskConfig]]:
      """Instantiate the adapter and return every declared task.

      Deliberately skips the ``_ensure_typesense_started`` side-effect that
      ``Orchestrator.load_tasks`` performs. Dry-run never queries TypeSense,
      so starting it would violate the "no heavy side effects" contract.
      Constructs the adapter via the same path ``Orchestrator._create_adapter``
      uses (reads ``run_config.adapters`` / ``run_config.evaluation.task_packs``).
      """


  def materialize_dry_run_sample(
      *,
      task: TaskConfig,
      task_dir: Path,
      adapter: BaseAdapter,
      agent_config: ModelConfig,
      judge_config: ModelConfig | None,
      runtime_choice: str,
  ) -> DryRunSample:
      """Produce a fully resolved :class:`DryRunSample` for one task.

      Pipeline:
      1. ``system_prompt = build_system_prompt(task=task, task_dir=task_dir, adapter=adapter)``
      2. ``user_prompt_text``, ``user_prompt_is_literal`` — from
         ``task.initial_user_message`` if set, else placeholder derived
         from ``task.user_simulator`` (mode / persona / backstory).
      3. ``tool_spec`` — ``adapter.to_task_description(task.task_id).agent_tools``
         converted to OpenAI shape (``{"type": "function", "function": {…}}``),
         passed through ``sanitize_schema_properties`` (name-charset) and
         then ``capabilities.schema_sanitizer.sanitize`` where capabilities
         is built via ``build_capabilities(agent_config.name, agent_config.provider,
         overrides=agent_config.capabilities)``. **No LLMClient instance is created.**
      4. Resolved lines — ``agent_model_line`` = ``f"{provider}/{name} · preset: {resolve_effective_preset(name, provider)}"``;
         ``judge_model_line`` = same or ``"(none)"``; ``runtime_line`` = ``run_config.orchestrator.runtime``.

      Every step is local Python + file reads. **No HTTP.** No Docker.
      """
  ```

**Behaviour to lock** (both tests in `tests/unit/core/`, marker `unit`):

1. `test_system_prompt.py::test_build_system_prompt_five_branches` — fixture directory with five variant `task.yaml`s exercises each branch: `agent_system_prompt` inline, `__adapter__`, file path, legacy `main_policy.md` split, minimal default. Byte-for-byte equality with a captured expected string. Also asserts `InProcessConductor(...)._build_system_prompt(task, [], task_dir) == build_system_prompt(task=task, task_dir=task_dir, adapter=conductor.adapter)` for one fixture — parity guard so the delegation stays true.
2. `test_dry_run.py::test_materialize_sample_populates_every_field` — fixture task with a static `initial_user_message`, one builtin tool (`read_file`), an agent model of `openrouter/anthropic/claude-sonnet-4.6`. Asserts every `DryRunSample` field is populated and non-empty, and `sample.tool_spec` has the sanitized OpenAI shape (`sample.tool_spec[0]["type"] == "function"` etc.).
3. `test_dry_run.py::test_materialize_sample_never_hits_network` — patches `httpx.Client.send` and `litellm.completion` with a raise-on-call sentinel, asserts `materialize_dry_run_sample(...)` completes without triggering either. This is the load-bearing "no HTTP" guarantee at the unit tier — the CLI integration test in Stage 2 re-checks the full end-to-end flow.
4. `test_dry_run.py::test_load_tasks_for_dry_run_no_typesense_start` — patches `tolokaforge.core.search.typesense_server.create_typesense_server` with a raise-on-call sentinel and calls `load_tasks_for_dry_run(...)` against a fixture run config that DOES declare `orchestrator.typesense.enabled=true`. Asserts the sentinel is never called and tasks load anyway.

**Compatibility**: **internal only**. `build_system_prompt` and `dry_run.py` are new public Python API in `tolokaforge.core`, but nothing external depends on them yet. `_build_system_prompt` on `InProcessConductor` is a private method — the delegation keeps its signature untouched.

**Deliverable**: `tolokaforge/core/system_prompt.py`, `tolokaforge/core/dry_run.py`, updated `tolokaforge/core/conductor.py` (delegator), plus four unit tests.

**Validation**:
- `uv run pytest tests/unit/core/test_system_prompt.py tests/unit/core/test_dry_run.py -v` — all pass.
- `uv run pytest tests/canonical -m canonical -k "conductor or trial"` — pre-existing snapshots unchanged (the delegation is a no-op for behaviour).
- Grep guard: `rg -n "^\s+def _build_system_prompt" tolokaforge/core/conductor.py` still finds the two-line delegator.

**Doc updates**: none in this stage. Stage 3 rewrites `docs/CLI.md` and the CHANGELOG entry.

---

### Stage 2: `--dry-run` / `--dry-run-samples` CLI flag + rendering + integration tests

**Contract**:

- **New module** `tolokaforge/cli/_dry_run_render.py` exposes:

  ```python
  def render_dry_run_preamble(*, n_rendered: int, n_available: int, console: Console) -> None:
      """Emit the single preamble line on ``console`` (stderr).

      Exact literal: ``[bold]Dry run:[/bold] rendering first {n_rendered} sample(s)
      (of {n_available} task(s) available)``. Under ``console.quiet=True`` this is
      a no-op (Rich contract)."""


  def render_dry_run_sample(*, sample: DryRunSample, console: Console) -> None:
      """Emit one :class:`rich.panel.Panel` on ``console`` (stderr).

      Panel shape is locked by :func:`Stage 3 golden`. Uses
      ``rich.syntax.Syntax`` for the tool-spec JSON, ``rich.text.Text`` /
      ``rich.markdown``-free plain text for the prompts."""
  ```

- **`tolokaforge run` gains two new flags** (Click options):

  | Flag | Type | Default | Behaviour |
  |------|------|---------|-----------|
  | `--dry-run` | flag | `False` | Activates the dry-run branch (see below). |
  | `--dry-run-samples` | `click.IntRange(min=1)` | `3` | Max number of `(task_id, trial=0)` samples to render. When `> len(tasks)`, renders every task and prints a note. Requires `--dry-run` — using it without raises `click.UsageError`. |

- **CLI body reshape**. `tolokaforge/cli/main.py::run` splits its body into two branches after the config is loaded and CLI overrides applied:

  1. Common resolution — unchanged: `load_effective_run_config`, apply `--user-model` / `--judge-model` / `--runtime` / `--workers` / `--cost-limit` / `--time-limit` / `--sample-limit` / `--model-cost-config` overrides, construct `RunConfig`, parse `--fallback-models` (validation only), activate preset overlay.
  2. Branch on `dry_run`:
     - **Dry-run path** (new):
       - Reject `--resume` + `--dry-run` with `click.UsageError("--dry-run and --resume are mutually exclusive; --dry-run does not consult run state")`.
       - Reject `--dry-run-samples` without `--dry-run` (Click-level `callback=`).
       - Call `_print_runtime_banner(...)` — parity: operator sees the resolved backend even in dry-run.
       - `adapter, tasks = load_tasks_for_dry_run(run_config=run_config, project=project)`.
       - If no tasks: `console.print("[red]No tasks found![/red]"); raise SystemExit(1)` (parity with the current "no tasks" path).
       - `agent_config = run_config.models["agent"]`; `judge_config = run_config.models.get("judge")`; `runtime_choice = run_config.orchestrator.runtime`.
       - `n_rendered = min(dry_run_samples, len(tasks))`.
       - `render_dry_run_preamble(n_rendered=n_rendered, n_available=len(tasks), console=console)`.
       - For each of `tasks[:n_rendered]`: `sample = materialize_dry_run_sample(task=t, task_dir=adapter.get_task_dir(t.task_id), adapter=adapter, agent_config=agent_config, judge_config=judge_config, runtime_choice=runtime_choice); render_dry_run_sample(sample=sample, console=console)`.
       - Return (implicit exit 0). No `emit_artifact_path`, no start/end banner, no `LiveRunDisplay`.
     - **Real-run path** (unchanged): today's body from `resolve_run_directory` onward.

**Behaviour to lock** (`tests/integration/cli/test_run_dry_run.py`, marker `integration` — needs no live API keys, just uses `respx` / `pytest-monkeypatch` to assert no HTTP):

1. `test_dry_run_exit_zero_stdout_empty` — runs `tolokaforge run --config examples/native/tool_use/run_config.yaml --dry-run` in-process via `click.testing.CliRunner(mix_stderr=False)`. Asserts `result.exit_code == 0`, `result.stdout == ""`, `"Dry run:" in result.stderr`, and the panel for `tool_use_public_example_01` appears in `result.stderr`. **This is the acceptance-criterion locking test.**
2. `test_dry_run_no_http_via_network_mock` — installs a `respx.MockRouter(assert_all_called=False)` that matches every host (`.route()`), then runs the dry-run command; asserts `mock.calls.call_count == 0`. Cross-check: also patches `litellm.completion` with `pytest.fail`-raising sentinel — never fires. **Acceptance criterion "never opens an HTTP connection to any provider" locked here.**
3. `test_dry_run_samples_flag_renders_n_panels` — a run config with 5 tasks; asserts `--dry-run-samples 5` renders 5 panels (5 occurrences of `Task ` header) and `--dry-run-samples 2` renders 2. When `--dry-run-samples 100` on the 5-task config, renders 5 panels and the preamble reads `rendering first 5 sample(s) (of 5 task(s) available)`.
4. `test_dry_run_preset_overlay_reflected` — fixture overlay YAML pins `anthropic/claude-sonnet-4.6` to a distinct preset name (e.g. `test_dry_run_overlay_preset`); asserts the `preset:` field on the rendered panel reflects that name. Parity assertion — the same value `_write_artifacts` records in `task.yaml.model_config.agent.resolved.effective_preset` for a real run.
5. `test_dry_run_resume_mutually_exclusive` — `--dry-run --resume` exits `2` with `"--dry-run and --resume are mutually exclusive"` in stderr.
6. `test_dry_run_samples_without_dry_run_rejected` — `--dry-run-samples 5` without `--dry-run` exits `2` with a `click.UsageError` about the flag pairing.
7. `test_dry_run_display_none_silences_stderr` — `--display=none --dry-run` renders panels; `console.quiet=True` short-circuits every write; `result.stderr` contains no `Dry run:` preamble and no panel titles. Exit 0.
8. `test_dry_run_bad_time_limit_still_fails_loudly` — `--dry-run --time-limit=30xyz` exits 2 with the same `--time-limit: <parse error>` diagnostic a real run produces. Locks the parity contract that flag validation runs identically in dry-run and real-run.
9. `test_dry_run_no_typesense_start_when_configured` — fixture run config sets `orchestrator.typesense.enabled=true`; patches `create_typesense_server` with a raise-on-call sentinel; runs `--dry-run`; asserts exit 0 and the sentinel is never called.

**Compatibility**: **CLI surface** — `--dry-run` and `--dry-run-samples` are new. Purely additive. `docs/CLI.md § stdout / stderr contract` is updated in Stage 3 to note the run row's dry-run behaviour.

**Deliverable**: `tolokaforge/cli/_dry_run_render.py`, updated `tolokaforge/cli/main.py`, nine integration tests. Stub `docs/CLI.md § Dry run` (single paragraph pointing to Stage 3 for the full text) — this is the minimum diff needed so the flag has a doc entry the moment it lands.

**Validation**:
- `uv run pytest tests/integration/cli/test_run_dry_run.py -v` — all pass (marker `integration`; no API keys needed).
- `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml --dry-run` — smoke check; three panels on stderr; stdout empty; exit 0.
- `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml --dry-run --dry-run-samples 1 2>&1 >/dev/null | wc -l` — confirms stdout is empty (redirected, dry-run emits nothing there).
- `uv run tolokaforge run --config examples/native/tool_use/run_config.yaml --dry-run --display=none` — silent (no panels).
- Grep guard: `rg -n "sys\.stdout|print\(" tolokaforge/cli/_dry_run_render.py` — zero hits (all output flows through the injected `console`, i.e. stderr). Existing `tests/canonical/test_cli_display_invariants.py::test_no_ad_hoc_console_in_cli` already forbids new `rich.Console(...)` — the new module imports `console` from `_display`; the guard passes without a code change.

**Doc updates**: stub `docs/CLI.md § Dry run` immediately after `## Display modes` (one paragraph): *"``tolokaforge run --dry-run`` resolves the run config and tasks, renders the first N (default 3) samples' first-turn prompts to stderr, and exits 0 without opening any HTTP connection to a provider. See § Dry run below for the full contract."* Full section body lands in Stage 3.

---

### Stage 3: SVG goldens + full docs + CHANGELOG

**Contract**:

- **Two new canonical SVG goldens** at `tests/canonical/golden/dry_run/panel_80.svg` and `panel_120.svg`, one per column width. Test: `tests/canonical/test_dry_run_goldens.py::test_dry_run_panel_matches_golden[80|120]`.

  Test structure mirrors `tests/canonical/test_run_display_goldens.py`:
  - Fresh `Console(stderr=True, soft_wrap=True, theme=THEME, width=W, record=True, force_terminal=True, color_system="truecolor")`.
  - Deterministic fixture `DryRunSample` (task_id `tool_use_public_example_01`, `initial_user_message` string, two builtin tools, `openrouter/anthropic/claude-sonnet-4.6` + `preset: anthropic_claude_4_7`, no judge, `runtime: shared`).
  - Call `render_dry_run_preamble(...)` then `render_dry_run_sample(...)`.
  - `svg = recorder.export_svg(theme=DEFAULT_TERMINAL_THEME, unique_id="tolokaforge-dry-run-{width}")`.
  - `assert svg == golden_path.read_text()`.
  - Regeneration: `uv run pytest tests/canonical/test_dry_run_goldens.py --update-canon` (same knob other canonical tests use).

- **`docs/CLI.md § Dry run`** — full section body replacing Stage 2's stub. Content: subsections `### Panel shape`, `### Sample selection`, `### Composition with other flags`, `### Zero-HTTP guarantee`, `### Typesense / Docker side-effects`. Every subsection describes the current state — no "previously X, now Y". Every subsection is short, prescriptive.

- **`docs/CLI.md § stdout / stderr contract`** — the `tolokaforge run` row gets a footnote-style clarification:

  > When `--dry-run` is set, stdout stays empty (no run directory is created) and stderr carries the rendered panels instead of the start/end banner + progress log lines.

- **`CHANGELOG.md`** — one bullet under an "Added" heading in the current release-in-progress section:

  > `tolokaforge run --dry-run [--dry-run-samples N]` — resolve config + tasks, render the first N (default 3) first-turn samples (system prompt, user prompt, sanitized tool spec, resolved model / judge / runtime), then exit 0 without any HTTP call to a provider. See `docs/CLI.md § Dry run`.

**Behaviour to lock** — the SVG goldens ARE the behaviour lock; they capture panel body ordering, Rich colour codes for `THEME` tokens, `Panel` border style, `Syntax` JSON theming, and column-width flow at 80 / 120.

**Compatibility**: **doc + changelog only**. No code change.

**Deliverable**: two SVG files, one canonical test module, updated `docs/CLI.md`, one CHANGELOG entry.

**Validation**:
- `uv run pytest tests/canonical/test_dry_run_goldens.py -v -m canonical` — passes.
- `uv run pytest tests/canonical -m canonical` — full canonical run passes (no regression).
- `rg -n "Dry run|--dry-run" docs/` — the two new mentions plus the existing plan file appear.
- `rg -n "previously|used to|before the|now the" docs/CLI.md` — zero drift-marker hits (per AGENTS.md Core Rule 8, docs are always current).

**Doc updates**: as above.

## Design decisions

### D1. Sample = one `(task_id, trial=0)` per task

**Options considered**:

- (a) Sample = every `(task_id, trial)` pair up to N (respects `orchestrator.repeats`).
- (b) Sample = one `(task_id, trial=0)` per task, cap at N.

**Decision: (b).** Prompt assembly is deterministic per task — every trial of the same task hits the identical first-turn system prompt / user prompt / tool spec (variance appears later in the multi-turn loop). Rendering `Trial 0..N-1` of the same task adds zero information, N× the output length. Operator intent under `--dry-run` is "show me what the LLM will see for each task" — trial 0 is representative.

If a future task type introduces per-trial prompt variance (seed-dependent initial state affecting `_build_system_prompt`), the sample structure widens to `(task_id, trial_idx)` with distinct panels — but the current codebase has no such surface (`_build_system_prompt` is a function of `task` + `task_dir` + `adapter`, none of which vary by trial index).

### D2. Materialize tool spec via `adapter.to_task_description` — not `runtime_backend.register_trial`

**Options considered**:

- (a) Boot the runtime backend, provision (Docker), call `register_trial`, extract `tool_schemas` from the result. Highest fidelity to what the runner sees.
- (b) Read tool schemas from `adapter.to_task_description(task_id).agent_tools` orchestrator-side, apply `sanitize_schema_properties` + `capabilities.schema_sanitizer.sanitize`. No Docker.

**Decision: (b).** Path (a) requires Docker Compose up, TypeSense possibly up, runner gRPC service up — heavy startup, and the failure surface (Docker daemon not running, image not built, port conflict) is orthogonal to what dry-run is trying to show. Path (b) uses the same orchestrator-side authoring source (`ToolSchema` values built inside the adapter's `to_task_description`), converts to OpenAI shape, then runs the identical sanitization the CLIENT applies at wire time (`capabilities.schema_sanitizer.sanitize` at `tolokaforge/core/llm/client.py:1019`).

The runner-side transformation between `adapter.to_task_description().agent_tools` and `runtime_backend.register_trial()["tool_schemas"]` is limited to:
- Property-name sanitization (`sanitize_schema_properties`) — captured in path (b) via the explicit sanitize call.
- No structural rewrites (the runner unpacks `ToolSchema.parameters` verbatim into the schema field the runner returns).

So (b) is equivalent to what the runner ships to the LLM, without a Docker dependency.

### D3. `build_system_prompt` extracted to a module-level function

**Options considered**:

- (a) Leave `_build_system_prompt` as a private method on `InProcessConductor`; dry-run reaches in via `getattr` or via constructing a synthetic conductor.
- (b) Extract to `tolokaforge/core/system_prompt.py::build_system_prompt`; conductor's private method becomes a two-line delegator.

**Decision: (b).** A 100-line prompt-priority-chain method with five branches is a natural pure function, not a conductor responsibility. Testability improves (Stage 1 unit test covers all five branches independently), dry-run consumes it without dragging in the conductor's per-run dependency graph, and the private-method delegator keeps every existing caller-site unchanged. Aligns with AGENTS.md Code Standard 3 (split complexity into smaller functions / modules) and Core Rule 6 (keep abstractions clean).

### D4. Reject `--dry-run --resume`, don't silently ignore

**Options considered**:

- (a) Silently ignore `--resume` when `--dry-run` is set (log a WARNING).
- (b) Reject with `click.UsageError` at flag validation time.

**Decision: (b).** AGENTS.md Core Rule 1 (surface failures explicitly). Silent ignore invites operator confusion ("I passed --resume and it dry-ran a resumed run? What does that mean?"). Reject with a clear diagnostic. B5 (#286) may relax later if it defines resume-aware dry-run semantics; that's a separate scope.

### D5. Reject `--dry-run-samples` without `--dry-run`

Same rationale as D4. `--dry-run-samples 5` alone is nonsensical — it configures a flag whose effect is gated by another flag. Reject at Click-callback time with `--dry-run-samples requires --dry-run`.

### D6. `--dry-run` renders on stderr through the shared `console`; stdout stays strictly empty

**Options considered**:

- (a) Render panels on stdout (JSON-adjacent shell composability: `tolokaforge run --dry-run | less`).
- (b) Render panels on stderr through the shared `console`; stdout empty.

**Decision: (b).** Composes cleanly with A4's stdout / stderr contract. `emit_artifact_path` is the sole sanctioned stdout write, and dry-run doesn't produce an artifact path — nothing to emit. Under `--display=none` the shared `console` silences (Rich `console.quiet=True` short-circuits) — no branching required. Operators wanting JSON-piped output route through a follow-up `--dry-run-json` flag (filed as an issue in "Discovered issues" if operator demand surfaces).

### D7. `load_tasks_for_dry_run` bypasses `_ensure_typesense_started`

**Options considered**:

- (a) Call `Orchestrator.load_tasks()`; live with the TypeSense-start side effect.
- (b) Introduce a lightweight helper `load_tasks_for_dry_run` that constructs the adapter directly.

**Decision: (b).** Dry-run's implicit contract per the AC ("assert via network mock" that no HTTP fires) is undermined if the command silently starts a Docker container. Path (b) constructs the same adapter (via the same `_create_adapter` code path, which is refactor-lifted to a module-level `create_adapter(run_config)` helper in Stage 1) and loops `adapter.get_task(task_id)` — no TypeSense preflight, no Docker.

Consequence: a run config that declares TypeSense-enabled and uses per-task search config renders panels with unresolved port/api_key ("auto" / null). Documented in Stage 3 as expected — dry-run shows what the CONFIG says, not what live provisioning would resolve. Operators wanting resolved TypeSense values run a real trial or `tolokaforge prepare`.

### D8. No LLMClient construction in dry-run — `build_capabilities` directly

**Options considered**:

- (a) Construct `LLMClient(agent_config)` and read `.capabilities.schema_sanitizer`. LLMClient init is HTTP-free but loads API keys from SecretManager.
- (b) Call `build_capabilities(agent_config.name, agent_config.provider, overrides=agent_config.capabilities)` directly. No client, no key access.

**Decision: (b).** Cleaner — dry-run needs the sanitizer, not the client. No API key load, no litellm module init side effects (litellm's import triggers a couple of setup routines), no coupling to `SecretManager` state. If a future preset materialisation ever needs the client's provider-routing side effects to compute the sanitizer, revisit.

### D9. Frozen `DryRunSample` — Pydantic vs `@dataclass(frozen=True)`

**Options considered**:

- (a) Pydantic `BaseModel` with `extra="forbid"`.
- (b) `@dataclass(frozen=True)`.

**Decision: (b).** Per AGENTS.md type table: Pydantic is for cross-boundary data (serialization). `DryRunSample` is an in-process value object — the CLI creates it and immediately consumes it in the rendering layer within the same call stack. No serialization boundary, no `extra="forbid"` payoff. Frozen dataclass is the right shape.

## Discovered issues

**Fix in this PR**: none. The refactor of `_build_system_prompt` (Stage 1) is a clean extraction, not a bug fix. No adjacent hygiene issues surfaced during discovery.

**Filed as issues**:

1. **#358 — Follow-up: `tolokaforge run --dry-run --json`** — machine-readable structured output of the same samples for tooling (rubric-authoring pipelines, ticket-writing tooling). Deferred to a separate issue; this plan explicitly excludes it.
2. **#359 — Follow-up: dry-run against TypeSense-required configs shows unresolved values** — currently rendered as `port="auto"` / `api_key=null` because `load_tasks_for_dry_run` skips the TypeSense start. A future enhancement could either eagerly resolve TypeSense config without starting the container (spec sniff) or note the unresolved fields with `[warn]…[/warn]` markup.

**Recommended follow-up (not yet filed — main can approve and file before Stage 1)**:

3. **Extract `Orchestrator._create_adapter` to module-level `create_adapter(run_config, project)`** — Stage 1 duplicates the adapter-creation logic inside `load_tasks_for_dry_run`. A dedicated refactor stage in a follow-up PR would lift this cleanly for reuse by dry-run and by any future non-orchestrator adapter consumer. Not scoped here because doing it in this PR expands blast radius beyond dry-run. Attempted filing during planning was blocked by the auto-mode permission classifier; deferred for main-side approval.

## Risks / open questions

- **Risk: schema-sanitizer parity is not byte-for-byte with what the runner sends.** The runner may apply additional transformations we don't see (e.g. per-provider tool-name discipline via litellm's internal routing). Mitigation: Stage 2 test compares dry-run's tool spec against `TrialArtifactWriter.write_tools_schemas`'s output (`sanitized = self.agent_client.capabilities.schema_sanitizer.sanitize(setup.tool_schemas)`) — the same call the real run persists in `tools_schemas.yaml`. If the two ever diverge, that's a genuine bug worth surfacing.
- **Risk: `respx` doesn't intercept all HTTP paths.** litellm may route through a provider SDK that uses raw sockets. Mitigation: Stage 2 test 2 also patches `litellm.completion` with a raise-on-call sentinel — belt-and-braces.
- **Open question: does `Panel` render acceptably at 80 columns with the JSON tool spec block?** Long single-line tool descriptions may overflow. Rich's `word_wrap=True` on `Syntax` handles line wrapping; the SVG golden at 80 cols catches any unreadable overflow. If it does overflow, Stage 3 adjusts the panel body composition (e.g. widen the panel or use `Group([...])` with a scroll-friendly layout).
- **Open question: what does the golden capture when a task has zero declared agent tools?** Fixture path — `render_dry_run_sample` emits the `(no agent tools declared)` line. Second golden at each width would double the coverage — decision deferred to Stage 3 (add a third golden if the zero-tools case is common in real task packs; skip if it's a corner case).
- **Open question: for LLM-generated first user messages, is showing the persona/backstory enough context?** The placeholder line lists `mode` / `persona` / `backstory[:120]`. Operators writing new tasks may want more (e.g. the greeting context `Hi! How can I help you today?` the simulator sees). Decision: ship the minimal placeholder; a follow-up enhancement issue can widen the placeholder if operator feedback demands.
