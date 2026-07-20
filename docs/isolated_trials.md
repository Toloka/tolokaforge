# Isolated trials

This guide explains what per-trial isolation buys you, when to use it,
and how it is decided. It covers the per-service declaration in the task
manifest that drives backend selection, the deprecated CLI/config
overrides, and a worked example you can run against an unchanged example
task pack.

For the underlying protocol and materialisation lifecycle see
[`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md).
For the tradeoff analysis behind the two backends see
[ADR-0016](adr/0016-runtime-backend-comparison.md); for the
provisioning contract see
[ADR-0010](adr/0010-runtime-backend-provisioning-contract.md).

## What "isolated trial" means

Every trial is a run of a single task by a single agent. Two runtime
backends decide how the trial's docker substrate is scoped:

- **`shared`** (default): one docker stack materialises at run start.
  Every trial in the run hits the same containers. When trial 1
  finishes, its DB rows, filesystem edits, and service state all live on
  and are visible to trial 2. Fast — the stack starts once.
- **`per_trial`**: a **fresh** docker stack materialises for each
  trial. New containers, new network, new volumes, fresh fixture state.
  When the trial ends the stack is torn down and everything in it is
  gone. Trial 2 starts from scratch.

Per-trial isolation is the strong guarantee. It's what lets a task
mutate its environment (write DB rows, edit files, POST to services)
without polluting any other trial's grading.

## When to use it

Label a task's services `reset`/`ephemeral` — so the run materialises a
fresh stack per trial — when **any** of the following is true:

- The task's grading depends on state the trial itself created (a row
  the agent wrote, a file it edited, a message it posted).
- The task's fixtures mutate during the trial (bin-mounted data the
  agent modifies, a DB that accepts writes).
- Two trials of the same task could otherwise see each other's residue
  through a shared service.
- You measure `pass@k` and need every retry to be genuinely
  independent.

Leave a task's services `shared` — so the run stays on the shared stack
— when **all** of the following are true:

- Grading only inspects the agent's output (a written file, a returned
  string).
- Every service the task touches is read-only or fully idempotent-on-
  reset for the duration of the run.
- The trials do not race on any shared state that could cause
  non-determinism.

The shared stack is where the task-driven selector lands when no service
requires isolation, because the cold-start cost is real: every trial in
a per-trial run pays the cost of `docker compose up` again. On a task
whose stack takes 30s to reach healthy, a 100-trial run costs an extra
~50 minutes of wall clock in materialisation alone. See [Cost](#cost)
below.

## How isolation is decided

Isolation is decided by the *task*, not opted into by the operator.
Every compose service in a task's manifest carries an isolation label
via `services.<name>.isolation` — `shared`, `reset`, or `ephemeral` (a
service with no manifest entry defaults to `ephemeral`). Backend
selection is task-driven and automatic: any `reset`/`ephemeral` service
on any task in the run routes the whole run onto per-trial
materialisation, and a run whose services are all `shared` (or whose
tasks carry no manifest at all) stays on the shared stack.

A task declares its per-service labels in `task.yaml`:

```yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
  services:
    db:
      isolation: ephemeral
```

That declaration is the whole mechanism. There is nothing to switch on
at run time — the orchestrator reads every task's manifest, and if any
service needs a fresh substrate it selects the per-trial backend that
satisfies it. The normal path never refuses to start; it auto-selects
the satisfying backend.

### Deprecated overrides

Two surfaces force a backend regardless of the task-driven signal, each
emitting a `DeprecationWarning`. They exist for edge cases —
backwards-compatibility, forcing a shared stack while profile-testing,
or forcing per-trial on a manifest-less pack to observe isolation
behaviour (exactly what the worked example below does).

**Config file.** Set `orchestrator.runtime` in the run config YAML:

```yaml
orchestrator:
  runtime: per_trial          # or "shared"
```

**CLI flag.** `--runtime` overrides whatever is in the config:

```bash
uv run tolokaforge run \
  --config examples/native/coding/run_configs/dev.yaml \
  --runtime per_trial
```

Forcing a *shared* backend against a task set that requires per-trial
materialisation is the one path that refuses to start. The orchestrator
rejects the conflict at startup with a `RuntimeError` naming the
offending task(s):

```
Runtime backend SharedStackRuntimeBackend shares state across every
trial in the run, but 2 task(s) require per-trial materialisation via
their `services.<name>.isolation` labels: ['orders_customers_join_01',
'support_triage_01']. These tasks would silently produce wrong
verdicts on a shared-stack backend.
  Fix: drop the deprecated `orchestrator.runtime` override so backend
  selection is task-driven, or label every service `isolation: shared`
  on the task(s) that genuinely tolerate shared state across trials.
```

The refusal is deliberate — silent cross-trial contamination is a
failure mode where the grader believes it, so making it a startup error
rather than a subtle grading bug is the safer default. Drop the override
and the task-driven selector picks the satisfying backend on its own.

## Worked example

This example forces the backend with the deprecated `--runtime`
override (see [Deprecated overrides](#deprecated-overrides)) precisely
to observe isolation behaviour on a manifest-less pack — it is not the
normal selection path. `--runtime per_trial` works on any task pack,
with or without a task-declared manifest. When the task has no manifest,
the engine's built-in stack (`runner` + `db-service`) is what gets
materialised per trial.

Take the smallest existing example (`examples/native/coding`, no
manifest) and run it under each backend:

```bash
# Default — shared backend. One stack, both trials share it.
uv run tolokaforge run \
  --config examples/native/coding/run_configs/dev.yaml

# Same tasks, per-trial backend. Fresh stack per trial.
uv run tolokaforge run \
  --config examples/native/coding/run_configs/dev.yaml \
  --runtime per_trial
```

While the second run is executing, in a separate shell:

```bash
docker compose ls | grep tolokaforge
```

You'll see one compose project per active trial (named with the
trial's identifier), and each project's containers, network, and
volumes disappear when the trial ends. Contrast with the shared run,
where a single compose project stays up for the entire run.

Grading verdicts should be identical between the two runs for this
example — the coding task doesn't mutate state, so isolation doesn't
change the outcome. What changes is startup cost (per-trial pays it
`n_trials` times) and the level of guarantee (per-trial trials can't
see each other, even in principle).

## Cost

Per-trial materialisation is not free. On a laptop with warm image
caches:

- **Built-in stack** (`runner` + `db-service`): 3–5s per trial.
- **`multi_service`** (adds nginx serving static JSON): 4–6s per
  trial.
- **`multi_service_postgres`** (adds postgres + PostgREST): 30–45s
  on the first trial (postgres has to initialise the schema), 5–8s on
  subsequent trials (image is cached).

The engine tears down each trial's stack after the trial completes.
Concurrency `orchestrator.workers > 1` compounds the cost — each
worker materialises its own stack — but also parallelises it.

If your task genuinely doesn't need `per_trial`, don't use it. If it
does, budget for the wall-clock cost.

## Interaction with multi-container tasks

Isolation and multi-container are orthogonal properties of the task, not
operator choices: isolation follows the task's per-service labels, and
multi-container follows whether the task declares extra services. All
four combinations exist:

| Task | Runtime | Behaviour |
| --- | --- | --- |
| No manifest, `shared` | Engine built-in stack once per run | Fastest |
| No manifest, `per_trial` | Engine built-in stack per trial | Isolation on the defaults |
| Task manifest, `shared` | Task-declared compose once per run | Task-specific services, shared |
| Task manifest, `per_trial` | Task-declared compose per trial | Task-specific services, isolated |

See [`MULTI_CONTAINER_GUIDE.md`](MULTI_CONTAINER_GUIDE.md) for the
task-manifest side of that matrix, and
[ADR-0018](adr/0018-multi-container-under-shared-runtime.md)
for the full case analysis.

## Further reading

- [`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md)
  — the `RuntimeBackend` protocol, provisioning lifecycle, sequence
  diagrams for each backend.
- [ADR-0007](adr/0007-runtime-backend-protocol.md)
  — the seam.
- [ADR-0010](adr/0010-runtime-backend-provisioning-contract.md)
  — per-trial provisioning contract.
- [ADR-0016](adr/0016-runtime-backend-comparison.md)
  — shared vs per-trial tradeoff analysis.
- [`docs/TASKS.md`](TASKS.md#multi-container-environments-environment_manifest)
  — reference schema for `services.<name>.isolation` and the other
  manifest fields.
