# 0047. Live tracing: a TrialObserver seam and an OTLP exporter behind the `otel` extra

- **Status:** Proposed (amended through 2026-09-18; "Where the decision stands" below is the current shape)
- **Date:** 2026-09-15, last amended 2026-09-18
- **Deciders:** @bberkes-toloka (proposer), @CiroGamboa (engine owner, review pending)
- **Supersedes:** —
- **Superseded by:** —

## Where the decision stands (2026-09-18)

The Decision and Consequences sections record the first cut of 2026-09-15; the amendments follow
them in date order. Read together they leave the decision here:

- **The engine owns the seam, nothing receiver-shaped.** `tolokaforge/observability/observer.py`
  (the `TrialObserver` hooks, `trial_persisted` included, the null and composite observers, the
  receipt), `ids.py` (the id contract, now **version 2**: observation kinds `root`, `gen`, `ugen`,
  `tool`, `grading`, `jgen`, `jtool`, `event`, and score ids, shared with the offline connector)
  and `factory.py` (the run identity, `run_identity.json`, `tracing_receipt.json`, and the
  discovery of trial-observer plugins under the `tolokaforge.trial_observers` entry-point group).
  `TracingConfig` keeps only receiver-neutral fields and rejects unknown fields. Plugin settings
  pass through `options.<plugin>`; the plugin validates its own namespace. The current pairing
  is plugin API **4**: version 4 widens the id contract's kind set with the **preview kinds**
  (`proot`, `pgen`, `pugen`, `ptool`, `pjgen`, `pjtool`), which a plugin writing for a receiver
  whose observations are append-only needs (`docs/OBSERVABILITY.md`, "The write-once layout"). The seam
  follows [ADR-0011](0011-seam-and-declaration-conventions.md): `InMemoryTrialObserver` with a
  call log and failure knobs, canonical hook tests, and strict snapshot-pinned persisted models.
  `ExportReceipt` follows [ADR-0021](0021-component-monitoring-seam.md)'s receiver-neutral shape:
  common span counters, namespaced plugin counters in `extra`, and opaque receiver facts in `details`.
- **The Langfuse observer is the `tolokaforge-langfuse` wheel** (`tolokaforge_langfuse/`, a
  workspace member released on its own cadence): the OTLP exporter, the attachment step, the
  trial-end projection of the bundle, the gradings, the deployment profile and the model-name
  resolution. The engine's `otel` extra resolves to it; the pairing is a versioned contract
  checked at run start, not a pip dependency. The Decision's "`tolokaforge.observability.otel` is
  the one implementation and lives behind the `otel` extra" is superseded by the packaging
  amendment.
- **Enablement and receiver:** `observability.tracing.exporter: otlp`, or the one switch
  `LANGFUSE_TRACING_ENABLED`; the receiver from the config, the standard `OTEL_EXPORTER_OTLP_*`
  variables or the plain `LANGFUSE_*` ones; the project checked fail-closed before the first
  export; the deployment's values in a profile file. A switch that is on while no plugin produced
  an observer refuses the run start.
- **What a live trace carries** has grown past the Decision's "scores stay outside the engine" and
  the Consequences' "judge generations are not exported live" and "media stays outside the
  engine": at `trial_persisted` the plugin attaches the trial's files and completes the trace from
  the bundle (grading with judge transcript and scores, user turns, tool executions, events,
  media), so the live trace and an offline upload of the same bundle are one trace; a golden
  parity test committed in both repositories guards the two projections.
- **The trace metadata** is a fixed 34-key schema plus the caller's keys (the receiver's table
  view renders nothing above 100 keys); everything else lives in the attached files, the
  observations, the scores, the tags and the native `environment` / `release` / `version` fields.
- **One vocabulary, one profile (vocabulary amendment):** the tag prefixes, the producer's
  derived tags (model facets, reasoning settings, route) and the default environment rule live
  in the plugin wheel and are imported by the offline uploader; a deployment's values arrive in
  one schema-2 profile both producers read. **Open, not decided:** a run-level object for the
  run's aggregate files, a curated / board marker, a task-version tag.

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
`observability.tracing.options.langfuse.attach` is `all` (the default) or `core`, then registers and uploads every
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
`observability.tracing.options.langfuse.expect_project` (or `TOLOKAFORGE_TRACING_EXPECT_PROJECT`): the
receiver-side project the credentials must open. Before the first export the factory asks the
receiver (Langfuse `GET /api/public/projects` through the OTLP headers, REST base derived from the
endpoint); a mismatch refuses to trace with a `TracingConfigError` before any service starts, an
unreachable receiver leaves the run `unverified`, and `tracing_receipt.json` records both
`expect_project` and `project_verified`. This gives the live path the same fail-closed guard the
offline connector runs, so a key pair that opens another project than a launcher claims (a
misnamed pair in an environment file is the everyday way this happens) can no longer trace into
it; a 401 (credentials that open no project) refuses as well, while a receiver that cannot list
projects from where the run happens (a WAF alias answering 403) leaves the run `unverified`.

## Amendment 2026-09-17: one switch, and the bundle's grading leaves with the trace

Two things were still missing for the engine to be the only producer of a new run's trace.

**The switch.** `LANGFUSE_TRACING_ENABLED=true` turns the exporter on without a tracing block in
the run config (or with one that says `exporter: none`). The receiver then comes from the plain
Langfuse variables: `LANGFUSE_BASE_URL` (traces at `<base>/api/public/otel/v1/traces`, REST at
`<base>`), `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` as the Basic header (read through the
`SecretManager` when one is initialised; half a pair is a configuration error), optional
`LANGFUSE_EXTRA_HEADERS` (`k=v,k2=v2`, a gateway's own header) and optional `LANGFUSE_PROJECT`,
which is both the expected project of the previous amendment and the trace's `project:` tag once
the check has run. The run's identity may come from the environment too
(`TOLOKAFORGE_TRACING_RUN_ID`, `_RUN_TAG`, `_SESSION_ID`, `_LABEL`), so a CI workflow traces a run
by setting variables, without editing a config. The standard `OTEL_EXPORTER_OTLP_*` variables keep
precedence: a launcher that owns the receiver still wins. The exporter itself stays OTLP; the
Langfuse names are the optional layer the attachment step already was.

**The grading.** Once the bundle is on disk (`trial_persisted`), the exporter also sends what the
live spans could not know: the run's own grading (`grade.yaml`) as a `grading:live:<run_id>`
observation under the root with its judge transcript (`judge_trajectory.yaml`) nested beneath and
its scores attached, the trace-level mirror of those scores, the trace's grading keys
(`primary_grading`, `gradings`, `grading_count` and the grade summary), and the simulated user's
turns as generations of the user model. The ids are the shared contract (`ids.py`: score ids are
`uuid5(NS, "score|trace|scope...|name")`, the live grading id is `live:<run_id>`) and the grading
carries the same content fingerprint the connector computes, so a later connector pass over the
same bundle updates these records instead of duplicating them, and finds nothing to change when
the content is equal. The events travel through the receiver's ingestion API under the attachment
step's budget; `observability.tracing.options.langfuse.gradings: false` switches them off;
`tracing_receipt.json` reports `gradings_sent`, `gradings_failed`, `scores_sent`,
`user_generations_sent`. What a live trace still lacks against an offline upload is now only what
the bundle itself lacks: INFO log events under `--attach all` and the connector's extra metadata
groups.

## Amendment 2026-09-17: the trace is completed from the bundle, the deployment speaks in a profile

A trial traced live and the same trial uploaded from its bundle by the offline uploader must be
one and the same trace, so a deployment whose engine traces live needs no upload pass at all. Two
constraints shape the answer. The engine stays tolokaforge-specific and vendor-neutral in what it
knows: it ships one **default projection** of a persisted trial directory into the receiver's
records (`tolokaforge/observability/langfuse_projection.py`) and carries no deployment value. The
offline uploader's projection is the reference; where the two must differ the engine adapts.

**The trial-end pass.** `trial_persisted` now completes the trace from the files, after the
attachment step and under its budget and breaker: the full trace metadata with every key of the
fixed schema explicit (the receiver merges metadata), the root observation, the agent generations
with their paired usage, the tool executions from the grader's `tool_log.yaml` with the
transcript text beside them (the user simulator's own calls included), the simulated user turns,
the grading with its judge transcript, its scores and the trace-level mirror, the events of the
trial logs, guard records, provisioning failures, budget hits and service captures, and media for
base64 image blocks with the token in the observation output; everything through the ingestion
API under the shared id contract, so the live spans are the preview and the bundle projection is
the truth (an upsert over the OTLP-created observations). The serialised events pass the same
data-safety scan as the files; a hit sends nothing and counts. `observability.tracing.options.langfuse.projection`
selects `full` (default), `gradings` (the previous amendment's behaviour) or `none`;
`tracing_receipt.json` counts projections, observations, events, scores and media. Drift between
the two implementations is caught by a golden parity test committed in both repositories over a
synthetic bundle; the metadata keys whose values differ by producer by design (`upload_mode`,
`uploader_version`, `trace_time_source`, `attach_mode`) and
the live root span's own keys (`generations_observed`, `tool_calls_observed`, `error`) are the
documented exclusions.

**The deployment profile.** Everything deployment-specific reaches the engine as configuration at
run time, in one TOML file named by `observability.tracing.options.langfuse.profile` or
`TOLOKAFORGE_TRACING_PROFILE` (`tolokaforge/observability/profile.py`): the receiver's native
`environment` as a literal or as a rule over one tag prefix's value with a default, the tags every
trace carries, fixed metadata, the profile version that joins
the native `version` field, and optionally the model-name rules file (which selects the `toloka`
normalizer). The engine validates the shape and applies the profile mechanically; a profile that
does not load, an environment outside the receiver's alphabet, a fixed tag under a
producer-owned prefix or a metadata key the projection writes itself is a configuration error at
run start. `LANGFUSE_ENVIRONMENT` overrides the rule and `TOLOKAFORGE_TRACING_METADATA`
(`key=value,...`) carries the launcher's per-run metadata, the same values the offline command
receives as `--metadata`.

**Native fields.** The receiver fixes a trace's `environment` at the first write it sees and no
later update changes it (verified on the instance on 2026-09-17), so the exporter puts
`langfuse.environment`, `langfuse.release` and `langfuse.version` on every span, the provisional
root included, and the trial-end pass repeats `environment` on every observation and score body
(a body without it is filed under `default`). `release` is the engine's own version, also written
into `run_identity.json` as `engine_version` for the offline uploader; `version` is the
producer's identity plus the model-name rules and the profile it ran under, so it differs by
producer by design. The `ModelNameResolver` Protocol gained `absent()` and `rules_version` so the
`model_*` metadata keys are the same set whichever resolver runs.

## Amendment 2026-09-17: the trace metadata is a fixed slim schema

The first projection wrote every fact it could read into the trace's metadata: the task facts, the
three model configurations, the model facets with their rule provenance, the environment identity,
the usage detail, the grade detail, the launcher's tags mirrored under their prefixes, the tag
profile's version. A trace carried 122 to 126 top-level keys, and the receiver's trace page renders
its metadata table only up to 100 (Langfuse 3.205.1, `PrettyJsonView`, `DEFAULT_MAX_ROWS_IF_ROOT`),
so the table under "Metadata" showed nothing for either producer, the attachment manifest included.

The trace metadata is now the fixed 34-key schema of `docs/OBSERVABILITY.md` ("The trace
metadata") plus the caller's per-run keys, the same from the trial-end pass and from the offline
uploader, with the producer keys reduced to `upload_mode`, `uploader_version`, `trace_time_source`,
`attach_mode`. What left the metadata is reachable where a reader looks for it: the attached
files (task facts, model configurations, environment identity, redaction stamp), the generations
(per-call usage), the grading observation and its scores (criteria and trace-check detail), the
tags (model facets, the launcher's tags) and the native `version` field (rules and profile
versions). The deployment profile lost `tag_profile_version` and `[tags] mirror_to_metadata` with
it (schema 1 refuses both as unknown keys), the model-name resolvers report an identity and its
tags only, and the golden parity test in both repositories was regenerated over the new schema
with the bound (fewer than 100 keys) as one of its assertions.

## Amendment 2026-09-17: the observer is a plugin in its own wheel

The first cut placed the OTLP observer behind the `otel` extra inside the engine
(`tolokaforge.observability.otel` and its siblings), so every fix to the projection, the profile or
the attachment step needed an engine release, and a deployment pinned to an engine version could
not take a Langfuse fix without moving that pin. The engine now owns the seam only:
`tolokaforge.observability.observer` (the hooks, the null and composite observers, the receipt),
`ids` (the id contract) and `factory` (the run identity, the sidecar files, and the discovery of
**trial-observer plugins** under the `tolokaforge.trial_observers` entry-point group with the
signature `build(tracing, identity, *, engine_run_id, output_dir) -> TrialObserver | None`). The
Langfuse observer is the `tolokaforge-langfuse` distribution, a workspace member released on its
own `langfuse-vX.Y.Z` cadence (`tolokaforge_langfuse/`: `otel.py`, `projection.py`,
`gradings.py`, `media.py`, `attachments.py`, `profile.py`, `model_names.py`, `plugin.py`; the
former `tolokaforge/observability/{otel,langfuse_projection,langfuse_gradings,langfuse_media,
attachments,profile,model_names}.py` named in the amendments above); the engine's `otel` extra
resolves to it. The plugin decides enablement (`exporter: otlp` or `LANGFUSE_TRACING_ENABLED`),
the engine composes what the plugins return and refuses a run that asks for an exporter no plugin
provides. The pairing is a versioned contract (`PLUGIN_API_VERSION` = `__api_version__` = 3, including the configuration and receipt contracts)
checked at run start, not a pip dependency: like the models wheel (ADR-0030) the plugin declares
no `tolokaforge` requirement, so the same wheel installs next to any engine pin and fails loud,
with both versions in the message, when the contract moved. `TracingConfig` stays in the engine
(its fields are the seam's public configuration) and gains `options`, a pass-through mapping for
plugin settings the engine has no field for. The import-linter contract `trial-observer-seam`
keeps the engine from importing the plugin statically; the golden parity test moved with the
plugin (`tolokaforge_langfuse/tests/unit/parity_bundle.py`, still byte-identical in the connector).

## Amendment 2026-09-17: one vocabulary in the wheel, one profile for both producers

Users of the receiver want to select trials by what a model is (its generation, tier, variant,
size, stage, snapshot) and by how it was run (reasoning mode, effort, budget, the provider the
calls were routed to), across evaluations; the receiver groups and filters by tags and native fields, and filters
but never groups by metadata. The two producers had two vocabularies: a closed core in the
offline uploader's code with a deployment profile for its defaults and lists, and a syntax-only
rule in the live observer with a second profile file for the environment and fixed tags; the
model facets the normalizer derives reached neither. The default vocabulary now lives once, in
the wheel (`tolokaforge_langfuse/vocabulary.py`), and the offline uploader imports it: the
prefixes with their producer / caller split, the value lists the engine's output format defines
(`run_kind`, `scope`), the tags derived from the bundle and the normalizer (`model_<facet>`,
`reasoning_mode`, `reasoning_effort`, `reasoning_budget`, `route`, all producer-owned, none
invented when the fact is absent), the ordering, and the environment rule (`production` for an
evaluation, `development` otherwise), which is now the default without a profile as well. The
deployment's values arrive in **one** profile file both producers read (`profile.py`, schema 2:
environment, fixed tags, derived-tag groups, value lists, required prefixes, the offline
command's derivations, the caller's metadata keys, fixed metadata, model rules; schema 1 still
loads). Both producers enforce the same discipline at run start or before upload: a closed prefix
list, closed values, one value per prefix, no contradiction of a fixed tag, the required set when
a profile is in force, the profile's metadata keys; a CI launcher pre-checks its inputs with the
module entry and degrades to an offline upload rather than failing a run. The live producer sets
`source:trial` itself, like `harness` and `task`. The native `version` of a live trace names the
wheel (`tolokaforge-langfuse-<version>+<rules>+<profile>`), `release` the engine. The golden
parity test carries the derived tags; the shared modules import no engine module, which a test
pins, so the offline uploader can depend on the wheel next to any engine pin.

## Amendment 2026-09-18: seam contracts and process-local delivery receipts

The Pattern A fixture is `InMemoryTrialObserver`, with `TrialObserverCallLog`, an explicit
receipt and per-hook failure injection. `tests/canonical/test_trial_observer_contract.py` pins
all six hook signatures, fan-out and failure isolation. `safely` logs ordinary exceptions;
process interrupts still propagate.

Pattern B applies to `TracingConfig`, `ExportReceipt`, and the persisted `RunIdentityDocument`:
all reject unknown fields and have canonical JSON round-trip snapshots. The run identity's
wire shape stays unchanged. Tracing plugin settings belong in the declared `options` field.

The receipt carries only common delivery counters, `flushed`, and `exporter` at the top level.
The explicitly sanctioned extension fields are `extra: dict[str, nonnegative int]` (additive,
plugin-namespaced counters) and `details: tuple[dict[str, str | None], ...]` (non-additive receiver
facts). The engine sums the former and concatenates the latter without knowing any receiver
key. Langfuse uses `langfuse.*` counters and a details entry containing `exporter`,
`expect_project`, and `project_verified`. This is plugin API **2**: older engines and plugins
must reject the pairing instead of silently dropping receiver data. The composite records a
missing child receipt as `export_failures += 1` and `flushed: false`.

`run_finished` is per process. `ExportReceipt.merge` provides the reduction for disjoint worker
receipts; collection, worker deduplication and partial/final accounting belong to the caller.
No automatic distributed aggregation is implied. Counts describe export attempts, while the
id contract makes receiver-side re-sends idempotent; summing a receipt twice is not idempotent.

The Protocol is an in-process tap, not a remote-ingest contract. Hook arguments are live engine
objects; a sidecar or remote collector requires an in-process plugin shim. The durable boundary
is the id contract plus persisted bundle projection. This preserves the transport-independent
observability direction of ADR-0021 and the address-boundary direction of ADR-0038 without
presenting Python objects or process-local counts as a distributed wire protocol.

## Amendment 2026-09-18: receiver-neutral configuration (ADR-0021)

The seam's configuration follows the same ownership boundary as its receipt. The engine's
`TracingConfig` declares exporter selection, endpoint, run identity, session/label, service name,
tags, metadata, queue/flush limits and span content limits. It does not prescribe a receiver's
project API, native fields, media protocol, grading projection or vocabulary. Exporter names
are extensible; an unsupported exporter fails at run start with a generic error listing the
installed observer plugins.

`options` is a mapping keyed by plugin name, passed through unchanged. The Langfuse wheel owns
the strict `LangfuseConfig` schema under `options.langfuse`: `expect_project`, `attach`,
`gradings`, `projection`, `attach_api_base`, `attach_timeout_s`, `attach_budget_s`, `profile`,
`environment`, `model_name_normalizer` and `model_name_rules`. These fields describe receiver
REST operations or deployment-specific projection and naming; another backend need not set
any of them. The active plugin validates its namespace before resolving or contacting the
receiver, ignores other plugins' namespaces, and supplies its existing defaults. The deployment
profile and environment variables keep their previous precedence.

This is plugin API **3**: version-2 engines and plugins reject the pairing. Existing configurations
must move receiver fields from `observability.tracing` into `observability.tracing.options.langfuse`;
old top-level fields and misspelled Langfuse options fail explicitly. There is no engine-owned
alias or hidden migration layer. The plugin's schema imports no engine module; canonical
round-trip snapshots pin both shapes, and a separate backend test exercises the engine with
its own exporter, endpoint, tags and options. This implements the engine-owner review boundary;
the ADR remains Proposed until the owner accepts it.

## Amendment 2026-09-19: a receiver whose observations are append-only

A receiver that takes observations over OTLP only, stores them append-only and makes a trace be its
root observation cannot take the provisional root and the re-sending trial-end pass above. The
observer detects that family by capability at run start and writes a different layout there:
declared preview rows while the trial runs, the record written once from the persisted bundle with
the root last, the verdict in `scope = primary` scores instead of the frozen trace metadata, and a
minimal error root at run end for a trace whose real root can no longer come. This is plugin API
**4**. The v3 family keeps everything above unchanged. The decision, its options and its
consequences are [ADR-0048](0048-write-once-observations-append-only-receiver.md); the layout is in
`docs/OBSERVABILITY.md`.

## Links

- Related ADRs: [ADR-0019](0019-front-end-plugin-namespace.md) (the optional-extra pattern),
  [ADR-0030](0030-tolokaforge-models-split.md) (a sibling wheel without a pip dependency on the engine,
  paired by a run-time check: the pattern the packaging amendment follows)
- Related code: `tolokaforge/observability/` (the seam), `tolokaforge_langfuse/` (the plugin
  wheel), `tolokaforge/core/loop.py`, `tolokaforge/core/conductor.py`,
  `tolokaforge/core/orchestrator.py`, `.importlinter` (contract `trial-observer-seam`),
  `docs/OBSERVABILITY.md`, `docs/RELEASING.md`
- External references: Langfuse OpenTelemetry ingestion attribute conventions; the workspace plan
  `docs/TECHDEL-497-langfuse-integration/PLAN.md` sections 3.10 to 3.12 and 8; the offline
  connector `tolokaforge-tools/tools/langfuse-connector` (id contract v2 in `ids.py`, the
  reference projection `mapping.py`, `verify-parity`).
