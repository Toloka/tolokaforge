# Trial Node Service

A trial node runs one whole trial per request. The control plane's
`--runtime remote` ships a trial's inputs to a leased node and gets the graded
result back; the node runs the conductor, the agent's LLM loop, the grader and
the trial's substrate itself. This document is the reference for the node's wire
contract, its behaviour and its deployment. For the design record see
[ADR-0063](adr/0063-whole-trial-node-service-and-dispatch-seam.md); for the
backend that dials nodes see [`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md).

```
control plane                                  trial node
─────────────                                  ──────────
RemoteTrialExecutor
  └─ RemoteRuntimeBackend.run_trial_node
       lease node ──── HealthCheck ──────────► version + message-size pre-flight
                  ──── RunTrial(inputs) ─────► run_trial_from_inputs
                  ◄─── TrialResult ──────────    (conductor, agent loop, grader, substrate)
       release node
  └─ write reduced bundle
```

## Wire contract

[`tolokaforge/trial_node/trial_node.proto`](../tolokaforge/trial_node/trial_node.proto),
package `tolokaforge.trial_node`. Regenerate the stubs from the repository root:

```bash
uv run python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. tolokaforge/trial_node/trial_node.proto
```

| RPC | Purpose |
| --- | --- |
| `RunTrial(RunTrialRequest) -> RunTrialResponse` | Run one trial to a graded result. A single unary call that lasts as long as the trial; there is no streaming progress and no mid-trial cancellation. |
| `HealthCheck(HealthCheckRequest) -> HealthCheckResponse` | `status` (`ready` / `busy`), `node_protocol_version`, `node_max_message_mb`, `trial_in_flight`. |

`RunTrialRequest` carries `trial_node_inputs_json` (a serialized
[`TrialNodeInputs`](../tolokaforge/core/trial.py); `${secret:NAME}` references
only, never secret values) and `client_protocol_version`. `RunTrialResponse`
carries `success`, `trial_result_json` (a serialized `TrialResult`, when
`success`), `error_type` / `stage` / `reason` (otherwise), and the node's
`node_protocol_version` and `node_max_message_mb`.

A failed trial is a successful RPC with `success = false`; only a transport
fault is a gRPC error.

### Error types

| `error_type` | Raised when | Control-plane handling |
| --- | --- | --- |
| `ProvisionError` | The trial's substrate failed to come up; `stage` names the lifecycle step. | `PROVISION_ERROR` result with a failure bundle; not retried. |
| `SecretPreflight` | The inputs reference a `${secret:NAME}` the node's secret payload lacks, a reference is malformed, or the node's payload is malformed. | `PROVISION_ERROR` result; not retried. |
| `TaskPackDrift` | The mounted task pack does not hash to the inputs' `task_pack_hash`. | `PROVISION_ERROR` result; not retried. |
| `NotImplemented` | The task's adapter is not node-runnable. | `PROVISION_ERROR` result; not retried. |
| `InvalidConfig` | The inputs were rejected by the node's schema, or name an unregistered runtime / conductor, or lack a user-simulator model. | `PROVISION_ERROR` result; not retried. |
| `VersionSkew` | The caller's protocol version is below the node's. | `PROVISION_ERROR` result; not retried. |
| `NodeBusy` | The node is already running a trial. | Raised; the orchestrator retries. |
| `Internal` | An unexpected failure inside the node (traceback in the node's log). | Raised; the orchestrator retries. |

Client-side conditions, reported by `TrialNodeClient`:

| Condition | Handling |
| --- | --- |
| Node unreachable within the connect budget, call cut by `UNAVAILABLE` / `DEADLINE_EXCEEDED` / `CANCELLED` | Raised as a transient error; retried. |
| `UNIMPLEMENTED` (node image predates the service) | Version skew naming the node image upgrade; not retried. |
| Node's `node_protocol_version` below the engine's | Version skew; not retried. |
| Node's `node_max_message_mb` below the engine's `compute.grpc_max_message_mb` | `MessageSizeSkew`; not retried. |
| `RESOURCE_EXHAUSTED` on `RunTrial` | `MessageSizeSkew` with the message-size remedy; not retried. |

A non-retried failure is recorded as a `PROVISION_ERROR` trial whose
`metrics.yaml` carries `error_reason` as `"<error_type>: <reason>"`.

## Behaviour

- **Version gate first.** `RunTrial` checks `client_protocol_version` before any
  work and answers `VersionSkew` for a caller below `NODE_PROTOCOL_VERSION`.
- **One trial at a time.** A non-blocking lock guards each `RunTrial`; a second
  concurrent call is answered `NodeBusy`. The control plane's node registry already
  leases one trial per node, so this is defence in depth for the process-global
  secret manager.
- **Secret pre-flight.** Before provisioning, every `${secret:NAME}` in the
  inputs is resolved against the node's secret manager (installed from
  `TOLOKAFORGE_SECRETS_JSON`); a missing name fails the trial with
  `SecretPreflight` before any container starts.
- **Node-supplied placement.** The node writes artifacts to
  `<output-root>/<run_id>` — its conductor persists the full bundle before the
  node replies — and, when `--task-pack-root` is set, reads the task's files from
  `<task-pack-root>/<task_id>`. The content-hash check applies to whatever is
  mounted. Without a pack root the node reads the path the engine sent.
- **Keepalive.** Both ends use `keepalive_options()` from
  [`tolokaforge/runner/grpc_limits.py`](../tolokaforge/runner/grpc_limits.py)
  (ping after 20 s idle, 10 s to answer, pings permitted with no call data), so a
  node that dies mid-trial fails the call with `UNAVAILABLE` instead of hanging.

## The reduced bundle

The control plane writes the following under
`<output_dir>/trials/<task_id>/<trial_index>/` from the returned trajectory:
`trajectory.yaml`, `tool_log.yaml`, `metrics.yaml` and, when graded, `grade.yaml`.
Prompts, tool schemas, logs and per-service log captures stay in the node's own
bundle. A configured `observability.tracing` exporter sees only this reduced
bundle: the node runs the trial with no observer.

## Running a node

```bash
tolokaforge trial-node --port 50061 --output-root /var/lib/tolokaforge/trials
# or: python -m tolokaforge.trial_node
```

| Option | Environment | Default |
| --- | --- | --- |
| `--port` | `TRIAL_NODE_PORT` | `50061` |
| `--output-root` | `TRIAL_NODE_OUTPUT_ROOT` | `<tmp>/tolokaforge-trial-node` |
| `--task-pack-root` | `TRIAL_NODE_TASK_PACK_ROOT` | unset |
| — | `TOLOKAFORGE_GRPC_MAX_MESSAGE_MB` | `50`; set to at least the engine's `compute.grpc_max_message_mb` |
| — | `TOLOKAFORGE_SECRETS_JSON` | the node's credential payload |

An invalid value, or a malformed secret payload, fails startup.

### Image

[`tolokaforge/docker/dockerfiles/trial_node.Dockerfile`](../tolokaforge/docker/dockerfiles/trial_node.Dockerfile)
installs the full base wheel (not the runner subset wheel — a node needs the
orchestration closure) and the Docker CLI, and runs `python -m tolokaforge.trial_node`.

```bash
docker build -f tolokaforge/docker/dockerfiles/trial_node.Dockerfile -t tolokaforge-trial-node .
docker run -d -p 50061:50061 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e TOLOKAFORGE_SECRETS_JSON="$(...)" \
  tolokaforge-trial-node
```

The image is built locally (`tolokaforge docker build --service trial_node`); publishing it to a registry is a separate follow-up.

The node builds no images. `tolokaforge-runner:local` and
`tolokaforge-db-service:local` must already exist on the Docker daemon the node
drives (`make docker-build-core` at node bootstrap, or pull and tag them), and a
runner must be reachable at `EXECUTOR_ADDRESS` (default `executor:50051`) for tasks
that declare no per-trial stack.

## Not supported

Refused at run start by the control plane (see
[`RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md#concrete-backends)): task-declared
stacks, non-native adapters and coding-harness mode, and the run-level settings
the inputs do not carry. Streaming progress, mid-trial cancellation and live
traces from the node are not provided.
