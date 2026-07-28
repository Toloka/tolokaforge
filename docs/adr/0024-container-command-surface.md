# 0024. Container command surface — the committed contract of `tolokaforge-runner`

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

M14 publishes `docker.io/tolokasoft1/tolokaforge-runner` to Docker Hub as a `docker pull`-able artifact ([ADR-0023](0023-runner-image-internals.md)). Once operators run a pinned tag in their own environments, the ways they interact with the container — how it starts, how they wire it, how they check its health, and what they may invoke inside it — become a contract those operators build against. That contract must be stated explicitly, because [ADR-0023](0023-runner-image-internals.md) makes the image *internals* uncommitted: the command surface is precisely the part that *is* stable across a future wheel carve.

The current image already defines this surface in [`runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) and [`runner/__main__.py`](../../tolokaforge/runner/__main__.py); the CLI subcommands live in [`dx/cli`](../../tolokaforge/dx/cli). What is missing is the decision that names *which* of these behaviours are the committed contract, which are internal tuning, and which are reserved for a future decision. Without that boundary, an operator cannot tell a stable interface from an implementation detail, and a maintainer cannot tell which changes are breaking.

## Decision Drivers

- **State the actual surface, not an aspirational one.** Every element committed here must match what the image does today (Core Rule 8 — documents actual state).
- **Committed ≠ everything the image can do.** Operational tuning knobs and a machine-only wire subcommand exist; the contract must scope which of them consumers may depend on.
- **A machine-facing command can be documented without being interactive-visible.** `run-trial` is a wire protocol, not a human command; hiding it from `--help` while documenting it for programmatic use is deliberate, not an oversight.
- **Reserve, don't smuggle.** A config-introspection command is a genuine new CLI contract that deserves its own interface design; naming it as reserved keeps this ADR honest rather than half-committing an undesigned surface.

## Considered Options

**Which exec subcommands to commit.**

1. **Commit `run-trial` + `--version` only; reserve `config-dump`.** Commit exactly the two subcommands that exist and are load-bearing today; forward-reference the undesigned dump command.
2. **Commit `run-trial` + `--version` + a new `config-dump`.** Design and add an operator config-introspection command as part of this milestone.

**`run-trial` visibility.**

1. **Keep `run-trial` `hidden=True`; document it here for `docker exec`.** It stays out of interactive `--help` (a machine protocol) but is a documented programmatic entry point.
2. **Un-hide `run-trial`** so it appears in `--help`.

## Decision

We adopt the current-state surface as the committed contract, choosing **Option 1** on both axes above. The committed command surface of `tolokasoft1/tolokaforge-runner` is:

### Default entrypoint — the gRPC runner service on `:50051`

The image has **no `ENTRYPOINT`**; its default command is:

```dockerfile
CMD ["python", "-m", "tolokaforge.runner"]
```

This starts the Runner gRPC service, which binds `[::]:50051` (the image `EXPOSE`s `50051`). Running the image with no arguments is running the service — that is the committed default behaviour.

### gRPC healthcheck

The image self-reports health via a Docker `HEALTHCHECK` that probes gRPC channel readiness on `localhost:50051` (not an HTTP endpoint):

```dockerfile
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=2)" || exit 1
```

`docker inspect --format '{{.State.Health.Status}}'` reaching `healthy` is the committed signal that the service is up.

### The `TOLOKAFORGE_*` / service-URL environment contract

The container is wired through these environment variables, read in [`runner/__main__.py`](../../tolokaforge/runner/__main__.py):

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `TOLOKAFORGE_SECRETS_JSON` | no | unset | JSON-serialised credential map. When set, it bootstraps the `SecretManager` singleton at container start — the **only** legitimate `os.environ` credential read inside the runner; every later access routes through `tolokaforge.secrets.get_default()`. When unset, the `SecretManager` lazy-inits from the `EnvProvider` / `.env` on first `get_secret()`. |
| `DB_SERVICE_URL` | no | `http://localhost:8000` | URL of the db-service. A wrong or unreachable URL fails loud on first call, so a localhost default is safe for bare/local runs; every stack injects the real URL. |
| `RAG_SERVICE_URL` | no | *(no default — honest absence)* | URL of the rag-service. Present **iff** a rag-service is actually running (injected by the full stack only). Unset means the runner builds no RAG client and the judge is offered no `search_kb` tool — an unset value is meaningful, so it deliberately has no default. |

`RUNNER_PORT`, `LOG_LEVEL`, and `MAX_WORKERS` are operational tuning knobs with defaults (`50051` / `INFO` / `10`); they are not part of the committed wiring contract above.

### Documented `docker exec` subcommands

Two CLI subcommands are committed for programmatic `docker exec` use against a running container:

- **`tolokaforge run-trial`** — runs one trial as a subprocess over a JSON-Lines pipe ([ADR-0022 §Surface 3](0022-runtime-independence.md#surface-3--tolokaforge-run-trial-subprocess-wire-format-538)): reads exactly one `start` request envelope on stdin, writes exactly one terminal `{"v":1,"type":"result"|"error",…}` envelope on stdout. It is a **machine-facing wire protocol**, so it is `hidden=True` in interactive `tolokaforge --help` **by design** — hidden from the human command list, but a committed, documented entry point for programmatic `docker exec`. Documented-but-hidden is deliberate; un-hiding it would change `--help` output, which is itself a compatibility surface.
- **`tolokaforge --version`** — prints the installed `tolokaforge` package version. A stable, human- and machine-readable version probe for a running container.

### `config-dump` — reserved, not yet committed

A `tolokaforge config-dump` subcommand (an operator introspection command dumping the resolved, secret-redacted runner config / effective environment contract) **does not exist today and is not part of the committed surface.** It is a genuine new CLI contract — what it dumps, how it redacts, how its output is behaviour-locked — that deserves its own interface design rather than being smuggled into a publish milestone. It is **reserved** and tracked as follow-up [#626](https://github.com/Toloka/tolokaforge/issues/626); until that lands, operators must not rely on it.

Option 2 on the visibility axis (un-hide `run-trial`) is rejected: it changes `--help`, a separate compatibility surface, for no operator benefit — the command is machine-facing and documented here. Option 2 on the subcommand axis (build `config-dump` now) is rejected to keep this ADR a record of the *actual* surface, not an aspirational one.

## Consequences

### Positive

- Operators have one authoritative statement of how to run, wire, health-check, and programmatically drive the published runner image — the stable half of [ADR-0023](0023-runner-image-internals.md)'s image contract.
- The committed surface is exactly what the rc-smoke test asserts (entrypoint starts, health reaches `healthy`, `--version` answers, `run-trial` speaks the wire), so the contract has automated teeth.
- Reserving `config-dump` records the intent without half-committing an undesigned command.

### Negative / Trade-offs

- Committing the surface means a future change to the entrypoint, the healthcheck mechanism, the three wiring variables, or the two exec subcommands is a breaking change requiring a version and CHANGELOG event — a deliberate constraint, the point of the contract.
- `run-trial` staying hidden means an operator reading `--help` will not discover it; the documentation burden moves here and to [`docs/STANDALONE_RUNNER.md`](../STANDALONE_RUNNER.md).

### Follow-ups

- Code changes required: none — this ADR records the surface the image already exposes.
- Documentation to update: [`docs/STANDALONE_RUNNER.md`](../STANDALONE_RUNNER.md) documents the entrypoint, healthcheck, env contract, and the two exec subcommands, cross-referencing this ADR.
- Tests to add: the publish workflow's keyless rc-smoke locks each committed element (entrypoint / health / `--version` / `run-trial` wire well-formedness).
- Deferred work: `tolokaforge config-dump` — the reserved config-introspection subcommand ([#626](https://github.com/Toloka/tolokaforge/issues/626)).

## Links

- Related ADRs:
  - [ADR-0022](0022-runtime-independence.md) — runtime independence; §Surface 3 specifies the `run-trial` JSON-Lines wire this ADR commits as a `docker exec` entry point
  - [ADR-0023](0023-runner-image-internals.md) — image internals are uncommitted; this ADR is the committed half of the published-image contract
- Related code:
  - [`tolokaforge/docker/dockerfiles/runner.Dockerfile`](../../tolokaforge/docker/dockerfiles/runner.Dockerfile) — entrypoint (`CMD`), `EXPOSE 50051`, gRPC `HEALTHCHECK`
  - [`tolokaforge/runner/__main__.py`](../../tolokaforge/runner/__main__.py) — the `TOLOKAFORGE_*` / service-URL environment contract
  - [`tolokaforge/dx/cli/run_trial_command.py`](../../tolokaforge/dx/cli/run_trial_command.py) — the `hidden=True` `run-trial` wire subcommand
- Related issues:
  - [GH #610](https://github.com/Toloka/tolokaforge/issues/610) — Milestone 14 umbrella (runner as a distributable service)
  - [GH #626](https://github.com/Toloka/tolokaforge/issues/626) — reserved `tolokaforge config-dump` subcommand
