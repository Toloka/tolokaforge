# Plan: A5 — Dashboard-URL banner (local `file://` links)

Issue: Toloka/tolokaforge#281 (milestone: Terminal DX, umbrella #297)
Branch: `feat/issue-281-a5-dashboard-url-banner` (already created; branches off `feat/terminal-dx`; PR targets `feat/terminal-dx`)

## Context

The Terminal DX milestone teaches the "terminal launches, artifact analyzes" mental model. A4 (#280) already emits the absolute run-dir path on stdout as the single sanctioned stdout write. What is missing is the human-visible bookend: a two-line banner at the top of `tolokaforge run` announcing the run and the (about-to-be-populated) results directory, and a three-line banner at the bottom announcing outcome + duration + the next command the user is expected to run (`tolokaforge browse <run-id>`).

**Already-shipped platform this builds on:**

- **A1 (#276)** — `tolokaforge/cli/_display.py`: shared `console` (stderr, `soft_wrap=True`, `THEME`). `THEME["link"] = "underline cyan"` — the semantic style A5 renders the `file://` URLs with. Grep-guard `tests/canonical/test_cli_display_invariants.py::test_no_ad_hoc_console_in_cli` forbids any new `rich.Console(...)` outside `_display.py`, so A5's helpers accept a `Console` parameter rather than constructing one.
- **A3 (#279)** — canonical SVG-golden pattern via `Console(record=True, force_terminal=True, color_system="truecolor")` + `Console.export_svg(theme=DEFAULT_TERMINAL_THEME, unique_id=...)`. Reused by B1's `tests/canonical/test_run_display_goldens.py` — A5 mirrors that pattern for its three banner goldens.
- **A4 (#280)** — `emit_artifact_path(path)` on stdout. A5 is the stderr framing on top of A4's stdout line.
- **B1 (#285)** — `LiveRunDisplay.for_mode(mode)` context manager; `RunDisplayEvents` protocol; `OrchestratorDeps(events=display.events)` wiring. A5 renders BEFORE `LiveRunDisplay.__enter__` and AFTER `LiveRunDisplay.__exit__` so the banner sits outside the Live region.
- **B2 (#282)** — `--display=none` calls `silence_console()` (sets `console.quiet = True`). A5 uses the shared `console` → banners auto-silence under `--display=none`, matching the issue's stderr-silent-on-success contract.

**Grep confirms the surface is net-new**: `rg -n "print_run_start_banner|print_run_end_banner|_run_banner|resolve_run_directory|format_duration" tolokaforge/ tests/` returns zero hits.

**Reproduced current behaviour** (dev MCP `run_tests` + code read):

- `tolokaforge/cli/main.py::run` at line 399 prints `Loading configuration…`, at line 456 prints `Output base: <path>`, at line 472 opens the Live region via `LiveRunDisplay.for_mode(...)`, at line 501 the orchestrator runs, at lines 503-504 prints `✓ Run complete!` and `Results saved to: <path>`, at line 505 emits the stdout artifact line. No file-URL, no duration, no browse suggestion.
- `Orchestrator.run()` (`tolokaforge/core/orchestrator.py:966-993`) is the sole computer of the run-id / run-dir: it takes `evaluation.output_dir`, extracts `Path(...).name` as `base_name`, appends `datetime.now().strftime("%Y%m%d_%H%M%S")`, and yields `run_id = f"{base_name}_{timestamp}"` + `output_dir = Path(base_output_dir).parent / run_id`. **The CLI does not know either value until `orchestrator.run()` returns.**
- `tolokaforge/cli/_run_display.py::_format_eta` already produces the exact `MM:SS` / `HH:MM:SS` shape the A5 end banner needs (the A3 goldens for the bottom bar pin it).

**Discovered while reading:**

- The CLI at `main.py` lines 407-410 defaults `evaluation.output_dir` to `results/run_{ts1}` when absent, and the orchestrator then appends another timestamp — producing `results/run_{ts1}_{ts2}/`. **Double-timestamp bug, preexisting, filed as #345**, out of A5 scope.
- Two `_format_eta` helpers with the same name and divergent output shapes live in `_run_display.py` (MM:SS / HH:MM:SS) and `main.py` (`Nh Mm Ss`). Confusing. Filed as #346. A5 fixes the near half (extracts a shared `format_duration` in `_display.py` and migrates `_run_display._format_eta` to consume it); the `main.py::_format_eta` migration used by `tolokaforge status` is left to #346.
- `main.py` lines 503-504 (`console.print("[bold green]✓ Run complete![/bold green]")` and `console.print(f"Results saved to: {output_dir}")`) become redundant once the A5 end banner ships. **A5 deletes both lines** — the banner supersedes them.

## Goal

Every `tolokaforge run` invocation frames its output with two banners on stderr, rendered through the shared `console` and styled via the A1 `link` theme token:

- **Start banner** (right after run-id resolution, BEFORE `LiveRunDisplay.__enter__`):
  ```
  → Run: <run-id>
  → Report: file:///<abs-path>/results/<run-dir>/
  ```
- **End banner** (right after `LiveRunDisplay.__exit__`, on both success and failure, BEFORE `emit_artifact_path`):
  ```
  ✓ Run complete in <duration>          # success
  ✗ Run failed in <duration>            # any exception
  → Report: file:///<abs-path>/results/<run-dir>/
  → Browse: tolokaforge browse <run-id>
  ```

The `file://` URL is absolute, canonicalised via `Path.resolve().as_uri() + "/"` (trailing slash marks it as a directory), and wrapped in Rich's `[link=URL]…[/link]` hyperlink markup so OSC 8-capable terminals render it clickable. The `link` style token from `THEME` (`underline cyan`) provides the visible style.

`<duration>` is `MM:SS` under one hour, `HH:MM:SS` above — the same shape B1's bottom bar uses. Duration is measured with `time.monotonic()` bracketing the run.

Under `--display=none`, both banners are silenced (they route through `console`, which B2 quiets); the stdout artifact line still fires. This preserves the machine-readable-only exit contract established in #280 / #282.

## Non-goals

- **Do NOT preempt C3's `tolokaforge browse` command (#289).** The end banner prints the invocation string `tolokaforge browse <run-id>` as a suggestion — users can copy it. When C3 lands, the string remains valid and starts working automatically. A5 does NOT add a `browse` command, does NOT check for its existence, and does NOT change its exit code based on whether it exists.
- **Do NOT fix the double-timestamp bug (#345).** A5's helper `resolve_run_directory` preserves the current logic verbatim; the fix belongs in #345.
- **Do NOT rewrite `tolokaforge status`'s duration formatter.** #346 tracks the migration; A5 only extracts the `MM:SS`/`HH:MM:SS` shape used by the banner and B1's bottom bar.
- **Do NOT change `emit_artifact_path` semantics or ordering.** The A5 end banner sits BEFORE `emit_artifact_path` (both after `__exit__`); the `test_emit_artifact_path_fires_after_display_exit` invariant remains intact.
- **Do NOT emit the banner on `tolokaforge prepare` / `worker`.** #281's scope is `tolokaforge run`. `prepare` already emits `emit_artifact_path` on the enqueue side; a banner there is out of scope.
- **Do NOT add colour to the `→` / `✓` / `✗` glyphs beyond the theme's `link` token on URLs and `success` / `error` tokens on the outcome line.** Keep the framing quiet visually — the URLs are the payload.
- **Do NOT swallow the failure traceback.** The `finally:` block prints the banner; the exception continues to propagate to Click, which renders its own traceback / `UsageError` / exit code. The banner is complementary, not a replacement.

## Stages

### Stage 1: Banner helpers + `format_duration` + `resolve_run_directory` + goldens/unit tests

**Contract:**

- New public function in `tolokaforge/cli/_display.py`:
  ```python
  def format_duration(seconds: float) -> str
  ```
  Returns `"MM:SS"` when `seconds < 3600`, `"HH:MM:SS"` otherwise. No `None` handling (callers compose the "n/a" case themselves). Zero-padded fields. Truncates to whole seconds via `int(seconds)`. Exported via `__all__`.

- `tolokaforge/cli/_run_display.py::_format_eta` is refactored to `return "n/a" if eta_seconds is None else format_duration(eta_seconds)`. B1's golden SVGs (`tests/canonical/golden/run_display/panel_{80,120}.svg`) must NOT change (same output, refactor-only).

- New module `tolokaforge/cli/_run_banner.py`, publishing two functions:
  ```python
  def print_run_start_banner(
      *,
      run_id: str,
      run_dir: Path,
      console: Console,
  ) -> None

  def print_run_end_banner(
      *,
      run_id: str,
      run_dir: Path,
      duration: float,
      success: bool,
      console: Console,
  ) -> None
  ```
  Each writes to the given `console` (never constructs its own). `run_dir` is `Path.resolve()`d inside the helper before being converted to a URI so callers can pass either a relative or absolute path safely. The URL is `Path(run_dir).resolve().as_uri() + "/"`. Rich markup: `f"[link={url}]{url}[/link]"` — Rich auto-emits OSC 8 for `[link=URL]` and applies the `link` style token from the theme. The `→` prefix is emitted with the `muted` theme token (`dim`); the outcome glyph is `success` (`green`) for `✓` and `error` (`bold red`) for `✗`. The literal shape is:
  ```
  [muted]→[/muted] Run: <run-id>
  [muted]→[/muted] Report: [link=file:///abs/results/run/]file:///abs/results/run/[/link]
  ```
  ```
  [success]✓[/success] Run complete in <duration>              (success=True)
  [error]✗[/error] Run failed in <duration>                    (success=False)
  [muted]→[/muted] Report: [link=file:///abs/results/run/]file:///abs/results/run/[/link]
  [muted]→[/muted] Browse: tolokaforge browse <run-id>
  ```

- New public function in `tolokaforge/core/orchestrator.py` (module-level, above the class):
  ```python
  def resolve_run_directory(base_output_dir: str) -> tuple[str, Path]
  ```
  Extracts lines 983-993 of `Orchestrator.run()` verbatim. Returns `(run_id, output_dir)` where `output_dir = Path(base_output_dir).parent / run_id`. `output_dir` is NOT `.resolve()`d here (matches current behaviour; the banner resolves independently). Empty basename raises `ValueError` with the current message. `datetime.now()` is called ONCE per invocation so the pair is coherent. Note: this helper does not yet reach into `Orchestrator.run()` in Stage 1 — the refactor happens in Stage 2.

- Public surface (`__all__` in `_run_banner.py`): `print_run_start_banner`, `print_run_end_banner`.

**Behaviour to lock:**

- **`unit`** — `tests/unit/test_run_banner.py::TestFormatDuration`: boundaries `0` → `"00:00"`, `59` → `"00:59"`, `60` → `"01:00"`, `3599` → `"59:59"`, `3600` → `"01:00:00"`, `3661` → `"01:01:01"`. Whole-second truncation for fractional inputs (`61.7` → `"01:01"`).
- **`unit`** — `tests/unit/test_run_banner.py::TestStartBanner`: `print_run_start_banner(...)` on a `Console(record=True, file=io.StringIO())` writes exactly two lines; each line contains `→`; second line contains `file:///` prefix and the string `run_id` (rendered inside the URL basename); the URL is absolute (starts with `file:///`, not `file://relative`); Rich markup renders `[link=…]` as an OSC 8 hyperlink when the recording console has `force_terminal=True` (assert via `record=True` + `export_text(styles=True)` containing the OSC 8 bytes, OR via `console.file.getvalue()` containing `\x1b]8;;file:///` when `force_terminal=True, color_system="truecolor"` is used).
- **`unit`** — `tests/unit/test_run_banner.py::TestEndBanner`: success variant contains `✓ Run complete in`; failure variant contains `✗ Run failed in`; both variants contain the `tolokaforge browse <run-id>` invocation string exactly (grep for the literal). Both variants contain the same `file:///` URL as `print_run_start_banner` when given the same `run_dir`.
- **`unit`** — `tests/unit/test_run_banner.py::TestSharedConsoleContract`: the shared `console` from `_display.py` accepted as `console=` argument produces output identical to a fresh recording console (proves helpers do not construct their own).
- **`unit`** — `tests/unit/test_orchestrator_resolve_run_directory.py`: `resolve_run_directory("results/my_run")` → `("my_run_<ts>", Path("results/my_run_<ts>"))` where `<ts>` matches `\d{8}_\d{6}`; empty basename (`"."`, `"/"`, `""`) → `ValueError` naming `evaluation.output_dir`; two successive calls with the same input yield different `run_id`s when `datetime.now()` advances (mock or `time.sleep(1.01)` — prefer freezegun-style monkeypatch on `datetime.now` for determinism).
- **`canonical`** — `tests/canonical/test_run_banner_goldens.py`: three SVG goldens at 80-column width, mirroring `tests/canonical/test_run_display_goldens.py`:
  - `banner_start.svg` — start banner with fixed `run_id="run_20260715_120000_20260715_120001"` and `run_dir=Path("/Users/ci/results/run_20260715_120000_20260715_120001")`.
  - `banner_end_success.svg` — end banner with `success=True`, `duration=125.4` (renders as `"02:05"`).
  - `banner_end_failure.svg` — end banner with `success=False`, `duration=3665.0` (renders as `"01:01:05"`, exercising HH:MM:SS branch).
  Each golden pins the exact `Console.export_svg(theme=DEFAULT_TERMINAL_THEME, unique_id="tolokaforge-run-banner")` bytes. Goldens live in `tests/canonical/golden/run_banner/`. Regeneration: `uv run pytest tests/canonical/test_run_banner_goldens.py --update-canon`.

**Compatibility:** Internal only. `resolve_run_directory` is net-new; `_format_eta` refactor preserves output. `format_duration` is new public export of `_display.py`.

**Deliverable:** Three new files (`tolokaforge/cli/_run_banner.py`, `tests/unit/test_run_banner.py`, `tests/canonical/test_run_banner_goldens.py`), three new goldens under `tests/canonical/golden/run_banner/`, one new file `tests/unit/test_orchestrator_resolve_run_directory.py`, three-line edit in `_run_display.py`, small additions to `_display.py` and `orchestrator.py`. No import-cycle risk: `_run_banner` depends on `_display` (for the `Console` type import); `_display` does not depend on `_run_banner`.

**Validation:**

- `uv run pytest tests/unit/test_run_banner.py tests/unit/test_orchestrator_resolve_run_directory.py -v`.
- `uv run pytest tests/canonical/test_run_banner_goldens.py -v` (fails first run; then `--update-canon` to regenerate, then commit the goldens).
- `uv run pytest tests/canonical/test_run_display_goldens.py -v` (B1 goldens must still pass — proves the `_format_eta` refactor is behaviour-preserving).
- `uv run pytest tests/canonical/test_cli_display_invariants.py -v` (grep-guards for shared console and stdout writes still pass — `_run_banner.py` does not construct a Console or call `print(` / `sys.stdout.write(`).
- `uv run ruff check tolokaforge/cli/_run_banner.py tests/unit/test_run_banner.py tests/canonical/test_run_banner_goldens.py`.

**Doc updates:** None yet (Stage 3 does docs after wiring is proven).

### Stage 2: Wire banners into `cli/main.py::run` + CLI integration test

**Contract:**

- `Orchestrator.run` gains two keyword-only parameters:
  ```python
  def run(
      self,
      *,
      run_id: str | None = None,
      output_dir: Path | None = None,
  ) -> Path
  ```
  When both are provided, `run()` uses them verbatim and skips its internal `resolve_run_directory` call. When either is `None`, `run()` calls `resolve_run_directory(self.config.evaluation.output_dir)` for the pair (preserving current behaviour). Validation: passing exactly one of the two raises `ValueError` naming which is missing (fail fast, no silent partial-resolve). Backward compatibility: existing callers (`orchestrator.run()` with no kwargs) unaffected.

- `cli/main.py::run` (the `@cli.command()` callback) is restructured to:

  1. Compute `run_id, run_dir = resolve_run_directory(run_config.evaluation.output_dir)` immediately after `run_config = RunConfig(**config_data)`.
  2. Print the start banner via `print_run_start_banner(run_id=run_id, run_dir=run_dir, console=console)`.
  3. Bracket the `with LiveRunDisplay.for_mode(...) as display:` block in a `try:` / `finally:` — inside `try:` the run body (unchanged except that `orchestrator.run(run_id=run_id, output_dir=run_dir)` now passes the pre-resolved pair); in `finally:` compute `duration = time.monotonic() - start_time` and call `print_run_end_banner(run_id=run_id, run_dir=run_dir, duration=duration, success=success, console=console)`, where `success` is a local flag set `True` immediately after `orchestrator.run(...)` returns and defaults to `False`.
  4. Delete the redundant lines 503-504 (`console.print("[bold green]✓ Run complete![/bold green]")` and `console.print(f"Results saved to: {output_dir}")`) — superseded by the end banner.
  5. `emit_artifact_path(output_dir)` remains on the success path only (after the `finally`, outside the `try` — i.e. reachable only when no exception propagated). The failing run's partial `output_dir` is still shown in the end banner's `→ Report:` line; only the stdout artifact-path emission is gated on success. This matches the current behaviour where a raised exception aborts before `emit_artifact_path`.

- The wall-clock timer is bound with `start_time = time.monotonic()` immediately BEFORE `print_run_start_banner` (so the "run duration" in the end banner covers the full time the user waited, including task loading).

**Behaviour to lock:**

- **`unit`** — `tests/unit/test_run_banner_cli_integration.py::TestStartBannerVisible` under `--display=rich` with the recording stub Orchestrator: `result.stderr` contains `"→ Run:"`, `"→ Report:"`, and `"file:///"`. The URL substring in `result.stderr` starts with `file:///` (absolute) — no `file://relative` or bare path.
- **`unit`** — `tests/unit/test_run_banner_cli_integration.py::TestEndBannerVisibleOnSuccess`: `result.stderr` contains `"✓ Run complete in"`, then `"→ Report:"`, then `"→ Browse: tolokaforge browse "`. The order in stderr is: start banner → (any interleaved log / Live output) → end banner → (no more banner lines).
- **`unit`** — `tests/unit/test_run_banner_cli_integration.py::TestEndBannerVisibleOnFailure`: stub Orchestrator raises `RuntimeError("boom")` from `.run()`; `result.exit_code != 0`; `result.stderr` contains `"✗ Run failed in"`, `"→ Report:"`, and `"→ Browse: tolokaforge browse "`. The `RuntimeError` is still surfaced via `result.exception` (banner did not swallow it).
- **`unit`** — `tests/unit/test_run_banner_cli_integration.py::TestBannerSilencedUnderDisplayNone`: `--display=none` → `result.stderr == ""` (banner obeys `console.quiet = True`); `result.stdout` still ends with a single newline-terminated absolute artifact path.
- **`unit`** — `tests/unit/test_run_banner_cli_integration.py::TestOrdering`: extending B1's `test_emit_artifact_path_fires_after_display_exit` recording pattern with `print_run_end_banner` recorded into the same ordering list, assert the sequence is `["__enter__", …, "__exit__", "print_run_end_banner", "emit_artifact_path"]` on success and `["__enter__", …, "__exit__", "print_run_end_banner"]` on failure (no `emit_artifact_path`).
- **`unit`** — `tests/unit/test_run_banner_cli_integration.py::TestOrchestratorReceivesPreResolvedRunId`: `Orchestrator.run` is monkey-patched to a stub that records its `run_id` / `output_dir` kwargs; the recorded values match what `resolve_run_directory` returned before the run, and NOT a fresh timestamp computed inside the orchestrator (proves the pre-resolved pair is threaded through end-to-end).

**Compatibility:** Internal only. `Orchestrator.run(*, run_id=None, output_dir=None)` is a purely-additive kwarg — existing callers unaffected. The two deleted `console.print` lines are internal UI, not a documented surface.

**Deliverable:** Edits to `tolokaforge/cli/main.py` (the `run` callback), `tolokaforge/core/orchestrator.py` (the `Orchestrator.run` signature + fallback call to `resolve_run_directory`), and one new test file `tests/unit/test_run_banner_cli_integration.py`. `tests/unit/test_run_display_cli_integration.py` may need a one-line update if its `_RecordingStubOrchestrator.run` signature does not accept `**kwargs` (it does not today — line 145 of that file is `def run(self) -> Path:`). Update the stub to `def run(self, **_: object) -> Path:` so it swallows the new kwargs without changing observable behaviour.

**Validation:**

- `uv run pytest tests/unit/test_run_banner_cli_integration.py -v`.
- `uv run pytest tests/unit/test_run_display_cli_integration.py -v` (B1 wiring test — must still pass after the stub-signature widen).
- `uv run pytest tests/unit/test_cli_display_flag.py -v` (B2 silencing tests — must still pass; `--display=none` silences the new banners too).
- `uv run pytest tests/unit/test_cli_stdout_contract.py -v` (A4 contract — the `emit_artifact_path` is still the sole stdout write and still runs after the display exits).
- `uv run pytest tests/canonical/test_cli_display_invariants.py -v` (still no ad-hoc Console in the CLI; still no bare `print(` / `sys.stdout.write(` outside `_display.py`).
- End-to-end smoke: `uv run tolokaforge run --config examples/native/custom_grading/run_config.yaml --display=rich` and confirm both banners render with `file://` links and a duration; then `--display=none` and confirm stderr is empty. (No LLM keys needed for `custom_grading`.)

**Doc updates:** None yet (Stage 3).

### Stage 3: Docs + CHANGELOG

**Contract:**

- `docs/CLI.md`:
  - **Rewrite** the `tolokaforge run` row of the `§ stdout / stderr contract` table's stderr column to name the new banner shape as authoritative — e.g. `"Start banner (run-id + file:// report URL), progress, log records, end banner (outcome + duration + file:// report URL + browse invocation)"`. Delete the reference to `"'Run complete' banner, 'Results saved to' line"` (deleted in Stage 2).
  - Add a new section `§ Run banner (\`tolokaforge run\`)` between `§ Display modes` and `§ stdout / stderr contract`. Section content: literal shape of the two banners; the `file://` URL semantics (canonicalised absolute, trailing slash, OSC 8 clickable in supporting terminals); duration format (`MM:SS` under 1h, `HH:MM:SS` above); the `tolokaforge browse <run-id>` suggestion referencing #289 as the source-of-implementation.
  - Do NOT add a "previously the banner said X" note. Document the current shape only (AGENTS.md Core Rule 8).
- `CHANGELOG.md`:
  - Add an `Unreleased → Feat` entry: `**cli**: \`tolokaforge run\` frames every invocation with a start banner (\`→ Run: <run-id>\`, \`→ Report: file:///…/results/<run-dir>/\`) and an end banner (\`✓ Run complete in <duration>\` / \`✗ Run failed in <duration>\`, \`→ Report: …\`, \`→ Browse: tolokaforge browse <run-id>\`) on stderr. \`file://\` URLs are OSC 8-clickable in supporting terminals. Silenced under \`--display=none\`. See [docs/CLI.md](docs/CLI.md) § Run banner. (#281)`.
- **No** update to `AGENTS.md` — A5 introduces no new invariant beyond the existing "shared console only" rule.
- **No** update to `docs/plans/2026-07-15-issue-281-a5-dashboard-url-banner.md` (the plan itself is a journal — Stage 3 completing means the plan is finished, not that it becomes canonical documentation).

**Behaviour to lock:** None new — this stage is docs only. A canonical test asserting the new sub-section exists would over-couple docs to tests; skip it.

**Compatibility:** Docs / CHANGELOG update. `docs/CLI.md § stdout / stderr contract` is user-facing — the rewritten row reads as current-state (no "previously X" wording).

**Deliverable:** Edits to `docs/CLI.md` and `CHANGELOG.md`.

**Validation:**

- `rg -n "Run complete!" docs/ CHANGELOG.md` — should return zero hits after Stage 3 (proves no stale reference to the deleted line).
- `rg -n "Results saved to" docs/ CHANGELOG.md` — same, zero hits after Stage 3.
- `rg -n "tolokaforge browse" docs/CLI.md CHANGELOG.md` — at least one hit each, referencing the invocation string and its #289 provenance.
- Preview `docs/CLI.md` rendering (no tooling change — GitHub markdown preview or an IDE preview is enough).

**Doc updates:** self-referential — this stage IS the doc update.

## Discovered issues

- **Fix in this PR:**
  - Delete lines 503-504 of `tolokaforge/cli/main.py::run` (the two `console.print("[bold green]✓ Run complete![/bold green]")` / `console.print(f"Results saved to: {output_dir}")` calls). Superseded by the A5 end banner. Done in Stage 2 as part of the wiring edit.
  - Extract `format_duration(seconds)` into `_display.py` and migrate `_run_display._format_eta` to consume it. In the neighbourhood — the banner needs the shared shape. Done in Stage 1. (The `main.py::_format_eta` used by `tolokaforge status` is a **different** shape — `1h 5m 30s` — and its migration is out of scope; see #346.)
- **Filed as issues:**
  - **#345** — double-timestamped default run directory (`results/run_{ts1}_{ts2}/`) when `evaluation.output_dir` is absent. Preexisting, unrelated to A5 UI.
  - **#346** — two divergent `_format_eta` helpers with the same name in `_run_display.py` (`MM:SS`/`HH:MM:SS`) and `main.py` (`1h 5m 30s`). A5 consolidates the first; the second's migration is deferred to this issue.

## Risks / open questions

- **Rich `[link=URL]` OSC 8 emission under `Console(record=True)`.** Rich records the hyperlink as an SVG `<a>` element in `export_svg`, and as OSC 8 bytes in `export_text(styles=True)`. Golden regeneration must be run against the same Rich version as CI (locked via `uv.lock` — currently rich `13.x`). If Rich upgrades between the golden write and CI, the golden may drift. Mitigation: `test_run_banner_goldens.py` uses `unique_id="tolokaforge-run-banner"` to freeze the CSS class prefix (same knob B1 uses). This is a known B1 tradeoff, not new.
- **Terminals without OSC 8 support** render the URL as plain underlined-cyan text (theme's `link` token). Still readable and copyable — banner remains useful. No behaviour flag needed.
- **Failure-path banner ordering.** The `finally:` block writes to `console` (stderr); Click subsequently writes its own traceback / `UsageError` to stderr. On the same stream, our banner appears first, then Click's error output. Both are stderr — no interleaving hazard. Verified by inspection of `test_run_display_cli_integration.py::TestFailurePathHandling` which already exercises this pattern for the display's `__exit__`.
- **`--display=log` and `--display=plain` do NOT silence the banner.** They only shape log-line output, not the shared `console`. The A5 banner uses `console.print` and therefore renders under both. This is consistent with A1 / B2 semantics (`log` is a log-line stream mode, not a general silence mode) but worth naming explicitly in `docs/CLI.md § Run banner` so operators know which mode gives which output.
- **`Path.resolve()` behaviour before `output_dir` exists.** The start banner runs BEFORE `orchestrator.run()` creates the directory, so `run_dir.resolve()` on macOS / Linux returns the would-be absolute path (parent must exist; `results/` is created lazily by `output_dir.mkdir(parents=True, exist_ok=True)` inside `orchestrator.run()`). If `results/`'s parent does not exist either, `.resolve()` still returns a plausible absolute path (Python's `pathlib.Path.resolve(strict=False)` is the default). Verify no `FileNotFoundError` under a fresh `tmp_path` in the CLI integration tests (which use `tmp_path / "out"` — parent exists).
- **`resolve_run_directory` uses wall-clock `datetime.now()`.** Two rapid successive `tolokaforge run` invocations in the same second produce the same `run_id` and would collide on `output_dir.mkdir(exist_ok=True)`. Preexisting behaviour of `Orchestrator.run()`; A5 does not make it worse. Filed considerations moved to #345.
