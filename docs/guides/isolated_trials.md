# Isolated trials

This guide explains what per-trial isolation buys you, when to use it,
and how to opt in. It covers the CLI, the run config, and the task-level
declaration, plus a worked example you can run against an unchanged
example task pack.

For the underlying protocol and materialisation lifecycle see
[`docs/architecture/RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md).
For the tradeoff analysis behind the two backends see
[ADR-0016](../architecture/adr/0016-runtime-backend-comparison.md); for the
provisioning contract see
[ADR-0010](../architecture/adr/0010-runtime-backend-provisioning-contract.md).

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

Choose `per_trial` when **any** of the following is true:

- The task's grading depends on state the trial itself created (a row
  the agent wrote, a file it edited, a message it posted).
- The task's fixtures mutate during the trial (bin-mounted data the
  agent modifies, a DB that accepts writes).
- Two trials of the same task could otherwise see each other's residue
  through a shared service.
- You measure `pass@k` and need every retry to be genuinely
  independent.

Choose `shared` when **all** of the following are true:

- Grading only inspects the agent's output (a written file, a returned
  string).
- Every service the task touches is read-only or fully idempotent-on-
  reset for the duration of the run.
- The trials do not race on any shared state that could cause
  non-determinism.

Default is `shared` because the cold-start cost is real: every trial in
a per-trial run pays the cost of `docker compose up` again. On a task
whose stack takes 30s to reach healthy, a 100-trial run costs an extra
~50 minutes of wall clock in materialisation alone. See [Cost](#cost)
below.

## How to opt in

Three surfaces, in ascending order of precedence:

**1. Config file (per-run default).** Set `orchestrator.runtime` in the
run config YAML:

```yaml
orchestrator:
  runtime: per_trial          # or "shared"
```

**2. CLI flag (override for this invocation).** `--runtime` overrides
whatever is in the config, so you can flip an existing config without
editing it:

```bash
uv run tolokaforge run \
  --config examples/native/coding/run_config.yaml \
  --runtime per_trial
```

**3. Task-level declaration (mandatory requirement).** A task that
cannot tolerate shared state declares its requirement in
`task.yaml`:

```yaml
environment_manifest:
  compose_file: "./environment.compose.yaml"
  runner_service: "runner"
  isolation: "per_trial"
```

This is a **hard requirement**: if the run's backend can't satisfy it,
the orchestrator refuses to start. You'll see a startup-time
`RuntimeError` naming the offending task(s):

```
Runtime backend SharedStackRuntimeBackend shares state across every
trial in the run, but 2 task(s) declare
`environment_manifest.isolation: per_trial`: ['support_triage_01',
'orders_customers_join_01']. These tasks would silently produce wrong
verdicts on a shared-stack backend.
  Fix: select a per-trial runtime backend in the run config (e.g.
  PerTrialRuntimeBackend), or set `isolation: shared_ok` on the task(s)
  that genuinely tolerate shared state across trials.
```

The refusal is deliberate — silent cross-trial contamination is a
failure mode where the grader believes it, so making it a startup error
rather than a subtle grading bug is the safer default.

## Worked example

`--runtime per_trial` works on any task pack, with or without a
task-declared manifest. When the task has no manifest, the engine's
built-in stack (`runner` + `db-service`) is what gets materialised per
trial.

Take the smallest existing example (`examples/native/coding`, no
manifest) and run it under each backend:

```bash
# Default — shared backend. One stack, both trials share it.
uv run tolokaforge run \
  --config examples/native/coding/run_config.yaml

# Same tasks, per-trial backend. Fresh stack per trial.
uv run tolokaforge run \
  --config examples/native/coding/run_config.yaml \
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

Isolation and multi-container are orthogonal — you choose them
independently. All four combinations exist:

| Task | Runtime | Behaviour |
| --- | --- | --- |
| No manifest, `shared` | Engine built-in stack once per run | Fastest |
| No manifest, `per_trial` | Engine built-in stack per trial | Isolation on the defaults |
| Task manifest, `shared` | Task-declared compose once per run | Task-specific services, shared |
| Task manifest, `per_trial` | Task-declared compose per trial | Task-specific services, isolated |

See [`multi_container_tasks.md`](multi_container_tasks.md) for the
task-manifest side of that matrix, and
[ADR-0018](../architecture/adr/0018-multi-container-under-shared-runtime.md)
for the full case analysis.

## Further reading

- [`docs/architecture/RUNTIME_BACKENDS.md`](../architecture/RUNTIME_BACKENDS.md)
  — the `RuntimeBackend` protocol, provisioning lifecycle, sequence
  diagrams for each backend.
- [ADR-0007](../architecture/adr/0007-runtime-backend-protocol.md)
  — the seam.
- [ADR-0010](../architecture/adr/0010-runtime-backend-provisioning-contract.md)
  — per-trial provisioning contract.
- [ADR-0016](../architecture/adr/0016-runtime-backend-comparison.md)
  — shared vs per-trial tradeoff analysis.
- [`docs/TASKS.md`](../TASKS.md#multi-container-environments-environment_manifest)
  — reference schema for `environment_manifest.isolation` and the other
  manifest fields.
