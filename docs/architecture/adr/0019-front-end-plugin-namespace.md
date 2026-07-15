# 0019. Front-end pluggability via `tolokaforge.dx`

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Tolokaforge's front-end — Rich panels, banners, dry-run renderer, and the Click command tree — used to live under `tolokaforge.cli`. Two forces made that layout wrong:

- **The engine is also deployed as a headless server package.** Server installs run `tolokaforge.core.orchestrator.Orchestrator` and never render anything on a terminal. Pulling Rich into the core dependency set for those installs adds bytes, transitive deps, and an unused surface.
- **Roadmap includes non-terminal front-ends.** A web dashboard (server-side UI) and a dedicated Textual TUI have been discussed. Both consume the same per-trial lifecycle stream the terminal panel consumes today (`RunDisplayEvents`); nothing about the CLI package name signals that this pluggability exists.

The engine-side seam is already correct: `RunDisplayEvents` (`tolokaforge/core/run_display_events.py`) is a `@runtime_checkable` Protocol with a `_NullRunDisplayEvents` no-op fixture, and zero `tolokaforge.core.*` files import from `tolokaforge.cli.*`. What was missing:

- A namespace that names the terminal front-end for what it is — one implementation of a swappable role — rather than as *the* CLI.
- A dependency graph that isolates terminal-UI packages behind an opt-in.
- An ADR that records the pattern so a future contributor building `tolokaforge.dx.web` knows the seam to plug into.

## Decision Drivers

- **Signal pluggability at layout time.** A future contributor should read the package layout and see that "front-ends" is a category. The `.cli` name did not communicate that.
- **Headless-server installs must not pull Rich.** Pipeline nodes running `Orchestrator` in the background must be installable without the terminal-front-end dep graph.
- **ADR-0011 Pattern A already applies.** `RunDisplayEvents` meets every criterion: Protocol + null-object fixture + engine emitters + reference consumer + canonical contract test. Codifying that here retires the "seams are aspirational for this component" concern.
- **Reversibility.** A namespace rename is cheap; a separate distribution package is not. The middle path lets us stop at "namespace + extras" and revisit "package split" when a second front-end lands.

## Considered Options

1. **Keep everything in `tolokaforge.cli` and ship the `[dx]` extras split only.** Isolates the dep graph but the package layout still communicates "the CLI" as a single monolith, not "one of possibly many front-ends".
2. **Move to a separate distribution package `tolokaforge-terminal` (or similar) now.** Cleanest isolation. Rejected as premature — no second front-end has landed yet, and shipping a second distribution now adds release, versioning, and CI overhead disproportionate to the current benefit.
3. **Rename `tolokaforge.cli` → `tolokaforge.dx` inside the existing distribution, and move Rich, `click-repl`, and `prompt-toolkit` behind `[project.optional-dependencies].dx`.** Preserves one distribution, one release cadence, one lockfile, while renaming the namespace to signal the pluggability role and isolating the terminal-UI deps. **This ADR.**

## Decision

We adopt **Option 3**.

### The seam and the naming rules

- `RunDisplayEvents` (`tolokaforge/core/run_display_events.py`) is *the* front-end seam. Every front-end consumes this Protocol.
- `_NullRunDisplayEvents` is the deterministic no-op fixture wired as the orchestrator's default sink so callers never branch on `events is None`.
- `tolokaforge.dx` is the namespace for the reference terminal front-end. Sub-packages inside `tolokaforge.dx` are named for the medium they render into:
  - `tolokaforge.dx.cli` — Click command tree.
  - `tolokaforge.dx.live_panel` — the Rich Live progress panel (`LiveRunDisplay`).
  - `tolokaforge.dx.banners` — start / end banners.
  - `tolokaforge.dx.dry_run_render` — dry-run panel renderer.
  - `tolokaforge.dx._display` — shared Rich console + theme + display-mode selector.
  - `tolokaforge.dx.repl` — interactive shell built on `click-repl`.
- Future front-ends: either add a sibling sub-package inside `tolokaforge.dx` (`tolokaforge.dx.web` when the web dashboard lands), or ship as a separate distribution package that registers itself as a `RunDisplayEvents` consumer. Both patterns are ADR-blessed; the choice depends on whether the front-end shares the dep graph with the terminal.

### Extras split

- Rich, `click-repl`, and `prompt-toolkit` live in `[project.optional-dependencies].dx`.
- `click` stays in the base dependency set — the plain-text argument parser is useful in library-only installs (introspection, packaging tests) and has no terminal-UI dep graph.
- The `tolokaforge` console script is served by a stdlib-only shim `tolokaforge._entry:main` that imports `tolokaforge.dx.cli.main.cli` on demand. Headless installs (`pip install tolokaforge`) get a working `tolokaforge` binary; invoking it prints an install hint pointing at `pip install 'tolokaforge[dx]'` and exits 1. The shim keeps zero Rich in the base install path.

### Alignment with ADR-0011 Pattern A

`RunDisplayEvents` meets every criterion in ADR-0011 § Pattern A:

- **Protocol.** `@runtime_checkable class RunDisplayEvents(Protocol)` in `tolokaforge/core/run_display_events.py`.
- **At least two implementations.** Reference production impl `LiveRunDisplay` in `tolokaforge/dx/live_panel.py`; deterministic fixture `_NullRunDisplayEvents` in `tolokaforge/core/run_display_events.py`.
- **Configurable failure knobs.** The fixture is a pure no-op; recording variants used by wiring tests (`_RecordingEvents` in `tests/unit/test_run_display_wiring.py`) extend the fixture pattern to capture call logs.
- **Canonical contract test.** `tests/canonical/test_cli_display_invariants.py` walks the surface and pins the public names of the reference implementation; `tests/unit/test_run_display.py` locks the Protocol's method surface.
- **ADR.** This document.

### What lives in `tolokaforge.dx`

Terminal-front-end code and only terminal-front-end code:

- Rich panels, banners, dry-run renderer.
- The Click command tree (`tolokaforge.dx.cli.*`), including subcommand modules (`docker`, `adapter`, `config`, `assets`).
- Any future terminal-only UI concept (terminal-based dashboards, keybinding managers).

### What does NOT live in `tolokaforge.dx`

- The `RunDisplayEvents` Protocol itself — it is the seam, so it lives in `tolokaforge.core`.
- Any config loader, secret reader, or orchestration entry point that a library consumer would want without a terminal — those live in `tolokaforge.core`.
- Anything imported by the runner, conductor, or gRPC service. Engine-side code must not import from `tolokaforge.dx` at any depth.

## Consequences

### Positive

- The package layout signals pluggability: a contributor sees `tolokaforge.dx` and understands "this is one front-end; another can live alongside".
- Headless-server installs (`pip install tolokaforge`) omit Rich and the terminal-UI dep graph.
- The `RunDisplayEvents` seam gets a documented reference implementation with an explicit position in the codebase.
- Future front-ends (web dashboard, alternative TUI, dedicated dashboard binary) have a well-defined target: implement the Protocol, register into `OrchestratorDeps.events`, and either sit under `tolokaforge.dx.<medium>` or ship as a separate distribution package.

### Negative / Trade-offs

- Contributors adding a new UI concept must decide "does it belong in `tolokaforge.core` or in a front-end?". The Decision section above gives the criteria; the trade-off is a five-minute judgment call at design time.
- A single-package layout still couples the terminal front-end's release cadence to the engine's. When a second front-end lands the follow-up is to consider splitting into distinct distributions.

### Follow-ups

- **Rename `_NullRunDisplayEvents` → `InMemoryRunDisplayEvents`** to match the ADR-0011 fixture-naming discipline (`InMemory{ProtocolName}` prefix). Deferred to keep this stage moves-only; the fixture is not otherwise touched.
- **`tolokaforge.dx.web`** — future web-dashboard front-end. Namespace anticipates it; implementation deferred until product commits.
- **Split `tolokaforge.dx` into its own distribution package** — reconsider when (a) a second front-end lands, or (b) headless-server deploys demand tighter dep isolation than the current extras split provides.

## Rejected alternatives

- **Option 1 (extras split only, no rename).** Loses the pluggability signal. A future contributor reading `tolokaforge.cli` would not know there is a seam to plug into, and adding a peer front-end would look like a rewrite rather than an extension.
- **Option 2 (separate distribution package now).** Premature. The refactor moves nine files and a few docs; splitting into a second distribution multiplies release, versioning, and CI overhead with no immediate consumer.

## Links

- Related ADRs: [0011](0011-seam-and-declaration-conventions.md) (Pattern A — seam definition).
- Related code:
  - Seam: `tolokaforge/core/run_display_events.py`.
  - Reference impl: `tolokaforge/dx/live_panel.py`.
  - Entry shim: `tolokaforge/_entry.py`.
  - Extras: `[project.optional-dependencies].dx` in `pyproject.toml`.
- Related tests: `tests/unit/test_run_display.py`, `tests/canonical/test_cli_display_invariants.py`.
