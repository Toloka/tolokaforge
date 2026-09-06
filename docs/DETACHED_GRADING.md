# Detached-Mode Grading — Operator Guide

Grade a trial **offline** — no live runner required — by producing a **grade bundle** during the run and grading it later with the `tolokaforge grade` CLI.

The three axes are independent:
- **`grader.name`** — transport (`runner_rpc`, `grader_rpc`, `queue`, `judge_only`).
- **`grader.snapshot.enabled`** — whether the trial's state materialises as a bundle. **Default: `false`. Opt in per run.**
- **`task.grading.grading_method`** — the typed kind that reads the substrate (`composite`, `test_execution`).

Design record: [ADR-0043](adr/0043-detached-mode-grader-and-typed-grader-kinds.md).

## When to use detached mode

Turn snapshot mode on when the run's grading is expensive to re-run OR needs to happen after the runner is gone:
- **Bulk backfill** — regrade a completed run with a new rubric / kind / model without re-executing the agent.
- **Cross-region replay** — bundle produced in region A, graded in region B.
- **Post-mortem** — the runner has been torn down; the bundle keeps grading independent.
- **Regression triage** — a rubric change scored differently than expected — regrade the same bundles across 3 sequential replays and diff.

Leave it OFF for typical runs. Bundles cost wire (~one substrate-read round trip per part) and storage (~10 KB to a few MB per trial). The 3-lane byte-parity gate locks that snapshot-mode grading is byte-identical to live grading for the eight snapshot-gradable pack shapes.

## Two-step lifecycle

Detached grading is a **two-step** operator flow:

1. **Produce** — during the run, opt into snapshot mode. The runner materialises a `grade bundle` at trial-end and stores it in a `BundleStore`. The trial's `trajectory.yaml` records where the bundle landed under `snapshot_status:`.
2. **Grade** — later, invoke `tolokaforge grade <bundle-uri>` (single trial) or `tolokaforge grade-run <run-dir>` (batch) to regrade against the stored bundle.

The bundle is the wire between the two steps. Bundles are portable across storage locations, bit-extractable by external tools (`jq` + `sha256sum` + `tar` are enough), and content-addressable (`sha256(manifest.json)` is the canonical name).

## Step 1 — Produce the bundle during a run

Add a `snapshot:` block to `grader:` in your `run_config.yaml`. Both flags below are OPT-IN.

```yaml
grader:
  name: grader_rpc                 # any transport; grader_rpc / runner_rpc are common
  expose_substrate: true           # REQUIRED with snapshot: the producer composes reads via SubstrateService
  snapshot:
    enabled: true                  # OPT-IN — default false
    max_bundle_mb: 32.0            # soft cap; over-cap bundles are discarded (SnapshotStatus.oversize)
    fallback_on_oversize: live_callback   # the trial keeps its live-callback grade — no data loss
    store:                         # discriminated union — LocalDisk OR S3
      type: local_disk             # or type: s3
      root_dir: /var/tolokaforge/grade_bundles
```

Or with S3:

```yaml
    store:
      type: s3
      bucket: my-eval-bundles
      prefix: grade_bundles         # optional; bundles land under s3://bucket/prefix/<content-hash>/
```

**Startup gate.** The orchestrator refuses `snapshot.enabled=true` at run-start when:
- The backend does not implement `RuntimeBackend.build_grade_bundle` (raises `NotImplementedError`), OR
- `grader.expose_substrate` is `false` (the producer needs `SubstrateService` to compose reads).

An actionable error names both preconditions.

**Backend support** (out of the box):
- `SharedStackRuntimeBackend` — supported.
- `PerTrialRuntimeBackend` — supported (delegates to the shared-stack impl).
- `InMemoryRuntimeBackend` — opts out via `NotImplementedError`.
- External `tolokaforge.runtime_backends` plugins — the Protocol extension is additive; a plugin either implements or raises `NotImplementedError` (safe opt-out).

### What ends up on disk

After a snapshot-mode run finishes, every trial's `trajectory.yaml` carries a `snapshot_status:` block:

```yaml
# <output_dir>/run_<timestamp>/trials/<task_id>/<trial_index>/trajectory.yaml
task_id: reconcile_ledger
trial_index: 0
...
snapshot_status:
  outcome: stored
  uri: bundle://local_disk/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  bundle_size_bytes: 42317
```

Four outcome shapes:

| `outcome` | Meaning | Side data |
|---|---|---|
| `stored` | Bundle produced + stored. Regrade-ready. | `uri`, `bundle_size_bytes` |
| `oversize` | Bundle exceeded `max_bundle_mb`; discarded. Trial kept its live-callback grade. | `bundle_size_bytes`, `cap_bytes`, `reason` |
| `produce_failed` | Producer raised; trial kept its live-callback grade. | `reason` |
| `ungraded` | Trial ended before grading. No bundle to produce. | — |
| `null` | Snapshot mode was off for this trial. | — |

The bundle itself lives at the resolved store location, e.g. `/var/tolokaforge/grade_bundles/e3b0c442.../`:

```
grade_bundle_<trial_id>/
├── manifest.json               # schema_version, per-part SHA-256 digests, trial_id
├── initial_state.json          # trial-start state (canonical JSON, sort_keys=True, %.6g floats)
├── final_state.json            # trial-end state
├── final_state_stable.json     # final state with unstable fields normalised
├── filesystem.tar              # workspace snapshot (USTAR, deterministic entries)
├── trajectory.json             # agent messages + tool calls + LLM turns
├── grading_config.json         # the grading block from the task pack
├── checks/                     # optional; per-check bytes (custom-check payloads)
│   ├── manifest.json
│   └── <check-name>/…
└── kb/                         # optional; per-hit KB payloads
```

Full format spec: [`docs/GRADE_BUNDLE.md`](GRADE_BUNDLE.md).

## Step 2 — Grade the bundle with the CLI

### Single-trial regrade

```bash
uv run tolokaforge grade \
    bundle://local_disk/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 \
    --grader-kind composite \
    --grader-config kind_config.yaml \
    --store-config store.yaml \
    --out ./regrade-output
```

The verb resolves the URI via the `tolokaforge.bundle_stores` registry, wraps the loaded bundle in a `SnapshotGradingSubstrate`, dispatches the kind through the `tolokaforge.grader_kinds` registry, and writes `regrade-output/grade.json`.

**Flags:**

| Flag | Required? | Meaning |
|---|---|---|
| `<bundle-uri>` | Yes (positional) | `bundle://<store-name>/<content-hash>` — the store name resolves via `load_bundle_store()`; the hash is `sha256(manifest.json)`. |
| `--grader-kind <k>` / `-k` | Yes | The grader-kind name (registered under `tolokaforge.grader_kinds`). No default — every kind runs against a different substrate profile, and a silent default that misbehaves is worse than a required arg. Unknown → `BadParameter` naming the registered set. |
| `--grader-config <path.yaml>` | Optional | Kind-specific config as a YAML mapping. Passed verbatim to `evaluate(kind_config=…)`. The kind validates it internally via its own Pydantic model (`extra="forbid"`). |
| `--store-config <path.yaml>` | Optional | `BundleStoreBackend` discriminated-union (`type: local_disk` OR `type: s3`) YAML. **Absent → `LocalDiskBundleStore(root_dir=cwd)`**. S3 always requires the flag. |
| `--out <dir>` | Yes | Output directory. Must be empty or non-existent — refuses to overwrite existing artifacts. `grade.json` lands under it. |

**Exit codes:**

| Exit | Meaning |
|---|---|
| `0` | Grade written to `<out>/grade.json`. |
| `1` | Kind refused (e.g. `test_execution` against snapshot substrate — no `test.sh` in bundle v1.0), substrate unreachable, grading failed, kind returned no verdict, or `--out` not empty. |
| `2` | Bad argument (unknown kind, malformed URI, store-name mismatch, malformed YAML). |

### Batch regrade across a completed run

```bash
uv run tolokaforge grade-run \
    ./results/custom_checks_example/run_2026-09-04T13-42-11 \
    --with-kind composite \
    --grader-config components.yaml \
    --store-config store.yaml \
    --out ./regrades/2026-09-04
```

`grade-run` walks `<run-dir>/trials/*/*/trajectory.yaml`, filters trials where `snapshot_status.outcome == stored`, and dispatches each through the same in-process pipeline as the single-trial verb. Output lands at `<out>/<task>/<idx>/grade.json` mirroring the source layout.

**Per-trial console output** (via the shared display):

```
regraded reconcile_ledger/0 → ./regrades/2026-09-04/reconcile_ledger/0/grade.json
skip     reconcile_ledger/1 — bundle oversize (40000000 > 33554432)
skip     reconcile_ledger/2 — no snapshot_status recorded (run predates snapshot mode?)
skip     reconcile_ledger/3 — trial ended before grading
failed   reconcile_ledger/4 — Substrate unreachable: SnapshotGradingSubstrate cannot serve db_probes offline (dsn=…)

Regraded: discovered 5, regraded 1, skipped 3, failed 1
```

Exit `0` iff no dispatched trial failed. Skips are non-error.

**Store constructed ONCE across the batch;** substrate + bundle materialisation happen per-trial (each URI resolves into its own `TemporaryDirectory`).

## Example — end-to-end walk-through

Uses the shipped `examples/native/custom_checks/` reference pack. Requires an
LLM provider key in `.env`.

### 1. Copy the pack's run config and add the `grader.snapshot` block

The shipped `examples/native/custom_checks/run_config.yaml` grades live and
does not stash a bundle. Copy it, then add the two-line `grader:` block that
opts the run into snapshot mode:

```yaml
# my_run_config.yaml — the shipped example plus snapshot mode.
models:
  agent:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.0
    max_tokens: 4096
  user:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4-6"
    temperature: 0.2

orchestrator:
  workers: 1
  repeats: 1
  max_turns: 6
  queue_backend: sqlite

evaluation:
  projects:
    - "examples/native/custom_checks/dataset"
  tasks_glob: "**/task.yaml"
  output_dir: "results/custom_checks_example"

grader:                          # NEW — opts the run into snapshot mode
  name: grader_rpc               # any transport works
  expose_substrate: true         # REQUIRED with snapshot
  snapshot:
    enabled: true
    max_bundle_mb: 32.0
    fallback_on_oversize: live_callback
    store:
      type: local_disk
      root_dir: ./results/grade_bundles
```

`grading_method: composite` is the shipped default on
`RunnerGradingConfig` in the task pack's `grading.yaml`, so no per-task
change is needed for the reference pack.

### 2. Run the trial:

```bash
scripts/with_env.sh uv run tolokaforge run --config my_run_config.yaml
```

### 3. Inspect the produced bundle:

```bash
$ yq '.snapshot_status' \
    results/custom_checks_example/run_*/trials/reconcile_ledger/0/trajectory.yaml
outcome: stored
uri: bundle://local_disk/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
bundle_size_bytes: 42317

$ ls results/grade_bundles/e3b0c442*/
manifest.json  initial_state.json  final_state.json  final_state_stable.json
filesystem.tar  trajectory.json  grading_config.json  checks/
```

### 4. Regrade later (LLM keys required if the kind touches a judge):

```bash
$ cat > store.yaml <<'EOF'
type: local_disk
root_dir: ./results/grade_bundles
EOF

$ scripts/with_env.sh uv run tolokaforge grade \
    bundle://local_disk/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 \
    --grader-kind composite \
    --grader-config components.yaml \
    --store-config store.yaml \
    --out ./regrades/first-pass

regraded bundle://local_disk/e3b0c442… → ./regrades/first-pass/grade.json

$ jq '.score, .binary_pass, .reasons' ./regrades/first-pass/grade.json
0.87
true
"custom_checks: 0.87 (reconcile_ledger matched)"
```

Note that `composite` currently needs a pre-computed `components:` map in
`--grader-config` (see § Known limits below and [#1465](https://github.com/Toloka/tolokaforge/issues/1465)); a `components.yaml` for the run above:

```yaml
components:
  custom_checks: 0.87
```

The primary composite path — the runner-side `_grade_trial_async` fold that
ran during step 2 — is unchanged. CLI-driven end-to-end composite regrade
(dispatching each sub-component against the substrate before folding) is
[#1465](https://github.com/Toloka/tolokaforge/issues/1465).

## Known limits (bundle format v1.0)

**Two substrate methods raise `SubstrateUnreachableError` on a snapshot substrate:**

- `db_probe(dsn, query)` — bundle v1.0 carries no pre-materialised probe rows. The `db_probes` state-check backend surfaces this as a `FAIL:` reason line rather than propagating; a task grading via `db_probes` on a snapshot substrate loses that component's score. Bundle v1.1 will pre-materialise probe rows (see [#1439](https://github.com/Toloka/tolokaforge/issues/1439)).
- `knowledge_search()` — bundle v1.0 carries raw KB bytes but no queryable index. Returns `None` (treated by the judge as "the trial declared no KB"). Bundle v1.1 will carry an indexed snapshot (see [#1438](https://github.com/Toloka/tolokaforge/issues/1438)).

**`test_execution` grading kind refuses offline on a snapshot substrate.** Bundle v1.0 carries no `test.sh` hook. The kind raises `GraderKindRefusedError`; the CLI exits `1` with an actionable message. Use `composite` for offline regrade until bundle v1.1 pre-materialises test-suite results.

**Composite kind requires pre-computed sub-component scores today** (`--grader-config` with `components: {…}`) — full sub-component dispatch through the kind is [#1465](https://github.com/Toloka/tolokaforge/issues/1465). The primary composite path lives in the runner-side `_grade_trial_async`; it works untouched.

**Regrade byte-parity holds only for kinds whose substrate reads all succeed offline.** The parity 10-pack gate covers 8 of 10 packs (`state_checks_db_probes_only` and `hash_and_all_four` refuse actionably). See [`tests/canonical/test_grader_parity_reference.py`](../tests/canonical/test_grader_parity_reference.py) for the coverage matrix.

## Extending — adding a new grader kind

Register a class that implements `GraderKind` under `tolokaforge.grader_kinds`:

```toml
# In your package's pyproject.toml
[project.entry-points."tolokaforge.grader_kinds"]
my_custom_kind = "my_package.grading:MyCustomGraderKind"

[project.entry-points."tolokaforge.grading_methods"]
my_custom_kind = "my_package.grading:MyCustomGradingMethod"
```

Both entry-points are required for `RegisterTrial` to accept the name (dual-registration; the marker registry validates the task-declared method at ingest; the kind registry drives runtime dispatch).

```python
from typing import ClassVar, Mapping, Any
from tolokaforge.core.grading.kinds import GraderKindRefusedError
from tolokaforge.core.grading.substrate import GradingSubstrate, SubstrateUnreachableError
from tolokaforge.core.models import Grade, GradeComponents
from tolokaforge.core.models.grading_method import GradingMethod
from tolokaforge.runner.models import RunnerGradingConfig
from tolokaforge.core.logging import StructuredLogger

class MyCustomGraderKind:
    NAME: ClassVar[str] = "my_custom_kind"

    def evaluate(
        self,
        *,
        substrate: GradingSubstrate,
        task_config: RunnerGradingConfig,
        kind_config: Mapping[str, Any] | None,
        trial_id: str,
        agent_tools: Mapping[str, Any],
        logger: StructuredLogger,
    ) -> Grade | None:
        try:
            final_state = substrate.final_state()
        except SubstrateUnreachableError as exc:
            # Substrate transport failure — propagate (dispatcher decides).
            raise
        # Your grading logic here.
        score = evaluate_my_criterion(final_state, kind_config)
        return Grade(
            binary_pass=score >= 0.5,
            score=score,
            components=GradeComponents(custom_checks=score),
            reasons=f"my_custom_kind: score={score:.2f}",
        )

# The marker Protocol keeps the older grading_methods registry happy.
class MyCustomGradingMethod:
    NAME: ClassVar[str] = "my_custom_kind"
```

Now `task.grading.grading_method: my_custom_kind` is a valid selector, and `tolokaforge grade <uri> --grader-kind my_custom_kind` dispatches through your class.

## Where to look next

- [ADR-0043](adr/0043-detached-mode-grader-and-typed-grader-kinds.md) — accepted-record for the substrate/kind/transport product.
- [ADR-0040](adr/0040-standalone-grader.md) — `GradingSubstrate` Protocol.
- [`docs/GRADE_BUNDLE.md`](GRADE_BUNDLE.md) — bundle format v1.0 spec (manifest, parts, digests, deterministic-serialisation rules).
- [`docs/RUNNER.md § Snapshot bundle mode`](RUNNER.md#snapshot-bundle-mode) — the producer side, config detail.
- [`docs/GRADER_SERVICE.md`](GRADER_SERVICE.md) — the grader seam this all sits behind.
- [`docs/GRADING.md`](GRADING.md) — the composite grading contract every kind stands on.

## Follow-up tickets that change this surface

- **[#1439](https://github.com/Toloka/tolokaforge/issues/1439)** — bundle v1.1 with pre-materialised db_probes rows (unlocks `state_checks_db_probes_only` offline).
- **[#1438](https://github.com/Toloka/tolokaforge/issues/1438)** — bundle v1.1 indexed KB snapshot.
- **[#1465](https://github.com/Toloka/tolokaforge/issues/1465)** — composite kind runtime dispatch (removes the pre-computed-components requirement).
- **[#1467](https://github.com/Toloka/tolokaforge/issues/1467)** — task-level `kind_config` plumbing on `RunnerGradingConfig`.
- **[#1468](https://github.com/Toloka/tolokaforge/issues/1468)** — grader-side dispatch through kinds (unblocks queue-transport `grade-run`).
- **[#1453](https://github.com/Toloka/tolokaforge/issues/1453)** — production wire-driven substrate selection (Lane B production variant currently uses a monkeypatch).
