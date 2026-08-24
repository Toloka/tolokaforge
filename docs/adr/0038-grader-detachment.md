# 0038. Grader detachment — grader as an independently deployable and scalable component

- **Status:** Proposed
- **Date:** 2026-08-14
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

The `TrialGrader` Protocol ([ADR-0014](0014-trial-grader-protocol.md)) is a clean plug-in seam: a single call site inside the conductor, entry-point-driven registration, zero grader imports in the orchestrator. The runtime-independence work ([ADR-0022](0022-runtime-independence.md)) established the plug-in registry; the deterministic-trace-checks work made `core/grading/` substrate-neutral — a pure evaluator both grading paths already call.

The remaining gap is **distribution**, not **design**. The sole registered `TrialGrader` implementation reaches its target through the runner service's grade RPC. That RPC lives on the same 7-RPC service that also owns tool execution and env-state — meaning:

- the grader address is the runner address by construction (the orchestrator has no way to point at a different address),
- the grader ships in the same image and scales as one unit with tool execution (latency-sensitive, low CPU) even though grading is LLM-bound (~seconds, high memory),
- the grade call blocks the orchestrator worker thread until the RPC returns — batch throughput on judge-heavy runs is bounded by grader latency per trial regardless of orchestrator worker count.

Two forces make this due now:

- **Batch scale.** The larger the batch, the more of the wall-clock cost is grader wait. Scaling grader capacity independently of orchestrator worker count is not currently a knob.
- **Deployment shape.** With the runner-wheel-split ([ADR-0025](0025-runner-wheel-split.md)), the `tolokaforge-models` split ([ADR-0030](0030-tolokaforge-models-split.md)), and pull-vs-build ([ADR-0031](0031-pull-vs-build-default-for-service-images.md)) all shipped, the pattern for shipping a slim component-specific image is stable. Grading is the next natural candidate — and unlike the runner, it holds no sandbox state, has a fundamentally different resource profile, and is naturally stateless per trial.

## Decision Drivers

- **The evaluator has already moved; only the transport is coupled.** After deterministic-trace-checks, `core/grading/` is a pure library both the runner-side and offline-rejudge paths call. What still lives inside the runner service is the RPC boundary + runner-specific glue (state fetch, KB gating, trajectory JSON encoding). The natural extract is the transport, not the evaluator.
- **Independent scale needs a queue, not just a remote RPC.** Remote gRPC gives independent deploy but the orchestrator client still blocks. To decouple orchestrator throughput from grader latency, the orchestrator must hand off ownership of the grade job and resume the worker. A queue provides that and native back-pressure.
- **The plug-in Protocol should stay synchronous.** Making `TrialGrader.grade` async would ripple through Conductor, Orchestrator, `run_trial`, tests, and every downstream impl. A queue-backed variant that blocks on a local future keyed by trial id gets the same wall-clock effect without the API change.
- **The plug-in seam is real but unverified.** One registered `TrialGrader` implementation held a Protocol for two years without pressure. A second implementation — either a queue-backed variant or a judge-only variant — is the only way to prove the seam accepts fundamentally different grader shapes.
- **Distribution pattern is proven.** The runner-wheel-split ADR established a same-package subset build target and a slim Docker image on that closure. The `tolokaforge-models` split established a separate wheel release cadence. Pull-vs-build made anonymous image pull the default for wheel consumers. Applying these patterns to grading is mechanical.
- **Preserve M14 / M15 commitments.** The runner image name / tag axis and container command surface do not change. The runner's grade RPC continues to work for one release cycle with a deprecation notice; only in the release after does it go away.

## Considered Options

**Extraction target — what carves off the runner?**

1. **Extract the grader transport (this ADR).** New RPC contract, standalone grader service binary, new grader plug-in registered under the existing group. Runner keeps its RPC for one release, then removes.
2. **Extract only the judge.** Splits the highest-latency stage into a service; leaves state / transcript / custom checks in the runner. Rejected: the operational win is smaller (runner still blocks on state/transcript for those tasks), and the pipeline stages get muddled (which side runs where?).
3. **Extract nothing; move grading off-thread inside the runner.** No new component; the runner's grade RPC becomes async internally. Rejected: solves throughput for a single runner instance but does not enable independent scale — grading capacity remains bound to runner replica count.

**Throughput property — how does the orchestrator stop blocking?**

1. **Direct remote gRPC.** Grader on a separate machine, same synchronous call. Buys independent deploy; orchestrator worker still blocks on judge latency.
2. **Queue-backed variant on top of the same service (this ADR).** Orchestrator publishes the grade job and blocks on a local future keyed by trial id; grader workers consume from the queue. Buys independent throughput scaling and native back-pressure. Both variants ship — direct gRPC for interactive one-shots, queue for large batches.
3. **Async-native `TrialGrader.grade`.** Rejected — ripple across Conductor, Orchestrator, `run_trial`, tests, every downstream impl for a property the queue variant delivers with a client-side blocking future.

**Wheel + image shape.**

1. **Custom subset build target + slim image (this ADR).** New hatch build target enumerating the grader's runtime graph; three-stage Dockerfile mirroring the runner image. Follows the runner-wheel-split pattern.
2. **`grader` extras on the base wheel.** Simpler but the closure is enforced by hope; pull-vs-build depends on the wheel shape and extras do not compose cleanly with the subset-wheel-in-Docker model.
3. **Separate PyPI wheel.** Rejected for the same reason [ADR-0025](0025-runner-wheel-split.md) rejected it — the published surface stays one wheel; multiple PyPI targets would fragment the discovery story.

**Reference queue backend.**

1. **Redis Streams (this ADR).** Already present in the standalone compose; native back-pressure via consumer groups; testcontainer-friendly.
2. **RabbitMQ.** More features than needed; new ops surface.
3. **SQS.** Cloud-locked.

Redis Streams becomes the reference impl; RabbitMQ / SQS remain future backends behind the same plug-in seam.

## Decision

Adopt the extraction target #1, the throughput property #2 (both variants ship), the wheel + image shape #1, and Redis Streams as the reference backend.

Concretely:

1. **Grader plug-in receives serialisable configuration only.** The construction context becomes endpoint URL + auth token + timeout + run-scoped logger. No live runtime-backend object crosses the seam.
2. **New RPC contract for the grader, owned by the grader.** The runner's grade RPC ships one more release with a deprecation notice, then goes away.
3. **Standalone grader service binary.** Embeds the pure evaluator plus the runner-specific grader glue. No tool execution, no sandbox, no env-state.
4. **New grader plug-in registered under the existing plugin group,** reaching the new service.
5. **Queue-backed variant registered under the same plug-in group.** Redis Streams reference impl; the caller-facing API is unchanged.
6. **Custom subset build target + `grader.Dockerfile` + publish workflow row.** Follows the runner-wheel-split pattern exactly. Image published as `tolokasoft1/tolokaforge-grader` under the existing publish workflow, RC-smoke gated.
7. **Import-boundary lint across the plug-in seams.** Contracts filled in as each seam gets its permanent home.
8. **Judge-backed plug-in as the second registered impl.** Offline rejudge folded onto the seam; three grader call paths collapse to one.
9. **Public documentation** lands on the release event that publishes the transport split or later.

## Consequences

**Positive.**

- Grader deploys, scales, and releases independently of the runner.
- Batch throughput on judge-heavy runs no longer bounded by grader latency per trial.
- Plug-in seam gains a second (and eventually third) registered implementation — the seam becomes verified rather than presumed.
- Distribution follows established patterns; no new registry, no new CI workflow.

**Negative / tradeoffs.**

- One additional binary and image to maintain, publish, and RC-smoke.
- Two implementations of the grader plug-in live simultaneously during the one-release deprecation window; the runner's grade RPC continues to receive changes for that window.
- Redis Streams becomes a load-bearing dependency in the reference impl (though other backends can plug behind the same seam later).

**Reversibility.**

- If the grader-service split proves not worth its overhead, the grader plug-in can revert to the runner-RPC impl (kept for one release for exactly this reason). No PyPI or consumer-visible change is required.
- If Redis Streams proves the wrong reference backend, the plug-in accepts alternatives — the swap is a plug-in change, not a Protocol change.
- If the queue variant proves premature (batch throughput was not actually a limit), the direct-gRPC grader still works and is the primary interactive path.

**Follow-ups (out of scope for this ADR).**

- Alternative queue backends (RabbitMQ, SQS) behind the same plug-in.
- Retirement of the runner's grade RPC — happens in the release after the split ships.
- A `--grader` command surface exposed on the CLI's single-trial entry, once the new plug-ins register.
