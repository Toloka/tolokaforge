# 0006. `EnvEndpoints` — typed runner service URLs on `TrialSpec`

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

`TrialSpec` (ADR-0003) shipped with `env_endpoints: dict[str, str]` as a deliberate placeholder — typed as raw key/value strings, defaulted to `{}`, and consumed by nobody. The slot exists so a successor PR can type it in place without revising the surrounding `TrialSpec` shape.

That successor is now needed. Today's runner reads its DB / RAG URLs from server-startup env vars (`DB_SERVICE_URL`, `RAG_SERVICE_URL`) with hardcoded `http://localhost:8000` / `http://localhost:8001` fallbacks. The orchestrator separately resolves the runner gRPC address from `ServiceStack.get_service_url("runner", 50051)`. There is no single, typed surface where the orchestrator declares "these are the URLs this trial should reach" and the runner declares "these are the URLs I read per trial."

The architecture proposal (`docs/CLOUD_RUNTIME_ARCHITECTURE.md` §14 coupling **C2** "localhost runner addr → P1") names this gap as the first follow-on after the control↔trial seam. Without it, a future where the runner is out-of-process — `RuntimeBackend` Protocol, remote `Conductor`, distributed execution — has no contract to flow URLs through.

## Decision Drivers

- **Type one of TrialSpec's last placeholder slots in place.** ADR-0003 was written so this PR is a single-annotation replacement, not a re-shaping.
- **One source of truth on the wire.** The producer (orchestrator) resolves the URLs once; the consumer (runner) reads the same values per trial. No drift between server-startup defaults and per-trial overrides.
- **`extra="forbid"` strictness.** The wire format is reviewed at PR time; misspelled fields fail validation rather than being silently dropped. Same convention as `TrialSpec` / `TrialResult`.
- **Fail-fast.** Required URLs (`db_url`, `runner_url`) raise on missing; `rag_url` is optional because `core_stack` does not include RAG.
- **No gRPC contract churn.** `trial_spec_json` stays a JSON string in the proto; the nested-model change is transparent to the wire schema.

## Considered Options

1. **Type `env_endpoints` in place with a new `EnvEndpoints` Pydantic model.** The slot already exists; replacing the annotation is a single field change. The orchestrator becomes the producer; the runner gains a typed contract to consume from in a follow-up. **This PR.**
2. **Add a new top-level field on `TrialSpec` (`endpoints: EnvEndpoints`) and leave the legacy `env_endpoints` dict in place.** Backwards-compatible. Costs a second field and a deprecation window for a feature that has zero readers today.
3. **Skip the type and just remove the localhost defaults at the runner.** Cheapest. Leaves the orchestrator with no place to declare "this trial's URLs are X"; the runner still depends on its own startup env vars.
4. **Defer until a remote conductor exists.** Each later seam (`RuntimeBackend`, `Conductor`, remote runner) ends up co-litigating the endpoint shape simultaneously with whatever else it's doing.

## Decision

We adopt **Option 1**.

- Add `EnvEndpoints` to `tolokaforge/core/trial.py` next to `TrialSpec`. Pydantic v2 `BaseModel` with `model_config = {"extra": "forbid"}`. Three fields:
  - `db_url: str` — required.
  - `rag_url: str | None = None` — optional (`rag-service` ships in `full_stack` only).
  - `runner_url: str` — required.
- `TrialSpec.env_endpoints` annotation flips from `dict[str, str] = Field(default_factory=dict)` to `EnvEndpoints` with no default — the orchestrator is required to supply it.
- The orchestrator builds an `EnvEndpoints` once per run from a free function (`_build_env_endpoints`) that reads the same env vars the docker stack injects into the runner container (`DB_SERVICE_URL`, `RAG_SERVICE_URL`) and the orchestrator's known runner address. Threaded through `_run_trial` as a kwarg; carried verbatim on every `TrialSpec` the run produces.
- Canonical contract tests (`tests/canonical/test_trial_spec_contract.py`) update the field assertions and add `test_env_endpoints_is_required`. A new unit test (`tests/unit/test_env_endpoints.py`) pins `EnvEndpoints`'s own shape, optional-`rag_url`, `extra="forbid"`, and JSON round-trip. Orchestrator unit tests (`TestBuildEnvEndpoints`) pin the producer's behaviour.

## Consequences

### Positive

- `TrialSpec.env_endpoints` is now typed end-to-end on the wire. The seam this PR establishes is the contract the next seam-definition steps (`RuntimeBackend`, `Conductor`) read from.
- Field set is reviewed at PR time, not at consumer-dashboard-breakage time.
- `extra="forbid"` catches typos.
- No gRPC `.proto` change; ADR-0003's promise that follow-ons replace placeholders in place is honoured.

### Negative / Trade-offs

- The runner does **not** yet consume `trial_spec.env_endpoints`. Today it still reads URLs from its own server-startup env vars (`DB_SERVICE_URL` / `RAG_SERVICE_URL`) with the docker-stack injection providing the value. Carrying the URLs on the wire is the architectural step; the runner-side consumer (per-trial client construction, removal of the startup defaults) is the next focused PR.
- Three fields cover today's services only. Engine-internal TypeSense flows through `TaskDescription.search_config` (unchanged); a hypothetical second runner-perspective service in a future stack adds a field then.

### Follow-ups

- **Runner consumer.** `RunnerServiceImpl` builds DB / RAG clients per `RegisterTrial` from `trial_spec.env_endpoints`. Delete `DEFAULT_DB_SERVICE_URL` / `DEFAULT_RAG_SERVICE_URL` from `runner/__main__.py`. Out of scope for this PR (separate ~400 LoC refactor).
- **Orchestrator dual-path fallback cleanup.** Three loops in `_run_trial` (`json_db_reset_urls`, `rag_service_urls`, `json_db_sync_urls`) try docker-DNS-name then localhost. These flow from the orchestrator-on-host to services, separate from the runner-perspective URLs on `EnvEndpoints`. Cleanup is its own concern.
- **`RuntimeBackend` Protocol.** Reads `EnvEndpoints` to know where to send a trial. Next architecture-seam step.
- **`Conductor` Protocol promoting `TrialRunner`.** Lifts `_run_trial` out of the orchestrator; reads `TrialSpec.env_endpoints` to know its own runner target.
- **Centralized config / service discovery for multi-machine deployments.** Today each worker reads its own `DB_SERVICE_URL` / `RAG_SERVICE_URL` / `EXECUTOR_ADDRESS` from the environment — fine for 1–2 boxes, friction at 10+. Per-trial routing (orchestrator decides "trial T should target DB-X") is also not expressible. Both belong to the Phase 2+ control-plane / scheduler scope sketched in [`docs/CLOUD_RUNTIME_ARCHITECTURE.md`](../../CLOUD_RUNTIME_ARCHITECTURE.md) §13 and are out of the seam-definition arc; calling them out here so a reader of this ADR sees where the trail goes.

## Rejected alternatives

- **Option 2 — keep both `env_endpoints` (dict) and a new typed field.** Backwards-compat for a feature that has zero readers today. Pure cost.
- **Option 3 — runner-side cleanup only.** Leaves the orchestrator with no typed surface to express "this trial's URLs." Half-measure that defeats the architectural goal.
- **Option 4 — defer.** Same argument as ADR-0004 and ADR-0005: each later seam ends up re-litigating the endpoint shape under deadline pressure.

## Scope notes

- **Producer side only.** This PR ships the type, the producer (orchestrator), and the contract tests. The runner-side consumer is a separate PR.
- **TypeSense is engine-internal.** Co-located with the orchestrator; flows through `TaskDescription.search_config`. Not in `EnvEndpoints`.
- **`executor:50051` literals** in `orchestrator.py` and `docker_runtime.py` are runner-address defaults, not separate-executor URLs. They belong to the `RuntimeBackend` seam, not here.
- **Dual-path fallback loops** in the orchestrator (`_run_trial`) are dev-mode-only host→service URL lookups, separate from runner-perspective URLs carried on `EnvEndpoints`. Out of scope.
