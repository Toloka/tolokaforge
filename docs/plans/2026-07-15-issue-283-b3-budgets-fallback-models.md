# Plan: B3 — Cost / time / sample budgets and `--fallback-models`

Issue: Toloka/tolokaforge#283 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-283-b3-budgets-fallback-models` (already created; branches off `feat/terminal-dx`; PR targets `feat/terminal-dx`)

## Context

Terminal-DX milestone has landed A1/A2/A3/A4/A5/B1/B2. B3 adds first-class hard caps on `tolokaforge run` plus a resilience flag that lets a batch survive provider outages. The AC calls for four CLI flags (`--cost-limit`, `--time-limit`, `--sample-limit`, `--fallback-models`), a shipped price table (`--model-cost-config` for overlays), a B1 cost-meter turning amber@80%/red@100%, and a graceful shutdown with a `LIMIT_HIT` marker file.

**What already exists** (grep + code read):

- `OrchestratorConfig.max_budget_usd` / `ComputeConfig.max_budget_usd` with `RunConfig.effective_max_budget_usd` (models.py:475, 671, 887). `Orchestrator.run()` already summarises `total_cost_usd`, stops enqueuing when the cap is hit, calls `state_manager.mark_run_paused()`, and emits `"Run paused due to budget cap"` at orchestrator.py:1499-1507. **No marker file today.**
- **No `--cost-limit` / `--time-limit` / `--sample-limit` CLI flag today.** `grep --limit tolokaforge/cli/` finds only unrelated help-formatter matches — the issue's parenthetical "verify overlap with existing `--limit`" is a misspec; there is no overlap to worry about.
- **Shipped pricing table already exists at `tolokaforge/core/data/pricing.json`** (JSON, ~800 models keyed by `<provider>/<name>`, `{input, output, [cache_read, cache_write]}` per 1M tokens). Loaded by `tolokaforge/core/pricing.py::_load_pricing`; refreshed by `tools/pricing-updater` from the OpenRouter API. The issue's `tolokaforge/data/model_prices.yaml` path is **wrong** — B3 reuses the existing JSON as the shipped table (see D5 + #353).
- **No fallback support anywhere.** `LLMClient` (client.py:355) has an inner tenacity `@retry` for network-level errors (5 attempts) but no cross-model fallback; the orchestrator's queue-level `mark_failed(retryable=True)` retries the same model.
- **B1 cost surface** (`LiveRunDisplay._total_cost_usd` at `_run_display.py:176`) accumulates via `trial_progress.cost_delta_usd`. The bottom bar renders `${cost:.2f}` unstyled (`_format_cost` at `_run_display.py:108`) — no budget context today.
- **A5 end banner** (`print_run_end_banner(*, run_id, run_dir, duration_seconds, success, console)` at `_run_banner.py:44`) has no "stopped" variant. `success=True` → `✓ Run complete`, `success=False` → `✗ Run failed`. A budget-cut needs a third state.
- **Cost source at runtime**: `LLMClient.generate` populates `GenerationResult.cost_usd` from litellm's `completion_cost` (usage.py `UsageExtractor`). `_AgentMetricsSink.record_generation` fires `trial_progress(cost_delta_usd=result.cost_usd or 0.0)` (runner.py:463-468). This is **agent-only** today — user-simulator and judge cost is not surfaced through the display event stream (see B1 discovered-issues bullet, #329). B3 enforces on `total_cost_usd` (agent), matching current semantics; user + judge cost enforcement is #350.

**Reproduced current behaviour** (dev MCP `run_python` + code read, no live keys spent):

- `uv run tolokaforge run --help` shows the existing flag surface — none of `--cost-limit` / `--time-limit` / `--sample-limit` / `--fallback-models` / `--model-cost-config` are present.
- `Orchestrator.run()`'s budget check (orchestrator.py:1288-1294, 1483-1492) fires when `total_cost_usd >= budget_limit` and stops calling `submit_one()`; running trials complete naturally in the `wait(...)` loop. No marker file is written. The return path is `output_dir.resolve()` unchanged.
- `pricing.py::reload_pricing(path=None)` accepts a custom path today (used by tests) but does NOT compose: passing a path REPLACES the shipped table wholesale — no overlay/merge semantics.

**Recommendation on scope (D0 below)**: ship as ONE PR. The five surfaces (three limits + fallback + price overlay) share the `Orchestrator.run()` extension point, the `--display=rich` cost meter, and the same end-banner surface. Splitting adds two extra `/dev-loop-tolokaforge` gates with no observable payoff since every stage lands on `feat/terminal-dx` (the milestone's single merge target).

## Goal

`tolokaforge run` gains:

1. **Hard caps** — `--cost-limit <usd>`, `--time-limit <duration>`, `--sample-limit <n>`. On hit: stop enqueuing new trials, let in-flight trials complete, write `LIMIT_HIT.json` under the run's `output_dir`, mark the run paused (same code path the existing `max_budget_usd` uses), return the resolved `output_dir` (A4 contract preserved).
2. **Fallback models** — `--fallback-models m1,m2,m3`. On a hard `generate()` failure from the primary agent model (post-retry), advance a per-trial cursor and retry the current generation on the next model. Chain exhausted → raise the last exception; the trial fails through the orchestrator's normal path.
3. **Price-table overlay** — `--model-cost-config <path>` accepts JSON or YAML overlaying the shipped `tolokaforge/core/data/pricing.json` field-by-field per model id.
4. **B1 cost meter with budget context** — under `--cost-limit`, `LiveRunDisplay`'s bottom-bar `$cost` segment renders in `warn` style at ≥80% of the budget and `error` style at ≥100%. Below 80%, unchanged (default THEME token). No new style tokens.
5. **A5 end banner with stopped variant** — on a budget-triggered graceful shutdown, the outcome line becomes `⏸ Run stopped (<reason>) in <duration>` (using the `warn` theme token); the report + browse lines are unchanged. Failure banner unchanged. Success banner unchanged.

## Non-goals

- **Do NOT rename `compute.max_budget_usd`.** The existing config field is the CLI's `--cost-limit` home; the flag writes to `compute.max_budget_usd` at parse time (mirroring `--workers` → `compute.workers`). Renaming to `cost_limit_usd` is filed as #349 (deprecation-alias path over a release cycle).
- **Do NOT ship `tolokaforge/data/model_prices.yaml`.** The existing `tolokaforge/core/data/pricing.json` is the shipped table (D5). Path clarification with the umbrella wording is filed as #353.
- **Do NOT extend fallback to the user simulator or the judge.** B3's `--fallback-models` applies to the agent client only. User / judge fallback is filed as #350.
- **Do NOT wire budgets into `Orchestrator.run_worker()`** (the distributed-worker path). Filed as #351 — no operator surface (no `LiveRunDisplay` in `worker`) makes the extension dead-on-arrival; when the Cloud Runtime lands, the same protocol threads through.
- **Do NOT change what `_AgentMetricsSink` tracks.** `--cost-limit` enforces on the same `total_cost_usd` the existing `max_budget_usd` did — the AGENT's cost. Enforcing on total (agent + user + judge) is cross-cut with B1's #329 and deferred there.
- **Do NOT preempt B4 (--dry-run, #284).** `--dry-run` skips provider calls entirely; budgets are trivially unreachable in that mode. No coordination needed.
- **Do NOT preempt B5 (--resume, #286).** Cross-resume budget accounting is `per-invocation` in this plan (see D9). Cumulative-across-resumes is #352.
- **Do NOT add a `--budget-mode` flag.** One mode ships (per-invocation, default). Cumulative filed as #352.
- **Do NOT add mid-trial model swap for fallback.** `FallbackLLMClient` (D6) advances the cursor at `generate()`-call granularity; a mid-trial swap after turn N > 0 continues subsequent turns on the fallback model (the trial's message history is preserved, only the wire endpoint changes). "Restart the trial from scratch on fallback" is rejected as too expensive.
- **Do NOT introduce a per-generation `budget_warning` event on `RunDisplayEvents`.** The panel derives its 80% / 100% thresholds locally from its own `_total_cost_usd` counter + `cost_budget_usd` constructor kwarg (D8); no new Protocol method.
- **Do NOT enforce budgets from inside `LLMClient.generate`.** Budgets are orchestrator-level (they cap the whole run, not one call). Enforcement stays in the `wait` loop, matching the existing `max_budget_usd` code path.

## Target module surface

### `tolokaforge/core/duration.py` — new module

```python
"""Duration-string parser for CLI flags accepting spans (e.g. --time-limit).

Accepts compound units — ``30m``, ``2h``, ``1h30m``, ``90s``, ``1d12h``.
Returns seconds as a float. Fractional units accepted (``1.5h`` → 5400.0).
Raises ``ValueError`` with a message naming the offending token on any
unparseable input; the CLI wraps this in ``click.BadParameter``.
"""

from __future__ import annotations

_UNITS: dict[str, float] = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(spec: str) -> float:
    """Return seconds represented by ``spec``.

    Accepts a run of ``<number><unit>`` tokens with units in ``{s, m, h, d}``.
    Bare numbers (no unit) are rejected — the units are load-bearing for
    operator intent. Empty / whitespace / unknown-unit input raises
    ``ValueError`` naming the token.
    """
```

### `tolokaforge/core/budgets.py` — new module

```python
"""Hard caps for ``Orchestrator.run()`` — cost, wall-time, sample count.

Every budget is a stateful tracker with a uniform surface. The orchestrator
holds ONE :class:`Budget` (a composite of the active trackers), calls
:meth:`record_generation_cost` after every ``trial_progress`` event and
:meth:`record_trial_terminated` after every terminal queue transition, and
polls :meth:`poll` before scheduling each new trial. On hit, the returned
:class:`BudgetHit` names which limit fired and its accumulated value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BudgetHit:
    """A budget has been exceeded — record for :class:`LIMIT_HIT.json` and banner.

    ``which`` is one of ``"cost"``, ``"time"``, ``"sample"``. ``threshold``
    and ``value_at_hit`` are floats so the same shape covers all three.
    ``timestamp`` is Unix epoch seconds; the marker-writer converts to ISO 8601.
    """

    which: str  # "cost" | "time" | "sample"
    threshold: float
    value_at_hit: float
    timestamp: float


class BudgetTracker(Protocol):
    """Protocol for a single limit tracker.

    ``poll()`` is idempotent — returns the same :class:`BudgetHit` on
    repeated calls after the first hit (the orchestrator reads it once
    and writes the marker; subsequent polls return the frozen hit).
    """

    def record_generation_cost(self, cost_delta_usd: float) -> None: ...
    def record_trial_terminated(self) -> None: ...
    def poll(self) -> BudgetHit | None: ...


class CostBudget:
    """Fires when cumulative agent cost crosses ``limit_usd``.

    Reads ``cost_delta_usd`` from every ``trial_progress`` event via
    :meth:`record_generation_cost`. Initial cost (from a resumed run's
    prior spend, via :meth:`Orchestrator._collect_existing_cost`) is
    seeded via the constructor's ``initial_cost_usd`` kwarg.
    """

    def __init__(self, *, limit_usd: float, initial_cost_usd: float = 0.0) -> None: ...


class TimeBudget:
    """Fires when ``time.monotonic() - start`` crosses ``limit_seconds``.

    ``start`` is set on the first :meth:`record_trial_terminated` OR
    the first :meth:`record_generation_cost` — whichever comes first.
    Rationale: task-loading time (which may be tens of seconds on large
    projects) does not count against the time budget; the clock starts
    when the run enters its execution phase.
    """

    def __init__(self, *, limit_seconds: float) -> None: ...


class SampleBudget:
    """Fires when :meth:`record_trial_terminated` has been called ``limit``
    times.

    "Terminated" = trial reaches a terminal queue state (``mark_completed``
    OR ``mark_failed`` after retries exhausted). Not "started" — otherwise
    fast-fail trials inflate the counter and confusingly cap a run before
    ``limit`` trials produced any output.
    """

    def __init__(self, *, limit: int) -> None: ...


class CompositeBudget:
    """First-tracker-to-hit wins. Records deltas on every child; polls
    every child on every call to :meth:`poll` and returns the first
    :class:`BudgetHit`."""

    def __init__(self, trackers: list[BudgetTracker]) -> None: ...


def make_budget(
    *,
    cost_limit_usd: float | None,
    time_limit_seconds: float | None,
    sample_limit: int | None,
    initial_cost_usd: float = 0.0,
) -> CompositeBudget | None:
    """Assemble the composite budget from CLI-resolved kwargs.

    Returns ``None`` if every kwarg is ``None`` — the orchestrator branches
    on ``is None`` at exactly one site (the call site) rather than paying
    for a no-op composite at every ``record_*`` call inside the hot loop.
    """


def write_limit_hit_marker(output_dir: Path, hit: BudgetHit) -> Path:
    """Write ``output_dir / LIMIT_HIT.json`` and return the written path.

    Payload — Pydantic model to lock the on-disk shape (AGENTS.md type
    table: "crosses a serialisation boundary" → Pydantic v2):

        {
            "which": "cost",
            "threshold": 5.00,
            "value_at_hit": 5.03,
            "timestamp": "2026-07-15T12:34:56Z"
        }

    Idempotent: overwrites an existing marker with the current hit.
    """
```

### `tolokaforge/core/pricing.py` — extension

```python
def reload_pricing(
    path: Path | None = None,
    *,
    overlay_path: Path | None = None,
) -> None:
    """Reload the pricing table, optionally overlaying a user-supplied file.

    Loading order (each layer merged field-level onto the next):

    1. Shipped ``tolokaforge/core/data/pricing.json``. ``path`` override
       replaces this baseline.
    2. ``overlay_path`` — JSON or YAML matching the shipped schema.
       Detected by suffix (``.json`` → JSON; ``.yaml`` / ``.yml`` → YAML).
       Missing file → ``FileNotFoundError``. Malformed file → ``ValueError``
       naming the parse-failure location.

    Field-level merge: overlay entries override matching keys in the
    shipped table; new model ids are additive; fields on a shipped model
    absent from the overlay survive. Same semantics as
    :func:`pricing_updater.fetcher.write_pricing_json`'s merge branch.
    """
```

### `tolokaforge/core/llm/fallback_client.py` — new module

```python
"""Ordered per-trial model-fallback wrapper implementing the ``LoopLLMClient``
Protocol (:mod:`tolokaforge.core.loop`).

Contract — ordered, not round-robin. Per-trial cursor. On a hard
``generate()`` failure the cursor advances one step in the chain and the
current call retries once; the trial's message history is preserved so
subsequent turns continue on the fallback model. Cursor never rewinds
inside a trial (a fallback that started serving turns cannot bounce
back). Cursor is reset when a new trial constructs a new instance.

The wrapper's own log emissions are structured (via ``get_logger``) and
land under the ``tolokaforge.core.llm.fallback`` namespace — the operator
sees ``Fallback triggered`` lines above the Rich Live region.
"""

from __future__ import annotations

from tolokaforge.core.llm.client import GenerationResult, LLMApiTimeoutError, LLMClient
from tolokaforge.core.models import ModelConfig


class FallbackLLMClient:
    """Wraps an ordered list of :class:`ModelConfig` behind the
    :class:`~tolokaforge.core.loop.LoopLLMClient` Protocol.

    Advances the cursor on:

    - :class:`LLMApiTimeoutError` (upstream timeout after the inner
      per-call retry budget is exhausted).
    - Any other non-``LLMApiTimeoutError`` ``RuntimeError`` /
      ``Exception`` that reaches the caller AFTER the inner tenacity
      ``@retry(stop_after_attempt=5)`` gave up (i.e. what the primary
      would have raised without fallback).

    Rate-limit-style transient errors ARE part of the wrapped
    :class:`LLMClient`'s retry classification and get 5 in-client attempts
    before reaching this wrapper — the fallback only fires on what the
    primary itself declared unrecoverable. This avoids swapping providers
    on every 429.
    """

    def __init__(
        self,
        *,
        primary: ModelConfig,
        fallbacks: list[ModelConfig],
    ) -> None:
        self._chain: list[ModelConfig] = [primary, *fallbacks]
        self._cursor: int = 0
        self._current: LLMClient = LLMClient(self._chain[0])

    def generate(self, *args, **kwargs) -> GenerationResult:
        """Delegate to the current model; advance-and-retry on hard failure.

        Chain-exhaustion: the last exception (from the final model in the
        chain) is re-raised — the orchestrator's `wait` loop classifies
        it identically to a non-fallback primary raise.
        """
```

### `tolokaforge/core/orchestrator.py` — extension

- `OrchestratorDeps` gains one field: `budget: Budget | None = None`. Default `None` = no budget (existing behaviour). When set, threaded onto `self._budget`.
- **CLI wiring**: `cli/main.py::run` builds the composite from `--cost-limit` / `--time-limit` / `--sample-limit` via `make_budget(...)` and passes `deps=OrchestratorDeps(events=..., budget=budget)`.
- `Orchestrator.run()` splices FIVE calls into the existing `wait` loop, immediately after the current `total_cost_usd` accounting site:
  1. `self._budget.record_generation_cost(trial_cost)` right after `total_cost_usd += trial_cost` at line 1383. (The completion sink already summed generation-level `cost_delta_usd`; we push the terminal `trial.cost_usd` — the source of truth for a completed trial. **Do NOT double-count** by also subscribing to `trial_progress` at the events layer; the orchestrator is authoritative.)
  2. `self._budget.record_trial_terminated()` on BOTH the success branch (after `mark_completed`) AND the retry-exhausted-transient branch AND the hard-exception non-retryable branch — anywhere a trial reaches a terminal queue state.
  3. `hit = self._budget.poll()` at the "should we schedule more work" gate — replaces the existing `total_cost_usd >= budget_limit` check (which subsumes `--cost-limit`; the legacy `max_budget_usd` field feeds `CostBudget.limit_usd` when the CLI flag isn't set).
  4. On `hit is not None and not budget_exhausted`: `budget_exhausted = True`, `write_limit_hit_marker(output_dir, hit)`, log the existing `"Budget limit reached"` line with the hit's `which` field appended (`limit_kind=hit.which`).
  5. Set `self._stopped_reason: str | None = f"{hit.which} limit"` when a hit is written; the CLI reads it after `run()` returns.
- The existing `max_budget_usd` code path is subsumed: `make_budget(cost_limit_usd=<from CLI or config>, ...)` returns a composite carrying only a `CostBudget`. If no cost limit is set, the composite is None and the run scales to completion.

### `tolokaforge/cli/main.py::run` — extension

Five new `@click.option` entries plus config-mutations before `RunConfig(**config_data)`:

```python
@click.option("--cost-limit", type=float, default=None,
              help="Hard cap on cumulative agent cost (USD). Stops enqueuing "
                   "new trials on hit; in-flight trials finish. Writes "
                   "LIMIT_HIT.json under the run directory.")
@click.option("--time-limit", type=str, default=None,
              help="Hard cap on wall-clock time. Accepts compound units "
                   "(e.g. '30m', '2h', '1h30m', '90s').")
@click.option("--sample-limit", type=int, default=None,
              help="Hard cap on terminated trials (completed + retry-"
                   "exhausted-failed). Distinct from the task-selection "
                   "cap; new trials stop enqueuing on hit.")
@click.option("--fallback-models", "fallback_models", type=str, default=None,
              help="Comma-separated ordered chain of fallback agent models "
                   "(e.g. 'anthropic/claude-sonnet-4.6,openai/gpt-4o'). On "
                   "a hard failure of the primary agent model, subsequent "
                   "turns for that trial use the next model in the chain.")
@click.option("--model-cost-config", "model_cost_config",
              type=click.Path(exists=True, dir_okay=False), default=None,
              help="JSON/YAML overlay merged onto the shipped pricing "
                   "table. Same schema as tolokaforge/core/data/pricing.json.")
```

Flow inside the `run` callback:

1. Parse `--time-limit` via `parse_duration` (wraps `ValueError` → `click.BadParameter`).
2. `--cost-limit` writes to `config_data.setdefault("compute", {})["max_budget_usd"]` (reuses the existing home; see D2). CLI value beats config, matching the `--workers` pattern.
3. `--model-cost-config` calls `pricing.reload_pricing(overlay_path=Path(...))` before `Orchestrator` construction.
4. `--fallback-models` parses comma-separated model ids into a `list[ModelConfig]` via the same helper the existing `--user-model` / `--judge-model` flags use (provider inferred from `provider_from_name` or the primary's provider).
5. `make_budget(cost_limit_usd=run_config.effective_max_budget_usd, time_limit_seconds=<parsed>, sample_limit=<parsed>, initial_cost_usd=<seed>)` → passed via `OrchestratorDeps(budget=budget)`.
6. After `orchestrator.run()` returns, read `output_dir / "LIMIT_HIT.json"` (if present) → parse `which` → thread as `stopped_reason=f"{which} limit"` into `print_run_end_banner(...)`.

### `tolokaforge/cli/_run_banner.py::print_run_end_banner` — extension

Add one optional kwarg `stopped_reason: str | None = None`:

- `stopped_reason is None and success=True` → existing `✓ Run complete in <dur>`.
- `stopped_reason is None and success=False` → existing `✗ Run failed in <dur>`.
- `stopped_reason is not None` (regardless of `success`) → new line: `[warn]⏸[/warn] Run stopped (<stopped_reason>) in <duration>`.
- Report + browse lines unchanged in every case.

Rationale for `⏸`: distinct glyph from `✓` / `✗`; conveys "the run reached a paused / not-really-failed state". Uses the existing `warn` theme token (yellow) — no new tokens.

### `tolokaforge/cli/_run_display.py::LiveRunDisplay` — extension

`__init__` gains `cost_budget_usd: float | None = None`; stored on `self._cost_budget_usd`. `for_mode(mode, *, cost_budget_usd=None)` threads it into the constructor under `RICH`/`FULL`.

`_render_bottom_bar` splits the cost-styling decision into a pure helper:

```python
def _cost_bar_style(cost_usd: float, budget_usd: float | None) -> str:
    """Return the theme-token name applied to the cost segment."""
    if budget_usd is None or budget_usd <= 0.0:
        return "default"  # unchanged
    ratio = cost_usd / budget_usd
    if ratio >= 1.0:
        return "error"
    if ratio >= 0.8:
        return "warn"
    return "default"
```

`_format_bottom_bar` accepts the style token via an added `_BottomBarStats.cost_style` field (populated by `_render_bottom_bar` under the lock). The rendered line wraps only the `$X.YY` segment in `[warn]…[/warn]` / `[error]…[/error]` — the rest of the bar is unchanged. Below 80%, the segment is unwrapped (existing shape).

**Golden regeneration**: the existing 80-col and 120-col goldens (`tests/canonical/golden/run_display/panel_{80,120}.svg`) are BUDGET-UNSET runs. B3's Stage 3 adds two NEW goldens under `panel_{80,120}_budget_amber.svg` and `panel_{80,120}_budget_red.svg`; the original goldens are asserted UNCHANGED (regression test for the "no budget" path).

## Stages

Every stage is one commit; every stage's tests pass without the next stage shipping.

### Stage 1: Foundations — duration parser + pricing overlay + budget/duration/pricing unit tests

- **Contract:**
  - New module `tolokaforge/core/duration.py` exports `parse_duration(spec: str) -> float`. Accepted: `<num><unit>` runs with units `{s, m, h, d}`; compound (`1h30m`); fractional (`1.5h`). Rejected: empty, whitespace-only, bare number, unknown unit, negative — each raises `ValueError` naming the offending token.
  - `tolokaforge/core/pricing.py::reload_pricing` gains `overlay_path: Path | None = None` kwarg. Field-level merge onto the shipped baseline; extension-based JSON/YAML detection; malformed overlay → `ValueError` naming the parse-failure line; missing overlay → `FileNotFoundError`.
  - **No orchestrator wiring yet.** Deliverable is a pair of pure modules with tests.
- **Behaviour to lock (tier: `unit`):**
  - `tests/unit/test_duration_parser.py` — table-driven: `"30m"` → `1800.0`; `"2h"` → `7200.0`; `"1h30m"` → `5400.0`; `"90s"` → `90.0`; `"1.5h"` → `5400.0`; `"1d12h"` → `129600.0`; `""` → `ValueError("empty spec")`; `"30"` → `ValueError("30" bare)`; `"30x"` → `ValueError("unknown unit 'x'")`; `"-30m"` → `ValueError("negative")`.
  - `tests/unit/test_pricing_overlay.py` —
    - Shipped baseline loads unchanged when `overlay_path=None` (regression on existing behaviour).
    - YAML overlay `{"models": {"openai/gpt-4o": {"input": 0.05, "output": 0.15}}}` overrides the shipped rates for `openai/gpt-4o`; every other model unchanged.
    - JSON overlay same schema — parity with YAML.
    - Overlay adds a NEW model id `synthetic/test-model` → present in `MODEL_PRICING` post-overlay; `estimate_cost("synthetic/test-model", …)` returns a numeric cost.
    - Overlay drops a field (`{"openai/gpt-4o": {"input": 0.05}}` — no `output`) — shipped `output` survives (field-level merge).
    - Missing overlay file → `FileNotFoundError` with the file path in the message.
    - Malformed YAML (unclosed quote) → `ValueError` naming the parse-failure line.
    - Unknown suffix (`.txt`) → `ValueError` naming the suffix.
- **Compatibility:** internal only. `parse_duration` and `reload_pricing(overlay_path=...)` are new public exports; existing `reload_pricing(path=None)` calls are unchanged.
- **Deliverable:**
  - `tolokaforge/core/duration.py` — ~50 LOC.
  - `tolokaforge/core/pricing.py` — ~30 LOC delta (overlay branch).
  - `tests/unit/test_duration_parser.py` — new file, ~80 LOC.
  - `tests/unit/test_pricing_overlay.py` — new file, ~120 LOC.
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_duration_parser or test_pricing_overlay")` green.
  - `dev.run_tests(marker="unit")` green — no regression on existing pricing tests.
  - `dev.lint_check(paths=["tolokaforge/core", "tests/unit"])` clean.
- **Doc updates:** none this stage.

### Stage 2: `BudgetTracker` + graceful shutdown protocol + LIMIT_HIT marker + orchestrator wiring

- **Contract:**
  - New module `tolokaforge/core/budgets.py` — `BudgetTracker` Protocol, `CostBudget` / `TimeBudget` / `SampleBudget` concrete classes, `CompositeBudget` composer, `make_budget` factory, `write_limit_hit_marker`, `BudgetHit` dataclass, `LimitHitMarker` Pydantic model (on-disk shape).
  - `LimitHitMarker` Pydantic model — `extra="forbid"`, four fields (`which`, `threshold`, `value_at_hit`, `timestamp`). Written under `output_dir/LIMIT_HIT.json`.
  - `OrchestratorDeps` gains `budget: Budget | None = None` (typed as `CompositeBudget | None` in practice; the Protocol name in the field annotation reads better).
  - `Orchestrator.run()`:
    - Reads `self._budget` from resolved deps.
    - Records generation cost on the success + retry-exhausted branches (using terminal `trajectory.metrics.cost_usd`, not the intermediate `trial_progress` deltas — the orchestrator is authoritative on completed-trial cost).
    - Records trial-terminated on every terminal branch (success + retry-exhausted-transient + hard-exception-non-retryable).
    - Polls the budget after each terminal branch. First hit writes the marker and sets `self._stopped_reason = f"{hit.which} limit"`.
    - The pre-existing `budget_limit`/`total_cost_usd` cost-cap logic is DELETED; the new `CostBudget` subsumes it. The `state_manager.mark_run_paused()` call and the "Run paused" log line remain (fire on any budget hit, not just cost).
  - `Orchestrator.run_worker()` — **untouched this PR**. Follow-up #351.
- **Behaviour to lock (tier: `unit`):**
  - `tests/unit/test_budgets.py`:
    - **`CostBudget`**: seed `initial_cost_usd=0.5`, `limit_usd=1.0`. `record_generation_cost(0.4)` → `poll()` returns `None` (0.9 < 1.0). `record_generation_cost(0.2)` → `poll()` returns `BudgetHit(which="cost", threshold=1.0, value_at_hit=~1.1)`. Second `poll()` returns the SAME hit (idempotent).
    - **`TimeBudget`**: monkey-patch `time.monotonic`. Construct with `limit_seconds=60`. Poll before any `record_*` → `None` (clock hasn't started). `record_generation_cost(0.001)` (fixture starts the clock). Advance monotonic by 30s → `poll()` returns `None`. Advance to 61s → `poll()` returns `BudgetHit(which="time", threshold=60, value_at_hit=~61)`.
    - **`SampleBudget`**: `limit=3`. Three `record_trial_terminated()` calls → `poll()` returns `BudgetHit(which="sample", threshold=3, value_at_hit=3)`. Fourth call → still returns the same first hit.
    - **`CompositeBudget`**: two trackers (cost + time). Advance monotonic past the time limit → `poll()` returns the time hit even though cost is below cap. Reverse order: cross cost first → `poll()` returns the cost hit.
    - **`make_budget`**: all-`None` args → returns `None`; single non-`None` arg → returns a `CompositeBudget` with one child; multiple non-`None` args → composite with matching child count.
  - `tests/unit/test_limit_hit_marker.py`:
    - `write_limit_hit_marker(tmp_path, BudgetHit(which="cost", threshold=1.0, value_at_hit=1.05, timestamp=1234567890.0))` → writes `tmp_path/LIMIT_HIT.json`; parsed content matches the Pydantic model; `timestamp` renders as ISO 8601 UTC.
    - Overwrite semantics: writing twice with different hits → file contains the second write.
    - Rejects unknown `which` values (Pydantic `extra="forbid"` + literal type on `which`).
  - `tests/unit/test_orchestrator_budget_shutdown.py`:
    - Extended `_RecordingEvents` from B1's `test_run_display_wiring.py` — reused via import.
    - `_make_stub_orchestrator` (from `test_cli_stdout_contract.py`) is extended with 4 synthetic trials taking artificially-set `cost_usd`.
    - Case A (cost hit): construct `Orchestrator(deps=OrchestratorDeps(budget=CompositeBudget([CostBudget(limit_usd=0.03)])))` with 4 trials at `cost_usd=0.02` each. Assert `_stopped_reason == "cost limit"`, only 2 trials complete before shutdown (0.04 crosses 0.03), `LIMIT_HIT.json` exists under `output_dir` with `which="cost"`.
    - Case B (sample hit): `SampleBudget(limit=2)` → `_stopped_reason == "sample limit"`, 2 trials complete, marker `which="sample"`.
    - Case C (time hit): monkey-patch `time.monotonic`. `TimeBudget(limit_seconds=0.01)` + `time.monotonic` returning `[0.0, 0.005, 0.02, ...]` → hits on the second poll. Marker `which="time"`.
    - Case D (no budget): `Orchestrator(deps=OrchestratorDeps(budget=None))` → all 4 trials run, `LIMIT_HIT.json` does NOT exist, `_stopped_reason is None`.
    - Case E (in-flight completes gracefully): with 12 workers and `SampleBudget(limit=3)`, all 12 in-flight trials at hit-time still complete. Assert `run_state.completed_trials + failed_trials >= 12` post-shutdown (fills the wait loop).
    - Case F (legacy `max_budget_usd` continues to work): construct Orchestrator with `config.compute.max_budget_usd=0.03` and `deps=OrchestratorDeps()` (no budget arg). Assert cost-cap fires at 0.03 via the composite built from the legacy field — same behaviour as before B3, just implemented via the new path. Locks that the deletion of the pre-B3 cost-cap code path preserved the observable shape.
- **Compatibility:**
  - **`OrchestratorDeps.budget` is a new field with `None` default** — additive.
  - **Deletion of the pre-B3 cost-cap code path is INTERNAL.** The observable shape (`state_manager.mark_run_paused()` fires on hit, `"Run paused"` log line, `output_dir` return preserved) is locked by Case F.
  - **`LIMIT_HIT.json` is a NEW on-disk contract.** Documented in Stage 4 (docs/OUTPUT_FORMAT.md § `LIMIT_HIT.json`).
- **Deliverable:**
  - `tolokaforge/core/budgets.py` — new file, ~250 LOC.
  - `tolokaforge/core/orchestrator.py` — ~40 LOC delta (deletes the pre-B3 cost-cap branch, splices `budget.record_*` + `poll` + marker-write into the wait loop, adds `_stopped_reason`).
  - `tests/unit/test_budgets.py` — new, ~250 LOC.
  - `tests/unit/test_limit_hit_marker.py` — new, ~80 LOC.
  - `tests/unit/test_orchestrator_budget_shutdown.py` — new, ~300 LOC.
- **Validation:**
  - `dev.run_tests(marker="unit", pattern="test_budgets or test_limit_hit_marker or test_orchestrator_budget_shutdown")` green.
  - `dev.run_tests(marker="unit")` green — no regression on existing orchestrator tests (`test_orchestrator_logic`, etc.).
  - `dev.run_tests(marker="canonical")` green — grep-guards + snapshot tests unchanged.
  - `dev.lint_check` clean.
- **Doc updates:** none yet (Stage 4).

### Stage 3: CLI flags + B1 cost-meter styling + fallback-model routing + banner "stopped" variant

- **Contract:**
  - **CLI**:
    - `--cost-limit <usd>` (float): mutates `config_data["compute"]["max_budget_usd"]` before `RunConfig(**config_data)`. CLI value overrides config (mirrors `--workers`).
    - `--time-limit <duration>` (str): parsed by `parse_duration`. Threaded into `make_budget(time_limit_seconds=…)`.
    - `--sample-limit <n>` (int): threaded into `make_budget(sample_limit=…)`.
    - `--fallback-models <m1,m2,...>` (str): comma-split, each token parsed into a `ModelConfig` via a new helper `_parse_fallback_model(spec: str, *, default_provider: str) -> ModelConfig`. Provider inference from `<provider>/<name>` prefix, else the PRIMARY agent's provider.
    - `--model-cost-config <path>` (`click.Path(exists=True)`): calls `pricing.reload_pricing(overlay_path=...)` before `Orchestrator` construction.
    - After `orchestrator.run()` returns, the CLI reads `output_dir / "LIMIT_HIT.json"` (if present) → threads `stopped_reason=f"{marker.which} limit"` into `print_run_end_banner`.
  - **`FallbackLLMClient`**: implements `LoopLLMClient` Protocol. Constructor takes primary + fallbacks. `generate(...)` delegates to the cursor-current `LLMClient`; on `LLMApiTimeoutError` OR any other exception the inner tenacity retry surfaced, advance the cursor, log `"Fallback triggered"` (structured), rebuild the internal `LLMClient(chain[cursor])`, retry the call ONCE. Chain-exhausted → re-raise the final exception.
  - **B1 cost-meter styling**: `LiveRunDisplay(*, cost_budget_usd: float | None = None)`. `for_mode(mode, *, cost_budget_usd=None)` threads it through. `_render_bottom_bar` computes `_cost_bar_style` and populates `_BottomBarStats.cost_style`; `_format_bottom_bar` wraps the `$X.YY` segment in the corresponding markup.
  - **`print_run_end_banner`**: gains `stopped_reason: str | None = None` (kwarg-only). New outcome line `[warn]⏸[/warn] Run stopped (<reason>) in <duration>` when set; report + browse lines unchanged.
  - `cli/main.py::run` orchestration:
    1. Build the primary agent `ModelConfig` from resolved run config (before `Orchestrator` construction).
    2. If `--fallback-models`, build the `FallbackLLMClient` and inject via a NEW `OrchestratorDeps.agent_client_factory: Callable[[ModelConfig], LoopLLMClient] | None = None` field (default `LLMClient(config)`). Orchestrator's `LLMClient(agent_config)` calls (line 1100, 1596) route through the factory.
    3. Build `budget` via `make_budget`, pass into `deps`.
    4. Compute `cost_budget_usd = run_config.effective_max_budget_usd` and thread into `LiveRunDisplay.for_mode(mode, cost_budget_usd=cost_budget_usd)`.
    5. Post-run, read `LIMIT_HIT.json`; thread `stopped_reason` into `print_run_end_banner`.
- **Behaviour to lock (tier: `unit`):**
  - `tests/unit/test_cli_budget_flags.py`:
    - `--cost-limit 0.03` → `run_config.effective_max_budget_usd == 0.03`; orchestrator's budget is a CostBudget with matching threshold.
    - `--time-limit 30m` → budget contains a TimeBudget with 1800s.
    - `--sample-limit 5` → budget contains a SampleBudget with limit=5.
    - Composed: `--cost-limit 0.5 --time-limit 1h --sample-limit 10` → composite of 3 trackers.
    - Invalid `--time-limit foo` → click.BadParameter with a message naming the bad token.
    - `--cost-limit` beats `compute.max_budget_usd` in the config (order-of-precedence test).
  - `tests/unit/test_cli_fallback_flag.py`:
    - `--fallback-models a/b,c/d` → CLI parses into two `ModelConfig(provider="a", name="b")` and `ModelConfig(provider="c", name="d")`.
    - Bare name (no `/`) → provider inferred from primary agent's provider.
    - Empty string / whitespace → click.BadParameter.
    - `OrchestratorDeps.agent_client_factory` is set when `--fallback-models` is passed; unset otherwise.
  - `tests/unit/test_cli_model_cost_config.py`:
    - `--model-cost-config <yaml_path>` calls `pricing.reload_pricing` with the overlay path; a subsequent `estimate_cost` for an override-model returns the overlaid rate.
    - Non-existent path → click's built-in `does not exist` error surfaces (click.Path(exists=True)).
    - After the command exits, the overlay stays applied (module-global `MODEL_PRICING` mutation — matches existing `reload_pricing` semantics). Documented in Stage 4.
  - `tests/unit/test_fallback_client.py`:
    - `FallbackLLMClient(primary=A, fallbacks=[B, C])`. Monkey-patch `LLMClient.generate` to raise `LLMApiTimeoutError` on models A and B, succeed on C. Call `generate(...)` → returns C's result; assert cursor advanced twice; two `"Fallback triggered"` structured-log lines fired.
    - All three raise → the LAST raise (from C) propagates.
    - Success on primary → cursor stays at 0; single call, no fallback logic invoked.
    - Non-timeout `RuntimeError` also triggers fallback (locks D6 semantics).
    - Rate-limit-style errors NEVER reach the wrapper (the inner tenacity `@retry` in `LLMClient` handles them for 5 attempts) — locked by monkey-patching the primary's `_call_completion_with_timeout_retry` to raise `RateLimitError`-shaped exception 4 times then succeed; assert `generate()` returns the primary's result (no fallback), matching the design intent.
    - Fallback advances at `generate()` granularity — a mid-trial swap after turn N > 0 continues on the fallback model; locked by simulating turn-3 primary raise + assert cursor=1 for subsequent calls on the same instance.
    - Per-trial cursor reset: each `FallbackLLMClient` instance advances independently; the orchestrator constructs ONE per trial (locked by inspecting `_build_trial_spec`).
  - `tests/unit/test_run_display_cost_meter.py`:
    - Given `LiveRunDisplay(cost_budget_usd=1.0)`. `run_started(total_trials=1, initial_completed=0)`. `trial_progress(cost_delta_usd=0.5)` → `_render_bottom_bar()`'s output contains `$0.50` UNWRAPPED (under 80%). `trial_progress(cost_delta_usd=0.35)` → `$0.85` wrapped in `[warn]…[/warn]`. `trial_progress(cost_delta_usd=0.2)` → `$1.05` wrapped in `[error]…[/error]`.
    - Given `LiveRunDisplay(cost_budget_usd=None)` → cost is NEVER wrapped, regardless of the cumulative value.
    - Boundary: exact 80% → `warn`; exact 100% → `error`.
    - `_cost_bar_style(0.79, 1.0)` → `"default"`; `_cost_bar_style(0.8, 1.0)` → `"warn"`; `_cost_bar_style(1.0, 1.0)` → `"error"`; `_cost_bar_style(0.5, None)` → `"default"`; `_cost_bar_style(0.5, 0.0)` → `"default"` (defensive against zero-budget div).
  - `tests/unit/test_run_banner_stopped.py`:
    - `print_run_end_banner(..., stopped_reason="cost limit")` → captured stderr contains `"⏸ Run stopped (cost limit) in"`; report + browse lines unchanged.
    - `stopped_reason=None` on success → existing `✓ Run complete` line (regression).
    - `stopped_reason=None` on failure → existing `✗ Run failed` line (regression).
    - `stopped_reason="time limit"` on success (budget cut a running-fine run) → still `⏸ Run stopped (time limit)` — the "stopped" line supersedes the success/failure axis. Note: success is False when the budget was hit *by design* (the CLI records the banner call with `success=False` when a LIMIT_HIT marker exists), but the outcome line is stopped-shaped regardless.
  - `tests/unit/test_cli_budget_integration.py`:
    - CliRunner test: stub Orchestrator writes `LIMIT_HIT.json` inside its stubbed `.run()` body. The CLI reads it and passes `stopped_reason="cost limit"` to `print_run_end_banner`. `result.stderr` contains `"⏸ Run stopped (cost limit)"`.
    - Under `--display=none`: the banner is silenced (existing B2 behaviour) even on a budget-cut run — regression on B2's silencer.
- **Compatibility:**
  - **CLI flag surface expanded (five new flags)** — compatibility surface per AGENTS.md. Documented in Stage 4 (`docs/CLI.md`).
  - **`LiveRunDisplay(*, cost_budget_usd=None)`** — additive kwarg; existing callers unaffected.
  - **`print_run_end_banner(*, stopped_reason=None)`** — additive kwarg; existing callers unaffected.
  - **`OrchestratorDeps.agent_client_factory`** — additive field, default `None` (orchestrator falls back to direct `LLMClient(...)` construction).
  - **`RunDisplayEvents` Protocol** — unchanged. The cost meter derives its budget context locally, not from an event.
- **Deliverable:**
  - `tolokaforge/core/llm/fallback_client.py` — new file, ~120 LOC.
  - `tolokaforge/cli/main.py` — ~80 LOC delta (five new options + `make_budget` + `agent_client_factory` wiring + banner-`stopped_reason` reader).
  - `tolokaforge/cli/_run_display.py` — ~30 LOC delta (`cost_budget_usd` kwarg, `_cost_bar_style`, `_BottomBarStats.cost_style`).
  - `tolokaforge/cli/_run_banner.py` — ~15 LOC delta (`stopped_reason` kwarg + new outcome line).
  - `tolokaforge/core/orchestrator.py` — ~10 LOC delta (`agent_client_factory` in `OrchestratorDeps`, replace `LLMClient(agent_config)` with `factory(agent_config) if factory else LLMClient(agent_config)`).
  - Six test files as listed above, ~1200 LOC combined.
- **Validation:**
  - `dev.run_tests(marker="unit")` green.
  - `dev.run_tests(marker="canonical")` green — the existing `panel_{80,120}.svg` goldens are UNCHANGED (unset-budget baseline preserved) plus TWO new goldens land here (`panel_80_budget_amber.svg` + `panel_80_budget_red.svg`).
  - `dev.lint_check` clean.
  - **Manual smoke** (quote in PR body):
    - `TOLOKAFORGE_DISPLAY=rich uv run tolokaforge run --config examples/native/custom_grading/run_config.yaml --cost-limit 0.01 --time-limit 30s` — the bottom bar's `$0.00` renders default; on hit the marker file lands, the end banner shows the stopped variant, exit code 0. No LLM keys needed for `custom_grading`.
    - `TOLOKAFORGE_DISPLAY=rich uv run tolokaforge run --config examples/native/tool_use/run_config.yaml --fallback-models openai/gpt-4o-mini` — real run against a real key. Cost ~$0.01. Confirm that a first-turn timeout on the primary swaps to gpt-4o-mini (may or may not surface in a healthy provider; the log line is what to verify).
- **Doc updates:** none this stage (Stage 4).

### Stage 4: Docs + CHANGELOG

- **Contract:**
  - `docs/CLI.md`:
    - Rewrite `§ tolokaforge run` (or add) to document the five new flags with concrete examples. Include: precedence (`--cost-limit` beats config); duration syntax (`30m` / `2h` / `1h30m` / `90s` / `1d`); sample-limit semantics ("terminated trials — completed OR retry-exhausted").
    - Extend `§ Run banner` with the "stopped" outcome shape and the `⏸` glyph.
    - Extend `§ Live run panel` with the amber@80%/red@100% cost-meter description.
    - Add `§ Fallback models` — ordered per-trial cursor semantics, cursor granularity, log-line shape.
    - Add `§ Custom pricing overlay` — `--model-cost-config` schema, JSON + YAML formats, precedence, mutation semantics.
  - `docs/OUTPUT_FORMAT.md`:
    - New section `§ LIMIT_HIT.json` documenting the on-disk shape (schema, examples, when the file is present, resume-time semantics).
  - `docs/CONFIG.md`:
    - Note in the `compute.max_budget_usd` row that `--cost-limit` CLI flag overrides.
  - `CHANGELOG.md`:
    - Under "Unreleased / Feat" — five bullets (one per surface), each cross-referencing #283 and the follow-up issues.
- **Behaviour to lock (tier: none — docs):**
  - No new tests. Grep-guards from Stages 1-3 stay green.
- **Compatibility:**
  - **CLI surface docs** — every new flag documented; `docs/CLI.md` reads as if the flags always existed (no "previously X, now Y").
  - **`LIMIT_HIT.json` docs** — new on-disk contract documented in `docs/OUTPUT_FORMAT.md`. Consumers of the run directory can now depend on its presence-when-hit.
- **Deliverable:**
  - `docs/CLI.md` — ~80 LOC delta.
  - `docs/OUTPUT_FORMAT.md` — ~30 LOC delta.
  - `docs/CONFIG.md` — one-line note.
  - `CHANGELOG.md` — five bullets.
- **Validation:**
  - `dev.run_tests(marker="canonical")` green — grep for `--cost-limit|--time-limit|--sample-limit|--fallback-models|--model-cost-config|LIMIT_HIT` in `docs/` returns hits only in the new sections.
  - `rg "TODO|FIXME|previously" docs/CLI.md docs/OUTPUT_FORMAT.md docs/CONFIG.md` returns nothing new.
- **Doc updates:** the stage IS docs.

## Design decisions

### D0. Scope — one PR, four stages

**Options considered**:

- (a) One PR covering all four stages.
- (b) Split after Stage 2: PR-A ships foundations + budgets + graceful shutdown; PR-B ships CLI flags + fallback + cost meter + docs.

**Decision: (a) one PR.** Rationale:

- Every stage is independently reviewable and lands as one commit. Bisecting mid-PR is well-supported.
- The B1 PR (#340) landed a similarly-sized surface (event Protocol + panel + wiring + tests + docs). Precedent.
- The overall milestone merges `feat/terminal-dx` → `main` as ONE PR at the end. Sub-milestone PRs are coordination convenience, not release gates.
- Splitting incurs two `/dev-loop-tolokaforge` runs (real internal ots costs money) + two rounds of PR review coordination. Payoff: none, since both halves ship into the same branch.

If plan-critic pushes back: split after Stage 2. PR-B is Stages 3+4 with a rebase; the delta on top is well-defined.

### D1. `--sample-limit` semantics — count TERMINATED trials, not STARTED

**Options considered**:

- (a) Count trials that have STARTED (leased from the queue).
- (b) Count trials that have TERMINATED (reached `mark_completed` OR retry-exhausted `mark_failed`).
- (c) Count only successful completions.

**Decision: (b).** Rationale:

- Fast-fail trials (spec-build errors, non-retryable exceptions) inflate a "started" count disproportionately; an operator expecting "give me 5 samples" gets 5 lease attempts, some of which never produce output. Confusing.
- Counting only successes (c) makes a run with a high failure rate never hit the cap — surprising when the operator was capping wall-cost via sample count.
- Terminated (b) matches the operator's intuition: "the sample budget is the number of trials that finish (well or not)".
- Locked in Stage 2's Case E (in-flight completes on hit).

### D2. `--cost-limit` flag home — reuse `compute.max_budget_usd`

**Options considered**:

- (a) Add a canonical `compute.cost_limit_usd` and deprecate `max_budget_usd` in this PR.
- (b) Reuse `compute.max_budget_usd`. `--cost-limit` writes to that field. Rename to a canonical name is filed as #349.

**Decision: (b).** Renaming `max_budget_usd` is a breaking change to every run-config on disk; it deserves its own PR with a deprecation warning + at-least-one-release grace period. B3's scope is the new features + flags, not a rename. Precedent: A5's `_format_eta` extraction (`main.py::_format_eta` vs `_run_display._format_eta` divergence) was filed to a follow-up, not folded in.

### D3. `--fallback-models` — ordered per-trial cursor, not round-robin

**Options considered**:

- (a) Round-robin: distribute trials across `[primary, m1, m2]` such that trial 0 uses primary, trial 1 uses m1, trial 2 uses m2, trial 3 uses primary. Even distribution.
- (b) Ordered fallback: every trial STARTS on primary; on primary failure the cursor advances to m1 for THAT trial only. Trial cursors are independent.
- (c) Ordered fallback with global cursor: after ONE trial's primary fails, EVERY subsequent trial starts on m1 (until m1 fails, then m2, etc.).

**Decision: (b).** Rationale:

- The issue's phrase "round-robin" was written for the AC prose; the spec intent — "let a batch survive provider outages" — is ordered fallback. If Anthropic goes down for one trial, we don't want to distribute across a mix of Anthropic + OpenAI trials; we want that trial to succeed on OpenAI, and the NEXT trial to try Anthropic again (in case the outage cleared).
- Global cursor (c) has cascading-failure semantics that operators don't expect ("one blip took my whole run off the primary").
- Round-robin (a) is a load-balancer, orthogonal to fallback. If load-balancing is what operators want, they configure `models.agent` as an OpenRouter routing spec (existing `openrouter.provider_order` field, models.py:380).
- Locked in Stage 3's `test_fallback_client.py` per-trial-cursor test.

### D4. Fallback cursor advances at `generate()` granularity, not per-trial

**Options considered**:

- (a) Restart the trial from scratch on the fallback model — clean single-model attribution, expensive on long-running trials (30+ minutes to re-execute).
- (b) Advance mid-trial on the failed `generate()` call — subsequent turns continue on the fallback model; the trial's `messages[]` and metrics carry mixed-model provenance.
- (c) Trial-scoped cursor: cursor advances at the granularity of an ENTIRE trial (all `generate()` calls in one trial use the same model); on a hard failure, ABORT the trial mid-way and re-run from scratch on the fallback.

**Decision: (b).** Rationale:

- (a) is prohibitively expensive.
- (c) has the same restart cost as (a) but with the extra complexity of tracking "we tried the primary and it failed halfway" state.
- (b)'s mixed-model attribution is a diagnostic wart, not a correctness bug. Per-call `Usage.calls[i].model` in the trajectory already tracks which model produced each call (see B1 discovered issues #329). Log the fallback event loud (`"Fallback triggered"` structured line) so post-run analytics can attribute correctly.
- Cache pollution concern: prompt-cache markers survive across models (`cache_control` is a wire-level annotation, models silently drop unrecognised keys) — Anthropic's prompt cache invalidates on-cursor-advance, which is desired behaviour (the fallback model shouldn't be charged for the primary's cache warmup).

### D5. Shipped pricing table stays at `tolokaforge/core/data/pricing.json`

**Options considered**:

- (a) Ship a new YAML at `tolokaforge/data/model_prices.yaml` (the issue's literal path). Duplicates the existing JSON; both must stay in sync; `pricing-updater` needs a second output target.
- (b) Migrate the shipped JSON to YAML at the correct existing path. Breaking change on `pricing.json`'s consumers (in-tree only; `_load_pricing` needs to become YAML-aware).
- (c) Keep the JSON as the shipped table; add `--model-cost-config <path>` accepting JSON or YAML (auto-detected by suffix). File the umbrella wording as #353.

**Decision: (c).** No duplicate data source; the OpenRouter refresh flow (`pricing-updater`) continues to write JSON; overlays accept either format because operators authoring a cost config prefer YAML for hand-editing. The umbrella #283 wording is a spec typo, not a design requirement.

### D6. `FallbackLLMClient` trigger — non-timeout exceptions too

**Options considered**:

- (a) Fallback on `LLMApiTimeoutError` only. Rate-limit / auth / API-key errors do NOT swap models.
- (b) Fallback on any exception surfacing out of `LLMClient.generate` after the inner tenacity retry.
- (c) Fallback on `LLMApiTimeoutError` + configurable "hard failure" predicate.

**Decision: (b).** Rationale:

- The inner `LLMClient` retry handles 429 / rate-limit / transient network errors for 5 attempts. If those STILL fail, they're not transient any more — swapping models is legit.
- (a) leaves a class of "provider down" failures (5xx with no retry-after, DNS failure, TLS handshake failure) uncovered — the fallback is useless when it's needed most.
- (c) is over-engineered for MVP; a configurable predicate is a stability escape hatch not needed today. If operators surface a false-fallback pattern (e.g. auth errors triggering swaps to a differently-authed provider), file a follow-up.
- Rate-limit errors specifically: reach the wrapper ONLY when the inner tenacity retry gave up. That's a legitimate signal — the primary provider is over-throttled beyond what retry can smooth; swapping is right.

### D7. Cost meter styling — theme tokens `warn` / `error`, no new tokens

**Options considered**:

- (a) Reuse existing `warn` / `error` theme tokens (yellow / bold red). Same semantic language as B2's fallback WARNING line and any `error` log record.
- (b) Add new `budget_warning` / `budget_critical` tokens for finer control.

**Decision: (a).** Two levels (amber@80%, red@100%) map cleanly to two existing tokens. Adding new tokens for one caller is scope creep; if a third level (say green@under-40%) ever surfaces we can revisit. Consistency payoff: an operator seeing yellow anywhere on the panel already knows "attention needed"; a new token wastes the mental-language investment.

### D8. Cost meter reads budget from constructor kwarg, NOT from an event

**Options considered**:

- (a) Add a `RunDisplayEvents.budget_context(cost_budget_usd, time_budget_seconds, sample_budget)` method fired once at run start. The panel reads it.
- (b) `LiveRunDisplay(*, cost_budget_usd=None)` — the CLI resolves the budget at flag-parse time and passes it to the constructor.

**Decision: (b).** Rationale:

- The budget is a run-scoped constant known at construction time; sending it as an event adds a lifecycle sequencing constraint ("budget_context must fire before the first trial_progress").
- The CLI is already resolving the composite budget for `Orchestrator.deps.budget`; passing the same `cost_budget_usd` to `LiveRunDisplay.for_mode(mode, cost_budget_usd=...)` is one extra kwarg.
- Keeps the `RunDisplayEvents` Protocol minimal — no new method for a static-per-run value.
- B1's "Non-goals" bullet explicitly forbids adding an event Protocol method for budget UI ("Do NOT preempt B3"), but the reverse holds too — B3 doesn't preempt future event surface.

### D9. Resume-time budget semantics — per-invocation, not cumulative

**Options considered**:

- (a) Cumulative: on resume, seed the cost budget from `_collect_existing_cost`; seed time budget from... nothing (wall-clock across resumes is meaningless); seed sample budget from `RunState.completed_trials + failed_trials`. Every invocation counts toward the cap.
- (b) Per-invocation: on resume, the wall clock restarts; the sample count restarts; the cost seed IS the prior-run cost (this one preserves the "how much money have I burned" answer, which is the operator's actual question).

**Decision: (b) for time + sample; (a) for cost.** Rationale:

- Time is meaningless across resumes (the wall clock kept ticking between invocations); cumulative semantics would make a resumed run hit its time cap on the first call.
- Sample-count-across-resumes is arguable — operators wanting "500 samples total, across invocations" is a legitimate CI pattern. File as #352 (cumulative mode).
- Cost is intrinsically cumulative (dollars spent are dollars spent). B3's `CostBudget` seeds `initial_cost_usd = _collect_existing_cost(output_dir)`; already-spent counts.
- Locked in Stage 2's `CostBudget(initial_cost_usd=…)` fixture.

### D10. `LIMIT_HIT.json` is written by the orchestrator, read by the CLI

**Options considered**:

- (a) Orchestrator surfaces `stopped_reason` on itself as an attribute; the CLI reads `orchestrator.stopped_reason` post-`run()`.
- (b) Orchestrator writes `LIMIT_HIT.json`; the CLI reads the file.
- (c) `Orchestrator.run()` returns a `RunOutcome` object with the reason field. Breaks the A4 "return Path" contract.

**Decision: both (a) and (b), with (b) load-bearing.**

- The disk marker (b) is the durable record — survives across CLI/library boundaries, survives across resume invocations, survives log-scraping tools that read the run directory offline.
- The in-memory attribute (a) is a convenience for callers wiring `Orchestrator` programmatically (embedders, tests) — avoids a filesystem round-trip.
- The CLI reads (b) so the banner-shaping logic doesn't leak orchestrator internals into the CLI. This also means resuming a `LIMIT_HIT` run and hitting a NEW limit correctly overwrites the marker; the CLI always reads the current state.

### D11. Config-side `time_limit_seconds` / `sample_limit` — punt to CLI-only for now

**Options considered**:

- (a) Add `compute.time_limit_seconds: float | None` and `compute.sample_limit: int | None` fields to `RunConfig`. CLI flags write to these; config authors can set them directly.
- (b) CLI-only for now. Config authors who want persistent limits can write a wrapper script `tolokaforge run --time-limit 30m …` or use the CLI-args-file feature (does one exist? — see #349).

**Decision: (b).** Rationale:

- The three flags are hard-cap operator ergonomics — an operator running the same config with different budgets wants CLI overrides, not config edits.
- Adding config-side fields introduces the same dual-home migration problem (`compute.time_limit_seconds` vs `orchestrator.time_limit_seconds`) — punt until a real request lands.
- `--cost-limit` DOES have a config-side home (`compute.max_budget_usd`) because the pre-B3 code path already exposed it. Symmetric extension for time/sample can land later without breaking Stage 3.
- Filed as # (implicit follow-up — add if operators ask; not in this plan's issue list).

## Test strategy

- **Unit tier for duration parser** (`test_duration_parser.py`) — table-driven positive and negative cases.
- **Unit tier for pricing overlay** (`test_pricing_overlay.py`) — layered load with real fixtures; the shipped table is the baseline, the fixture overlay is the delta.
- **Unit tier for budgets** (`test_budgets.py`) — construct each budget with concrete thresholds, feed synthetic deltas, poll after each; assert `BudgetHit` shape.
- **Unit tier for LIMIT_HIT marker** (`test_limit_hit_marker.py`) — round-trip write + read; Pydantic validation on malformed input.
- **Unit tier for orchestrator budget shutdown** (`test_orchestrator_budget_shutdown.py`) — end-to-end at the orchestrator level using `InMemoryConductor` stand-ins from `tests/unit/test_orchestrator_logic.py`. NO real docker, NO real LLM, NO real gRPC.
- **Unit tier for fallback client** (`test_fallback_client.py`) — monkey-patch `LLMClient.generate` to raise/succeed on demand.
- **Unit tier for CLI flags** (`test_cli_budget_flags.py`, `test_cli_fallback_flag.py`, `test_cli_model_cost_config.py`) — CliRunner + stub Orchestrator (extended `_make_stub_orchestrator`).
- **Unit tier for CLI budget integration** (`test_cli_budget_integration.py`) — CliRunner + a stub that writes `LIMIT_HIT.json` internally; assert the CLI reads it and shapes the banner correctly.
- **Unit tier for cost meter styling** (`test_run_display_cost_meter.py`) — synthetic event replay against a `LiveRunDisplay` with a fixed `cost_budget_usd`.
- **Unit tier for banner stopped variant** (`test_run_banner_stopped.py`) — table-driven across `success` × `stopped_reason` combinations.
- **Canonical tier — two new SVG goldens** (added inside `tests/canonical/test_run_display_goldens.py`): `panel_80_budget_amber.svg` (cost at 85% of budget) and `panel_80_budget_red.svg` (cost at 105% of budget). Existing goldens are asserted UNCHANGED (regression on unset-budget path).
- **NO integration tier** — the deterministic surfaces here don't need it. The one real-run smoke lives in Stage 3's manual validation quoted in the PR body (`custom_grading` — no LLM cost; `tool_use` with `--fallback-models` — bounded LLM cost). Both go into the PR description.
- **Grep-guards continue to pass** — `_run_display.py` continues to use `make_live` + `console` from `_display.py`; `_run_banner.py` and CLI code continue to route stderr through `console.print`; no new `Console(...)` instances; no new bare stdout writes.

## Discovered issues

**Fix in this PR** — none. Every neighbouring cleanup I noticed is filed rather than folded in, per the plan's tight-scope posture.

**Filed as follow-up issues** (via `gh issue create`, real numbers below):

1. **#349 — [terminal-dx followup] Align run-config field name with `--cost-limit`** — deprecate `compute.max_budget_usd`, introduce canonical `compute.cost_limit_usd`. Rename is out-of-scope for B3 (breaks every run-config on disk); belongs in a dedicated deprecation PR.
2. **#350 — [terminal-dx followup] Extend `--fallback-models` to user-simulator + judge roles** — B3 covers the agent client only. User simulator (`models.user`) and rubric judge (`models.judge`) are first-class LLM clients that don't benefit from fallback today.
3. **#351 — [terminal-dx followup] Wire `BudgetTracker` + `LIMIT_HIT` through `run_worker` (distributed)** — B3's plumbing lives in the single-process `Orchestrator.run()`. `run_worker` has its own budget check but does not write `LIMIT_HIT.json` and does not observe `--time-limit` / `--sample-limit`.
4. **#352 — [terminal-dx followup] Cumulative-across-resumes budget mode** — B3 defaults to per-invocation budgets (time/sample reset on resume; cost seeds from `_collect_existing_cost`). Some CI operators want lifetime caps.
5. **#353 — [terminal-dx followup] Ship pricing-table location alignment** — B3 keeps `tolokaforge/core/data/pricing.json` as the shipped table; the umbrella #283 wording says `tolokaforge/data/model_prices.yaml`. Reconcile the docs.

**Not filed (rejected)**:

- "Add per-turn cost enforcement inside `LLMClient.generate`" — budgets are RUN-level, not CALL-level. A per-call budget is a token-limit / max-tokens concept, orthogonal to hard caps.
- "Add `--budget-mode={cumulative,per-invocation}`" — #352 covers this. Not needed in the base PR.
- "Migrate `pricing.json` → `pricing.yaml`" — see #353. Separate PR if the umbrella asks.
- "Add a `BUDGET_STATUS.json` streaming update file for external monitoring" — over-engineered for MVP. LIMIT_HIT.json + log lines suffice.

## Risks / open questions

- **`FallbackLLMClient` and `Orchestrator._events.trial_progress`** — when the fallback fires mid-trial, `trial_progress` events fire with `cost_delta_usd` billed to the fallback model's pricing. The bottom bar accumulates cost correctly, but the "which model does this trial run on" answer becomes ambiguous. The structured `"Fallback triggered"` log line is the operator's ground truth. Callers of `trajectory.metrics.usage.calls[i].model` continue to see per-call attribution — B1 already surfaces this.
- **`FallbackLLMClient` and the inner LLMClient's `@retry` decorator** — the wrapper wraps `generate()` but `LLMClient.generate` itself is decorated with `@retry(stop_after_attempt=5, ...)`. The wrapper's fallback fires AFTER the inner retry gave up. Chain-in-chain: `[primary_retry(5) → fallback → m1_retry(5) → fallback → m2_retry(5) → raise]`. Worst-case attempts per generation: `5 × (1 + len(fallbacks))`. With 12 workers and `--fallback-models a,b,c`, a widespread outage could produce 5*4*12 = 240 wire attempts before every trial gives up. Bounded, acceptable; the pattern matches the operator's "try everything reasonable before failing" expectation. Filed as a note in the docs.
- **Fallback attribution in `trajectory.model_config`** — the persisted trial artifact today records the primary agent's `ModelConfig` on `TrialSpec.model_config`. A trial that swapped mid-way records only the primary's config. A truthful record would need `model_history: list[tuple[ModelConfig, turn_range]]`. Filed as follow-up in #350's body (attribution is cross-cut with the role-extension work).
- **`--model-cost-config` overlay mutation persists across CLI invocations in the same process** — because `MODEL_PRICING` is a module-global mutated by `reload_pricing`. Not a real problem for the CLI (each invocation is a fresh process), but embedders reading two configs in one Python process see the LATEST overlay's rates on both configs. Documented in Stage 4.
- **`_stopped_reason` on `Orchestrator` — attribute-vs-return-value trade-off** — an attribute is a soft contract (tests must read it via `.` access; typos silently succeed). A stronger contract is a Pydantic `RunOutcome` returned from `run()`; but that breaks A4's "return Path" contract. Trade-off accepted: `LIMIT_HIT.json` (disk) is the durable record; the attribute is the ergonomic sugar.
- **Time budget doesn't apply to task loading** (`TimeBudget.start` set on first `record_*` call, not at construction). On a project with 1000 tasks that loads over 60 seconds, an operator setting `--time-limit 30s` gets 30s of EXECUTION time, not 30s of wall-time. Documented in Stage 4's `--time-limit` explainer.
- **Sample budget hit is announced AFTER a trial terminates**, meaning `--sample-limit 3` typically ends the run with `completed_trials ∈ {3, 4, ..., 3 + effective_workers - 1}` — because in-flight trials that were already leased when the third terminated still complete. This is the "graceful shutdown" contract, but operators may be surprised. Documented as "at least N, up to N + workers - 1" in Stage 4.
- **Pricing overlay YAML support requires `pyyaml`** — already a project dependency (present in `pyproject.toml`; used by adapters, tasks, config loader). No new dependency.
- **`click.Path(exists=True)` on `--model-cost-config` runs `os.access`** — on a network-mounted overlay path with slow stat, this could add latency. Bounded to CLI startup; not in the hot path.
- **The B4 (`--dry-run`) interaction** — under `--dry-run`, no provider calls happen; the cost budget is trivially unreachable and the time budget clock never starts (no `record_*` call). Sample budget: `--dry-run` renders N samples but never terminates trials via the queue path, so the counter also never advances. Effectively, `--dry-run` bypasses budgets entirely. B4 hasn't landed yet, but the interaction is safe by construction. Add a docs note when B4 lands.
