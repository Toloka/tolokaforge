# 0043. Detached-Mode Grader, Typed Grader Kinds, Adapter Grading Contract

- **Status:** Accepted
- **Date:** 2026-09-04
- **Accepted-on:** 2026-09-04
- **Deciders:** @CiroGamboa
- **Consulted:** UK AISI Inspect AI, OpenAI Evals, METR task-standard, Harbor / Terminal-Bench
- **Milestone:** [#39](https://github.com/Toloka/tolokaforge/milestone/39) (umbrella: TECHDEL-591)
- **Supersedes:** none
- **Extends:** [0040 — Standalone-Grader Substrate](0040-standalone-grader.md)

## Context

ADR-0040 shipped the `GradingSubstrate` Protocol with two operating substrates (`InProcessGradingSubstrate`, `LiveRunnerCallbackGradingSubstrate`) and reserved three future ones. Grading became a pure function of a substrate, and three deployment topologies (aggregate image, independent grader container, trajectory-storage) sat behind one abstraction. The composite dispatch drove five sub-component seams over whichever substrate the topology handed it.

Three surfaces remained open after ADR-0040 shipped:

1. **Offline / cross-region / replay grading.** ADR-0040 reserved `SnapshotGradingSubstrate` as a stub. Kill the runner and the grader had nothing to read — every trial had to be graded live, at trial-end, on a runner that stayed alive long enough. Bulk backfills of already-recorded trials, cross-region regrades, and replay after a runner tear-down were all Python-driving-the-substrate-by-hand operations with no operator surface.

2. **Typed grader kinds.** `grading_method` was `Literal["composite", "test_execution"]`; the test-execution path was a hardcoded `if grading_method == "test_execution":` branch inside `RunnerServiceImpl` that reached into a runner container's `bash test.sh` and read `/logs/verifier/reward.txt` directly. A third grading kind (preference-pair, source-diff, model-graded, terminal-bench-native) needed a framework PR — no plug-in seam existed above the substrate.

3. **Adapter grading contract.** Adapters expressed their grading needs through `AdapterType.NATIVE` string comparisons at four call sites in `_task_loader.py`, two class-identity branches at `orchestrator.py` and `conductor.py`, and one adapter-string reach in `rubric_migration.py`. Every new adapter shape (terminal-bench, harbor-style) needed the framework to sprout another branch.

The invariant ADR-0040 established — "grading is a pure function of a `GradingSubstrate`" — was the right shape to close all three surfaces without introducing a new axis. The right axes were already visible: substrate topology (three shipped, two reserved) × grading kind (registry, not `Literal`) × adapter contract (structural Protocol, not `isinstance`).

## Decision

**Kind × transport is a product, not a matrix cell.** `RunConfig.grader.name` picks the transport (`runner_rpc` / `grader_rpc` / `queue` / `judge_only`) — how the grader reaches the substrate. `RunConfig.grader.snapshot.enabled` picks whether the trial's state materialises as a bundle. `task.grading.grading_method` picks the kind — the typed evaluator that reads the substrate. The three axes are independent; the composite dispatch above the substrate does not know which of the eight combinations is running.

**Ship the third substrate topology.** `SnapshotGradingSubstrate(bundle_view: GradeBundleView)` reads every `GradingSubstrate` method from a v1.0 grade bundle (see [`../GRADE_BUNDLE.md`](../GRADE_BUNDLE.md)). Two methods raise `SubstrateUnreachableError` by design: `db_probe` (bundle format v1.0 carries no probe rows) and `knowledge_search` (bundle format v1.0 carries no queryable KB index). Every other method reads bytes from bundle parts.

**Ship the bundle format and its transport.** `core.grading.bundle` (manifest-first, part-addressable, USTAR tar with sorted entries and fixed mtime, SHA-256 per part, deterministic-JSON with `%.6g` float normalisation, `manifest_digest = sha256(manifest.json)` as canonical name). `BundleStore` Protocol + `LocalDiskBundleStore` + `S3BundleStore` under the new `tolokaforge.bundle_stores` entry-point group. Bundles are content-addressable across storage location moves; external consumers write against the manifest, not against engine code.

**Ship the bundle producer as an opt-in runtime hook.** `RuntimeBackend.build_grade_bundle(trial_id, *, out_dir) -> GradeBundleManifest` on the Protocol (additive; external plugins add a real impl or `NotImplementedError` stub). `SharedStackRuntimeBackend` + `PerTrialRuntimeBackend` compose bundle reads via `LiveRunnerCallbackGradingSubstrate` — no new gRPC surface required. Orchestrator producer seam runs after `_grade` completes, walks the produced bundle on disk, stores via `BundleStore.put`, records outcome on the new `Trajectory.snapshot_status: SnapshotStatus | None` field with 4 outcomes (`STORED` / `OVERSIZE` / `PRODUCE_FAILED` / `UNGRADED`).

**Ship the typed grader-kind registry.** `tolokaforge.grader_kinds` entry-point group with kwargs-only `GraderKind.evaluate(*, substrate, task_config, kind_config, trial_id, agent_tools, logger) -> Grade | None`. Two built-ins: `composite` (reference impl over `CompositeFold`) and `test_execution` (moves off the runner-side inline branch onto `SubstrateService.RunTestSuite` + a `GraderKindRefusedError`-narrow refusal). `RunnerServiceImpl._dispatch_via_grader_kind` routes every non-composite method through the registry via `functools.partial` kwargs binding. `test_execution` grading against a snapshot substrate refuses actionably (`SubstrateUnreachableError` → `GradingFailedError` with named message) — bundle format v1.0 carries no test-suite hook.

**Ship the adapter grading contract.** `tolokaforge.adapters.grading_contract.AdapterGradingContract` `typing.Protocol` matched structurally. Capability flags on `BaseAdapter` (`requires_docker_cli_in_runner`, `grades_from_task_grading_file`, `syncs_adapter_env_to_state`) with default `False`. Every `AdapterType == NATIVE` branch replaced by an `adapter.method()` delegation or a `getattr(adapter, capability_flag, False)` check. `AdapterGradingContractSuite` at `tolokaforge/testing/adapters/grading_contract.py` ships as the shared conformance test — external adapters subclass it.

**Ship the regrade CLI.** `tolokaforge grade <bundle-uri> --grader-kind <k> --out <dir>` regrades a single stored bundle. `tolokaforge grade-run <run-dir> --with-kind <k> --out <dir>` batches: walks `<run-dir>/trials/*/*/trajectory.yaml`, filters trials where `snapshot_status.outcome == STORED`, dispatches each through the same in-process pipeline. Regrade is a first-class op decoupled from any `RunConfig` — the bundle is the grading contract.

**Fold logic lives once.** `CompositeFold.finalise(...)` in `core.grading.composite_fold` is the single fold callsite — three sanctioned callers (runner service, grader composite dispatch, composite kind), locked by `tests/canonical/test_fold_defined_once.py`. Both dispatchers reduce component scores through the same reduction; a fourth caller silently re-collapses the seam.

**Sub-component seams stay pure.** Runner-side implementations of `evaluate_jsonpath_checks` and `evaluate_db_probes` moved to `core.grading.jsonpath_evaluators` and `core.grading.db_probes`. Composite package split by concern (`composite/{state_checks, transcript_rules, trace_checks, llm_judge, custom_checks}.py`); trace-check operators split into a per-cluster package (`trace_checks/{constraints/, evaluator, dispatch, matcher, bindings, resolver, truth}`). `.importlinter` grows contracts: `no-runner-reach-from-core-grading`, `no-pb2-reach-from-core-grading`, `composite-fold-purity`, `filesystem-view-purity`, `bundle-library-purity`, `bundle-store-purity`, `bundle-producer-purity`, `grader-kinds-purity` — the negative space of grading purity is now enforced mechanically.

**Opt-in property.** `SnapshotBundleConfig.enabled` defaults to `False`. `GraderConfig.expose_substrate` defaults to `False`. `tests/canonical/test_grader_defaults.py` locks both defaults mechanically — any PR flipping either fails CI until it edits the lockfile explicitly. Every existing task pack and run config runs untouched.

## What ships and what stays follow-up

**Ships this milestone:**

- `SnapshotGradingSubstrate` real impl (issue #1353).
- `core.grading.bundle` format v1.0 library — reader, producer, manifest schema (#1354).
- `BundleStore` Protocol + `LocalDiskBundleStore` + `S3BundleStore` + `tolokaforge.bundle_stores` entry-point group (#1355).
- `RuntimeBackend.build_grade_bundle` opt-in hook + `SnapshotBundleConfig` + orchestrator producer seam + `Trajectory.snapshot_status` field (#1356).
- Grader wire v3 additive fields (`bundle_manifest_json`, `bundle_parts_uri`) — additive, no consumer wired (#1357).
- `tolokaforge.grader_kinds` typed registry + `GraderKind` Protocol + `CompositeGraderKind` + `TestExecutionGraderKind` + `GraderKindRefusedError` + `SubstrateService.RunTestSuite` RPC + `_dispatch_via_grader_kind` (#1358).
- `tolokaforge grade` + `tolokaforge grade-run` CLI verbs (#1359).
- Parity gate 3 lanes + opt-in defaults lock (#1360).
- `AdapterGradingContract` Protocol + capability flags + `NativeAdapter` migrations + `AdapterGradingContractSuite` — Phase A (#1339–#1345).
- `CompositeFold` pure library + `trace_timeline` + `filesystem_view` + jsonpath/db_probes runner-side kill + `SubstrateService.RunDbProbe` RPC + `composite.py` split + `trace_checks.py` split — Phase B (#1346–#1352).

**Stays follow-up:**

- Grader-side dispatch reading `bundle_manifest_json` / `bundle_parts_uri` off the wire and constructing `SnapshotGradingSubstrate` at grade time — follow-up #1468.
- `RunConfig.grader.kind` run-level override for the kind × transport product — follow-up #1464.
- Composite kind runtime dispatch (composite path stays runner-side inline this milestone) — follow-up #1465.
- Per-task `kind_config` plumbing on `RunnerGradingConfig` — follow-up #1467.
- Retire `tolokaforge.grading_methods` marker registry once every consumer moves to `tolokaforge.grader_kinds` — follow-up #1466.
- Bundle format v1.1 sidecar for pre-materialised `db_probes.json` + `test_execution_result.json` — future ticket.
- `RunTestSuite.stdout` truncation-signal wire field — follow-up #1469.
- Queue-transport dispatch for `grade-run` (needs grader-side kind dispatch) — folded into #1468.
- `AGENTS.md` and `.importlinter` fences on the new negative space — this ADR ships them.

## Consequences

**Positive**

- Third substrate topology (offline / cross-region / replay) is a shipped path, not a reserved stub.
- Grading kind is a plug-in seam — a fourth kind adds one entry-point line and one class implementing `GraderKind`.
- Adapter grading needs are expressed structurally; a new adapter shape adds capability flags and Protocol method overrides, no framework PR to `_task_loader.py` or `orchestrator.py`.
- Regrade is a first-class operator op — CI machines can bulk-regrade a run's stored bundles without a live runner, and cross-region regrades ride the same CLI.
- Fold logic lives once — `test_fold_defined_once.py` catches a copy-paste of the fold into a fourth dispatcher.
- Sub-component purity is enforced by `.importlinter` — the negative-space of "grading does not reach into runner internals" is a build-time gate, not a review-time hope.
- Byte-parity 10-pack gate expanded to three lanes (Lane A `runner_rpc` vs `grader_rpc`; Lane B `runner_rpc` vs `grader_rpc + snapshot=on`; Lane C regrade-parity property — 3 sequential replays against frozen bundle bytes produce byte-identical `Grade`). Lane C's "same inputs → same outputs" property is the offline-grading contract.
- Opt-in defaults (`snapshot.enabled=False`, `expose_substrate=False`) are mechanical CI locks — no shipped task pack or run config auto-migrates.

**Negative**

- Bundle production adds one wire hop per snapshot-enabled trial (via `LiveRunnerCallbackGradingSubstrate`); measured cost is one substrate-read round trip per part. Documented in `docs/RUNNER.md § Snapshot bundle mode`; `SnapshotBundleConfig.max_bundle_mb` caps size with fallback to live-callback.
- `SnapshotGradingSubstrate` refuses `db_probe` + `knowledge_search` offline. `state_checks_db_probes_only` is un-gradable offline in bundle format v1.0; the refusal is a contract, not a bug. Bundle format v1.1 sidecar unlocks it.
- `test_execution` grading against a snapshot substrate refuses actionably (`GraderKindRefusedError`); no test-suite hook rides bundle format v1.0. Same v1.1 sidecar shape unlocks it.
- Composite kind ships as a reference impl fold-wrapper reading pre-computed component scores from `kind_config["components"]`; runtime dispatch through it is #1465. Existing composite path (runner-side inline through `_grade_trial_async`) is untouched — byte-parity locked by Lane A of the 10-pack.
- `Trajectory.model_dump(mode="json")` carries wall-clock timestamps into `trajectory.json`; bundle bytes across sessions are not byte-identical on the trajectory part. Grade equality holds because no grader-side code reads `trajectory.json`. A future bundle-digest lockfile would need `Message.ts` pinning.
- The two entry-point groups (`tolokaforge.grading_methods` + `tolokaforge.grader_kinds`) coexist by dual registration; `RegisterTrial` validates against both. A downstream adapter registering in only the older group fails at trial registration — documented in CHANGELOG as the migration. Retirement of the marker group is #1466.
- Grader wire v3 additive fields (`bundle_manifest_json`, `bundle_parts_uri`) ship without a consumer this milestone; the grader-side dispatch that reads them is #1468. External adapters compiling `grader.proto` themselves see the two new fields — additive, ignorable.

## Alternatives considered

- **Snapshot-on-wire (Harbor pattern).** Wire the whole state into `GradeRequest`. Reject: coding tasks have ~100 MB workspaces; the wire budget is set by grpc frame size and gateway-side proto size caps. Bundle-with-URI decouples wire size from state size.
- **Trajectory-storage-first.** Ship the trajectory-storage substrate before snapshot. Reject: trajectory-storage is in development inside Toloka and would gate this milestone on a service outside the framework's control. The bundle format is designed to be trajectory-storage-consumable — same manifest, different reader — when it ships (see `docs/GRADE_BUNDLE.md § Storage-agnostic layout`).
- **Grader-side dispatch reading the wire v3 fields this milestone.** Wire the fields AND wire the reader. Reject: shipping dead fields is scope-creep the milestone's ticket sequencing avoided (#1357 landed the wire without a consumer; #1468 wires the reader in a separate milestone). "Wire-runs-ahead-of-runner-population" is the shipped pattern (see `docs/GRADER_SERVICE.md § Wire evolution`).
- **Co-located `test.sh` (Terminal-Bench pattern).** Grading lives inside the trial's env container. Reject: the whole point of the substrate abstraction is re-runnability against a bundle after the runner is gone; grading inside the trial container is the opposite direction. Terminal-Bench-shaped grading enters as a registered `test_execution` kind that reads the substrate — not container-embedded grading.
- **Model-graded kind (`--kind model_judge`) in this milestone.** Extend the built-ins beyond `composite` + `test_execution`. Reject: the model-judge shape is a follow-up milestone; this milestone shipped the seam so registration is a one-line entry-point.
- **METR "score gets a submission string" model.** Narrow the grading input to a single submission string. Reject: our submission is a workspace snapshot, not a string; the state-checks / transcript / trace / judge / custom-checks surface reads the full trial.

## Follow-ups

Open at ADR authorship (2026-09-04):

- **#1453** — grader-side dispatch reading `bundle_manifest_json` / `bundle_parts_uri` and building `SnapshotGradingSubstrate` per-trial (blocks byte-parity Lane B's production-wire variant; monkeypatched today).
- **#1454** — per-trial live-callback fallback when snapshot produce fails or bundle exceeds cap.
- **#1455** — run-summary snapshot bundle produce stats.
- **#1456** — snapshot-mode compatibility on external terminal_bench adapter runtime.
- **#1457** — probe S3 credentials via `head_bucket` at run-start.
- **#1464** — `RunConfig.grader.kind` run-level override.
- **#1465** — migrate composite runner-side path onto `CompositeGraderKind.evaluate`.
- **#1466** — retire `tolokaforge.grading_methods` entry-point group once every consumer moves.
- **#1467** — task-level `kind_config` plumbing on `RunnerGradingConfig`.
- **#1468** — grader-side dispatch through kinds (queue-transport `grade-run` blocked on this).
- **#1469** — truncation-signal field on `RunTestSuite.stdout` wire cap.
- **#1477** — fix pre-existing wrong-ticket ref in ADR-0040 doc (unrelated audit trail).
- **#1479** — validate/quote `script_path` + `reward_path` bash interpolation in `RunTestSuite` (defence for #1467 landing).

## Cross-references

- Extends: [ADR-0040 — Standalone-Grader Substrate](0040-standalone-grader.md).
- Bundle format: [`docs/GRADE_BUNDLE.md`](../GRADE_BUNDLE.md).
- Grader service: [`docs/GRADER_SERVICE.md`](../GRADER_SERVICE.md).
- Grading dispatch: [`docs/GRADING.md`](../GRADING.md).
- Adapter grading contract: [`docs/ADAPTER_INTERFACE.md § AdapterGradingContract`](../ADAPTER_INTERFACE.md#adaptergradingcontract).
- Runner snapshot mode: [`docs/RUNNER.md § Snapshot bundle mode`](../RUNNER.md#snapshot-bundle-mode).
- Milestone: [#39 Grader v3](https://github.com/Toloka/tolokaforge/milestone/39).
