# 0021. Component-oriented monitoring for `tolokaforge run`

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** @CiroGamboa
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Before this ADR, tolokaforge treated infrastructure health as **log lines**. Docker services, gRPC runner-connect probes, per-trial containers — each source emitted free-form INFO / WARNING records that scrolled into the same global log stream. Two consequences fell out of that shape:

- **Retry loops looked like errors.** The gRPC runner-connect loop in `tolokaforge/core/shared_stack_runtime.py` polled at 1 s cadence with a 30 s ceiling and logged `"Waiting for Runner service (attempt N, elapsed=Xs/30s)"` **per attempt**. Every startup produced ~30 INFO lines that read as "something is broken" even though the runner came up cleanly.
- **The health of any individual component was invisible.** The panel's services widget was a snapshot from `phase_changed(services=[…])` — two batch updates per run (`starting_services` / `services_ready`). Between them, per-service transitions were unobservable. Runs against complex infrastructures (remote k8s clusters, N copies of each component type) would drown in log lines with no compact status view.

We wanted a monitoring model that:

1. Treats each independently-monitored runtime entity — a docker service, a gRPC runner instance, a k8s pod, a per-trial container, a future ssh-tunnelled subprocess — as **one row in a status board**, keyed by a stable id.
2. Keeps the happy path **quiet** — a healthy component is one compact line, not a wall of INFO records.
3. **Escalates** failures — an unhealthy component's log tail auto-expands beneath its row without printing above the panel.
4. Stays **transport-agnostic** — a future k8s watcher fires the same events; the panel does not care about the transport.
5. Mirrors [ADR-0019](0019-front-end-plugin-namespace.md)'s pluggability contract — reporters implement a narrow Protocol; adding a new transport is reporter-side work with zero panel changes.

## Decision Drivers

- **Independence of components.** The engine, the runner, the per-trial substrate, and future k8s pods must self-report; the panel does not scrape or parse log strings.
- **Compact happy path.** A healthy 5-service run should render one 7-row widget (5 components + 2 borders), not 30+ scrolling log lines.
- **Failure surface.** When a component transitions to unhealthy mid-run, the panel must resurface the widget so the operator sees the failure — even if trials are already dispatching.
- **Backwards compatibility.** Existing callers that only fire `phase_changed(services=[…])` must keep working; the ServiceSnapshot / ContainerSnapshot rows should populate the new model via an adapter shim, not through a new call site.

## Decision

Introduce a **Components** abstraction alongside the existing `RunDisplayEvents` seam: transport-agnostic status types, four new kwarg-only Protocol methods, one panel widget, and adapter shims that lift the legacy `phase_changed(services=…)` / `trial_provisioned(containers=…)` records into the new model.

### Types (in `tolokaforge/core/run_display_events.py`)

- `ComponentKind` — `Literal["docker.service", "grpc.client", "container", "k8s.pod", "process", "remote"]`. Extend the literal set when a new reporter shape is added; the panel's renderer falls back to the raw string for unknown kinds.
- `ComponentPhase` — `Literal["pending", "starting", "healthy", "degraded", "unhealthy", "stopped", "dead"]`. `{degraded, unhealthy, dead}` trigger the auto-expand-on-fail behaviour.
- `ComponentSnapshot` — `TypedDict` with `id`, `kind`, `phase`, `detail`, `owner`.
- `build_component_id(namespace, kind, instance)` — canonical id builder. All reporters route through it to preserve the panel's one-row-per-id invariant.

### Protocol methods (all kwarg-only, defaulted no-ops on `_NullRunDisplayEvents`)

```python
def component_registered(self, *, snapshot: ComponentSnapshot) -> None: ...
def component_status_changed(self, *, snapshot: ComponentSnapshot) -> None: ...
def component_log_appended(self, *, component_id: str, level: str, message: str, ts: float) -> None: ...
def component_unregistered(self, *, component_id: str) -> None: ...
```

`component_status_changed` on an already-known id updates the same row in place. Per-attempt polling loops fire it repeatedly with the same id and a fresh `detail` — the panel's row updates, no log line scrolls.

`component_log_appended` is a **distinct channel** from the panel's global `_LogSink` ring buffer. Records land in a small per-component ring bounded at `_COMPONENT_TAIL_BUFFER_MAX = 32` (rendered at most `_COMPONENT_TAIL_MAX_LINES = 5`) and surface beneath the component's row whenever the buffer is non-empty and the phase is NOT `healthy` / `stopped`. This is what stops WARNING-level retry chatter from scrolling above the panel.

**The generic escape hatch for any startup subsystem** — an emitter can tag its own `logger.*` calls with `extra={"component_id": ...}` and `_LogSink` routes them to the tagged component's tail automatically, at the natural log level. No downgrade to DEBUG, no bespoke wiring per subsystem. The gRPC runner-connect loop and `GrpcRunnerClient.health_check` are the reference users; docker-service startup, HealthProbe retries, and any future k8s / SSH reporter can opt in the same way.

### Namespace convention

- `engine/…` — the run's engine-level infrastructure (Docker services, gRPC runner client, EngineStack).
- `trial/<trial_id>/…` — per-trial substrate (multi-container tasks; today populated via the `trial_provisioned` adapter shim).
- `worker/<n>/…` — future: worker threads / processes.
- Any transport-native namespace, e.g. `k8s/<pod-name>`, is up to the reporter.

The panel groups by leading namespace (component `owner`) then by id. Widget sort is deterministic across refreshes.

### Panel widget (in `tolokaforge/dx/live_panel.py`)

- `_components: dict[str, ComponentSnapshot]` — keyed by id, last-write-wins on update.
- `_component_log_buffers: dict[str, deque[tuple[float, str, str]]]` — one bounded ring per component.
- `_render_components_table` — flat `Text` (no Rich `Table`) inside a `Panel`, one row per component: `[icon] [id] [phase] [detail]`. Unhealthy components render up to five tail lines indented beneath their row, prefixed with `└─`.
- Visibility rule: shown when `_components` is non-empty **and** either `_total_trials == 0` (startup window) **or** at least one component is in an unhealthy phase. Healthy post-startup runs hide the widget; a mid-run failure re-surfaces it.

### Adapter shims (backwards compat)

`phase_changed(services=[…])` and `trial_provisioned(containers=[…])` still fire. Inside the panel, `_service_to_component` and `_container_to_component` lift each `ServiceSnapshot` / `ContainerSnapshot` into a `ComponentSnapshot` at the `phase_changed` / `trial_provisioned` handler. Callers that only fire the legacy events populate the Components widget for free.

### Reference reporter: gRPC runner-connect (`shared_stack_runtime.py`)

`GrpcRunnerClient.__init__` gains `events: RunDisplayEvents | None = None`. `connect()` fires:

1. `component_registered(snapshot={"phase": "starting", "detail": f"connecting to {addr}"})` before the loop.
2. `component_status_changed(snapshot={"phase": "starting", "detail": f"attempt {n}, elapsed=…"})` per attempt — same id, in-place update.
3. `component_status_changed(snapshot={"phase": "healthy" | "unhealthy", "detail": …})` on outcome.

Per-attempt `logger.info` at `shared_stack_runtime.py:193` is downgraded to `logger.debug`, so `-v` still surfaces attempts for debugging. `SharedStackRuntimeBackend.__init__` accepts `events=` and threads it to the client; `Orchestrator._create_runtime_backend` passes `self._events`.

## Consequences

### Positive

- **Compact happy path.** A cold-Docker startup renders one Components row per service + one for the runner client, all transitioning `starting → healthy`. No per-attempt log lines scroll.
- **Auto-expand-on-fail.** A component that fails during a run resurfaces the widget with its tail. The operator never hunts for context.
- **Pluggable transport.** A future `K8sRuntimeBackend` implements the same events. Zero panel work.
- **Backwards compatible.** Every existing caller keeps working via the adapter shims. Out-of-tree implementers of `RunDisplayEvents` inherit the no-ops via structural typing.
- **Debuggable.** `-v` (`--verbose`) surfaces per-attempt DEBUG records for diagnosis. The panel view stays calm at INFO+.

### Negative

- **Two coexisting paths.** `phase_changed(services=[…])` and `component_*` events both target the same widget. Ownership overlap can confuse a reader; the adapter shim doc-string names the direction.
- **Widget-title vs region-name split.** The layout children are `"engine_components"` and `"task_infrastructure"` (both filtered views of the same `_components` dict); the panel titles are `"Engine Components"` and `"Infrastructure"`. Every existing test that referenced the older single `"services"` / `"components"` region name had to be updated.
- **Per-component log buffers add memory.** Each tracked component owns a 32-entry deque. Worst-case runs with hundreds of components would grow the memory footprint; not a concern today.

### Neutral

- **The old services widget is gone.** Its Panel title changed from "Services" to "Components". Two canonical SVG goldens (`panel_boot_log_{80,120}.svg`) were regenerated to match.
- **Two-widget split over the same model.** `_components` stays a single flat dict keyed by component id; the render side splits it into two adjacent widgets — **Engine Components** (rows where `owner == "engine"`, i.e. tolokaforge's own docker services and gRPC clients) and **Infrastructure** (rows where `owner` starts with `"trial/"`, i.e. the per-trial services declared by the task pack). The Infrastructure widget is elided when no trial component is present, so single-container task packs (e.g. the built-in `coding` engine stack) see only the top Engine Components widget. Rationale: engine health and task-declared per-trial infrastructure are separate operator concerns — mixing them into one table obscures which one is actually broken when something goes red.
- **Focus (highlight) and reveal (`l`) are separate operator gestures.** `[` / `]` walk focus across the union of both widgets (see `docs/CLI.md § Keyboard navigation`); the focused row is rendered in reverse-video with a leading `▶ ` glyph so it stands out on any terminal theme. The tail does NOT auto-expand on focus — the operator presses `l` while a row is focused to reveal that component's buffered history, and `l` again to collapse it. Unhealthy rows still auto-expand independently of this toggle so failures surface without ceremony. Rationale: keep navigation cheap on layout rows; the operator can walk the widget without every row ballooning to a multi-line block just because it was passed through.
- **`l` is context-sensitive.** With a component focused, `l` toggles that component's `_component_logs_shown` entry. With no component focused, `l` retains its pre-existing meaning — flip the focused-trial log pane between structured summary and log tail. The overload is safe because the two modes never share a target: the trial-log gesture only makes sense when no component row is selected.
- **Docker `created` under per-trial isolation is `stopped`, not `unhealthy`.** `_map_docker_status_to_component_phase("created", "services_ready")` returns `"stopped"`: per-trial-mode engine services are legitimately declared but never started because the per-trial stack owns runtime. The visual is a neutral `·` glyph rather than a red `✗`.

## Alternatives Considered

- **Log-level tweaks only** (downgrade the runner-connect chatter to DEBUG, extend the Boot-log filter). Cheapest fix, but does nothing to model complex infrastructures. Rejected on grounds of "the ugly logs are a symptom of a design gap, not a logging bug."
- **Per-phase `logging.Filter` on `_LogSink`.** A filter that recognises retry-in-progress records and drops them during the startup phase. Works for known noise sources; opaque, hard to debug, and does not model the component graph. Kept as a future option if new noise sources appear.
- **Rate-limited dedup summariser.** `"Waiting for Runner service … [+27 more]"`. Solves the noise but not the monitoring gap.

## References

- [ADR-0011](0011-seam-and-declaration-conventions.md) — seam-definition conventions this ADR follows (Pattern A: behaviour on the Protocol, snapshots as TypedDicts on the wire).
- [ADR-0019](0019-front-end-plugin-namespace.md) — the `tolokaforge.dx` plug-in namespace this ADR extends.
- `docs/CLI.md § Components widget` — user-facing rendering contract.
- `docs/RUNTIME_BACKENDS.md § Reporting component status` — reporter-side authoring guide.
