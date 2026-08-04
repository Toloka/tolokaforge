# Standalone Runner Guide

The tolokaforge runner — the piece that owns per-trial container lifecycle,
executes agent tool calls, grades outcomes, and returns typed results — can be
used as a component on its own, without the batch orchestrator, without a
YAML config file, and (via the subprocess CLI) without Python. This guide is
for consumers who want to embed the runner in their own code rather than run
a whole benchmark through `tolokaforge run`.

If you want the batch flow instead (prepare / worker / status against a queue,
distributed workers across machines), see [RUNNER.md](RUNNER.md). The two guides
cover different tools that live in the same repo; you rarely need both at once.

## When this guide is for you

- You have your own agent loop, scheduler, or research pipeline, and you want
  tolokaforge to execute + grade one trial at a time.
- You want to drive the runner from a language that isn't Python.
- You want to compare models or task variants in a small sweep without
  standing up the full batch machinery.
- You want to plug in your own runtime backend, grader, or conductor without
  editing tolokaforge.

## Mental model — three layers, three surfaces

The runtime is three Protocol seams stacked on top of one another. Each
Protocol has one or more built-in implementations; a downstream `pip install`
can add more via entry-point registration.

```
                                    +--------------------------------------+
your code / your language  <------> |  three consumer surfaces             |
                                    |                                      |
                                    |  * tolokaforge.runner.run_trial(...) |
                                    |    (Python library entry)            |
                                    |                                      |
                                    |  * tolokaforge run-trial              |
                                    |    (subprocess CLI, JSON-Lines wire) |
                                    |                                      |
                                    |  * importlib.metadata entry points   |
                                    |    (register your own Protocol impl) |
                                    +---------------+----------------------+
                                                    |
                                    +---------------v----------------------+
                                    |  three Protocol seams                |
                                    |                                      |
                                    |  Conductor    — runs one trial:      |
                                    |                  agent loop, grading |
                                    |  TrialGrader  — turns a trajectory   |
                                    |                  into a Grade        |
                                    |  RuntimeBackend — provisions the     |
                                    |                    substrate         |
                                    +---------------+----------------------+
                                                    |
                                    +---------------v----------------------+
                                    |  container / process substrate       |
                                    |  (Docker today; K8s/Modal/E2B via    |
                                    |   registered plug-ins in future)     |
                                    +--------------------------------------+
```

The three consumer surfaces all compose the same Protocol seams — they differ
in how *you* reach them, not in what the runner does underneath.

## Which surface should you use

| Your situation | Reach for |
|---|---|
| Python codebase, want in-process control, care about types | `tolokaforge.runner.run_trial(...)` |
| Non-Python control plane (Rust / Go / TypeScript / shell) | `tolokaforge run-trial` subprocess |
| Want to add a new runtime backend, grader, or conductor | Entry-point registries |
| Want isolation between trials (crash containment, per-trial resource caps) | `tolokaforge run-trial` subprocess, one process per trial |
| Want the lowest per-trial overhead | `tolokaforge.runner.run_trial(...)`, in-process |
| Want to run 10,000 trials with distributed workers | Not this guide — use [RUNNER.md](RUNNER.md) (batch mode) |

## Published images

The four first-party images are published to Docker Hub, so a host with only
Docker installed can `docker pull` them instead of building from a repo
checkout:

| Image | Docker Hub repository | Role |
|---|---|---|
| runner | `docker.io/tolokasoft1/tolokaforge-runner` | the runner gRPC service (this guide's subject) |
| db-service | `docker.io/tolokasoft1/tolokaforge-db-service` | JSON state store the runner and tasks read/write |
| rag-service | `docker.io/tolokasoft1/tolokaforge-rag-service` | retrieval service backing the `search_kb` judge tool |
| mock-web | `docker.io/tolokasoft1/tolokaforge-mock-web` | deterministic web fixtures for browser tasks |

All four share one coordinated semver tag axis:

| Tag | Kind | Points at |
|---|---|---|
| `:X.Y.Z` | immutable | an exact release (e.g. `:1.4.0`) |
| `:X.Y.Z-rc.N` | immutable | a release candidate (e.g. `:1.4.0-rc.1`) |
| `:X.Y` | moving | the latest patch of a minor line (e.g. `:1.4`) |
| `:latest` | moving | the newest stable release |

Pin an immutable `:X.Y.Z` tag for reproducible runs; `:X.Y` and `:latest` track
forward. Release candidates publish under `:X.Y.Z-rc.N` and never move `:latest`.

```bash
docker pull docker.io/tolokasoft1/tolokaforge-runner:latest
docker pull docker.io/tolokasoft1/tolokaforge-db-service:latest
```

What the published image guarantees about its *internals* is deliberately
narrow: the stable contract is the image name + tag axis above plus the command
surface below, not how the image is composed inside
([ADR-0023](adr/0023-runner-image-internals.md)).
[`deploy/standalone/docker-compose.yaml`](../deploy/standalone/docker-compose.yaml)
wires all four `tolokasoft1/tolokaforge-*` images into a single stack: `docker
compose up` from that directory stands them up on one network, reading LLM
provider keys from a sibling `.env` (copy
[`deploy/standalone/.env.example`](../deploy/standalone/.env.example)) and
selecting the image tag via `TOLOKAFORGE_IMAGE_TAG` (`latest` once the first
image publish lands, `local` for locally-built images). `make docker-up` from a
checkout remains the build-from-source composed-stack path.

## Command surface of the published runner image

Running `tolokasoft1/tolokaforge-runner` is a committed contract, recorded in
[ADR-0024](adr/0024-container-command-surface.md). The elements below are stable
across releases — a change to any of them is a breaking, versioned change; the
image's internal layout is not
([ADR-0023](adr/0023-runner-image-internals.md)).

**Default entrypoint — the gRPC runner service on `:50051`.** The image has no
`ENTRYPOINT`; its default command is `python -m tolokaforge.runner`, which
starts the Runner gRPC service bound to `[::]:50051` (the image `EXPOSE`s
`50051`). Running the image with no arguments runs the service.

**Healthcheck.** The image self-reports health via a Docker `HEALTHCHECK` that
probes gRPC channel readiness on `localhost:50051` (not an HTTP endpoint).
`docker inspect --format '{{.State.Health.Status}}'` reaching `healthy` is the
committed "service is up" signal.

**Environment contract.** The container is wired through these variables:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TOLOKAFORGE_SECRETS_JSON` | no | unset | JSON credential map. When set, it bootstraps the `SecretManager` singleton at container start; when unset, the manager lazy-inits from the `EnvProvider` / `.env` on first secret read. |
| `DB_SERVICE_URL` | no | `http://localhost:8000` | URL of the db-service. A wrong or unreachable URL fails loud on first call. |
| `RAG_SERVICE_URL` | no | *(none — honest absence)* | URL of the rag-service. Present iff a rag-service is running; unset means the runner builds no RAG client and offers no `search_kb` tool. |

`RUNNER_PORT`, `LOG_LEVEL`, and `MAX_WORKERS` are operational tuning knobs with
defaults (`50051` / `INFO` / `10`); they are not part of the committed wiring
contract.

**Documented `docker exec` subcommands.** Two CLI subcommands are committed for
programmatic `docker exec` against a running container:

- `tolokaforge run-trial` — runs one trial over the JSON-Lines wire (see
  [Wire format quick-reference](#wire-format-quick-reference)): one `start`
  envelope on stdin, one terminal `{"v":1,"type":"result"|"error",…}` envelope
  on stdout. It is hidden from interactive `tolokaforge --help` **by design** —
  a machine-facing wire protocol, documented here for programmatic use, not
  surfaced in the human command list.
- `tolokaforge --version` — prints the installed subset wheel's version;
  a stable version probe for a running container.

A `tolokaforge config-dump` command does **not** exist and is not part of the
committed surface; it is reserved and tracked as
[#626](https://github.com/Toloka/tolokaforge/issues/626).

**Subset-native CLI shim ([ADR-0026](adr/0026-subset-native-cli-shim.md)).**
Inside the published runner image, `tolokaforge` binds to a subset-native shim
(`tolokaforge.runner._cli:main`), *not* the base wheel's
`tolokaforge._entry:main`. The two ADR-0024 subcommands above are preserved
verbatim; other operator ergonomics differ from a base-wheel install:

- **`tolokaforge --version`** reports the subset wheel's version. In a
  release build the subset wheel and the base wheel are cut from the same
  commit and carry the same version literal, so external consumers see no
  drift; the substring the rc-smoke gate asserts against (the tagged base
  version) matches.
- **`tolokaforge run-trial`** is *narrower* than the base wheel's
  `run-trial`. The subset-native driver orchestrates in-process against the
  local runner gRPC service on `localhost:50051`; it **cannot** spin up
  compose stacks, switch backends, or exercise adapter-specific setup —
  the adapter machinery (`tolokaforge.adapters.*`, `tolokaforge.core.run_trial`,
  runtime-backend factories) is base-wheel-only per [ADR-0025](adr/0025-runner-wheel-split.md).
  A `start` envelope whose task shape needs adapter processing surfaces as
  a well-formed `{"v":1,"type":"error","error_type":"ProvisionError",…}`
  wire response, naming the base wheel's
  `tolokaforge.core.run_trial.run_trial(…)` or the runner service's raw
  `RegisterTrial` gRPC as the right entry.
- **No other CLI subcommands** are available inside the image. Commands
  that live under the base wheel's `tolokaforge/dx/cli/*` — `tolokaforge run`,
  `tolokaforge adapter`, `tolokaforge docker …` — are not part of the
  runner image and produce a click "No such command" error. Drive those
  from a host-side base-wheel install.

## Quickstart

Two things you need on the host either way:

1. **An LLM provider key** in a `.env` file at the repo root (e.g.
   `OPENROUTER_API_KEY=sk-…`). The same bootstrap `tolokaforge run` uses.
2. **A live runner container.** `make docker-up` builds and starts the
   `tolokaforge-runner:local` container plus the small support stack from a
   repo checkout. This is the substrate the trial runs inside; both surfaces
   below dispatch through it. To skip the build, `docker pull` a pinned tag
   from Docker Hub instead — see [Published images](#published-images) — or
   stand the four published images up together with no checkout via
   [`deploy/standalone/docker-compose.yaml`](../deploy/standalone/docker-compose.yaml).

Then pick a surface.

### Standalone quickstart — pull, compose up, drive, tear down

The cold-start path for a host that has only Docker: stand the four published
images up with [`deploy/standalone/docker-compose.yaml`](../deploy/standalone/docker-compose.yaml),
drive one real trial to a graded `TrialResult`, and tear the stack down. The
example driver lives in the tree, so this path assumes a repo checkout; only the
*images* are pulled (or built), not the harness.

> **Architecture.** The published images are `linux/amd64` only (arm64 is a
> follow-up). The compose recipe defaults `platform` to `linux/amd64` on every
> service, so `docker compose up` works on Apple-Silicon (arm64) Macs too —
> Docker Desktop pulls the amd64 image and runs it under emulation (rag-service
> is heavy and is noticeably slower emulated). The pin is overridable: to run
> locally-built native arm64 `:local` images (from `make docker-build` on Apple
> Silicon) without emulation, set `TOLOKAFORGE_PLATFORM=linux/arm64` (e.g.
> `TOLOKAFORGE_PLATFORM=linux/arm64 docker compose up`). For any manual
> `docker pull` / `docker run` outside compose, pass `--platform linux/amd64` or
> export `DOCKER_DEFAULT_PLATFORM=linux/amd64`.

1. **Get the four images.** Pull the published tag, or build them locally.

   ```bash
   # Published images (Docker Hub):
   export TOLOKAFORGE_IMAGE_TAG=latest
   docker pull tolokasoft1/tolokaforge-runner:latest
   docker pull tolokasoft1/tolokaforge-db-service:latest
   docker pull tolokasoft1/tolokaforge-rag-service:latest
   docker pull tolokasoft1/tolokaforge-mock-web:latest
   ```

   Or build them from the checkout and run the stack against those instead.
   `make docker-build` builds the four images content-hash-tagged
   (`tolokaforge-<component>:<hash>`), so retag the latest of each under the
   recipe's `tolokasoft1/…:local` names — the compose recipe references
   `tolokasoft1/tolokaforge-*:${TOLOKAFORGE_IMAGE_TAG}` — so
   `TOLOKAFORGE_IMAGE_TAG=local` resolves:

   ```bash
   # Locally-built images:
   export TOLOKAFORGE_IMAGE_TAG=local
   make docker-build
   for c in runner db-service rag-service mock-web; do
     ref=$(docker images --filter "reference=tolokaforge-$c" --format '{{.Repository}}:{{.Tag}}' | head -1)
     docker tag "$ref" "tolokasoft1/tolokaforge-$c:local"
   done
   ```

2. **Set a provider key.** Copy the example env file and add a key for the
   provider the trial's models use.

   ```bash
   cd deploy/standalone
   cp .env.example .env
   # edit .env: set e.g. OPENROUTER_API_KEY=sk-...
   ```

3. **Bring the stack up.** `docker compose` reads `TOLOKAFORGE_IMAGE_TAG` (and
   the sibling `.env`) from this directory.

   ```bash
   docker compose up -d --wait
   ```

4. **Drive one trial.** The [example driver](../deploy/standalone/examples/drive_one_trial.py)
   serialises a bundled task pack with the public `tolokaforge.runner.load_task`,
   copies its file assets into the runner, and drives `tolokaforge run-trial` over
   the exec wire. It prints the grade.

   ```bash
   python examples/drive_one_trial.py
   ```

   See [`examples/README.md`](../deploy/standalone/examples/README.md) for the
   driver's host prerequisites and model overrides.

5. **Tear the stack down.**

   ```bash
   docker compose down -v
   ```

### From Python — `tolokaforge.runner.run_trial(...)`

The library entry is a single keyword-only function. It takes a `TaskConfig`, a
models dict, and returns a typed `TrialResult`. No config file, no
`Orchestrator`, no filesystem side effects unless you pass an `output_dir`.

```python
from tolokaforge.runner import load_task, run_trial
from tolokaforge.secrets import init_default

init_default()  # reads .env into the singleton SecretManager

task = load_task("examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml")

result = run_trial(
    task=task,
    models={"agent": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6", "temperature": 0.0}},
)

print(result.trajectory.grade)
```

`runtime="auto"` is the default and delegates to the same task-driven substrate
selection the CLI uses — you don't have to know whether the task needs a
shared or per-trial backend. Errors surface as three named types (all
importable from `tolokaforge`): `UnknownImplementationError` (registry lookup
missed), `pydantic.ValidationError` (bad models config), `ProvisionError`
(substrate failed to come up). Nothing is swallowed; `runtime_backend.close()`
is guaranteed in a `finally`, so a driver that runs `run_trial` in a loop does
not leak gRPC channels or Docker stacks.

Runnable version: [`examples/library/run_trial.py`](../examples/library/run_trial.py).

### From any language — `tolokaforge run-trial`

`tolokaforge run-trial` reads one JSON-Lines message from stdin, runs one trial,
and writes one JSON-Lines message to stdout. Every message carries a wire
version (`"v":1`) that changes independently of the tolokaforge package
version — a downstream harness pins against the wire, not the release.

```bash
$ echo '{"v":1,"type":"start","task":{...},"models":{"agent":{"provider":"openrouter","name":"anthropic/claude-sonnet-4.6"}}}' \
    | tolokaforge run-trial
{"v":1,"type":"result","result":{"trajectory":{"grade":{...},...}}}
```

- **stdin** — exactly one `start` message per invocation.
- **stdout** — the wire only: exactly one `result` or `error` message, then
  the process exits. Logs, tracebacks, and everything else go to stderr, so
  the wire stays parseable.
- **Exit codes** — `0` on `result`, non-zero on `error`. The `error` message
  itself carries a typed `error_type` (`unknown_implementation`, `validation`,
  `provision`, `protocol`, `internal`) so a caller can branch without parsing
  prose.
- **Signals** — `SIGTERM` and premature stdin EOF terminate the trial cleanly.

Because the trial task is serialised across the wire, it carries no source
directory of its own. If your task references files (grading rubrics, fixtures,
tool code), spawn the subprocess with its working directory at the task-pack
root, and file paths in the task will resolve.

The exec wire is the any-language surface. The runner's gRPC server exposes only
per-trial primitives (`RegisterTrial` / `ExecuteTool` / `GradeTrial` / …) plus
`HealthCheck`, with reflection off — there is no whole-trial RPC, so `grpcurl`
cannot drive a trial. Any language that can spawn `docker compose exec` and read
a JSON line drives `tolokaforge run-trial` exactly as these examples do.

Runnable versions:
- [`examples/run-trial/drive_run_trial.py`](../examples/run-trial/drive_run_trial.py) —
  minimal "hello world": one trial, print the grade.
- [`examples/run-trial/drive_run_trial_sweep.py`](../examples/run-trial/drive_run_trial_sweep.py)
  — end-user shape: runs both bundled `tool_use` tasks against two models,
  aggregates per-task per-model scores + cost + latency, prints a readable
  comparison table. Handles `error` messages typed rather than as tracebacks.
- [`deploy/standalone/examples/drive_one_trial.sh`](../deploy/standalone/examples/drive_one_trial.sh)
  — the standalone-stack any-language driver: POSIX `sh` + `jq`, host needs only
  Docker and `jq` (no host tolokaforge). It serialises the task *inside* the
  runner via the public `load_task`, builds the `start` envelope with `jq`, and
  drives this same exec wire against the composed stack.

Full wire format spec: [`docs/API.md`](API.md#tolokaforge-run-trial).

### As an extension point — entry-point registries

Each of the three Protocol seams has a named `importlib.metadata` entry-point
group:

- `tolokaforge.runtime_backends`
- `tolokaforge.trial_graders`
- `tolokaforge.conductors`

Anything registered in one of those groups — by tolokaforge itself for the
built-ins, or by any `pip install`ed downstream package — resolves by name.
To ship your own runtime backend named `my_substrate`:

```toml
# your_package/pyproject.toml
[project.entry-points."tolokaforge.runtime_backends"]
my_substrate = "your_package.runtime:factory"
```

Where `factory` is a callable matching the `RuntimeBackendFactory` type alias
(`Callable[[RuntimeBackendContext], RuntimeBackend]`). Same shape for graders
and conductors.

Then, in code that composes your plug-in:

```python
result = run_trial(
    task=task,
    models={"agent": {...}},
    runtime="my_substrate",  # resolves by name; unknown names fail loud
)
```

The registry loader is fail-loud by design:

- Duplicate names raise `DuplicateRegistrationError` at load, naming both
  providing distributions — no silent last-wins.
- Unknown names raise `UnknownImplementationError` with the list of known
  names — no silent fallback.
- A broken plug-in's `ImportError` propagates rather than being swallowed —
  no silent skip.

A broken third-party plug-in never breaks resolution of a healthy sibling,
because discovery enumerates names and distributions without eagerly
importing.

## Use cases

Concrete shapes of code that this decouples.

### Agent lab — score my agent, don't reinvent grading

You have your own agent loop, your own tool-invocation runtime, your own
scheduler. You need trial execution + grading, not a whole harness. Drop the
runner in as a scoring service:

```python
for problem in my_problem_set:
    task = build_task_from_problem(problem)     # your code
    result = run_trial(
        task=task,
        models={"agent": my_model_config()},    # your model
    )
    my_result_store.record(problem, result)     # your storage
```

You get typed `TrialResult` back with a full `Trajectory` (every message,
every tool call), `Grade`, and cost/token/latency metrics. What you do with
them is up to you — a research dashboard, a reward signal, a leaderboard.

### Research sweep — model or task-variant comparison

Compare two models across a small task set without standing up a batch run:

```python
for task in tasks:
    for model_name in ["anthropic/claude-sonnet-4.6", "openai/gpt-4o"]:
        result = run_trial(
            task=task,
            models={"agent": {"provider": "openrouter", "name": model_name}},
        )
        table.append((task.task_id, model_name, result.trajectory.grade.score))
```

For a runnable end-to-end version of this pattern — with error handling, a
comparison table printed to stdout, and use of `tolokaforge run-trial` (so each
trial runs in an isolated subprocess), see
[`examples/run-trial/drive_run_trial_sweep.py`](../examples/run-trial/drive_run_trial_sweep.py).

### Non-Python control plane

You already have orchestration in Go / Rust / TypeScript / shell. Pipe JSON
into `tolokaforge run-trial` and read one JSON message back. No FFI, no
embedded interpreter, no cross-language type gymnastics.

```bash
# shell — one trial through the subprocess CLI
cat trial_start.json | tolokaforge run-trial > trial_result.json
```

```typescript
// TypeScript — same shape, different plumbing
const child = spawn("tolokaforge", ["run-trial"]);
child.stdin.write(JSON.stringify(startMessage) + "\n");
child.stdin.end();
const [resultLine] = await readOneJsonLine(child.stdout);
```

The wire format is versioned separately from the tolokaforge release — pin
against `"v":1` and your driver keeps working across package upgrades.

### Custom plug-in — swap a Protocol seam

You want a rubric-based grader that calls a different judge model than the
built-in one, without editing tolokaforge:

```python
# your_package/graders.py
from tolokaforge.core.grading import Grade, TrialGrader

class MyRubricGrader:
    def grade(self, trajectory, ctx) -> Grade:
        # your grading logic here, returning a Grade
        ...

def factory(ctx):
    return MyRubricGrader()
```

```toml
# your_package/pyproject.toml
[project.entry-points."tolokaforge.trial_graders"]
my_rubric = "your_package.graders:factory"
```

Install alongside tolokaforge (`pip install your_package tolokaforge`), then:

```python
result = run_trial(
    task=task,
    models={"agent": {...}},
    grader="my_rubric",   # your grader, resolved by name
)
```

Same shape for custom runtime backends (`runtime="my_substrate"`) and custom
conductors (`conductor="my_agent_loop"`).

## Wire format quick-reference

`tolokaforge run-trial` speaks a small JSON-Lines protocol. Every message
carries `"v":1`.

**Client → runner (stdin):**

```json
{"v":1,"type":"start","task":{...},"models":{"agent":{...}},"runtime":"auto","conductor":"in_process"}
```

`task` is a serialised `TaskConfig` (the shape `load_task` returns, via
`task.model_dump(mode="json")`).
`models` is a dict; the `"agent"` key is the AI model that plays the trial's
agent role. `runtime` / `conductor` / `grader` are optional; `"auto"` /
`"in_process"` / `"runner_rpc"` are the defaults.

**Runner → client (stdout, exactly one message):**

```json
{"v":1,"type":"result","result":{"trajectory":{...},"metrics":{...},"grade":{...}}}
```

or

```json
{"v":1,"type":"error","error_type":"unknown_implementation","message":"..."}
```

`error_type` is one of: `unknown_implementation`, `validation`, `provision`,
`protocol`, `internal`. The five map 1:1 to the named error types from
`run_trial`, plus `protocol` (bad wire shape) and `internal` (runner bug —
should be reported).

Unknown top-level keys on `start` raise `protocol` errors rather than being
silently ignored. A typo like `"runtme": "auto"` fails loud with the offending
key name rather than falling back to the default.

Full spec, including the (experimental) `event` progress subtypes and the
`cancel` control message: [`docs/API.md`](API.md#tolokaforge-run-trial).

## Compatibility guarantees

Three surfaces are versioned commitments — the milestone-13 ADR
([`docs/adr/0022-runtime-independence.md`](adr/0022-runtime-independence.md))
locks them:

1. **Entry-point group names + built-in registration names.** New built-ins
   can be added; existing names won't be renamed or removed without a
   deprecation cycle.
2. **`run_trial(...)`'s signature, return type, and named error types.** New
   optional keyword arguments can be added; positional shape and return
   surface stay stable.
3. **`tolokaforge run-trial` wire format at `"v":1`.** New optional fields on
   existing message types are allowed (additive); breaking changes require a
   `v` bump. New message subtypes (like `event`) are experimental until
   promoted.

Everything else — internal module layout, container image tag naming,
Dockerfile structure, `.env` bootstrap details — may change without notice.

## Under review

The current runner receives its LLM / OAuth credentials via a single env var
(`TOLOKAFORGE_SECRETS_JSON`) set by the substrate stack builder at container
spawn time. That works for today's single-operator, single-tenant use, but the
architectural question of whether per-trial credential delivery should flow
through the Conductor's control surface (instead of being baked into the
container env) is under review — see issue #573. The outcome may reshape how
custom `Conductor` and `RuntimeBackend` plug-ins interact with secrets;
today's env-var path is stable for the shipped surfaces, but check that issue
before building a plug-in that leans heavily on the current model.

## See also

- [`docs/API.md`](API.md) — full API reference for `run_trial` and the
  `run-trial` wire format.
- [`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md) — the `RuntimeBackend`
  Protocol contract, existing implementations, and plug-in registration
  mechanics.
- [`docs/RUNNER.md`](RUNNER.md) — the batch orchestration flow (prepare /
  worker / status, distributed workers). Different tool for a different job.
- [`docs/adr/0022-runtime-independence.md`](adr/0022-runtime-independence.md)
  — the design decision this guide describes, with rationale and the
  compatibility-surfaces table.
- [`examples/library/`](../examples/library/) — Python library-entry example.
- [`examples/run-trial/`](../examples/run-trial/) — subprocess CLI examples,
  minimal + comparison-sweep.
