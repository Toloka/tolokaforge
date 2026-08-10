# 0026. Service-readiness contract as a fourth entry-point-registry seam

- **Status:** Accepted
- **Date:** 2026-08-03
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

A docker `healthcheck:` reporting a container `Healthy` is not the same signal as
"a client can invoke this service." A container-internal loopback probe can pass
while the published host port reaches no listener. The runner image
(`runner.Dockerfile`) probes `localhost:50051` from *inside* the container; on a
host that binds IPv6-only (`bindv6only=1`) against a `[::]` server bind, the
loopback probe stays green while the IPv4 published-port DNAT reaches nothing
from the calling process.

The #801 localization (`docs/plans/2026-08-03-issue-801-runner-host-port-readiness.md`)
is the empirical evidence: it established that this failure class is real but
**not reproducible in core on the available platforms** — the `[::]` bind
collapses to IPv4 where there is no container IPv6 stack, so CI and Mac cannot
exhibit the wedge. Today `PerTrialRuntimeBackend.provision` trusts
`docker compose up --wait` and hands off; a caller then hits a 30×1 s connect
loop against an unreachable endpoint with no diagnostic captured at the failure
point.

The boundary — *container-healthy* versus *client-invokable* — has no name in
the codebase. Without one, the class of bug can be reintroduced silently every
time a new service is added, and every failure surfaces as a downstream timeout
rather than an actionable diagnostic. This ADR names that boundary as a
first-class contract.

## Decision Drivers

- **The boundary must be authoritative from the calling process**, not inferred
  from a container-internal healthcheck that cannot observe host reachability.
- **Substrate-agnosticism.** The readiness answer depends only on `host:port` +
  a timeout budget; it must not couple to the docker/compose handle so the same
  contract serves any endpoint source.
- **Swappability is real.** gRPC, HTTP, and TCP are three genuinely different
  readiness checks today, and downstream adapters will want their own — a named
  second variant exists, so ADR-0011's "introduce a Protocol" test is met.
- **Reuse the fail-loud registry idiom** rather than inventing a parallel
  discovery mechanism; the three existing entry-point groups already encode the
  duplicate/unknown/broken-import policy.
- **Cheap by construction.** Readiness must cost milliseconds so it can gate
  every provision without reintroducing a slow loop.

## Considered Options

1. **Probe inline in the backend** — hard-code a gRPC channel-ready check inside
   `PerTrialRuntimeBackend.provision`.
2. **A `ServiceReadinessProbe` Protocol behind an entry-point registry** — with
   built-in `grpc`/`http`/`tcp` probes, an `InMemory*` fixture, and a canonical
   contract test. **This ADR.**
3. **Extend `RuntimeBackend.await_ready`** — fold readiness into the existing
   lifecycle method on the backend Protocol.

## Decision

We adopt **Option 2**: a `@runtime_checkable ServiceReadinessProbe` Protocol
(`probe(endpoint, *, timeout) -> ReadinessResult`) over frozen-dataclass value
objects `ResolvedEndpoint` and `ReadinessResult`, three built-in production
probes (`GrpcReadinessProbe`, `HttpReadinessProbe`, `TcpReadinessProbe`), an
`InMemoryServiceReadinessProbe` fixture with a call log + failure knobs, and a
canonical contract test — the full ADR-0011 Pattern-A shape.

The probes are discovered through a **new entry-point group**
`tolokaforge.service_readiness_probes`, keyed by endpoint kind (`grpc` / `http`
/ `tcp`), reusing the existing fail-loud `discover_entry_points` / `_load` machinery in
`plugin_registry.py`. This is the **fourth entry-point-registry-backed seam**:
three registry groups ship today in `pyproject.toml` (`runtime_backends`,
`trial_graders`, `conductors`); this adds the fourth, validating that the
registry idiom generalises beyond the orchestrator seams. (The count worth
stating is the registry-group count, three→four — not a Pattern-A-consumer
count, since ADR-0011's table already lists ≥6 Pattern-A consumers.) The
adapter-declared-name work in #800 introduces a related registry-shaped seam by
the same idiom; it is not yet merged, so this ADR cross-references it by shape
rather than by number.

Unlike the three orchestrator groups, each of which carries a per-group
frozen-dataclass context, a readiness probe needs no build dependencies, so its
factory is arg-less: `ReadinessProbeFactory = Callable[[], ServiceReadinessProbe]`.
The factory-callable indirection is kept — rather than registering the class
directly — to match the sibling groups' idiom (uniform arg-less construction, so
a factory is the faithful match, not a divergent-constructor workaround). No
`*Context` dataclass is invented for a seam with no deps to pass.

### Readiness is cheap; the per-trial connect stays deferred

Readiness answers *reachable + protocol-ready* in milliseconds — a gRPC channel
reaching READY, `GET /health` returning 2xx, or a TCP connect completing. It is
deliberately **not** a full protocol handshake. This is orthogonal to the
existing deferred per-trial gRPC connect (`per_trial_runtime.py`), which stays
lazy and unchanged: the runner client's `connect()` is deferred to the first
per-trial RPC so provisioning latency is not charged the connect cost for a
trial that never exercises the RPC surface. The readiness gate is a cheap
host-side reachability check at provision time; the deferred connect is the
full channel established on first use. Both remain — they answer different
questions.

### We are not fighting the container loopback healthcheck

The `runner.Dockerfile` loopback `HEALTHCHECK` stays exactly as-is. It answers
"is the process up inside the container", which is a useful and correct signal.
The readiness probe is the new *authoritative* signal from the calling process:
it answers "can I reach and speak to this service from here." The two are
complementary, not redundant — the whole point is that the loopback check cannot
observe a host-reachability gap, so a second, host-side signal is required
rather than a stronger container-internal one.

## Consequences

### Positive

- The container-healthy vs client-invokable boundary is a named, tested
  contract; the #801 class cannot be reintroduced silently.
- Readiness is swappable per endpoint kind with no substrate edit — an adapter
  registers a custom probe under a kind name via entry-point.
- The registry idiom is shown to generalise beyond the orchestrator seams
  (three→four groups), reducing the cost of the next registry-backed seam.

### Negative / Trade-offs

- The readiness factory diverges slightly from the three existing groups (arg-less
  vs context-carrying). The divergence is honest — probes have no build deps — and
  is documented here rather than papered over with an empty context dataclass.
- A fourth registry group is one more discovery surface to keep in the canonical
  registration snapshot (`test_builtin_plugin_registrations.py`).
- The HTTP probe path is a v1 convention: `HTTP_HEALTH_PATH = "/health"` is
  hard-coded rather than configurable. If a real pack ever needs a different
  path, the escalation is an additive optional `ReadinessSpec.path` field
  (default `/health`, so existing packs validate unchanged) threaded to the
  probe — not introduced here to avoid config surface no pack yet needs.

### Follow-ups

- **#805** — `fix(runner)`: bind the gRPC server to explicit IPv4 wildcard
  (`0.0.0.0`) instead of `[::]`. The readiness gate *diagnoses* the #801 class;
  this bind change is what makes an IPv6-only-bound environment actually run.
- **#806** — `feat(runtime)`: multi-service readiness dependency graph
  (service B waits on service A's readiness). This contract is per-service only.

## Links

- Related ADRs: [0011](0011-seam-and-declaration-conventions.md) (Pattern A),
  [0007](0007-runtime-backend-protocol.md),
  [0010](0010-runtime-backend-provisioning-contract.md)
- Related code: `tolokaforge/core/service_readiness.py`,
  `tolokaforge/core/plugin_registry.py`
- External references: #801 (localization), #803 (this ticket), #805, #806
