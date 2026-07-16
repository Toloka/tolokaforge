# Plan: Boot-window log tail widget in the Rich panel

Issue: #394
Branch: feat/boot-log-tail-widget
Base/target branch: `feat/terminal-dx` (NOT `main` — panel plumbing lives there; per umbrella #395 branch policy)

## Context

Under `--display=rich`, during the ~10–30s engine-stack boot window
(`service_stack.start_all(wait=True)`, `tolokaforge/core/orchestrator.py:1347`)
the panel shows only a spinner + `"Starting services… (docker compose up)"` and
the services widget. Meanwhile the docker subsystem emits INFO milestones
(`"Building images for N services"`, `"Starting container '…'"`,
`"Service '…' is healthy"`) that are already captured into the panel's
`_log_buffer` ring but never displayed — INFO/DEBUG are swallowed on purpose so
the boot log wall can't scroll the Live region off-screen (see the `_LogSink`
docstring). The user therefore has no per-step feedback while several images
build and health probes run in sequence.

Grounding (verified, zero LLM tokens):

- The docker submodules log via `logging.getLogger(__name__)`, giving names
  `tolokaforge.docker.stack` / `.container` / `.builder` / `.image` / `.health`
  / `.network` / `.registry`. `Orchestrator.__init__` raises the
  `tolokaforge.docker` namespace to INFO (`orchestrator.py:386`) so these
  propagate to the root `_LogSink` in the default (INFO) run mode.
- A `run_python` probe entered a real `LiveRunDisplay`, emitted three
  `tolokaforge.docker.*` INFO records plus one `tolokaforge.runner` record, and
  read them back: `log_records()` returned all four; the prefix predicate
  `name.startswith("tolokaforge.docker.")` kept exactly the three docker records
  and dropped the runner one. The short-name (`name.rsplit(".",1)[-1]`) resolved
  to `stack` / `container` / `health`.
- The panel is already active during boot — the existing services widget
  (gated on `_total_trials == 0 and _services`) proves the display's
  `__enter__` runs before the orchestrator boots the stack.

This is a pure consumer of the existing ring buffer. No engine change, no new
`RunDisplayEvents` method, no `EngineStack.get_status()` polling.

## Goal

Add a compact **"Boot log"** region to the Rich panel that shows the last few
`tolokaforge.docker.*` milestone records during the startup window, so the
operator sees real per-step progress instead of a bare spinner. The region
disappears the moment trials dispatch, and its presence never changes the
panel's total rendered height (the stable-height invariant from `d2fff0a`).

Observable contract:

- During the boot window (`_total_trials == 0`) with at least one buffered
  `tolokaforge.docker.*` record, the panel shows a `Panel(title="Boot log")`
  region between the services widget and `main`, listing the last N docker
  records most-recent-last, one per line as
  `HH:MM:SS.mmm | {short-name} | {message}`.
- Once `run_started` fires (`_total_trials > 0`) the region is gone and `main`
  reclaims its rows.
- Total renderable height stays `max(12, viewport - 1)` whether or not the
  region is present.

## Non-goals

- A full log-tail widget with search / level-filter — `--display=full`'s
  Textual TUI already owns that (`tolokaforge/dx/tui.py`).
- New engine hooks / callbacks / `RunDisplayEvents` methods.
- Displaying non-docker INFO records (LLM/runner/orchestrator chatter) in the
  boot region — the region is scoped to `tolokaforge.docker.*` only.
- Gating on the phase set `{starting_services, connecting_runtime,
  priming_queue}` as the issue proposed. `priming_queue` is never emitted (filed
  as #399) and `loading_tasks` / `services_ready` are also legitimate
  `_total_trials == 0` windows; gating on the phase set would be coupling to a
  dead phase and would blank the widget outside a narrow slice. The gate is
  `_total_trials == 0` + non-empty filtered buffer, exactly mirroring the
  services-widget collapse pattern. (See Risks — this is a deliberate deviation.)
- Fixing / removing the dead `priming_queue` phase label (filed as #399).

## Stages

### Stage 1: Boot-log render helpers + filter predicate (pure functions)

- **Contract:**
  - New module-level pure function in `tolokaforge/dx/live_panel.py`:
    `_docker_boot_records(records: Iterable[logging.LogRecord]) -> list[logging.LogRecord]`
    — returns, in input order, the records whose `.name` starts with
    `"tolokaforge.docker."` (trailing dot is load-bearing: it excludes the bare
    `tolokaforge.docker` namespace logger and any sibling like
    `tolokaforge.dockerx`). No truncation here.
  - New module-level pure function
    `_render_boot_log_tail(records: Iterable[logging.LogRecord], max_lines: int = 5) -> Panel`
    — filters via `_docker_boot_records`, keeps the **last** `max_lines`
    (most-recent-last), formats each as
    `{HH:MM:SS.mmm} | {short-name} | {message}` where
    `short-name = record.name.rsplit(".", 1)[-1]`,
    `message = record.getMessage()`, and the timestamp is derived from
    `record.created` / `record.msecs` rendered **in UTC** so the bytes are
    stable across dev-box and CI timezones:
    `datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S") + f".{int(record.msecs):03d}"`.
    (`timezone` is already imported in `live_panel.py:32`.) `int(record.msecs)`
    truncates rather than rounds — `record.msecs` is a float in `[0, 1000)` and
    `f"{999.6:03.0f}"` would overflow to a 4-char `"1000"` and misalign the
    column; `int()` caps at `999`. Returns `Panel(Text("\n".join(lines)),
    title="Boot log", border_style="muted", padding=(0, 1))`. Mirrors
    `_render_services_table`'s flat-`Text`-inside-`Panel` shape (a `Table` inside
    a tight fixed-height `Layout` can silently drop rows — see the
    `_render_services_table` docstring). The function is only ever called by
    `_build_layout` when the filtered list is non-empty, so it does not need an
    empty-state branch; guard the empty case at the call site (Stage 2).
  - `short-name` is `record.name.rsplit(".", 1)[-1]`; the prefix predicate
    admits the **full** docker logger set — not just the six the issue named —
    so the column must tolerate the longest token. Confirmed loggers under
    `tolokaforge.docker.*`: `stack`, `container`, `builder`, `image`, `health`,
    `network`, `registry`, `config`, `ports`, `mount`, `policy`, `logging`,
    `wait_for_services`, `wheel_resolver`, plus `stacks.*` submodules. The
    boot-milestone emitters observed are `stack` / `container` / `builder`;
    `wait_for_services` (17 chars) is the widest realistic short-name and is
    exercised in the Stage 3 golden fixture.
  - No change to the ring buffer, `_LogSink`, or `log_records()`.
- **Behaviour to lock (unit, `tests/unit/test_run_display.py`):**
  - `_docker_boot_records` keeps `tolokaforge.docker.stack` /
    `.container` / `.health` records and drops a `tolokaforge.runner` record and
    a bare `tolokaforge.docker` record (trailing-dot boundary).
  - `_render_boot_log_tail` with 8 docker records and `max_lines=5` renders
    exactly 5 lines, the **last** 5 in order (assert first-kept and last-kept
    messages via `Console(...).export_text()`), each line matching
    `HH:MM:SS.mmm | {short-name} | {message}` (build records with fixed
    `created`/`msecs` so the timestamp segment is deterministic). Because the
    helper renders `record.created` in UTC, the asserted `HH:MM:SS` segment is
    TZ-stable without any `TZ`/`tzset` manipulation — assert the exact expected
    UTC time string for the fixed `created` epoch.
  - A record with `msecs = 999.6` renders `.999` (not `.1000`) — locks the
    `int()` truncation so the column never widens to 4 digits.
  - Records are built as real `logging.LogRecord` (via `logging.makeLogRecord`
    or `LogRecord(...)`) — no mocks — so `getMessage()` / `%`-formatting is
    exercised for real (AGENTS.md Core Rule 5).
- **Compatibility:** internal only. Private module functions; no CLI flag, no
  config field, no public-API change.
- **Deliverable:** the two helper functions + unit tests green. No layout change
  yet (region not wired in until Stage 2), so the panel is visually unchanged
  after this stage.
- **Validation:** `run_tests` marker `unit` on `tests/unit/test_run_display.py`;
  `lint_check` + `format_check`. Reviewer checks the prefix predicate uses the
  trailing dot and that the helper takes the **last** `max_lines`, not the first.
- **Doc updates:** none this stage (helpers are wired + documented in Stage 2).

### Stage 2: Wire the boot-log region into `_build_layout` (stable-height)

- **Contract:**
  - `_build_layout` gains an optional `Layout(name="boot_log", size=boot_log_h)`
    inserted **between** the `services` region and `main`.
  - Activation gate (computed under `self._lock`, alongside the existing
    `banner` / `services` / `in_startup` snapshot): the region is present iff
    `self._total_trials == 0` **and** `_docker_boot_records(self._log_buffer)`
    is non-empty. Snapshot the filtered records inside the lock (iterate the
    `deque` under the lock, same discipline as `log_records()`), then release
    before rendering.
  - Desired height: `desired_boot_log_h = min(len(filtered), max_lines) + 2`
    (one row per shown record + 2 border rows), where `max_lines = 5`. This
    adapts to the record count exactly as the services widget sizes to
    `len(services) + 2`.
  - **Stable-height invariant — the sum of every top-level `Layout` size MUST
    equal `total = max(12, viewport - 1)` in every state, including when the
    viewport is too small to grant every region its desired height.** A row sum
    that exceeds `total` re-anchors Rich Live and re-introduces the #392 panel
    stacking. The current `main_h = max(5, total - … - bottom_h)` formula does
    NOT hold this on its own: when the `max(5, …)` floor engages (small
    viewport, tall services + boot-log), the summed rows exceed `total`. The
    boot-log region MUST therefore be granted only rows that would otherwise go
    to `main`, never rows that push `main` below its floor.
  - **Priority + clamp (contract; the exact arithmetic is the implementer's
    call as long as the invariant above holds):** `bottom` is fixed at 1;
    `main` keeps a floor of 5; `services` keeps its desired height; `boot_log`
    is the lowest priority and absorbs only the remaining budget:
    `budget = total - services_h - bottom_h - 5`;
    `boot_log_h = min(desired_boot_log_h, budget)`; **if `boot_log_h < 3` the
    region is dropped entirely** (a bordered panel needs ≥ 3 rows to show ≥ 1
    line). Then `main_h = max(5, total - banner_h - services_h - boot_log_h -
    bottom_h)`. When `boot_log_h > 0` the budget subtraction guarantees the
    `max(5, …)` floor is not the binding term, so the sum is exactly `total`.
    When `boot_log_h == 0` the layout is byte-for-byte the pre-existing
    services-window layout (no behaviour change when the region is absent).
    Note: banner and boot_log never co-occur — banner appears only on an
    auth-shaped `trial_failed` (`_total_trials > 0`), boot_log only during the
    startup window (`_total_trials == 0`) — so `banner_h` is 0 whenever
    `boot_log_h > 0`.
  - The boot-log region steals rows from `main`; it never lengthens the overall
    renderable. `main` still splits into `trials` (ratio 2) / `focused`
    (ratio 3) unchanged.
  - **The render's line budget MUST equal the region's granted content rows.**
    Call `_render_boot_log_tail(filtered, max_lines=boot_log_h - 2)` — NOT a
    hardcoded `max_lines=5`. Rich crops a Panel taller than its `Layout(size=…)`
    from the **bottom** (keeps the top border + oldest content lines), so if the
    helper renders 5 lines into a clamped `size=4` region it would drop the 3
    *newest* milestones — inverting the "most-recent-last" contract exactly on
    small terminals. Passing `boot_log_h - 2` makes the Panel render precisely
    the `boot_log_h - 2` content rows the region can show, and because the helper
    keeps the **last** `max_lines`, the newest records survive the clamp. (When
    unclamped, `boot_log_h - 2 == min(len(filtered), 5)`, so the full-height path
    is unchanged.) When inactive, the region is simply not added to `row_defs`
    (same shape as the existing `services` conditional).
  - `max_lines` is a module constant (e.g. `_BOOT_LOG_MAX_LINES = 5`) so the
    render helper default and the layout height derive from one source.
- **Behaviour to lock (unit, `tests/unit/test_run_display.py`):**
  - *Region present during boot:* fresh display, no `run_started`, append 3
    `tolokaforge.docker.*` records to `display._log_buffer`; `_build_layout`
    child names include `"boot_log"` and it sits between `"services"` (when
    present) and `"main"`. (Assert order via `[c.name for c in
    layout.children]`.)
  - *Region absent once trials dispatch:* after `run_started(total_trials=1,
    …)` with docker records still in the buffer, `"boot_log"` is not in the
    child names (collapse mirrors the services widget).
  - *Region absent when no docker records:* fresh display with only a
    `tolokaforge.runner` record buffered → `"boot_log"` absent (empty filtered
    list, not an empty-bordered panel).
  - *Height invariant (three regimes, all must hold `sum(child.size) ==
    max(12, viewport - 1)` AND `main_h >= 5`):* use a `_StubLive`/`_StubConsole`
    fixing `console.height` (mirror
    `test_main_region_size_caps_at_viewport_when_trials_overflow` at
    `test_run_display.py:1062`), populate `_services` with 2 services and
    `_log_buffer` with 5 `tolokaforge.docker.*` records:
    - **No clamp (tall):** viewport 40 → boot-log gets its full
      `desired_boot_log_h` (7), region present, sum == 39.
    - **Clamp-but-present (mid):** viewport 15 → `budget = 14 - 4 - 1 - 5 = 4`,
      so `boot_log_h` is clamped to 4 (shows 2 records), region **present** and
      shrunk, `main_h == 5`, sum == 14. This is the regime that would silently
      overflow (`4 services-rows + 7 desired boot-log + 5 main-floor + 1 bottom
      = 17 > 14`) under the naive formula — it is the #392 stacking regression
      lock and MUST be asserted. Additionally render the boot-log region to text
      (`Console(...).export_text()` on `_render_boot_log_tail(filtered,
      max_lines=boot_log_h - 2)`) and assert it contains the **last 2** records'
      messages and NOT the older ones — this locks the "most-recent-last under
      clamp" contract (the round-2 crop-inversion bug), which a height-only
      assertion (`boot_log_h == 4`) would miss.
    - **Drop (tiny):** viewport 12 → `budget = 12 - 4 - 1 - 5 = 2 < 3`, so the
      boot-log region is **absent**, sum == 12, layout identical to the
      services-only window.
    Assert the summed top-level sizes and `main_h` in all three; assert
    `"boot_log"` presence/absence per regime.
- **Compatibility:** internal only — a new optional render region. No CLI flag,
  config field, or public-API change.
- **Deliverable:** the boot-log region renders during a real boot and collapses
  when trials dispatch; unit tests green; the four existing run-display goldens
  remain byte-identical (they replay `run_started(total_trials=50)` so the
  region is never active — the implementer MUST confirm zero drift, not
  regenerate them).
- **Validation:**
  - `run_tests` marker `unit` (`tests/unit/test_run_display.py`) + the full
    unit+canonical baseline stays green.
  - Confirm existing goldens unchanged: run the golden test **without**
    `--update-canon` and see the four `panel_*.svg` pass untouched.
  - **Live acceptance (implementer + reviewer):**
    `scripts/with_env.sh uv run tolokaforge run --config examples/native/multi_service/run_config.yaml`
    (real OpenRouter key in `.env`). During the ~15s boot window the panel shows
    real docker milestones scrolling in the "Boot log" region (image build →
    container start → health), the region collapses when trials dispatch, and no
    panel stacking occurs (regression guard for the #392 fix). Optionally set
    `TOLOKAFORGE_STDERR_PROBE=/tmp/probe-394.log` to reconfirm no stderr bypass.
- **Doc updates:** in `docs/CLI.md` § "Live run panel" (around line 136–139),
  add the boot-log region to the region list — rewrite the intro sentence
  ("three regions, plus two conditional ones") to reflect the third conditional
  region, and add a bullet after the services-widget bullet:
  *"**Optional Boot log (below the services widget).** During the startup window
  (before the first `run_started`) when the panel has buffered any
  `tolokaforge.docker.*` record: a `Panel(title="Boot log")` of the last five
  docker milestones, most-recent-last, formatted `HH:MM:SS.mmm | short-name |
  message`. Steals rows from `main` (total height unchanged) and disappears once
  trials dispatch."* Add a `### Feat` entry under `## Unreleased` in
  `CHANGELOG.md` referencing #394. Run `rg -n "conditional|Boot log|boot_log"
  docs/CLI.md` to confirm the region count reads consistently.

### Stage 3: Canonical golden for the boot-log region

- **Contract:**
  - New byte-level SVG golden(s) in
    `tests/canonical/golden/run_display/` capturing the panel **during** the
    boot window with the boot-log region populated, at 80 and 120 columns:
    `panel_boot_log_80.svg` / `panel_boot_log_120.svg`.
  - New test function in `tests/canonical/test_run_display_goldens.py` that:
    replays a boot-window state (`phase_changed(phase="starting_services",
    detail="docker compose up", services=[…])`, no `run_started`), appends a
    fixed sequence of `logging.LogRecord`s (built with explicit
    `created`/`msecs` so the timestamp bytes are deterministic — do **not** use
    live `getLogger().info(...)`) to `display._log_buffer`, then
    `recorder.print(display._build_layout())` and compares to / writes the
    golden under `--update-canon`. Reuse the module's determinism knobs
    (`frozen_clock`, `SVG_UNIQUE_ID`, `DEFAULT_TERMINAL_THEME`,
    truecolor recorder). Timestamps are TZ-stable because the helper renders
    `record.created` in UTC (Stage 1) — no `TZ`/`tzset` pin needed in the
    golden test.
  - The fixed record sequence MUST include one `tolokaforge.docker.wait_for_services`
    record (the widest realistic short-name, 17 chars) alongside `stack` /
    `container` / `builder` records, so the golden locks the column width for
    long module short-names, and enough records that at the chosen viewport the
    boot-log region is present at full (un-clamped) height.
- **Behaviour to lock (canonical):** the two new goldens match byte-for-byte;
  regenerated with
  `uv run pytest tests/canonical/test_run_display_goldens.py --update-canon`.
- **Compatibility:** internal only (test artifacts).
- **Deliverable:** two new golden SVGs + the test; canonical suite green. The
  four pre-existing goldens are **not** touched.
- **Validation:** `run_tests` marker `canonical`
  (`tests/canonical/test_run_display_goldens.py`). Reviewer diffs the new SVGs
  to confirm the "Boot log" panel renders the expected lines and the total
  height matches the width's viewport.
- **Doc updates:** the golden-regeneration note already covers the new files;
  add the new golden filenames to the CLI.md sentence that references
  `panel_{80,120}.svg` only if that reads cleanly — otherwise no doc change
  (the goldens are self-describing test artifacts).

## Discovered issues

- **Fix in this PR:** none. The widget is self-contained; the dead
  `priming_queue` label is deliberately out of scope (the gate does not use it).
- **Filed as issues:**
  - **#399** — dead `priming_queue` phase label: present in `_PHASE_LABELS`
    (`live_panel.py`) and the `run_display_events.py` docstring but never emitted
    by any `phase_changed` call. Violates AGENTS.md Core Rule 8. Either remove or
    wire the emission. Independent of this widget.

## Risks / open questions

- **Deviation from the issue's phase-set gate (deliberate).** The issue proposed
  gating on `_current_phase in {starting_services, connecting_runtime,
  priming_queue}`. `priming_queue` is never emitted (#399), and `loading_tasks`
  / `services_ready` are also `_total_trials == 0` windows with real docker
  records. Gating on the phase set would blank the widget during
  `services_ready` (right after boot) and depend on a dead phase. The plan gates
  on `_total_trials == 0` + non-empty docker buffer, matching the services
  widget exactly. Evidence: `orchestrator.py` emits only `loading_tasks` (1176),
  `starting_services` (1342), `services_ready` (1348), `connecting_runtime`
  (1394); `rg priming_queue` finds no emission.
- **Quiet mode blanks the widget (acceptable).** Under `tolokaforge run -q`
  (root/handler level WARNING) the `_LogSink` filters INFO docker milestones
  before they reach the buffer, so the boot-log region is empty and therefore
  absent. This matches operator intent (`-q` = fewer logs) and the widget
  degrades cleanly (no empty panel). Not worth special-casing. The live
  acceptance run must use default verbosity (INFO) to see the region.
- **Ring-buffer eviction is a non-issue for the boot window.** The buffer is
  500 records; during `_total_trials == 0` only a few dozen docker records
  exist, far below the cap, so the tail is never evicted before display.
- **Container-log records are correctly excluded.** `LogRouter`
  (`docker/logging.py`) logs per-trial container stdout on `container.{name}`
  loggers with `propagate=False` — a different tree, not `tolokaforge.docker.*`
  — so they never enter the boot-log region. This is desired (container logs are
  trial-time noise, not boot milestones).
- **Timestamp determinism in goldens (closed).** `record.created` is
  wall-clock AND `datetime.fromtimestamp(...)` without a `tz` renders in the
  local timezone — so pinning `created`/`msecs` alone is insufficient; the
  rendered `HH:MM:SS` would still shift PST-dev-box vs UTC-CI. Fix: the helper
  renders in UTC (`tz=timezone.utc`), which stabilises both the Stage 1 unit
  assertion and the Stage 3 goldens with no `TZ`/`tzset` manipulation. Trade-off:
  the boot log shows UTC, not local time — acceptable, as the panel establishes
  no local-time convention (bottom bar / services widget show no timestamps) and
  the value is relative boot progression, not an absolute wall-clock the user
  cross-references.
- **Pre-existing services-only overflow is out of scope (unchanged).** The
  `main_h = max(5, …)` floor can already overflow `total` in the degenerate case
  of very many services on a tiny terminal (services_h alone > total - bottom -
  5) — this predates #394 and is unreachable with the engine stack's 2–4
  services. The Stage 2 clamp scopes only the new boot-log region so that adding
  it never introduces a *new* overflow: when boot-log is present the sum is
  exactly `total`; when it is dropped the layout is byte-identical to the
  existing services-window layout. Fixing the services-only degenerate case is
  not in this PR.
