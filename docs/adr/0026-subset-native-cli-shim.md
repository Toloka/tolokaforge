# 0026. Subset-native CLI shim — bridging the ADR-0024 exec surface and the ADR-0025 partition

- **Status:** Proposed
- **Date:** 2026-08-04
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

M15 execution surfaced a contradiction between two Accepted ADRs. [ADR-0024 § "Documented docker exec subcommands"](0024-container-command-surface.md#documented-docker-exec-subcommands) commits **`tolokaforge --version`** and **`tolokaforge run-trial`** as `docker exec` entry points on the published runner image; the keyless rc-smoke test suite gates promotion on both. [ADR-0025 § "The module partition"](0025-runner-wheel-split.md#the-module-partition) places `tolokaforge/_entry.py` (the console-script shim) and all of `tolokaforge/dx/` (the Click command tree) in "Not in the runner subset (base wheel only)". The runner-subset build target added in [PR #828](https://github.com/Toloka/tolokaforge/pull/828) further omits `[project.scripts]` from the subset wheel entirely, because there is no CLI entrypoint code in the subset to bind them to.

Result: the runner image built from the subset wheel installs cleanly but `docker exec tolokaforge-runner tolokaforge --version` fails with `executable file not found in $PATH`. The gRPC contract on `:50051` and the healthcheck are preserved, but the two committed exec subcommands from ADR-0024 vanish.

The static AST closure from `_entry.py` + `dx/cli/main.py` + `dx/cli/run_trial_command.py` reaches 77 additional first-party files (all of `adapters/`, all of `dx/`, `core/orchestrator.py`, `core/run_trial.py`, `core/plugin_registry.py`, `core/output/`, `core/compose_materialisation.py`, `core/shared_stack_runtime.py`, `core/per_trial_runtime.py`, `docker/`, `core/evaluators/`). Extending the subset to swallow that closure would erase the point of the subset.

## Decision Drivers

- **Preserve the ADR-0024 committed contract without gutting the ADR-0025 partition.** Both ADRs remain valid as-written; this ADR adds the bridging surface between them.
- **The gRPC contract on `:50051` is the runner container's primary operator surface** ([ADR-0022 § Surface 1](0022-runtime-independence.md#surface-1--entry-point-protocol-registries-536) and the runner's proto). The exec subcommands are a **secondary** surface — a convenience for external harnesses that prefer stdin/stdout wire-driven trials to a raw gRPC client. Preserving both surfaces in the subset means providing a subset-native CLI that talks to the local gRPC service, not shipping the orchestrator's in-process trial runner.
- **`--version` is a light diagnostic**; `run-trial` is a wire-protocol subprocess (ADR-0022 § Surface 3). Both can be satisfied by a small subset-native module without touching the orchestrator or the adapters.
- **Reversibility.** If the subset CLI shim turns out to be more maintenance than it's worth, ADR-0025's partition can be broadened later, or ADR-0024's exec surface narrowed. Neither is easier if we don't first ship the honest fix now.

## Considered Options

1. **Subset-native CLI shim (this ADR).** Add `tolokaforge/runner/_cli.py` inside the runner subset. `--version` reads the subset wheel's own installed version via `importlib.metadata`. `run-trial` reads the [ADR-0022 § Surface 3](0022-runtime-independence.md#surface-3--tolokaforge-run-trial-subprocess-wire-format-538) JSON-Lines `start` envelope on stdin and drives a single trial by orchestrating in-process against the **local runner service on `localhost:50051`** — using components already in the runner subset (`core.llm.client`, `core.loop.ToolCallingLoop`, `runner_pb2_grpc`) rather than the orchestrator's task-driven-backend-selection machinery. The subset builder emits `[project.scripts]` `tolokaforge = tolokaforge.runner._cli:main`.
2. **Amend ADR-0024** to narrow the runner image's committed exec surface to remove `tolokaforge --version` and `tolokaforge run-trial`. Retire the corresponding rc-smoke tests. Cheapest, but breaks the operator contract for anyone driving trials via `docker exec run-trial`.
3. **Extend ADR-0025's partition to swallow the CLI closure.** Bring `_entry.py` + adapters + dx + orchestrator + run_trial + friends into the subset. Roughly restores the base wheel; erases the milestone's slim-image goal.
4. **Shim-as-stub.** Add a subset-native `_cli.py` where `--version` works but `run-trial` only emits a well-formed `error` envelope on any input. Preserves the rc-smoke's error-path assertion but breaks the operator use case genuinely — a hollow contract preservation.

## Decision

We adopt **Option 1** — the subset-native CLI shim that actually drives trials against the local runner service.

### The shim

A new module **`tolokaforge/runner/_cli.py`** lands in the runner subset. It exposes a `main()` function bound to the subset wheel's `[project.scripts]` entry `tolokaforge = tolokaforge.runner._cli:main`. Two subcommands, matching the ADR-0024 committed surface verbatim:

- **`tolokaforge --version`.** Prints the installed subset-wheel version resolved via `importlib.metadata.version("tolokaforge-runner-subset")`. When invoked inside the runner image, the reported version identifies the subset build; when invoked (hypothetically) outside the image where the subset wheel is not installed, the metadata lookup fails loudly — no silent misreport.
- **`tolokaforge run-trial`.** Implements ADR-0022 § Surface 3 verbatim: JSON-Lines on stdin/stdout, `"v":1`, one `start` message then EOF, one `result` or `error` envelope out, standard exit codes and SIGTERM handling. Internally it:
  1. Parses the `start` envelope into `TaskConfig` + `models` (post-#818 shared spine).
  2. Constructs an LLM client via `core.llm.client` (in the subset).
  3. Constructs a `ToolCallingLoop` from `core.loop` (in the subset).
  4. Instantiates a gRPC client against `localhost:50051` (the local runner service — `runner_pb2_grpc` in the subset) and wires it as the loop's tool executor and grader.
  5. Drives the loop through the trial, calls `GradeTrial`, assembles a `TrialResult`.
  6. Writes the JSON-Lines `result` envelope on stdout, exits 0.

The shim is **thin by design.** It reuses the runner subset's existing gRPC client machinery, `ToolCallingLoop`, and LLM abstractions. It does **not** import from `tolokaforge/_entry.py`, `dx/cli/*`, `adapters/*`, or `core/orchestrator.py`. The subset partition (`RUNNER_SUBSET_PACKAGES` + `LOOSE_FILES` + `EXCLUDED_FILES`) grows by exactly one file: the shim itself.

### What ADR-0024 keeps promising

Every element of [ADR-0024 § "Documented docker exec subcommands"](0024-container-command-surface.md#documented-docker-exec-subcommands) is preserved by the shim:

- `tolokaforge --version` returns a version string (now the subset wheel's version, which tracks the base wheel's version in locked-step within a release — no consumer-visible drift).
- `tolokaforge run-trial` accepts a JSON-Lines `start` envelope on stdin, emits `result` or `error` on stdout, honours SIGTERM, exits with documented codes. The **wire format is bit-for-bit ADR-0022 § Surface 3.**
- `tolokaforge run-trial` remains `hidden=True` in interactive `--help` (a machine protocol, deliberately). The shim's Click / argparse tree honours the hidden convention.

### What ADR-0025 keeps promising

The partition remains as ADR-0025 § "The module partition" wrote it:

- `tolokaforge/_entry.py` and `tolokaforge/dx/*` stay **base-wheel only**. Not in the subset.
- The subset's runtime graph still passes the negative import-boundary invariant test.
- The `RUNNER_SUBSET_PACKAGES` deliverable from [#820](https://github.com/Toloka/tolokaforge/issues/820) gains one file (`tolokaforge/runner/_cli.py`) via `RUNNER_SUBSET_LOOSE_FILES`; the packages list is otherwise unchanged.

Options 2, 3, and 4 are rejected:

- **Option 2** breaks the operator contract for real users of `docker exec run-trial`. That surface exists precisely because JSON-Lines is easier than protobuf-aware gRPC for language-agnostic and shell-driven harnesses; amending it away costs those users a real capability.
- **Option 3** brings the CLI closure's 77 files into the subset. The subset would be within ~10% of the base wheel by file count; the M15 slim-image goal evaporates.
- **Option 4** satisfies the letter of the rc-smoke by making `run-trial` always error. It preserves nothing an operator actually needs. Do not ship a contract we've already hollowed out.

## Consequences

### Positive

- ADR-0024's committed exec surface is preserved on the subset image with no observable change to operators or the rc-smoke.
- ADR-0025's partition stands as written; the "slim runner image" goal is preserved (the subset gains one file, not 77).
- The runner-image consumer gets a **more honest** CLI: `run-trial` inside the subset now clearly routes through the *local* runner service via gRPC, rather than reconstructing an orchestrator in-container. That matches what the runner image actually IS.
- Third-party harnesses driving trials via `docker exec tolokaforge-runner tolokaforge run-trial` continue to work.

### Negative / Trade-offs

- **A small chunk of new code lands in the runner subset.** `_cli.py` will grow to include ADR-0022 § Surface 3 wire framing, an LLM-loop driver, and gRPC client wiring. Bounded (single file, no new dependencies) but non-trivial.
- **Trial-execution semantics inside `docker exec run-trial` are *narrower* than the base wheel's `tolokaforge run-trial`.** The subset shim orchestrates in-process against the local gRPC runner; it cannot spin up compose stacks, cannot switch backends, cannot exercise adapter-specific setup. Documented in `docs/STANDALONE_RUNNER.md`. For any operator today driving a trial that touches those surfaces via `docker exec run-trial`, this is a regression — but the runner container never had the machinery for those things anyway, so any pre-M15 `docker exec run-trial` invocation that "worked" was accidentally using in-container orchestrator scaffolding that was neither documented nor guaranteed.
- **The subset wheel now declares a `[project.scripts]` entry.** The base wheel already has `tolokaforge = tolokaforge._entry:main`; the subset wheel declares `tolokaforge = tolokaforge.runner._cli:main`. Installing both wheels in one Python environment would collide on the console script — but M15's whole point is that they never install in the same environment (the subset is Docker-only; the base wheel is PyPI-only for users).

### Follow-ups

**Code changes required (all in one PR, `feat/822-dockerfile-subset-v2`):**

- Add `tolokaforge/runner/_cli.py` per the § Decision above.
- Update `scripts/hatch/hatch_runner_subset_builder.py` to emit `[project.scripts]` `tolokaforge = tolokaforge.runner._cli:main` in the subset wheel's metadata.
- Update `tolokaforge/core/_runner_subset.py` to include `tolokaforge/runner/_cli.py` in `RUNNER_SUBSET_LOOSE_FILES` (or `PACKAGES` if `_cli.py` grows into a small subpackage — implementer's judgment).
- Multi-stage `tolokaforge/docker/dockerfiles/runner.Dockerfile` per the original #822 scope, installing the subset wheel from the builder stage.
- Extend `tests/canonical/test_runner_subset_partition.py` with subset-CLI structural assertions (the shim exists; `[project.scripts]` binds it; the shim does NOT import from `dx/`, `adapters/`, or `core.orchestrator`).
- Fold in [#830](https://github.com/Toloka/tolokaforge/issues/830)'s data-files omission (`pricing.json`, `model_presets.yaml`) since the subset builder is being rebuilt.

**Documentation to update:**

- `docs/STANDALONE_RUNNER.md` — document the subset-native CLI shim's `run-trial` semantics (routes to local gRPC; not equivalent to base-wheel `tolokaforge run-trial`).
- `docs/RUNNER.md` — cross-reference this ADR from the "Runner subset" section.
- `docs/ROADMAP.md` — no change until release event.

**Tests to add:**

- Structural canonical tests as listed above.
- Integration test: `docker exec tolokaforge-runner tolokaforge --version` returns the subset wheel's version.
- Integration test: `docker exec tolokaforge-runner tolokaforge run-trial < a-real-start-envelope.jsonl` returns a well-formed `result` envelope for a bundled mock-graded example.
- Existing rc-smoke tests (`test_runner_version_matches_tag`, `test_runner_run_trial_speaks_wire`) must continue to pass unchanged.

## Links

- Related ADRs:
  - [ADR-0022 § Surface 3](0022-runtime-independence.md#surface-3--tolokaforge-run-trial-subprocess-wire-format-538) — the wire the subset shim honours.
  - [ADR-0023](0023-runner-image-internals.md) — image internals uncommitted; the subset shim IS the kind of internal change this ADR reserved room for.
  - [ADR-0024 § "Documented docker exec subcommands"](0024-container-command-surface.md#documented-docker-exec-subcommands) — the committed surface the shim preserves.
  - [ADR-0025 § "The module partition"](0025-runner-wheel-split.md#the-module-partition) — the partition the shim respects; the shim adds one file to the subset via `RUNNER_SUBSET_LOOSE_FILES`.
- Related code:
  - [`tolokaforge/runner/_cli.py`](../../tolokaforge/runner/_cli.py) — new file, lands with the implementation PR.
  - [`scripts/hatch/hatch_runner_subset_builder.py`](../../scripts/hatch/hatch_runner_subset_builder.py) — updated to emit `[project.scripts]` in the subset.
  - [`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) — multi-stage subset install.
- Related issues:
  - [GH #822](https://github.com/Toloka/tolokaforge/issues/822) — the Dockerfile rewire ticket this ADR unblocks.
  - [GH #830](https://github.com/Toloka/tolokaforge/issues/830) — data-files omission, folded into the same PR.
  - [GH #622](https://github.com/Toloka/tolokaforge/issues/622) — M15 umbrella.
