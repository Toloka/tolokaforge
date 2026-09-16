# 0046. Live tracing: a TrialObserver seam and an OTLP exporter behind the `otel` extra

- **Status:** Proposed
- **Date:** 2026-09-15
- **Deciders:** @bberkes-toloka (proposer), @CiroGamboa (engine owner, review pending)
- **Supersedes:** —
- **Superseded by:** —

## Context and Problem Statement

Trajectories reach an observability backend today only after the run: the private
`langfuse-uploader` (tolokaforge-tools) replays a finished trial bundle into Langfuse through its
legacy ingestion API. Reviewers of the arena want to watch a trial while the agent is still
working, per LLM call and per tool call, and later re-upload the same trial from its bundle
without producing a second trace. The engine has the three seams the plan for that work names
(`ToolCallingLoop` owns every model call and tool execution, `RunDisplayEvents` carries per-call
timing but no content, `TrialArtifactWriter` fires at trial end) but no hook that hands a
consumer the *content* of a call, and no transport that leaves the machine while the run goes.

Three forces make the shape non-trivial:

1. The engine is public and vendor-neutral. Langfuse is one receiver; the wire must be OTLP and
   nothing in core may depend on a Langfuse client or on a private library.
2. A live trace and an offline re-upload of the same trial must be **one** trace: the ids must be
   derivable from run, task, trial and attempt alone, on both producers (id contract v1 of the
   Langfuse plan, already proven on the instance with a hand-built OTLP payload).
3. Export must never slow down or fail a trial: bounded queue, background export, a visible drop
   counter, flush points at run end, "warn, never fail".

A secondary force: arena consumers want the trace to name the model with one identity across
route spellings (`openrouter/openai/gpt-6-astra` and `openai/gpt-6-astra` are one model) and with
the arena's own naming rules (a stem such as `gpt55_xhigh`, an abbreviated id such as
`tencent/hy3`). That knowledge is configuration that lives outside the engine.

## Decision Drivers

- Parity: one trial, one trace, whichever producer wrote it (live exporter or bundle uploader).
- Vendor neutrality of the public engine: OTLP out, no Langfuse-specific code in core.
- Zero effect on a trial's outcome or speed: the observability layer only warns.
- Determinism and testability without a collector: spans built from known ids and clocks,
  asserted with an in-memory exporter.
- The trial's parallel workers: no ambient context propagation, parents passed explicitly.
- Model identity as configuration: the engine emits raw `(provider, name)` by default and can be
  pointed at a normaliser plus a rules file the deployment owns.

## Considered Options

1. **`TrialObserver` Protocol in core, one implementation in a `tolokaforge[otel]` extra.** A
   no-op default; the conductor opens and closes a trial, the loop reports each generation and
   tool call with content; the OTel implementation synthesises finished spans with deterministic
   ids and pushes them through a bounded queue to an OTLP/HTTP exporter.
2. **OpenTelemetry auto-instrumentation of `litellm` plus tracer spans in the loop.** Uses the
   SDK's tracer and context propagation; ids are random unless an `IdGenerator` is smuggled
   per span; litellm's attribute schema differs from the bundle's; parents would ride contextvars
   across worker threads.
3. **Per-trial export from the artifact writer.** A writer implementation that uploads the finished
   bundle at trial end. No engine change beyond a writer, but nothing is visible until the trial
   ends, so it does not answer the request.
4. **Gateway-side logging (LiteLLM callback).** No engine change; covers only model calls on the
   gateway route, no tool spans, no trial structure. Rejected in the plan (D10).

## Decision

We will adopt **Option 1**.

- `tolokaforge.observability` is a new core package with no third-party dependency:
  `ids` (the id contract v1: `trace_id = uuid5(NS, "trace|<run_tag>|<run_id>|<task_id>|<trial_index>|<attempt>")`,
  `observation_id = uuid5(NS, "obs|<trace_id>|<kind>|<index>")[:16]`, `kind` in `root`, `gen`,
  `tool`, `judge`, `judge_tool`, `index` = the message position in the recorded trajectory),
  `observer` (the `TrialObserver` Protocol, `TrialIdentity`, `NullTrialObserver`,
  `CompositeTrialObserver`, and `LoopObserverBinding`, the per-trial, per-role view the loop
  sees), `model_names` (a `ModelNameResolver` Protocol with a raw default and an adapter for
  `toloka-model-name-normalizer` loaded lazily when configured), and `factory`
  (`build_trial_observer(config, ...)`).
- `tolokaforge.observability.otel` is the one implementation and lives behind the `otel` extra
  (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`). It does not use the SDK's
  tracer: it builds `ReadableSpan` objects with the contract's ids and the trial's own clocks
  (child spans as each call ends, the root span at trial end with the grade), and hands them to a
  bounded `SpanQueue` (background thread, batches, drop counter, `flush`, `shutdown` with a
  budget). Parents are explicit; no contextvars. Attributes follow the Langfuse OTel conventions
  (`langfuse.trace.*`, `langfuse.session.id`, `langfuse.observation.*`, `gen_ai.*`) so the
  receiver renders them like the uploader's traces; a non-Langfuse collector still gets valid
  spans.
- Hook points: `ToolCallingLoop.observer` (`generation` after the assistant message is recorded,
  `tool_call` after the tool message is recorded, so `index` equals the message position the
  bundle uploader will see), `Conductor.run` (`trial_started` before the loop, `trial_finished`
  after grading and before the bundle is written), `Orchestrator.run` (build at start,
  `run_finished` at the end, receipt written as `tracing_receipt.json` in the run directory).
  Every observer call is wrapped: an exception is logged and swallowed.
- Configuration under the existing `observability.tracing` block: `exporter: otlp` with
  `endpoint` switches it on (default `none`); `run_id` (the external execution identity handed in
  by a workflow; default the engine run id), `run_tag` (id namespace, default `v1`),
  `session_id`, `label`, `tags`, `metadata`, `model_name_normalizer` (`none` or `toloka`),
  `model_name_rules` (the rules file for the normaliser), queue and flush knobs. Credentials for
  the receiver travel in the standard `OTEL_EXPORTER_OTLP_HEADERS` variable the SDK reads; the
  engine never logs them.
- The bundle starts carrying the trial's final `attempt_id` in `trajectory.yaml`, and a run with
  tracing on writes `run_identity.json` (`run_id`, `run_tag`) into the run directory, so the
  offline uploader derives the same trace ids from the bundle.
- Redaction: tool-call arguments go through `SensitiveKeyRedaction`; tool outputs and message
  text are free text (key-based redaction does not apply) and are capped, not redacted; base64
  image blocks are dropped from span attributes (media stays a receiver-specific step outside the
  engine). The receiver's headers are read through the `SecretManager`, so their value is in the
  log-redaction set.
- Scores stay outside the engine: the grade rides as root-span attributes (`pass`, `score`,
  component values in metadata); Langfuse scores are posted by the uploader's `attach-grades`
  or a receiver-side step.

## Consequences

### Positive

- Reviewers see a trial's generations and tool calls seconds after each call, with the model
  identity, tags and metadata the offline uploader would produce; a later re-upload from the
  bundle updates the same trace.
- Core gains one small Protocol and no dependency; the exporter is optional and testable offline.
- The observer seam serves other consumers (a live dashboard sink, a cost meter) unchanged.

### Negative / Trade-offs

- Two producers share one span schema by convention; the parity test (a trial traced live, then
  uploaded from its bundle, leaves one trace with one record per observation) is the guard and
  runs against the TEST project, not in unit CI.
- Judge generations are not exported live in this increment: the judge may run in the runner
  service, out of the orchestrator's process; the uploader's `attach-grades` covers them.
- The root span (trace name, input, output, grade) is emitted at trial end; child spans carry
  the trace name, session and tags so the trace is browsable earlier, but the grade appears last.
- One more optional dependency set to keep current.

### Follow-ups

- Code changes required: this ADR's implementation (observability package, loop, runner,
  conductor, orchestrator, config), `attempt_id` on `Trajectory`, `run_identity.json`.
- Documentation to update: `docs/OBSERVABILITY.md` (new), run-config reference for
  `observability.tracing`.
- Tests to add: id parity with the uploader's golden ids, in-memory exporter tests (attributes,
  deterministic ids, redaction, queue saturation, flush), loop hook indices, conductor lifecycle,
  config defaults locked (`exporter: none`), a TEST-project parity probe (manual, documented).
- Later increments: judge spans when the judge runs in-process; per-call ids and roles in
  `metrics.usage.calls`; an external run id on the CLI.

## Amendment 2026-09-16: the persisted bundle is attached to the trace

The seam gains a fourth trial hook, `trial_persisted(identity, trial_dir)`, called through the
conductor's `trial_persisted(spec)` by the trial executor once nothing writes into the trial
directory any more (after its `metrics.yaml` amendment and service-log capture; the first live
round trip caught a `provisioning_duration_s` line the conductor-time announcement missed) and, on
the conductor's error path, when a bundle was left behind, so a receiver can attach the files a
trial directory holds. The OTLP observer, when
`observability.tracing.attach` is `all` (the default) or `core`, then registers and uploads every
regular top-level file of the trial directory through the receiver's media REST API (Langfuse:
`POST /api/public/media`, the presigned `PUT` with `x-ms-blob-type: BlockBlob` on Azure Blob, the
confirmation `PATCH` reported as 200 so the receiver's sha256 dedup works), `env.yaml` and
`trajectory.yaml` gzipped with `mtime 0`, and writes **manifest v2** to the trace-level metadata
(`attachments_schema: 2`, `attachments` keyed by the file name with media id, token, both hashes,
both sizes, content type and encoding, `attachments_complete`, `attachments_skipped`) through a
`trace-create` update under the same trace id on the legacy ingestion API: metadata keys merge,
so nothing the spans wrote changes and the root span's end time stays the trial end. The base URL
derives from the OTLP endpoint and the headers are the exporter's own. Every file is scanned
first against the `SecretManager`'s credential values and the key-shaped patterns the offline
connector uses; a hit skips the file and lands in `attachments_skipped`, bytes are never
rewritten. The step runs in the trial's thread after the trial is over, bounded by per-request
timeouts, and never raises; `tracing_receipt.json` counts attachments registered, uploaded,
deduplicated, skipped and failed. The offline `langfuse-connector download` rebuilds a trial
directory from either producer's manifest. Media remains receiver-specific, which is why the
step lives next to the OTLP observer behind the `otel` extra and not in core.

## Amendment 2026-09-16: the receiver comes from the environment, the project is checked

A live run's destination (which Langfuse project, under which keys) is the deployment's
configuration, not the engine's: the connector in tolokaforge-tools keeps a registry of named
destinations and launches the engine under one of them (`with-destination`). To make that
possible without any Toloka concept in this repository, the exporter reads the endpoint from the
standard `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_ENDPOINT` when
`observability.tracing.endpoint` is absent (the config validator no longer requires the field; the
factory does, at run start), merges `TOLOKAFORGE_TRACING_TAGS` into the config's tags (a prefix
carrying two different values is a configuration error), and gains one setting,
`observability.tracing.expect_project` (or `TOLOKAFORGE_TRACING_EXPECT_PROJECT`): the
receiver-side project the credentials must open. Before the first export the factory asks the
receiver (Langfuse `GET /api/public/projects` through the OTLP headers, REST base derived from the
endpoint); a mismatch refuses to trace with a `TracingConfigError` before any service starts, an
unreachable receiver leaves the run `unverified`, and `tracing_receipt.json` records both
`expect_project` and `project_verified`. This gives the live path the same fail-closed guard the
offline connector runs, so a key pair that opens another project than a launcher claims (a
misnamed pair in an environment file is the everyday way this happens) can no longer trace into
it; a 401 (credentials that open no project) refuses as well, while a receiver that cannot list
projects from where the run happens (a WAF alias answering 403) leaves the run `unverified`.

## Links

- Related ADRs: [ADR-0019](0019-front-end-plugin-namespace.md) (the optional-extra pattern)
- Related code: `tolokaforge/observability/`, `tolokaforge/core/loop.py`,
  `tolokaforge/core/conductor.py`, `tolokaforge/core/orchestrator.py`
- External references: Langfuse OpenTelemetry ingestion attribute conventions; the workspace plan
  `docs/TECHDEL-497-langfuse-integration/PLAN.md` section 8; `tolokaforge-tools/tools/langfuse-uploader`
  (id contract v1, `ids.py`).
